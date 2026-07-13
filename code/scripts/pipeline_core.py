"""
pipeline_core.py
================
Lógica compartilhada de orquestração do pipeline. Usado por:
  - setup_teste.ipynb     (smoke test — cada fluxo no menor range possível)
  - setup_inicial.ipynb   (carga histórica — range definido pelo usuário)
  - run_secundario.ipynb  (rotina diária — liquidações D-3 .. D-1)
  - scripts/run_diario.py (entrypoint agendável no Task Scheduler)

Cada passo é executado como subprocesso do CLI que já existe em scripts/ — não
duplica lógica e mantém cada script independente (com seu próprio log/email).

**Fonte única do CLI:** cada fluxo tem UMA função (boletim, anbima_deb, ntnb,
calc_taxa, ...). O comando de linha de cada script fica escrito num único lugar
(essa função). Se o CLI de um script mudar, altera-se só a função aqui — o
notebook e as rotinas (RodarDia/RodarUltimosN/RodarSetup) chamam essas funções.

Datas seguem a lógica de liquidação X / X-1u de [[11 - Pipeline de Execucao]].

Uso típico:
    import pipeline_core as pc
    pc.Boletim(Xant, X)          # testar um fluxo isolado
    pc.RodarUltimosN(5)          # rotina diária (usada pelo run_diario.py)
    pc.RodarIntervalo(ini, fim)  # reprocessar um range
"""

import csv
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

RAIZ = Path(__file__).parent.parent          # code/
SCRIPTS = Path(__file__).parent              # code/scripts/
FERIADOS_CSV = RAIZ / "data" / "feriados_anbima.csv"


# ---------------------------------------------------------------------------
# Dias úteis (feriados Anbima + fim de semana)
# ---------------------------------------------------------------------------

def Feriados() -> set[date]:
    """Feriados Anbima a partir de data/feriados_anbima.csv (coluna 'data', ISO)."""
    fer: set[date] = set()
    if FERIADOS_CSV.exists():
        with FERIADOS_CSV.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    fer.add(date.fromisoformat(row["data"].strip()))
                except (ValueError, KeyError):
                    pass
    return fer


def EhDiaUtil(d: date, fer: set[date]) -> bool:
    return d.weekday() < 5 and d not in fer


def DiaUtilAnterior(d: date | str, fer: set[date] | None = None) -> date:
    """Dia útil imediatamente anterior a d (exclui fim de semana e feriados)."""
    fer = fer if fer is not None else Feriados()
    d = date.fromisoformat(d) if isinstance(d, str) else d
    d -= timedelta(days=1)
    while not EhDiaUtil(d, fer):
        d -= timedelta(days=1)
    return d


def UltimosNDiasUteis(n: int, ref: date | str | None = None) -> list[date]:
    """Os n dias úteis mais recentes até `ref` (inclusive se ref for dia útil),
    em ordem cronológica (mais antigo → mais recente)."""
    fer = Feriados()
    d = date.today() if ref is None else (date.fromisoformat(ref) if isinstance(ref, str) else ref)
    dias: list[date] = []
    while len(dias) < n:
        if EhDiaUtil(d, fer):
            dias.append(d)
        d -= timedelta(days=1)
    return list(reversed(dias))


def DiasUteisEntre(inicio: date | str, fim: date | str) -> list[date]:
    """Dias úteis no intervalo [inicio, fim] (cronológico)."""
    fer = Feriados()
    ini = date.fromisoformat(inicio) if isinstance(inicio, str) else inicio
    end = date.fromisoformat(fim) if isinstance(fim, str) else fim
    out, cur = [], ini
    while cur <= end:
        if EhDiaUtil(cur, fer):
            out.append(cur)
        cur += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# Runner de subprocesso
# ---------------------------------------------------------------------------

def Iso(d: date | str) -> str:
    return d.isoformat() if isinstance(d, date) else d


