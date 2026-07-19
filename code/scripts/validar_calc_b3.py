"""
validar_calc_b3.py
==================
Gate de CONFIANÇA da precificação local: um ativo é `stFluxoValidado = 1` se a **nossa
calc reproduz uma calculadora de mercado** — a **B3** (fonte primária do cadastro) OU,
se a B3 não confirmar, a **FI Analytics** — em PU/taxa e em VÁRIAS datas. Basta UM
oráculo reproduzir a nossa calc (decisão do usuário 15/07: "se bater com a FI, também
é válido"). O gate é BIDIRECIONAL: promove quem passa e rebaixa quem falha.

Por que existe: o `validar_fluxos` marcava o fluxo da B3 como "nasce validado" (sem
conferir se a calc precifica certo) e só reconferia o SALDO pela FI, que cobre a
minoria. Resultado: ativos ficavam validados com a calc errando o PU 1-70%. Este
gate fecha o buraco — validado passa a significar "a calc bate a B3 ou a FI".

Testado em 3 datas: [8 pregões atrás, mais recente com curva, D+1]. O D+1 é uma data
futura, sem curva própria — precificada por CARRY-FORWARD (a curva mais recente projeta
o D+1, que é o que a B3/FI também fazem); ver SemearCurvaCarryForward.

O que cada oráculo testa:
  - B3 (primário): PU no par (taxa=emissão) valida FLUXO+VNA; PU a +100 bps valida o
    DESCONTO — CalcularPuGov. Régua: erro RELATIVO de PU ≤ 1e-5 (= 0,001% = R$0,01 num
    PU de R$1.000) em toda data confirmada. Como é PU (não taxa), %CDI usa a mesma régua.
  - FI (secundário, só se a B3 não bater): round-trip de TAXA. A FI não devolve um PU
    limpo dado a taxa (o m2m é mark-a-mercado noutra base), mas devolve a taxa dado um
    PU (ChamarPrimaria). Então: pego o PU que a NOSSA calc gera na taxa T (par e
    T+100bps) e pergunto à FI a taxa — tem que voltar T. Régua: ≤ 5 bps. É o que
    RESGATA os ativos que a B3 não cobre.

Nenhum oráculo respondeu (B3 e FI vazias) ⇒ não-confirmável ⇒ INVÁLIDO (stFluxoValidado=0):
não dá para dar fé, cai na cascata de API no calc_taxa (decisão do usuário 19/07).

Revalidação: um ativo já validado é PULADO enquanto a dtValidacaoFluxo tiver menos de
`--revalidar-dias` (default 15) — só revalida quando vence. Fluxo que muda de verdade zera
stFluxoValidado pelo trigger e é re-testado na hora, sem esperar.

CLI:
    python scripts/validar_calc_b3.py                 # audita e (des)valida; revalida vencidos
    python scripts/validar_calc_b3.py --dry-run       # só reporta, não grava
    python scripts/validar_calc_b3.py --tickers A,B   # testa esses, ignora a janela de revalidação
    python scripts/validar_calc_b3.py --datas 2026-07-10,2026-07-08
    python scripts/validar_calc_b3.py --revalidar-dias 0   # re-testa tudo, ignora a data
    python scripts/validar_calc_b3.py --limite 100    # smoke
"""
import argparse
import csv
import sys
import traceback
import concurrent.futures as cf
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import importlib.util

from lib.calc import CarregarAtivo, CalcularPu, CalcularTaxa, ImportarCalc
from lib.b3_calc_api import CalcularPuGov, CalcularYield, ObterDetalhesAtivo
from lib.fianalytics_api import ChamarPrimaria
from lib.db import ObterBanco
from lib.email_outlook import EnviarEmailConclusao
from lib.logger import ObterLogger
from lib.relatorio_execucao import RelatorioExecucao

NOME_SCRIPT = "validar_calc_b3"

