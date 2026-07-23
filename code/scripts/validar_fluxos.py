"""
validar_fluxos.py
=================
Decide quais ativos a calculadora pode precificar (`InfoAtivos.stFluxoValidado`).

Desde 13/07/2026 a B3 é a fonte PRIMÁRIA do cadastro (ver `scrape_b3_bond_details`), e
isso parte o trabalho em dois — que é o que este script faz:

1. VALIDAÇÃO — só para fluxo de origem Anbima (`cdFonteCadastro = 'AnbimaData'`).
   O fluxo da B3 já nasce validado: ela *é* a fonte, não há contra o que conferir.
   Confere pela FI: vencimento, taxa de emissão, a cauda futura da agenda e o SALDO.
   Régua: divergente **ou não-confirmável** ⇒ não valida (rigor > cobertura).
   Cobertura é magra — a FI só tem 146 dos ~1.670 ativos que a B3 não cobre. B3 e FI
   cobrem quase o mesmo universo, então onde uma falha a outra falha junto.

2. TRIPWIRE — para TODO ativo já validado, seja qual for a origem (`ConferirSaldo`).
   Existe porque "fluxo da B3 = válido" é uma tautologia, e fonte também apodrece: o
   EMIV11 foi aditado em fev/26 (seis meses de carência), a agenda velha continuou de
   pé, e a taxa dava −19,6%. E não é vício só da Anbima — a B3 diz que o FGEN13 vale
   1.280, a FI diz 508, e o mercado negocia a 503. Divergiu ⇒ DESVALIDA.

O ConferirSaldo é o único teste que enxerga o PASSADO da agenda: a FI só devolve
eventos futuros, então a comparação evento a evento cobre só a cauda — e a cauda bate
perfeitamente num papel cuja agenda passada mudou. O saldo devedor, não: ele é função
de toda a agenda. Um número só, que já vem na resposta.

Contrato das colunas (ver `lib/db.py` e [[04 - Banco de Dados]]):
  - `stTemFluxo`            — do INGESTOR (scrapers). Este script **só lê**.
  - `stFluxoValidado`       — 1 aqui (ou pelo scrape_b3_bond_details); zerado pelo
                              ingestor quando o fluxo muda, e por este script quando o
                              saldo diverge.
  - `dtValidacaoFluxo`      — ISO da validação OK.
  - `cdFonteValidacaoFluxo` — 'B3' | 'FiAnalytics'.
  - `dtUltimaTentativa`     — ISO de TODA tentativa (alimenta o throttle).

Saída: `data/diagnosticos/divergencias_fluxo.csv`.

CLI:
    python scripts/validar_fluxos.py                    # a fila (rotina)
    python scripts/validar_fluxos.py --limite 50        # smoke test
    python scripts/validar_fluxos.py --tickers ABFR12   # ignora o throttle
    python scripts/validar_fluxos.py --sem-tripwire     # só validação, sem reconferir
"""

import argparse
import csv
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.calc import CalcularVnaAtivo, CarregarAtivo, ImportarCalc
from lib.db import ObterBanco
from lib.email_outlook import EnviarEmailConclusao
from lib.fianalytics_api import ChamarCompleto
from lib.logger import ObterLogger
from lib.relatorio_execucao import RelatorioExecucao

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

NOME_SCRIPT = "validar_fluxos"

TOL_PCT = 0.01        # tolerância de %amortização/%incorporação (pontos percentuais)
TOL_TAXA = 1e-4       # tolerância da taxa de emissão (as fontes trazem 4 casas)
TOL_CONVENCAO = 0.5   # folga para detectar "último evento ~100" (saldo_restante)
TOL_SALDO = 0.05      # tolerância do saldo devedor, em % do VNA (ver ConferirSaldo)

DIAS_THROTTLE = 10    # não re-bater na fonte antes disso (ativo já tentado e não validado)

CSV_DIVERGENCIAS = Path("data/diagnosticos/divergencias_fluxo.csv")

calc = ImportarCalc()
FERIADOS = calc.FERIADOS_ANBIMA