def RodarPasso(script: str, *args: str, resultados: list | None = None,
             pararEmErro: bool = False) -> bool:
    """Executa `python scripts/<script>.py <args>` como subprocesso.
    Retorna True se exit code == 0. Loga cabeçalho e OK/FALHA.
    Se `resultados` for passado, anexa (rótulo, ok). `pararEmErro` re-levanta."""
    cmd = [sys.executable, str(SCRIPTS / f"{script}.py"), *[Iso(a) for a in args]]
    rotulo = f"{script} {' '.join(Iso(a) for a in args)}".strip()
    print(f"\n{'=' * 70}\n>> {rotulo}\n{'=' * 70}", flush=True)
    proc = subprocess.run(cmd, cwd=str(RAIZ))
    ok = proc.returncode == 0
    print(f"{'[OK]   ' if ok else '[FALHA]'} {rotulo} -- exit {proc.returncode}", flush=True)
    if resultados is not None:
        resultados.append((rotulo, ok))
    if not ok and pararEmErro:
        raise RuntimeError(f"Passo falhou: {rotulo} (exit {proc.returncode})")
    return ok


def ImprimirResumo(resultados: list[tuple[str, bool]]) -> None:
    print(f"\n{'#' * 70}\n# RESUMO — {sum(1 for _, ok in resultados if ok)}/{len(resultados)} passos OK\n{'#' * 70}")
    for rotulo, ok in resultados:
        print(f"  {'OK  ' if ok else 'FALHA'}  {rotulo}")
    falhas = [r for r, ok in resultados if not ok]
    if falhas:
        print(f"\n{len(falhas)} passo(s) com falha — revisar acima.")


def ArgsData(inicio, fim):
    """('--date', d) para data única; ('--start', i, '--end', f) para intervalo."""
    return ("--date", inicio) if fim is None else ("--start", inicio, "--end", fim)


# ---------------------------------------------------------------------------
# Fluxos individuais — 1 função por script. O CLI vive SÓ aqui.
# (mude aqui se o CLI de um script mudar; notebook e rotinas chamam estas funções)
# ---------------------------------------------------------------------------

def Boletim(inicio, fim=None, resultados=None) -> bool:
    """Boletim B3 (negócios). Na diária: inicio=X-1u, fim=X. fim=None → 1 pregão só."""
    return RodarPasso("scrape_b3_boletim", *ArgsData(inicio, fim), resultados=resultados)

def BondDetails(inicio, fim=None, resultados=None) -> bool:
    """B3 getBondDetails — cadastro + fluxo dos tickers que negociaram. FONTE PRIMÁRIA.

    Roda logo depois do boletim e **antes** do anbima_data: assim a Anbima só preenche o
    que a B3 não cobriu. Tem gate próprio (só chama a API para ticker com campo faltante)."""
    return RodarPasso("scrape_b3_bond_details", *ArgsData(inicio, fim), resultados=resultados)

def AnbimaDeb(inicio, fim=None, resultados=None) -> bool:
    """Anbima debêntures (taxa indicativa). Aceita data única ou intervalo."""
    return RodarPasso("scrape_anbima_debentures", *ArgsData(inicio, fim), resultados=resultados)

def AnbimaCriCra(inicio, fim=None, resultados=None) -> bool:
    """Anbima CRI/CRA (taxa indicativa) — Playwright. Data única ou intervalo."""
    return RodarPasso("scrape_anbima_cri_cra", *ArgsData(inicio, fim), resultados=resultados)

def FiAnalytics(resultados=None) -> bool:
    """FI Analytics planilha (características) — Playwright + login. Sem data."""
    return RodarPasso("scrape_fianalytics_planilha", resultados=resultados)

def AnbimaData(inicio=None, fim=None, full=False, limit=None, force=False,
                resultados=None) -> bool:
    """Anbima Data (características + fluxo) — Playwright.
    full=True → `--mode full` (universo completo, setup); senão intervalo incremental.
    limit/force: só para smoke test (raspar N tickers ignorando o cache)."""
    extra = []
    if limit is not None:
        extra += ["--limit", str(limit)]
    if force:
        extra += ["--force"]
    if full:
        return RodarPasso("scrape_anbima_data_ativos", "--mode", "full", *extra, resultados=resultados)
    return RodarPasso("scrape_anbima_data_ativos", *ArgsData(inicio, fim), *extra, resultados=resultados)

