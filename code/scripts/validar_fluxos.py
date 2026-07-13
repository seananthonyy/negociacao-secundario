"""
validar_fluxos.py
=================
Confere o **fluxo** de cada ativo (agenda de amortização e incorporação) contra a
B3 (primária) e a FI Analytics (secundária), e marca `InfoAtivos.stFluxoValidado`.

Por quê: a agenda vem do scraper da Anbima e pode estar errada ou truncada (cupom
classificado como amortização, data DU-ajustada, evento faltando). A calculadora de
renda fixa só deve precificar ativo com fluxo **confirmado** — `stFluxoValidado = 1`.
Migrado do projeto `calculadora-renda-fixa` (handoff 12/07/2026); a lógica de
comparação é a mesma que rodou lá sobre 4.840 ativos em 11/07.

Régua central: divergente **ou não-confirmável** ⇒ **não valida** (rigor > cobertura).
Validado = zero divergência.

Contrato das 5 colunas (ver `lib/db.py` e [[04 - Banco de Dados]]):
  - `stTemFluxo`            — do INGESTOR (scrapers). Este script **só lê**.
  - `stFluxoValidado`       — 1 aqui; zerado pelo ingestor quando o fluxo muda.
  - `dtValidacaoFluxo`      — ISO da validação OK.
  - `cdFonteValidacaoFluxo` — 'B3' | 'FiAnalytics'.
  - `dtUltimaTentativa`     — ISO de TODA tentativa (alimenta o throttle).

Fila (idempotente, com throttle): ativos com `stFluxoValidado <> 1` cuja
`dtUltimaTentativa` é NULL ou mais velha que DIAS_THROTTLE. A invalidação do
ingestor zera `dtUltimaTentativa`, então ativo que mudou volta pro topo da fila.

Saídas: `data/divergencias_fluxo.csv` e `data/carencia_conferir_pu.csv`.

CLI:
    python scripts/validar_fluxos.py                    # a fila (rotina)
    python scripts/validar_fluxos.py --limite 50        # smoke test
    python scripts/validar_fluxos.py --tickers ABFR12   # ignora o throttle
"""

import argparse
import csv
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.b3_calc_api import ObterDetalhesAtivo
from lib.calc import ImportarCalc
from lib.db import ObterBanco
from lib.email_outlook import EnviarEmailConclusao
from lib.fianalytics_api import ChamarCompleto
from lib.logger import ObterLogger

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

NOME_SCRIPT = "validar_fluxos"

TOL_PCT = 0.01        # tolerância de %amortização/%incorporação (pontos percentuais)
TOL_TAXA = 1e-4       # tolerância da taxa de emissão (as fontes trazem 4 casas)
TOL_CONVENCAO = 0.5   # folga para detectar "último evento ~100" (saldo_restante)
TOL_SALDO = 0.05      # tolerância do saldo devedor, em % do VNA (ver ConferirSaldo)

DIAS_THROTTLE = 10    # não re-bater na fonte antes disso (ativo já tentado e não validado)

CSV_DIVERGENCIAS = Path("data/divergencias_fluxo.csv")
CSV_CARENCIA = Path("data/carencia_conferir_pu.csv")

# 'method' da B3 → nosso cdIndexador.
MAPA_INDEXADOR = {
    "IPCA-I": "IPCA", "IPCA": "IPCA",
    "DI-PERC": "%CDI", "DI-SPREAD": "CDI+",
    "PRE": "PREFIXADO",
}

calc = ImportarCalc()
FERIADOS = calc.FERIADOS_ANBIMA


# ---------------------------------------------------------------------------
# Datas e convenções
# ---------------------------------------------------------------------------

def MapearIndexador(method) -> str | None:
    """A B3 tem duas variantes de IPCA (IPCA e IPCA-I) — normaliza por prefixo."""
    if not method:
        return None
    if str(method).upper().startswith("IPCA"):
        return "IPCA"
    return MAPA_INDEXADOR.get(method)


def AjustarDu(d: date) -> date:
    return d if calc.EhDu(d, FERIADOS) else calc.ProximoDu(d, FERIADOS)


def DatasBatem(a: date, b: date) -> bool:
    """Simétrico: batem se caem no mesmo dia útil. Cobre o caso da nossa base
    guardar o dia cru (ex.: 15) e a fonte o ajustado (17)."""
    return a == b or AjustarDu(a) == AjustarDu(b)