# reutiliza o refresh de cadastro/fluxo da B3 do scraper (mesma lógica do pacote)
_spec = importlib.util.spec_from_file_location(
    "scrape_b3_bond_details", str(Path(__file__).parent / "scrape_b3_bond_details.py"))
_sb3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sb3)


def RefrescarCadastroB3(conn, cdTicker: str) -> dict | None:
    """Re-busca o getBondDetails e regrava o pacote (VNE + início + FLUXO) pela B3.
    Cura a deriva do fluxo (o scrape de rotina só re-scrapeia info faltando, então
    fluxo já existente envelhece). Devolve o ativo recarregado, ou None."""
    det = ObterDetalhesAtivo(cdTicker)
    if not det:
        return None
    try:
        _sb3.GravarAtivo(conn, cdTicker, det, date.today().isoformat())
        conn.commit()
    except Exception:
        conn.rollback()
        return None
    return CarregarAtivo(conn, cdTicker)

# Régua de PU: erro RELATIVO |puNosso/puB3 - 1|, então independe da escala do PU (papel
# de emissão 1, 1.000 ou 10.000 usa a mesma régua). 1e-5 = 0,001% = R$0,01 num PU de
# R$1.000 (decisão do usuário). É apertado: barra ~160 CDI+ cujo erro é ruído de
# interpolação da curva DI esparsa (não erro da calc) — esses caem na cascata FI/B3.
TOL_PU = 1e-5
TOL_TAXA_BPS = 5.0     # round-trip; folga p/ o ruído numérico do %CDI (2-4 bps, PU perfeito)
TOL_FI_BPS = 5.0       # round-trip de taxa contra a FI (2o oráculo, papel que a B3 não cobre)
DELTA = 1.0            # +100 bps para o teste fora do par

# No %CDI a "taxa" é um MULTIPLICADOR do CDI (p.ex. 98,5 = 98,5% do CDI), não uma taxa
# a.a. — 1 ponto de %CDI ≈ 10 bps de yield. Converter a diferença de taxa com o mesmo
# fator do filtrar_trades (corretorMaxBps 2,5 ↔ corretorMaxPctCdi 0,25; pfMinBps 20 ↔
# pfMinPctCdi 2,0 → razão 10). Sem isso, `diff*100` (p.p.→bps) inflaria o erro do %CDI
# ~10× e reprovaria por engano quem está dentro da tolerância.
BPS_POR_PONTO_PCTCDI = 10.0


def DiffTaxaEmBps(cdIndexador: str | None, delta: float) -> float:
    """Converte uma diferença de taxa (na unidade nativa do indexador) para bps.
    %CDI: pontos de %CDI × 10. Demais: pontos percentuais × 100."""
    if cdIndexador == "%CDI":
        return delta * BPS_POR_PONTO_PCTCDI
    return delta * 100.0


WORKERS = 10
CSV_SAIDA = Path("data/validar_calc_b3.csv")

C = ImportarCalc()


def UltimoDuUtil() -> date:
    d = date.today()
    while not C.EhDu(d, C.FERIADOS_ANBIMA):
        d -= timedelta(days=1)
    return d


def ProximoDu(d: date) -> date:
    d += timedelta(days=1)
    while not C.EhDu(d, C.FERIADOS_ANBIMA):
        d += timedelta(days=1)
    return d


def SemearCurvaCarryForward(dHoje: str, dMais1: str) -> bool:
    """Deixa a calc precificar o D+1 (data futura, sem curva própria) projetando com a
    curva mais recente — é o que a B3/FI também fazem. A calc lê a curva de projeção por
    `_ObterAccProjDi(dataCalc)` (chave = data exata); então semeamos o cache dela com a
    curva de `dHoje` sob a chave de `dMais1`. Em memória, sem escrever no di.db, sem
    tocar na calculadora_rf.py. Devolve True se semeou. É válido porque `dMais1` é o DU
    seguinte ao dado mais recente: a série realizada de DI já cobre até `dHoje`, e só o
    trecho >= dMais1 é projetado."""
    import sqlite3
    di = sqlite3.connect("data/di.db")
    try:
        vertices = di.execute(
            "SELECT du, vrTaxa FROM CurvaDi WHERE dtReferencia=? ORDER BY du", (dHoje,)).fetchall()
    finally:
        di.close()
    if not vertices:
        return False
    C._ACCPROJ_CACHE[dMais1] = C._MontarAccProjDi(vertices)
    return True