def Ntnb(inicio, fim=None, workers=None, resultados=None) -> bool:
    """Anbima NTN-B (MtM). Na diária: intervalo X-1u..X."""
    extra = ["--workers", str(workers)] if workers else []
    return RodarPasso("scrape_anbima_ntnb", *ArgsData(inicio, fim), *extra, resultados=resultados)

def CurvaDi(d, resultados=None) -> bool:
    """Curva DI B3. Dois destinos num download: MtmAnbima (contratos DI1, p/ o
    relatório) e di.db/CurvaDi (curva inteira, p/ a calculadora).
    Só aceita data única — rodar 1× por pregão."""
    return RodarPasso("scrape_b3_curva_di", "--date", d, resultados=resultados)


# --- Insumos da calculadora de renda fixa (ver lib/calc.py) -----------------
# Não dependem da liquidação X: rodam uma vez por ciclo, no bloco global.

def IpcaIbge(resultados=None) -> bool:
    """IPCA realizado (IBGE/SIDRA) → ipca.db/IPCA. Sem data: reprocessa a série."""
    return RodarPasso("scrape_ipca_ibge", resultados=resultados)

def IpcaProjetado(resultados=None) -> bool:
    """Projeção de IPCA (Anbima) → ipca.db/IPCAProjetado. Rodar ANTES das 17h30."""
    return RodarPasso("scrape_ipca_projetado_anbima", resultados=resultados)

def DiBcb(inicio=None, fim=None, resultados=None) -> bool:
    """DI realizado (BCB/SGS) → di.db/DiHistorico. Sem args: incremental desde o
    último dia gravado. Com intervalo: força a janela (backfill)."""
    args = ArgsData(inicio, fim) if inicio else ()
    return RodarPasso("scrape_di_bcb", *args, resultados=resultados)

def ValidarFluxos(limite=None, tickers=None, resultados=None) -> bool:
    """Valida o fluxo dos ativos contra B3/FI e marca InfoAtivos.stFluxoValidado.
    DEPOIS do anbima_data (que popula FluxoAtivos) — a fila tem throttle de 10 dias,
    então rodar todo dia é barato. limite/tickers: só para smoke test."""
    extra = []
    if limite is not None:
        extra += ["--limite", str(limite)]
    if tickers:
        extra += ["--tickers", tickers]
    return RodarPasso("validar_fluxos", *extra, resultados=resultados)

def Outstanding(inicio, fim=None, resultados=None) -> bool:
    """Outstanding via Bloomberg — SÓ NO BANCO. Data única ou intervalo."""
    return RodarPasso("scrape_outstanding_bloomberg", *ArgsData(inicio, fim), resultados=resultados)

def CalcTaxa(X, workers=None, limit=None, force=False, resultados=None) -> bool:
    """Calcula taxa por trade (cascata FI Analytics → B3). ANTES de filtrar.
    limit: só para smoke test (N trades, priorizando os que precisam de API)."""
    extra = []
    if workers:
        extra += ["--workers", str(workers)]
    if limit is not None:
        extra += ["--limit", str(limit)]
    if force:
        extra += ["--force"]
    return RodarPasso("calc_taxa_negocios", "--date", X, *extra, resultados=resultados)

def Filtrar(X, resultados=None) -> bool:
    """Classifica VALIDO / FUNDO / BROKER / PF."""
    return RodarPasso("filtrar_trades", "--date", X, resultados=resultados)

def SpreadAnbima(d, resultados=None) -> bool:
    """Spread Anbima das indicativas na data d."""
    return RodarPasso("calc_spread_anbima", "--date", d, resultados=resultados)

def MatchRef(resultados=None) -> bool:
    """Preenche cdReferencia faltante (global, sem data). ANTES de spread_over."""
    return RodarPasso("match_referencias", resultados=resultados)

def SpreadOver(X, resultados=None) -> bool:
    """Spread dos trades vs MtM (casado por dtNegocio)."""
    return RodarPasso("calc_spread_over", "--date", X, resultados=resultados)