# ---------------------------------------------------------------------------
# Datas e convenções
# ---------------------------------------------------------------------------



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





# ---------------------------------------------------------------------------
# Fonte secundária — FI Analytics
# ---------------------------------------------------------------------------

def UltimoDuUtil() -> date:
    """Último dia útil <= hoje (a FI não precifica em fim de semana nem no futuro)."""
    d = date.today()
    while not calc.EhDu(d, FERIADOS):
        d -= timedelta(days=1)
    return d


def ConferirSaldo(cdTicker: str, dados: dict, dtRef: date, conn) -> tuple | None:
    """Confere o saldo devedor: VNA da calculadora × `adjustedFaceValue` da FI.

    É o único teste que enxerga o PASSADO da agenda — ver o cabeçalho do módulo.

    Devolve None quando não dá para comparar. Isso NÃO é divergência: o ativo fica
    não-confirmável e é re-tentado depois."""
    fi = dados.get("adjustedFaceValue")
    if fi is None:
        return None
    fi = float(fi)
    if fi <= 0:
        return None

    ativo = CarregarAtivo(conn, cdTicker)
    if ativo is None:
        return None
    try:
        vna = CalcularVnaAtivo(ativo, dtRef)
    except Exception:
        return None  # a calc não conseguiu andar a agenda → não-confirmável

    desvio = (vna / fi - 1) * 100
    if abs(desvio) > TOL_SALDO:
        return ("saldo", f"VNA={vna:.4f}", f"adjustedFaceValue={fi:.4f} ({desvio:+.2f}%)")
    return None


def Tripwire(cdTicker: str, conn, dtRef: date) -> tuple | None:
    """Reconfere o saldo de um ativo JÁ validado, contra a FI. Divergiu ⇒ desvalida.

    Roda em ativo de qualquer origem, inclusive B3. É a única checagem independente que
    sobra depois que a B3 virou fonte primária — e ela pega tanto agenda desatualizada
    (EMIV11, aditado) quanto dado ruim na própria B3 (FGEN13: B3 diz 1.280, FI diz 508,
    o mercado negocia a 503).

    Devolve a divergência, ou None quando bate / a FI não cobre."""
    ativo = CarregarAtivo(conn, cdTicker)
    if ativo is None:
        return None
    dados = ChamarCompleto(cdTicker, dtRef.isoformat(), ativo["vrTaxaEmissao"])
    if not isinstance(dados, dict):
        return None  # a FI não cobre → sem rede, mas não é divergência
    return ConferirSaldo(cdTicker, dados, dtRef, conn)


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

    divSaldo = ConferirSaldo(cdTicker, dados, corte, conn)
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
        description="Valida o fluxo da Anbima pela FI e reconfere o saldo de tudo que está validado."
    )
    parser.add_argument("--limite", type=int, default=None, help="processa só os N primeiros de cada fila")
    parser.add_argument("--tickers", default=None, help="lista separada por vírgula; ignora o throttle")
    parser.add_argument("--sem-tripwire", dest="semTripwire", action="store_true",
                        help="pula a reconferência de saldo dos já validados")
    return parser.parse_args()