def DatasPadrao(conn) -> list[str]:
    """As 3 datas do gate: [8 pregões atrás, mais recente com curva ("hoje"), D+1].
    Só datas com curva DI na base — sem curva, todo papel DI levanta exceção na calc e
    seria reprovado por engano. O D+1 é o DU seguinte à data mais recente, precificado
    por carry-forward (ver SemearCurvaCarryForward). Se não houver curva, cai no último
    DU sozinho."""
    import sqlite3
    di = sqlite3.connect("data/di.db")
    try:
        datas = [r[0] for r in di.execute(
            "SELECT DISTINCT dtReferencia FROM CurvaDi ORDER BY dtReferencia DESC LIMIT 10")]
    finally:
        di.close()
    if not datas:
        return [UltimoDuUtil().isoformat()]
    dHoje = datas[0]
    d8 = datas[min(8, len(datas) - 1)]
    dMais1 = ProximoDu(date.fromisoformat(dHoje)).isoformat()
    escolhidas = {dHoje, d8}
    if SemearCurvaCarryForward(dHoje, dMais1):
        escolhidas.add(dMais1)
    return sorted(escolhidas, reverse=True)


def RtFi(tk: str, cdInstrumento: str | None, dt: str, vrPU: float) -> float | None:
    """FI: dado um PU, devolve a taxa (m2mRate, em % a.a.). Usa o endpoint do
    instrumento; se desconhecido, tenta DEB e depois CRI/CRA. None se a FI não cobre
    o papel ou não respondeu."""
    cands = [cdInstrumento] if cdInstrumento else ["DEB", "CRA"]
    for cand in cands:
        try:
            r = ChamarPrimaria(tk, cand, dt, vrPU)
        except Exception:
            r = None
        if r is not None:
            return r
    return None