def ParaOriginal(eventos: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """Converte %amortização para % do principal ORIGINAL, seja qual for a convenção.

    A nossa base não é uniforme: uns ativos vêm em saldo_original (soma 100), outros
    em saldo_restante (último evento = 100). A B3 sempre manda saldo_original. Detecta
    pelo próprio schedule (último ~100 e mais de um evento ⇒ saldo_restante) e converte
    — assim a comparação fica invariante à convenção."""
    if len(eventos) <= 1:
        return list(eventos)

    if abs(eventos[-1][1] - 100.0) <= TOL_CONVENCAO:  # saldo_restante → converter
        convertidos, restante = [], 1.0
        for dt, pct in eventos:
            pago = (pct / 100.0) * restante
            convertidos.append((dt, pago * 100.0))
            restante -= pago
        return convertidos

    return list(eventos)  # já é saldo_original


# ---------------------------------------------------------------------------
# Nosso lado (trades.db)
# ---------------------------------------------------------------------------

def NossoFluxo(cdTicker: str, conn) -> tuple:
    """(info, amortizacoes, incorporacoes) do ativo na nossa base."""
    info = conn.execute(
        "SELECT dtInicioRentabilidade, dtVencimento, vrTaxaEmissao, cdIndexador, vrVNE "
        "FROM InfoAtivos WHERE cdTicker = ?", (cdTicker,)).fetchone()
    eventos = conn.execute(
        "SELECT dtEvento, vrPctAmortizacao, vrPctIncorporacao FROM FluxoAtivos "
        "WHERE cdTicker = ? ORDER BY dtEvento", (cdTicker,)).fetchall()

    amortizacoes = [(date.fromisoformat(e["dtEvento"]), e["vrPctAmortizacao"])
                    for e in eventos if (e["vrPctAmortizacao"] or 0) > 0]
    incorporacoes = [(date.fromisoformat(e["dtEvento"]), e["vrPctIncorporacao"])
                     for e in eventos if (e["vrPctIncorporacao"] or 0) > 0]
    return info, amortizacoes, incorporacoes


def FluxoParaCalc(cdTicker: str, conn) -> list[tuple[date, float, float]]:
    """A agenda no formato que a calculadora consome: (dtEvento, pctAmort, pctIncorp)."""
    return [(date.fromisoformat(e["dtEvento"]),
             e["vrPctAmortizacao"] or 0.0,
             e["vrPctIncorporacao"] or 0.0)
            for e in conn.execute(
                "SELECT dtEvento, vrPctAmortizacao, vrPctIncorporacao FROM FluxoAtivos "
                "WHERE cdTicker = ? ORDER BY dtEvento", (cdTicker,))]


def CarenciaCemNoInicio(cdTicker: str, conn) -> tuple[bool, date | None]:
    """Detecta a carência clássica: a base incorpora 100% de forma CONTÍGUA no início
    (todas as incorporações ~100, antes de qualquer cupom pago ou amortização).

    Nesses, a B3 (estilo 'IPCA' simples) colapsa a carência dentro do VNE e não expõe
    a % em lugar nenhum — mas, sendo 100% por definição, dá para validar o resto e
    conferir a JANELA (fim da carência ↔ startingdate), deixando o PU para depois.

    Devolve (é carência limpa, data de fim da carência)."""
    eventos = conn.execute(
        "SELECT dtEvento, vrPctAmortizacao, vrPctIncorporacao FROM FluxoAtivos "
        "WHERE cdTicker = ? ORDER BY dtEvento", (cdTicker,)).fetchall()

    incorporacoes = [(date.fromisoformat(e["dtEvento"]), e["vrPctIncorporacao"])
                     for e in eventos if (e["vrPctIncorporacao"] or 0) > 0]
    if not incorporacoes:
        return False, None
    if any(abs(pct - 100.0) > TOL_PCT for _, pct in incorporacoes):
        return False, None  # incorporação parcial → não é carência limpa

    fim = incorporacoes[-1][0]
    for e in eventos:  # contiguidade: nada de cupom pago/amortização antes do fim
        if (e["vrPctIncorporacao"] or 0) > 0:
            continue
        if date.fromisoformat(e["dtEvento"]) < fim:
            return False, None
    return True, fim


# ---------------------------------------------------------------------------
# Comparação de agendas
# ---------------------------------------------------------------------------

def CasarEventos(nossos: list, referencia: list, dtVencRef: date | None = None) -> list:
    """Divergências entre a nossa agenda e a da fonte. Casa cada evento da fonte com
    um nosso por data (régua DatasBatem) + percentual. Sobra nossa no vencimento é
    aceita (a B3 não lista o principal do vencimento como amortização)."""
    divergencias = []
    usados: set[int] = set()

    for dtRef, pctRef in referencia:
        casou = False
        for i, (dtNosso, pctNosso) in enumerate(nossos):
            if i in usados:
                continue
            if DatasBatem(dtNosso, dtRef) and abs(pctNosso - pctRef) <= TOL_PCT:
                usados.add(i)
                casou = True
                break
        if not casou:
            divergencias.append(("fonte_sem_par", dtRef.isoformat(), pctRef))

    for i, (dtNosso, pctNosso) in enumerate(nossos):
        if i in usados:
            continue
        if dtVencRef and DatasBatem(dtNosso, dtVencRef):
            continue
        divergencias.append(("nosso_sem_par", dtNosso.isoformat(), pctNosso))

    return divergencias


# ---------------------------------------------------------------------------
# Fonte primária — B3 (getBondDetails)
# ---------------------------------------------------------------------------

def ReferenciaB3(cdTicker: str) -> dict | None:
    """Cadastro + agenda da B3, normalizados. None se a B3 não cobre o ativo."""
    bond = ObterDetalhesAtivo(cdTicker)
    if not isinstance(bond, dict) or not bond.get("events"):
        return None

    # SÓ o estilo 'IPCA-I' codifica a %incorporação no yield do evento 'J'. Nos demais
    # (IPCA simples, DI-PERC, DI-SPREAD) o yield do 'J' é a TAXA do cupom PAGO — ler
    # como incorporação transformaria todo IPCA simples em falso-divergente.
    lerIncorporacao = bond.get("method") == "IPCA-I"

    amortizacoes, incorporacoes = [], []
    for evento in bond["events"]:
        try:
            dtEvento = date.fromisoformat(evento["date"])
            pct = evento.get("yield") or 0.0
        except Exception:
            continue
        if evento.get("eventType") == "A":
            amortizacoes.append((dtEvento, pct))
        elif evento.get("eventType") == "J" and pct > 0 and lerIncorporacao:
            incorporacoes.append((dtEvento, pct))

    return {
        "info": {
            "inicio": bond.get("startingdate"),
            "venc": bond.get("expiredate"),
            "taxa": bond.get("yield"),
            "indexador": MapearIndexador(bond.get("method")),
            "method": bond.get("method"),
        },
        "amort": amortizacoes,
        "incorp": incorporacoes,
    }


def ValidarPelaB3(cdTicker: str, conn) -> tuple:
    """(validado, divergências, meta) — ou (None, None, {}) se a B3 não cobre o ativo.
    meta['conferirPu'] = True marca carência-100% (incorporação não conferida aqui)."""
    ref = ReferenciaB3(cdTicker)
    if ref is None:
        return None, None, {}

    info, amortizacoes, incorporacoes = NossoFluxo(cdTicker, conn)
    if not info:
        return False, [("info", "ausente", None)], {}

    refInfo = ref["info"]
    dtVencRef = date.fromisoformat(refInfo["venc"]) if refInfo.get("venc") else None
    divergencias: list[tuple] = []
    meta: dict = {}

    # Carência-100% no estilo 'IPCA' simples: a B3 colapsa a carência no VNE e usa o
    # startingdate como FIM da carência. Aí o nosso início legitimamente não bate.
    carenciaLimpa, fimCarencia = False, None
    if incorporacoes and not ref["incorp"] and refInfo.get("method") == "IPCA":
        carenciaLimpa, fimCarencia = CarenciaCemNoInicio(cdTicker, conn)

    # Campos críticos. RIGOR: só passa se der para CONFIRMAR a igualdade dos dois
    # lados — nulo/faltando em qualquer um deles é divergência.
    if carenciaLimpa:
        if not refInfo.get("inicio") or not DatasBatem(fimCarencia, date.fromisoformat(refInfo["inicio"])):
            divergencias.append(("carencia_fim", fimCarencia.isoformat(), refInfo.get("inicio") or "NULO"))
        meta["conferirPu"] = True  # início e %incorporação ficam para conferência por PU
    else:
        inicio = info["dtInicioRentabilidade"]
        if not inicio or not refInfo.get("inicio"):
            divergencias.append(("inicio", inicio or "NULO", refInfo.get("inicio") or "NULO"))
        elif not DatasBatem(date.fromisoformat(inicio), date.fromisoformat(refInfo["inicio"])):
            divergencias.append(("inicio", inicio, refInfo["inicio"]))

    venc = info["dtVencimento"]
    if not venc or not dtVencRef:
        divergencias.append(("venc", venc or "NULO", refInfo.get("venc") or "NULO"))
    elif not DatasBatem(date.fromisoformat(venc), dtVencRef):
        divergencias.append(("venc", venc, refInfo["venc"]))

    taxa = info["vrTaxaEmissao"]
    if taxa is None or refInfo.get("taxa") is None:
        divergencias.append(("taxa", "NULO" if taxa is None else taxa,
                             "NULO" if refInfo.get("taxa") is None else refInfo["taxa"]))
    elif abs(float(taxa) - float(refInfo["taxa"])) > TOL_TAXA:
        divergencias.append(("taxa", taxa, refInfo["taxa"]))

    indexador = info["cdIndexador"]
    if not indexador or not refInfo.get("indexador"):
        divergencias.append(("indexador", indexador or "NULO", refInfo.get("indexador") or "NULO"))
    elif indexador != refInfo["indexador"]:
        divergencias.append(("indexador", indexador, refInfo["indexador"]))

    # vrVNE NÃO é comparado: a B3 devolve o VNA corrente (ex.: 1006), não o VNE de emissão.

    divergencias += [("amort",) + d for d in CasarEventos(
        ParaOriginal(amortizacoes), ParaOriginal(ref["amort"]), dtVencRef)]

    if not carenciaLimpa:
        incorpRef = ref["incorp"]
        if incorporacoes and not incorpRef and refInfo.get("method") == "IPCA":
            # Incorporação na base + IPCA simples, mas não é carência limpa (parcial ou
            # anômala): a B3 não expõe a % em lugar nenhum → não confirmável → não valida.
            incorpRef = None
        if incorpRef is None:
            divergencias.append(("incorp", "NAO_CONFIRMAVEL", None))
        else:
            divergencias += [("incorp",) + d for d in CasarEventos(incorporacoes, incorpRef)]

    return (len(divergencias) == 0), divergencias, meta


# ---------------------------------------------------------------------------
# Fonte secundária — FI Analytics
# ---------------------------------------------------------------------------

def UltimoDuUtil() -> date:
    """Último dia útil <= hoje (a FI não precifica em fim de semana nem no futuro)."""
    d = date.today()
    while not calc.EhDu(d, FERIADOS):
        d -= timedelta(days=1)
    return d


def ConferirSaldo(cdTicker: str, info, dados: dict, dtRef: date, conn) -> tuple | None:
    """Confere o saldo devedor: VNA da calculadora × `adjustedFaceValue` da FI.

    É o único teste que enxerga o PASSADO da agenda. A FI só devolve eventos futuros,
    então a comparação evento a evento cobre só a cauda — e a cauda bate perfeitamente
    num papel cuja agenda passada mudou. Foi o caso do EMIV11: aditado em fev/26 (seis
    meses de carência de amortização), cauda idêntica à nossa, saldo 25% errado.

    O saldo resolve isso porque é função de TODA a agenda passada — cada amortização e
    cada incorporação entram nele. Um número só, que já vem na resposta: custo zero.

    Devolve None quando não dá para comparar — isso NÃO é divergência (o ativo cai em
    sem_fonte e é re-tentado depois)."""
    fi = dados.get("adjustedFaceValue")
    inicio = info["dtInicioRentabilidade"]
    if fi is None or not inicio or info["vrTaxaEmissao"] is None:
        return None
    fi = float(fi)
    if fi <= 0:
        return None

    # A calculadora só distingue IPCA (VNA corrigido pelo índice) de PREFIXADO (VNA
    # nominal). CDI é nominal também — o acúmulo do DI vive no PU par, não no VNA.
    cdIndexador = "IPCA" if info["cdIndexador"] == "IPCA" else "PREFIXADO"
    try:
        vna = calc.CalcularVna(
            dtRef, date.fromisoformat(inicio),
            vne=float(info["vrVNE"] or 1000.0),
            fluxo=FluxoParaCalc(cdTicker, conn),
            cdIndexador=cdIndexador,
            taxaCupom=float(info["vrTaxaEmissao"]),
        )
    except Exception:
        return None  # a calc não conseguiu andar a agenda → não-confirmável

    desvio = (vna / fi - 1) * 100
    if abs(desvio) > TOL_SALDO:
        return ("saldo", f"VNA={vna:.4f}", f"adjustedFaceValue={fi:.4f} ({desvio:+.2f}%)")
    return None


def ValidarPelaFi(cdTicker: str, conn) -> tuple:
    """(validado, divergências) — ou (None, None) se a FI não cobre/não confirma.

    Só roda quando a B3 não cobre E o ativo não tem incorporação (a FI omite
    incorporação, então nunca poderia confirmá-la). Confere vencimento, taxa de
    emissão, a CAUDA da agenda de amortização, o SALDO devedor e o início (por probe)."""
    info, amortizacoes, incorporacoes = NossoFluxo(cdTicker, conn)
    if incorporacoes:
        return False, [("incorp", "FI nao valida incorporacao", None)]

    taxa = info["vrTaxaEmissao"]
    if taxa is None:
        return None, None

    dtChamada = UltimoDuUtil().isoformat()
    dados = ChamarCompleto(cdTicker, dtChamada, float(taxa))
    if not isinstance(dados, dict) or not dados.get("cashFlowEvents"):
        return None, None  # a FI também não cobre → sem_fonte

    divergencias: list[tuple] = []

    venc = info["dtVencimento"]
    vencFi = (dados.get("maturityDate") or "")[:10]
    if not venc or not vencFi or not DatasBatem(date.fromisoformat(venc), date.fromisoformat(vencFi)):
        divergencias.append(("venc", venc, vencFi or "NULO"))

    taxaFi = dados.get("issueRate")  # vem em decimal
    if taxaFi is None or abs(float(taxa) - float(taxaFi) * 100) > TOL_TAXA:
        divergencias.append(("taxa", taxa, None if taxaFi is None else round(float(taxaFi) * 100, 6)))

    # Amortização: a FI só devolve os eventos A PARTIR da data da chamada, então
    # comparamos só a cauda da nossa agenda. Convenção (confirmada com ACOV15):
    # 'rate' da AMORTIZATION = saldo_restante; FINAL_AMORTIZATION é o resgate do
    # restante → 100 (saldo_restante). Depois ParaOriginal dos dois lados.
    amortFi = []
    for evento in dados["cashFlowEvents"]:
        try:
            dtEvento = date.fromisoformat(evento["date"][:10])
        except Exception:
            continue
        tipo = evento.get("eventType")
        if tipo == "AMORTIZATION" and (evento.get("rate") or 0) > 0:
            amortFi.append((dtEvento, evento["rate"] * 100))
        elif tipo == "FINAL_AMORTIZATION":
            amortFi.append((dtEvento, 100.0))

    corte = date.fromisoformat(dtChamada)
    nossaCauda = [(dt, pct) for dt, pct in amortizacoes if dt >= corte]
    divergencias += [("amort",) + d for d in CasarEventos(
        ParaOriginal(nossaCauda), ParaOriginal(sorted(amortFi)))]

    divSaldo = ConferirSaldo(cdTicker, info, dados, corte, conn)
    if divSaldo:
        divergencias.append(divSaldo)

    # Início (probe): na data de início, o VNA tem que ser igual ao VNE e os juros
    # acumulados, zero. RIGOR: probe roda e contradiz → divergência; probe NÃO roda
    # → início não-confirmável → não valida, mas fica sem_fonte (re-tenta depois).
    probeIndefinido = False
    inicio = info["dtInicioRentabilidade"]
    if not inicio:
        divergencias.append(("inicio", "NULO", None))
    else:
        sonda = ChamarCompleto(cdTicker, inicio, float(taxa))
        if isinstance(sonda, dict) and sonda.get("adjustedFaceValue") is not None:
            vna = float(sonda["adjustedFaceValue"])
            juros = float(sonda.get("accruedInterest") or 0)
            vne = float(info["vrVNE"]) if info["vrVNE"] else 1000.0
            if abs(vna - vne) > 0.01 or abs(juros) > 0.01:
                divergencias.append(("inicio_probe", inicio, f"VNA={vna:.4f} juros={juros:.4f}"))
        else:
            probeIndefinido = True

    if divergencias:
        return False, divergencias
    if probeIndefinido:
        return None, None
    return True, []


# ---------------------------------------------------------------------------
# CLI e persistência
# ---------------------------------------------------------------------------

def LerArgumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Valida o fluxo dos ativos contra B3 (primária) e FI Analytics (secundária)."
    )
    parser.add_argument("--limite", type=int, default=None, help="processa só os N primeiros da fila")
    parser.add_argument("--tickers", default=None, help="lista separada por vírgula; ignora o throttle")
    return parser.parse_args()