def Relatorio(resultados=None) -> bool:
    """Regenera o relatório HTML (toda a base)."""
    return RodarPasso("gerar_relatorio_credito", resultados=resultados)


# ---------------------------------------------------------------------------
# Cadeia diária — liquidação X
# ---------------------------------------------------------------------------

def RodarDia(X: date | str, resultados: list | None = None,
            gerarRelatorio: bool = True) -> list:
    """Cadeia completa para a liquidação X (ver [[11 - Pipeline de Execucao]]).
    Raspa Anbima deb/CRI/CRA de X E X-1u (Anbima casado por dtNegocio).
    Passe gerarRelatorio=False ao rodar em loop (relatório 1× no fim)."""
    res = resultados if resultados is not None else []
    Xant = DiaUtilAnterior(X)

    Boletim(Xant, X, resultados=res)
    # B3 primeiro (fonte primária do cadastro), Anbima depois só para o que sobrar.
    BondDetails(Xant, X, resultados=res)
    for d in (Xant, X):
        AnbimaDeb(d, resultados=res)
        AnbimaCriCra(d, resultados=res)
    FiAnalytics(resultados=res)
    AnbimaData(Xant, X, resultados=res)
    Ntnb(Xant, X, resultados=res)
    for d in (Xant, X):
        CurvaDi(d, resultados=res)

    # Insumos da calculadora (sem data) + validação do fluxo, que depende do
    # anbima_data acima ter atualizado InfoAtivos/FluxoAtivos.
    IpcaIbge(resultados=res)
    IpcaProjetado(resultados=res)
    DiBcb(resultados=res)
    ValidarFluxos(resultados=res)

    CalcTaxa(X, resultados=res)
    Filtrar(X, resultados=res)
    for d in (Xant, X):
        SpreadAnbima(d, resultados=res)
    MatchRef(resultados=res)
    SpreadOver(X, resultados=res)

    if gerarRelatorio:
        Relatorio(resultados=res)
        ImprimirResumo(res)
    return res


def RodarCadeiaDias(dias: list[date], rotulo: str) -> list:
    """Roda a cadeia completa para uma lista de liquidações `dias` (cronológica) e
    gera o relatório 1× no fim. Passos globais (fianalytics, anbima_data, insumos da
    calculadora, validar_fluxos, match_ref) rodam uma vez sobre a janela inteira.
    Base das duas rotinas públicas: RodarUltimosN (padrão) e RodarIntervalo (range)."""
    if not dias:
        raise SystemExit("Sem dias úteis para processar — confira as datas.")
    res: list[tuple[str, bool]] = []
    Xant0 = DiaUtilAnterior(dias[0])
    print(f"{rotulo} — liquidações {dias[0]} .. {dias[-1]} ({len(dias)} dias); X-1u da 1ª = {Xant0}")

    FiAnalytics(resultados=res)

    # Insumos da calculadora: não dependem de X, uma passada por ciclo.
    IpcaIbge(resultados=res)
    IpcaProjetado(resultados=res)
    DiBcb(resultados=res)

    for X in dias:
        Xant = DiaUtilAnterior(X)
        Boletim(Xant, X, resultados=res)
        AnbimaDeb(X, resultados=res)
        AnbimaCriCra(X, resultados=res)
        Ntnb(Xant, X, resultados=res)
        CurvaDi(X, resultados=res)
    # a ponta X-1u da 1ª liquidação (fora do laço acima)
    AnbimaDeb(Xant0, resultados=res)
    AnbimaCriCra(Xant0, resultados=res)
    CurvaDi(Xant0, resultados=res)

    # Cadastro: B3 primeiro (fonte primária), Anbima depois só para o que ela não cobriu.
    BondDetails(Xant0, dias[-1], resultados=res)
    AnbimaData(Xant0, dias[-1], resultados=res)
    # Depois dos dois: eles atualizam InfoAtivos/FluxoAtivos e, quando o fluxo muda de
    # verdade, zeram a validação — o ativo volta pro topo da fila.
    ValidarFluxos(resultados=res)

    for X in dias:
        CalcTaxa(X, resultados=res)
        Filtrar(X, resultados=res)
        SpreadAnbima(X, resultados=res)
    SpreadAnbima(Xant0, resultados=res)
    MatchRef(resultados=res)
    for X in dias:
        SpreadOver(X, resultados=res)

    Relatorio(resultados=res)
    ImprimirResumo(res)
    return res