def AvaliarAtivo(ativo: dict, datas: list[str], comTaxa: bool = False,
                 comFi: bool = True) -> dict:
    """Roda a calc contra a B3 e, se a B3 não confirmar, contra a FI Analytics. Devolve
    piorPU (B3), piorFi (bps, round-trip FI), quantas datas cada oráculo confirmou, e a
    `fonte` que validou ('B3' | 'FiAnalytics' | None se nenhum bateu).

    B3 (primário): PU no par + fora do par. Dois pontos da curva PU×taxa batendo já
    validam fluxo, VNA e desconto. (`--com-taxa` liga o round-trip Newton, caro em DI e
    quase redundante — só para auditar o %CDI.)

    FI (secundário, só se a B3 não bater): round-trip de taxa — o PU que a nossa calc
    gera na taxa T tem que devolver T quando a FI o inverte. Resgata o que a B3 não
    cobre. Erro só é FALHA quando o oráculo RESPONDEU e divergiu; oráculo mudo (HTTP
    500/timeout/não-cobre) é NÃO-CONFIRMA — pula, não penaliza."""
    tk = ativo["cdTicker"]
    taxa = ativo["vrTaxaEmissao"]
    instr = ativo.get("cdInstrumento")
    piorPU = 0.0
    piorTaxa = 0.0
    nConfB3 = 0
    detalhe = []
    for dt in datas:
        d = date.fromisoformat(dt)
        try:
            puParB3, _ = CalcularPuGov(tk, dt, taxa)
        except Exception:
            puParB3 = None
        if not puParB3:
            continue
        nConfB3 += 1
        # PU no par (puParB3 já respondeu — passamos do continue)
        try:
            pc = CalcularPu(ativo, d, taxa)
            ePar = abs(pc / puParB3 - 1) if pc else 9.9   # B3 ok, calc falhou = falha real
        except Exception:
            ePar = 9.9
        # PU fora do par
        eFora = None
        try:
            puForaB3, _ = CalcularPuGov(tk, dt, taxa + DELTA)
        except Exception:
            puForaB3 = None
        if puForaB3:
            try:
                pf = CalcularPu(ativo, d, taxa + DELTA)
                eFora = abs(pf / puForaB3 - 1) if pf else 9.9
            except Exception:
                eFora = 9.9
        # taxa round-trip (opcional): dado o PU fora-do-par da B3, a taxa da calc bate o calcYield?
        eTaxa = None
        if comTaxa and puForaB3:
            try:
                tc = CalcularTaxa(ativo, d, puForaB3)
                tb = CalcularYield(tk, dt, puForaB3)
                if tc is not None and tb is not None:
                    eTaxa = DiffTaxaEmBps(ativo["cdIndexador"], abs(tc - tb))
            except Exception:
                eTaxa = None
        for e in (ePar, eFora):
            if e is not None:
                piorPU = max(piorPU, e)
        if eTaxa is not None:
            piorTaxa = max(piorTaxa, eTaxa)
        detalhe.append((dt, ePar, eFora, eTaxa))

    b3Pass = nConfB3 > 0 and piorPU <= TOL_PU and piorTaxa <= TOL_TAXA_BPS

    # 2o oráculo: só se a B3 não confirmou (economiza chamada e mantém a B3 primária).
    piorFi = 0.0
    nConfFi = 0
    if comFi and not b3Pass:
        for dt in datas:
            d = date.fromisoformat(dt)
            confirmou = False
            for tt in (taxa, taxa + DELTA):
                try:
                    pc = CalcularPu(ativo, d, tt)
                except Exception:
                    pc = None
                if not pc:
                    continue
                ft = RtFi(tk, instr, dt, pc)
                if ft is None:      # FI não cobre esse ponto → não penaliza
                    continue
                confirmou = True
                piorFi = max(piorFi, DiffTaxaEmBps(ativo["cdIndexador"], abs(ft - tt)))
            if confirmou:
                nConfFi += 1
    fiPass = nConfFi > 0 and piorFi <= TOL_FI_BPS

    fonte = "B3" if b3Pass else ("FiAnalytics" if fiPass else None)
    return {"tk": tk, "idx": ativo["cdIndexador"], "piorPU": piorPU,
            "piorTaxa": piorTaxa, "piorFi": piorFi, "nConfB3": nConfB3,
            "nConfFi": nConfFi, "fonte": fonte, "detalhe": detalhe}


def LerArgumentos() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Gate de confiança: desvalida ativo cuja calc não reproduz a B3.")
    p.add_argument("--dry-run", dest="dryRun", action="store_true", help="só reporta, não grava")
    p.add_argument("--tickers", default=None, help="lista separada por vírgula")
    p.add_argument("--datas", default=None, help="datas YYYY-MM-DD separadas por vírgula")
    p.add_argument("--com-taxa", dest="comTaxa", action="store_true",
                   help="também testa a taxa round-trip da B3 (Newton, lento em DI); default só PU")
    p.add_argument("--sem-fi", dest="semFi", action="store_true",
                   help="não usa a FI como 2o oráculo (só B3); volta ao gate B3-only")
    p.add_argument("--sem-refresh", dest="semRefresh", action="store_true",
                   help="não tenta refrescar o cadastro pela B3 antes de desvalidar")
    p.add_argument("--negociados-dias", dest="negociadosDias", type=int, default=None,
                   help="testa só validados que negociaram nos últimos N dias (rotina)")
    p.add_argument("--revalidar-dias", dest="revalidarDias", type=int, default=15,
                   help="revalida um ativo já validado só se a dtValidacaoFluxo tiver "
                        "mais de N dias (default 15). Validado há menos que isso é pulado. "
                        "0 = re-testa tudo, ignora a data. Não se aplica a --tickers.")
    p.add_argument("--limite", type=int, default=None)
    return p.parse_args()