def MontarFilas(args: argparse.Namespace, conn, log) -> tuple[list[str], list[str]]:
    """(validação, tripwire).

    Validação — fluxo de origem Anbima ainda não validado. O de origem B3 nasce validado
    (ele é a fonte) e não entra aqui.

    Tripwire — tudo que está validado, de qualquer origem: reconfere o saldo contra a FI.
    """
    throttle = "(dtUltimaTentativa IS NULL OR dtUltimaTentativa < date('now', ?))"
    dias = (f"-{DIAS_THROTTLE} days",)

    if args.tickers:
        # Pedido manual re-tenta na hora, sem esperar a janela do throttle. O ticker vai
        # para a fila que couber pela origem dele.
        alvos = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        marcas = ",".join("?" * len(alvos))
        validar = [r["cdTicker"] for r in conn.execute(
            f"SELECT cdTicker FROM InfoAtivos WHERE cdTicker IN ({marcas}) "
            "AND COALESCE(cdFonteCadastro, 'AnbimaData') <> 'B3'", tuple(alvos))]
        tripwire = [r["cdTicker"] for r in conn.execute(
            f"SELECT cdTicker FROM InfoAtivos WHERE cdTicker IN ({marcas}) "
            "AND stFluxoValidado = 1", tuple(alvos))]
        return validar, ([] if args.semTripwire else tripwire)

    validar = [r["cdTicker"] for r in conn.execute(
        "SELECT cdTicker FROM InfoAtivos "
        " WHERE stFluxoValidado <> 1 AND stTemFluxo = 1 "
        "   AND COALESCE(cdFonteCadastro, 'AnbimaData') <> 'B3' "
        f"  AND {throttle} ORDER BY cdTicker", dias)]

    tripwire = [] if args.semTripwire else [r["cdTicker"] for r in conn.execute(
        "SELECT cdTicker FROM InfoAtivos "
        f" WHERE stFluxoValidado = 1 AND {throttle} ORDER BY cdTicker", dias)]

    log.info("%s: %d a validar (fluxo Anbima) | %d no tripwire (saldo) | throttle %dd",
             NOME_SCRIPT, len(validar), len(tripwire), DIAS_THROTTLE)
    return validar, tripwire


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


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Principal() -> None:
    log = ObterLogger(NOME_SCRIPT)
    args = LerArgumentos()
    rel = RelatorioExecucao(NOME_SCRIPT, args=vars(args))
    success = True

    try:
        conn = ObterBanco()
        try:
            filaValidar, filaTripwire = MontarFilas(args, conn, log)
            if args.limite:
                filaValidar = filaValidar[:args.limite]
                filaTripwire = filaTripwire[:args.limite]

            fhDiv, csvDiv = AbrirCsv(CSV_DIVERGENCIAS,
                                     ["cdTicker", "fonte", "campo", "situacao", "nosso", "fonte"])
            hoje = date.today().isoformat()
            dtRef = UltimoDuUtil()
            rel.Datas([dtRef.isoformat()])

            cont = {"validados": 0, "divergentes": 0, "sem_fonte": 0,
                    "incorp_sem_fonte": 0, "desvalidados": 0, "conferidos": 0, "sem_rede": 0}
            try:
                # -- 1. Validacao: so o fluxo que veio da Anbima -----------------
                for i, cdTicker in enumerate(filaValidar, 1):
                    validado, fonte, divergencias = 0, None, None

                    linha = conn.execute("SELECT stTemFluxo FROM InfoAtivos WHERE cdTicker = ?",
                                         (cdTicker,)).fetchone()
                    if not linha or not linha["stTemFluxo"]:
                        cont["sem_fonte"] += 1
                    else:
                        _info, _amort, incorporacoes = NossoFluxo(cdTicker, conn)
                        if incorporacoes:
                            # A FI omite eventos de incorporacao: nunca poderia confirma-los.
                            # Nao-validavel, o que NAO e divergencia.
                            cont["incorp_sem_fonte"] += 1
                        else:
                            ok, divs = ValidarPelaFi(cdTicker, conn)
                            if ok is True:
                                validado, fonte = 1, "FiAnalytics"
                                cont["validados"] += 1
                                rel.Contar("validados")
                                rel.Exemplo("validados", {"cdTicker": cdTicker, "fonte": "FiAnalytics"})
                            elif ok is False:
                                divergencias = divs
                                cont["divergentes"] += 1
                            else:
                                cont["sem_fonte"] += 1

                    conn.execute(
                        "UPDATE InfoAtivos SET stFluxoValidado = ?, dtValidacaoFluxo = ?, "
                        "cdFonteValidacaoFluxo = ?, dtUltimaTentativa = ? WHERE cdTicker = ?",
                        (validado, hoje if validado else None, fonte, hoje, cdTicker))

                    for div in (divergencias or []):
                        csvDiv.writerow(LinhaDivergencia(cdTicker, "FI", div))
                        rel.Exemplo("divergentes", {"cdTicker": cdTicker, "campo": div[0],
                                                    "nosso": str(div[1])[:40], "fonte": str(div[2])[:40]})
                    if i % 25 == 0:
                        conn.commit()
                        log.info("%s: validacao [%d/%d] ok=%d div=%d", NOME_SCRIPT, i,
                                 len(filaValidar), cont["validados"], cont["divergentes"])
                conn.commit()

                # -- 2. Tripwire: reconfere o SALDO de tudo que esta validado ----
                # Inclusive o de origem B3. "Fluxo da B3 = valido" e uma tautologia, e a
                # fonte tambem apodrece (EMIV11 aditado; FGEN13 com saldo 2,5x errado NA
                # PROPRIA B3). Divergiu no saldo -> desvalida.
                for i, cdTicker in enumerate(filaTripwire, 1):
                    div = Tripwire(cdTicker, conn, dtRef)
                    if div is None:
                        # Bateu, ou a FI nao cobre. So ha rede onde ela cobre.
                        cont["conferidos"] += 1
                        conn.execute("UPDATE InfoAtivos SET dtUltimaTentativa = ? WHERE cdTicker = ?",
                                     (hoje, cdTicker))
                    else:
                        cont["desvalidados"] += 1
                        rel.Contar("desvalidados")
                        rel.Exemplo("desvalidados", {"cdTicker": cdTicker,
                                                     "nosso": str(div[1])[:40], "FI": str(div[2])[:50]})
                        conn.execute(
                            "UPDATE InfoAtivos SET stFluxoValidado = 0, dtValidacaoFluxo = NULL, "
                            "cdFonteValidacaoFluxo = NULL, dtUltimaTentativa = ? WHERE cdTicker = ?",
                            (hoje, cdTicker))
                        csvDiv.writerow(LinhaDivergencia(cdTicker, "tripwire", div))
                    if i % 50 == 0:
                        conn.commit()
                        log.info("%s: tripwire [%d/%d] desvalidados=%d", NOME_SCRIPT, i,
                                 len(filaTripwire), cont["desvalidados"])
                conn.commit()
            finally:
                fhDiv.close()

            rel.Contar("divergentes", cont["divergentes"])
            rel.Metrica("Fila de validacao (fluxo Anbima)", len(filaValidar))
            rel.Metrica("Fila do tripwire (saldo)", len(filaTripwire))
            rel.Metrica("Validados pela FI", cont["validados"])
            rel.Metrica("Divergentes (nao validam)", cont["divergentes"])
            rel.Metrica("Nao-confirmaveis (FI nao cobre)", cont["sem_fonte"])
            rel.Metrica("Com incorporacao (FI nao valida)", cont["incorp_sem_fonte"])
            rel.Metrica("Saldo reconferido e OK", cont["conferidos"])
            rel.Metrica("DESVALIDADOS pelo saldo", cont["desvalidados"])

            total = conn.execute("SELECT COUNT(*) FROM InfoAtivos WHERE stFluxoValidado = 1").fetchone()[0]
            rel.Metrica("Total validado na base (apos a rodada)", total)
            if cont["desvalidados"]:
                rel.Aviso(f"{cont['desvalidados']} ativo(s) perderam a validacao: o saldo devedor "
                          f"da nossa agenda nao bate com o da FI. Ver {CSV_DIVERGENCIAS}.")
            log.info("%s: concluido. %s", NOME_SCRIPT, cont)
        finally:
            conn.close()

    except Exception:
        success = False
        rel.Erro("A rodada abortou — ver traceback.")
        log.error("%s: falhou:\n%s", NOME_SCRIPT, traceback.format_exc())
        EnviarEmailConclusao(NOME_SCRIPT, False, rel,
                             tracebackErro=traceback.format_exc(), logger=log)
        sys.exit(1)

    EnviarEmailConclusao(NOME_SCRIPT, success, rel, logger=log)


if __name__ == "__main__":
    Principal()