def RodarUltimosN(n: int = 5) -> list:
    """MODO PADRÃO da rotina diária: reprocessa os últimos `n` dias úteis (pega
    alterações retroativas). É o que o run_diario.py roda sem argumentos."""
    return RodarCadeiaDias(UltimosNDiasUteis(n), f"Rotina diária (últimos {n})")


def RodarIntervalo(inicio: date | str, fim: date | str) -> list:
    """MODO INTERVALO: reprocessa TODOS os dias úteis de [inicio, fim] (inclusive).
    Use para refazer um período específico. Mesma cadeia da rotina diária."""
    return RodarCadeiaDias(DiasUteisEntre(inicio, fim), f"Intervalo {Iso(inicio)}..{Iso(fim)}")


# ---------------------------------------------------------------------------
# Setup inicial — bootstrap da base
# ---------------------------------------------------------------------------

def RodarSetup(inicioBoletim: date | str,
              diasIndicativas: int = 130,
              diasCurvaDi: int = 20,
              diasCriCra: int = 5,
              rodarOutstanding: bool = False) -> list:
    """Bootstrap da base no banco (roda 1 vez). Raspa o histórico largo que cada
    fonte ainda entrega e roda a cadeia de cálculo sobre todos os pregões da
    janela do boletim. Tolerante a falha (segue em frente; resumo no fim).
    Ver [[13 - Migracao Banco]] §5.

      inicioBoletim    : 1º dia do boletim B3 (define a janela do relatório)
      diasIndicativas  : janela (dias corridos) de deb/NTN-B — fonte guarda ~4 meses
      diasCurvaDi      : nº de pregões da curva DI B3 (guarda ~20)
      diasCriCra       : nº de pregões de CRI/CRA (portal guarda ~5)
      rodarOutstanding : True só no banco (terminal Bloomberg)
    """
    res: list[tuple[str, bool]] = []
    hoje = date.today()
    iniInd = hoje - timedelta(days=diasIndicativas)

    # O boletim vem PRIMEIRO: o cadastro passou a ser puxado por demanda (só o que
    # negociou), então sem os negócios não há de quem buscar cadastro.
    Boletim(inicioBoletim, hoje, resultados=res)          # janela escolhida
    BondDetails(inicioBoletim, hoje, resultados=res)      # B3 — fonte primária
    FiAnalytics(resultados=res)
    AnbimaData(inicioBoletim, hoje, resultados=res)       # só o que a B3 não cobriu

    AnbimaDeb(iniInd, hoje, resultados=res)              # ~4 meses
    Ntnb(iniInd, hoje, resultados=res)                    # ~4 meses
    for d in UltimosNDiasUteis(diasCurvaDi, ref=hoje):
        CurvaDi(d, resultados=res)                        # ~20 pregões
    for d in UltimosNDiasUteis(diasCriCra, ref=hoje):
        AnbimaCriCra(d, resultados=res)                   # ~5 pregões
    if rodarOutstanding:
        Outstanding(inicioBoletim, hoje, resultados=res)  # só no banco

    # Insumos da calculadora: séries inteiras (IPCA desde 1979, DI desde 2000).
    IpcaIbge(resultados=res)
    IpcaProjetado(resultados=res)
    DiBcb(resultados=res)
    # Validação: o fluxo da B3 já nasce validado, então a fila aqui é só o que veio da
    # Anbima, mais o tripwire de saldo sobre tudo que está validado.
    ValidarFluxos(resultados=res)

    dias = DiasUteisEntre(inicioBoletim, hoje)
    for X in dias:
        CalcTaxa(X, resultados=res)
        Filtrar(X, resultados=res)
        SpreadAnbima(X, resultados=res)
    MatchRef(resultados=res)
    for X in dias:
        SpreadOver(X, resultados=res)

    Relatorio(resultados=res)
    ImprimirResumo(res)
    return res