def MontarFila(args: argparse.Namespace, conn, log) -> list[str]:
    if args.tickers:
        # Pedido manual re-tenta na hora, sem esperar a janela do throttle.
        return [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    fila = [r["cdTicker"] for r in conn.execute(
        "SELECT cdTicker FROM InfoAtivos "
        "WHERE stFluxoValidado <> 1 "
        "  AND (dtUltimaTentativa IS NULL OR dtUltimaTentativa < date('now', ?)) "
        "ORDER BY cdTicker", (f"-{DIAS_THROTTLE} days",))]
    log.info("validar_fluxos: %d ativo(s) na fila (throttle de %d dias)", len(fila), DIAS_THROTTLE)
    return fila


def LinhaDivergencia(cdTicker: str, origem: str, div: tuple) -> list:
    """Normaliza os dois formatos de divergência numa linha de CSV.

    Campo de cadastro → (campo, nosso, fonte)                     [3 itens]
    Evento de agenda  → (campo, situacao, dtEvento, percentual)   [4 itens]
    Sem isso o percentual do evento se perde (o valor cai fora do cabeçalho)."""
    campo = div[0]
    if len(div) >= 4:
        situacao, dtEvento, pct = div[1], div[2], div[3]
        evento = f"{dtEvento} ({pct})"
        if situacao == "fonte_sem_par":       # a fonte tem o evento, nós não
            return [cdTicker, origem, campo, situacao, "", evento]
        return [cdTicker, origem, campo, situacao, evento, ""]

    nosso = div[1] if len(div) > 1 else ""
    fonte = div[2] if len(div) > 2 else ""
    return [cdTicker, origem, campo, "divergente", nosso, fonte]


def AbrirCsv(caminho: Path, cabecalho: list[str]):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    novo = not caminho.exists() or caminho.stat().st_size == 0
    fh = open(caminho, "a", newline="", encoding="utf-8")
    escritor = csv.writer(fh)
    if novo:
        escritor.writerow(cabecalho)
    return fh, escritor


def MontarResumo(contadores: dict, total: int) -> str:
    linhas = [
        "Resultado:",
        f"Ativos na fila                 : {total}",
        "",
        f"VALIDADOS por B3               : {contadores['B3']}",
        f"VALIDADOS por FI Analytics     : {contadores['FiAnalytics']}",
        "",
        f"Divergentes                    : {contadores['divergente']}",
        f"Sem fonte (nem B3 nem FI)      : {contadores['sem_fonte']}",
        f"Com incorporação e sem B3      : {contadores['incorp_sem_b3']}",
        f"Sem fluxo na base (stTemFluxo=0): {contadores['sem_fluxo']}",
        "",
        f"Carência-100% (conferir PU)    : {contadores['carencia_pu']}",
        "",
        f"Divergências detalhadas em {CSV_DIVERGENCIAS}",
    ]
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Principal() -> None:
    log = ObterLogger(NOME_SCRIPT)
    args = LerArgumentos()
    summary = ""
    success = True

    try:
        conn = ObterBanco()
        try:
            fila = MontarFila(args, conn, log)
            if args.limite:
                fila = fila[:args.limite]

            fhDiv, csvDiv = AbrirCsv(CSV_DIVERGENCIAS,
                                     ["cdTicker", "fonte", "campo", "situacao", "nosso", "fonte"])
            fhPu, csvPu = AbrirCsv(CSV_CARENCIA, ["cdTicker", "inicio", "fimCarencia", "vencimento",
                                                  "indexador", "taxa", "dtValidacao"])

            contadores = {"B3": 0, "FiAnalytics": 0, "divergente": 0, "sem_fonte": 0,
                          "incorp_sem_b3": 0, "sem_fluxo": 0, "carencia_pu": 0}
            hoje = date.today().isoformat()

            try:
                for i, cdTicker in enumerate(fila, 1):
                    validado, fonte, divergencias = 0, None, None

                    # stTemFluxo é do ingestor — aqui só se LÊ. Sem fluxo na base não há
                    # o que validar (mesmo que a B3 tenha; quem preenche é o scraper).
                    linha = conn.execute("SELECT stTemFluxo FROM InfoAtivos WHERE cdTicker = ?",
                                         (cdTicker,)).fetchone()
                    if not linha or not linha["stTemFluxo"]:
                        contadores["sem_fluxo"] += 1
                        conn.execute("UPDATE InfoAtivos SET dtUltimaTentativa = ? WHERE cdTicker = ?",
                                     (hoje, cdTicker))
                        continue

                    ok, divs, meta = ValidarPelaB3(cdTicker, conn)

                    if ok is True:
                        validado, fonte = 1, "B3"
                        contadores["B3"] += 1
                        if meta.get("conferirPu"):
                            # Validado sem conferir %incorporação/início: registrar para
                            # bater o PU depois com a calculadora.
                            contadores["carencia_pu"] += 1
                            info = conn.execute(
                                "SELECT dtInicioRentabilidade, dtVencimento, cdIndexador, vrTaxaEmissao "
                                "FROM InfoAtivos WHERE cdTicker = ?", (cdTicker,)).fetchone()
                            fimCarencia = conn.execute(
                                "SELECT MAX(dtEvento) FROM FluxoAtivos "
                                "WHERE cdTicker = ? AND vrPctIncorporacao > 0", (cdTicker,)).fetchone()[0]
                            csvPu.writerow([cdTicker, info["dtInicioRentabilidade"], fimCarencia,
                                            info["dtVencimento"], info["cdIndexador"],
                                            info["vrTaxaEmissao"], hoje])
                    elif ok is False:
                        divergencias = divs
                        contadores["divergente"] += 1
                    else:
                        # A B3 não cobre o ativo.
                        _info, _amort, incorporacoes = NossoFluxo(cdTicker, conn)
                        if incorporacoes:
                            # REGRA: nunca validar por FI um ativo COM incorporação — a FI
                            # omite esses eventos. Fica não-validável (não é divergência).
                            contadores["incorp_sem_b3"] += 1
                        else:
                            okFi, divsFi = ValidarPelaFi(cdTicker, conn)
                            if okFi is True:
                                validado, fonte = 1, "FiAnalytics"
                                contadores["FiAnalytics"] += 1
                            elif okFi is False:
                                divergencias = divsFi
                                contadores["divergente"] += 1
                            else:
                                contadores["sem_fonte"] += 1

                    # dtUltimaTentativa em TODA tentativa (validou ou não) — é o throttle.
                    conn.execute(
                        "UPDATE InfoAtivos SET stFluxoValidado = ?, dtValidacaoFluxo = ?, "
                        "cdFonteValidacaoFluxo = ?, dtUltimaTentativa = ? WHERE cdTicker = ?",
                        (validado, hoje if validado else None, fonte, hoje, cdTicker))

                    if divergencias:
                        origem = "B3" if ok is not None else "FI"
                        for div in divergencias:
                            csvDiv.writerow(LinhaDivergencia(cdTicker, origem, div))

                    if i % 25 == 0:
                        conn.commit()
                        log.info("validar_fluxos: [%d/%d] B3=%d FI=%d div=%d sem_fonte=%d",
                                 i, len(fila), contadores["B3"], contadores["FiAnalytics"],
                                 contadores["divergente"], contadores["sem_fonte"])
                conn.commit()
            finally:
                fhDiv.close()
                fhPu.close()
        finally:
            conn.close()

        summary = MontarResumo(contadores, len(fila))
        log.info("validar_fluxos: concluído.\n%s", summary)

    except Exception:
        success = False
        summary = traceback.format_exc()
        log.exception("validar_fluxos: erro inesperado")

    finally:
        EnviarEmailConclusao(NOME_SCRIPT, success, summary, logger=log)

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    Principal()