def Principal() -> None:
    log = ObterLogger(NOME_SCRIPT)
    args = LerArgumentos()
    rel = RelatorioExecucao(NOME_SCRIPT, args=vars(args))
    success = True

    try:
        conn = ObterBanco()
        try:
            datas = args.datas.split(",") if args.datas else DatasPadrao(conn)
            rel.Datas(datas)
            # Gate BIDIRECIONAL: candidatos NÃO se restringem a stFluxoValidado=1 — quem
            # passa é promovido, quem falha é rebaixado. Mas quem já está validado há menos
            # de `--revalidar-dias` é PULADO (confia até vencer). Fluxo que muda de verdade
            # zera stFluxoValidado pelo trigger → cai em não-validado e é re-testado na hora.
            # `--tickers` ignora essa janela (o usuário pediu aqueles explicitamente).
            corteReval = None
            if args.revalidarDias and args.revalidarDias > 0 and not args.tickers:
                corteReval = (date.today() - timedelta(days=args.revalidarDias)).isoformat()
            # cláusula que mantém: não-validado (promover) OU validado-vencido (revalidar)
            recente = ("(i.stFluxoValidado IS NULL OR i.stFluxoValidado = 0 "
                       "OR i.dtValidacaoFluxo IS NULL OR i.dtValidacaoFluxo < ?)")
            if args.tickers:
                alvos = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
                marcas = ",".join("?" * len(alvos))
                tickers = [r["cdTicker"] for r in conn.execute(
                    f"SELECT cdTicker FROM InfoAtivos WHERE cdTicker IN ({marcas})", tuple(alvos))]
            elif args.negociadosDias:
                # os que NEGOCIARAM na janela (validados ou não) — é o que aparece no
                # relatório, e mantém o gate barato na rotina (a base toda leva ~13 min).
                corte = (date.today() - timedelta(days=args.negociadosDias)).isoformat()
                sql = ("SELECT DISTINCT i.cdTicker FROM InfoAtivos i "
                       "JOIN NegociosBrutos nb ON nb.cdTicker = i.cdTicker "
                       "WHERE nb.dtLiquidacao >= ? AND nb.cdSituacao != 'Cancelado'")
                params = [corte]
                if corteReval:
                    sql += " AND " + recente
                    params.append(corteReval)
                sql += " ORDER BY i.cdTicker"
                tickers = [r["cdTicker"] for r in conn.execute(sql, tuple(params))]
            else:
                sql = "SELECT i.cdTicker FROM InfoAtivos i"
                params = []
                if corteReval:
                    sql += " WHERE " + recente
                    params.append(corteReval)
                sql += " ORDER BY i.cdTicker"
                tickers = [r["cdTicker"] for r in conn.execute(sql, tuple(params))]
            if args.limite:
                tickers = tickers[:args.limite]

            # cdInstrumento (DEB/CRI/CRA) por ticker — a FI precisa para escolher o
            # endpoint; e o estado ATUAL de validação, para reportar promoções × rebaixos.
            instrMap = {r["cdTicker"]: r["cdInstrumento"] for r in conn.execute(
                "SELECT cdTicker, cdInstrumento FROM InfoAtivos")}
            estavaValidado = {r["cdTicker"] for r in conn.execute(
                "SELECT cdTicker FROM InfoAtivos WHERE stFluxoValidado = 1")}

            # pré-carrega na thread principal (SQLite não cruza threads); workers só chamam B3/calc/FI
            ativos = {}
            for tk in tickers:
                a = CarregarAtivo(conn, tk)
                if a:
                    a["cdInstrumento"] = instrMap.get(tk)
                    ativos[tk] = a
            log.info("%s: %d candidato(s), %d carregável(is), datas=%s",
                     NOME_SCRIPT, len(tickers), len(ativos), datas)

            comFi = not args.semFi
            resultados = []
            with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
                fut = {ex.submit(AvaliarAtivo, a, datas, args.comTaxa, comFi): tk
                       for tk, a in ativos.items()}
                for i, f in enumerate(cf.as_completed(fut), 1):
                    resultados.append(f.result())
                    if i % 200 == 0:
                        log.info("%s: [%d/%d]", NOME_SCRIPT, i, len(ativos))

            # confiável = algum oráculo (B3 ou FI) reproduziu a calc → r["fonte"] setado.
            # não-confirmável = nenhum oráculo respondeu (B3 e FI vazias) → INVÁLIDO também
            # (decisão do usuário: "se nenhuma retorna, impossível validar → inválido"). Cai
            # na cascata de API no calc_taxa. Distinto de REPROVADO só para o relatório/CSV.
            def NaoConfirmavel(r):
                return r["nConfB3"] == 0 and r["nConfFi"] == 0

            reprovados, confirmados, naoConfLista = [], [], []
            for r in resultados:
                if NaoConfirmavel(r):
                    naoConfLista.append(r)
                elif r["fonte"]:
                    confirmados.append(r)
                else:
                    reprovados.append(r)
            naoConf = len(naoConfLista)

            # PASS 2 — refresh-on-fail: cura a deriva do fluxo antes de rebaixar. O scrape
            # de rotina só re-scrapeia info FALTANDO, então fluxo já existente envelhece;
            # aqui, quem falha ganha um cadastro fresco da B3 e é re-testado (contra os dois
            # oráculos). Só rebaixa quem AINDA falha (falha genuína de metodologia).
            recuperados = 0
            if reprovados and not args.dryRun and not args.semRefresh:
                aindaFalha = []
                log.info("%s: refresh-on-fail em %d reprovado(s)...", NOME_SCRIPT, len(reprovados))
                for r in reprovados:
                    a2 = RefrescarCadastroB3(conn, r["tk"])
                    if a2:
                        a2["cdInstrumento"] = instrMap.get(r["tk"])
                        r2 = AvaliarAtivo(a2, datas, args.comTaxa, comFi)
                    else:
                        r2 = None
                    if r2 and r2["fonte"]:
                        recuperados += 1
                        confirmados.append(r2)
                    else:
                        aindaFalha.append(r)
                reprovados = aindaFalha
                log.info("%s: refresh recuperou %d; restam %d reprovado(s)",
                         NOME_SCRIPT, recuperados, len(reprovados))

            reprovados.sort(key=lambda r: -r["piorPU"])
            confPorTk = {r["tk"]: r for r in confirmados}
            CSV_SAIDA.parent.mkdir(parents=True, exist_ok=True)
            with open(CSV_SAIDA, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["cdTicker", "idx", "fonteConfirma", "piorPU", "piorFiBps",
                            "nConfB3", "nConfFi", "veredito"])
                for r in resultados:
                    rr = confPorTk.get(r["tk"], r)   # se recuperado no refresh, usa o r2
                    if NaoConfirmavel(rr):
                        v = "nao_confirmavel"
                    elif rr["fonte"]:
                        v = "confiavel"
                    else:
                        v = "REPROVADO"
                    w.writerow([rr["tk"], rr["idx"], rr["fonte"] or "-", f"{rr['piorPU']:.2e}",
                                f"{rr['piorFi']:.2f}", rr["nConfB3"], rr["nConfFi"], v])

            # UPDATE bidirecional: promove os confiáveis (validado=1 + fonte + dtValidacaoFluxo
            # de hoje), rebaixa os inválidos = reprovados + não-confirmáveis (validado=0).
            invalidos = reprovados + naoConfLista
            if not args.dryRun:
                hoje = date.today().isoformat()
                if confirmados:
                    conn.executemany(
                        "UPDATE InfoAtivos SET stFluxoValidado = 1, dtValidacaoFluxo = ?, "
                        "cdFonteValidacaoFluxo = ? WHERE cdTicker = ?",
                        [(hoje, r["fonte"], r["tk"]) for r in confirmados])
                if invalidos:
                    conn.executemany(
                        "UPDATE InfoAtivos SET stFluxoValidado = 0, dtValidacaoFluxo = NULL, "
                        "cdFonteValidacaoFluxo = NULL WHERE cdTicker = ?",
                        [(r["tk"],) for r in invalidos])
                conn.commit()

            from collections import Counter
            porFonte = Counter(r["fonte"] for r in confirmados)
            promovidos = [r for r in confirmados if r["tk"] not in estavaValidado]
            rebaixados = [r for r in invalidos if r["tk"] in estavaValidado]
            porIdx = Counter(r["idx"] for r in reprovados)
            rel.Metrica("Candidatos testados", len(ativos))
            rel.Metrica("Confiáveis (calc reproduz um oráculo)", len(confirmados))
            rel.Metrica("- por oráculo (B3 / FI)", dict(porFonte))
            rel.Metrica("- resgatados pela FI (B3 não bateu)", porFonte.get("FiAnalytics", 0))
            rel.Metrica("Promovidos (eram não-validados)", len(promovidos))
            rel.Metrica("Recuperados por refresh da B3", recuperados)
            rel.Metrica("INVÁLIDOS (rebaixados p/ 0)" + ("" if not args.dryRun else " (dry-run)"),
                        len(invalidos))
            rel.Metrica("- por divergência (REPROVADOS)", len(reprovados))
            rel.Metrica("- por não-confirmável (B3 e FI mudas)", naoConf)
            rel.Metrica("- eram validados (rebaixados de fato)", len(rebaixados))
            rel.Metrica("Reprovados por indexador", dict(porIdx))
            rel.Secao("Piores reprovados",
                      ["ticker", "idx", "piorPU", "piorFiBps", "nConfB3", "nConfFi"],
                      [[r["tk"], r["idx"], f"{r['piorPU']:.1e}", f"{r['piorFi']:.1f}",
                        r["nConfB3"], r["nConfFi"]] for r in reprovados[:30]])
            total = conn.execute("SELECT COUNT(*) FROM InfoAtivos WHERE stFluxoValidado = 1").fetchone()[0]
            rel.Metrica("Total validado na base (após a rodada)", total)
            if invalidos and not args.dryRun:
                rel.Aviso(f"{len(invalidos)} ativo(s) INVÁLIDOS ({len(reprovados)} por divergência, "
                          f"{naoConf} sem oráculo): a calc não pôde ser confirmada. Caem na cascata "
                          f"de API no calc_taxa. Ver {CSV_SAIDA}.")
            log.info("%s: confiáveis=%d (B3=%d FI=%d) promovidos=%d reprovados=%d nao_conf=%d",
                     NOME_SCRIPT, len(confirmados), porFonte.get("B3", 0),
                     porFonte.get("FiAnalytics", 0), len(promovidos), len(reprovados), naoConf)
        finally:
            conn.close()
    except Exception:
        success = False
        rel.Erro("A rodada abortou — ver traceback.")
        log.error("%s: falhou\n%s", NOME_SCRIPT, traceback.format_exc())
        EnviarEmailConclusao(NOME_SCRIPT, False, rel, tracebackErro=traceback.format_exc(), logger=log)
        sys.exit(1)

    EnviarEmailConclusao(NOME_SCRIPT, success, rel, logger=log)


if __name__ == "__main__":
    Principal()
