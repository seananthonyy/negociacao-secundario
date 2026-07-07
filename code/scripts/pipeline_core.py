"""
pipeline_core.py
================
Lógica compartilhada de orquestração do pipeline. Usado por:
  - pipeline.ipynb        (interativo, 1 bloco por fluxo)
  - scripts/run_diario.py (entrypoint agendável no Task Scheduler)

Cada passo é executado como subprocesso do CLI que já existe em scripts/ — não
duplica lógica e mantém cada script independente (com seu próprio log/email).

**Fonte única do CLI:** cada fluxo tem UMA função (boletim, anbima_deb, ntnb,
calc_taxa, ...). O comando de linha de cada script fica escrito num único lugar
(essa função). Se o CLI de um script mudar, altera-se só a função aqui — o
notebook e as rotinas (run_dia/run_ultimos_n/run_setup) chamam essas funções.

Datas seguem a lógica de liquidação X / X-1u de [[11 - Pipeline de Execucao]].

Uso típico:
    import pipeline_core as pc
    pc.boletim(Xant, X)          # testar um fluxo isolado
    pc.run_ultimos_n(5)          # rotina diária
    pc.run_setup("2026-03-02")   # bootstrap inicial
"""

import csv
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).parent.parent          # code/
_SCRIPTS = Path(__file__).parent              # code/scripts/
_FERIADOS_CSV = _ROOT / "data" / "feriados_anbima.csv"


# ---------------------------------------------------------------------------
# Dias úteis (feriados Anbima + fim de semana)
# ---------------------------------------------------------------------------

def _feriados() -> set[date]:
    """Feriados Anbima a partir de data/feriados_anbima.csv (coluna 'data', ISO)."""
    fer: set[date] = set()
    if _FERIADOS_CSV.exists():
        with _FERIADOS_CSV.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    fer.add(date.fromisoformat(row["data"].strip()))
                except (ValueError, KeyError):
                    pass
    return fer


def _eh_dia_util(d: date, fer: set[date]) -> bool:
    return d.weekday() < 5 and d not in fer


def dia_util_anterior(d: date | str, fer: set[date] | None = None) -> date:
    """Dia útil imediatamente anterior a d (exclui fim de semana e feriados)."""
    fer = fer if fer is not None else _feriados()
    d = date.fromisoformat(d) if isinstance(d, str) else d
    d -= timedelta(days=1)
    while not _eh_dia_util(d, fer):
        d -= timedelta(days=1)
    return d


def ultimos_n_dias_uteis(n: int, ref: date | str | None = None) -> list[date]:
    """Os n dias úteis mais recentes até `ref` (inclusive se ref for dia útil),
    em ordem cronológica (mais antigo → mais recente)."""
    fer = _feriados()
    d = date.today() if ref is None else (date.fromisoformat(ref) if isinstance(ref, str) else ref)
    dias: list[date] = []
    while len(dias) < n:
        if _eh_dia_util(d, fer):
            dias.append(d)
        d -= timedelta(days=1)
    return list(reversed(dias))


def dias_uteis_entre(inicio: date | str, fim: date | str) -> list[date]:
    """Dias úteis no intervalo [inicio, fim] (cronológico)."""
    fer = _feriados()
    ini = date.fromisoformat(inicio) if isinstance(inicio, str) else inicio
    end = date.fromisoformat(fim) if isinstance(fim, str) else fim
    out, cur = [], ini
    while cur <= end:
        if _eh_dia_util(cur, fer):
            out.append(cur)
        cur += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# Runner de subprocesso
# ---------------------------------------------------------------------------

def _iso(d: date | str) -> str:
    return d.isoformat() if isinstance(d, date) else d


def run_step(script: str, *args: str, resultados: list | None = None,
             parar_em_erro: bool = False) -> bool:
    """Executa `python scripts/<script>.py <args>` como subprocesso.
    Retorna True se exit code == 0. Loga cabeçalho e OK/FALHA.
    Se `resultados` for passado, anexa (rótulo, ok). `parar_em_erro` re-levanta."""
    cmd = [sys.executable, str(_SCRIPTS / f"{script}.py"), *[_iso(a) for a in args]]
    rotulo = f"{script} {' '.join(_iso(a) for a in args)}".strip()
    print(f"\n{'=' * 70}\n>> {rotulo}\n{'=' * 70}", flush=True)
    proc = subprocess.run(cmd, cwd=str(_ROOT))
    ok = proc.returncode == 0
    print(f"{'[OK]   ' if ok else '[FALHA]'} {rotulo} -- exit {proc.returncode}", flush=True)
    if resultados is not None:
        resultados.append((rotulo, ok))
    if not ok and parar_em_erro:
        raise RuntimeError(f"Passo falhou: {rotulo} (exit {proc.returncode})")
    return ok


def _resumo(resultados: list[tuple[str, bool]]) -> None:
    print(f"\n{'#' * 70}\n# RESUMO — {sum(1 for _, ok in resultados if ok)}/{len(resultados)} passos OK\n{'#' * 70}")
    for rotulo, ok in resultados:
        print(f"  {'OK  ' if ok else 'FALHA'}  {rotulo}")
    falhas = [r for r, ok in resultados if not ok]
    if falhas:
        print(f"\n{len(falhas)} passo(s) com falha — revisar acima.")


def _date_args(inicio, fim):
    """('--date', d) para data única; ('--start', i, '--end', f) para intervalo."""
    return ("--date", inicio) if fim is None else ("--start", inicio, "--end", fim)


# ---------------------------------------------------------------------------
# Fluxos individuais — 1 função por script. O CLI vive SÓ aqui.
# (mude aqui se o CLI de um script mudar; notebook e rotinas chamam estas funções)
# ---------------------------------------------------------------------------

def boletim(inicio, fim, resultados=None) -> bool:
    """Boletim B3 (negócios). Na diária: inicio=X-1u, fim=X."""
    return run_step("scrape_b3_boletim", "--start", inicio, "--end", fim, resultados=resultados)

def anbima_deb(inicio, fim=None, resultados=None) -> bool:
    """Anbima debêntures (taxa indicativa). Aceita data única ou intervalo."""
    return run_step("scrape_anbima_debentures", *_date_args(inicio, fim), resultados=resultados)

def anbima_cricra(inicio, fim=None, resultados=None) -> bool:
    """Anbima CRI/CRA (taxa indicativa) — Playwright. Data única ou intervalo."""
    return run_step("scrape_anbima_cri_cra", *_date_args(inicio, fim), resultados=resultados)

def fianalytics(resultados=None) -> bool:
    """FI Analytics planilha (características) — Playwright + login. Sem data."""
    return run_step("scrape_fianalytics_planilha", resultados=resultados)

def anbima_data(inicio=None, fim=None, full=False, resultados=None) -> bool:
    """Anbima Data (características + fluxo) — Playwright.
    full=True → `--mode full` (universo completo, setup); senão intervalo incremental."""
    if full:
        return run_step("scrape_anbima_data_ativos", "--mode", "full", resultados=resultados)
    return run_step("scrape_anbima_data_ativos", *_date_args(inicio, fim), resultados=resultados)

def ntnb(inicio, fim=None, resultados=None) -> bool:
    """Anbima NTN-B (MtM). Na diária: intervalo X-1u..X."""
    return run_step("scrape_anbima_ntnb", *_date_args(inicio, fim), resultados=resultados)

def curva_di(d, resultados=None) -> bool:
    """Curva DI B3 (MtM). Só aceita data única — rodar 1× por pregão."""
    return run_step("scrape_b3_curva_di", "--date", d, resultados=resultados)

def outstanding(inicio, fim=None, resultados=None) -> bool:
    """Outstanding via Bloomberg — SÓ NO BANCO. Data única ou intervalo."""
    return run_step("scrape_outstanding_bloomberg", *_date_args(inicio, fim), resultados=resultados)

def calc_taxa(X, resultados=None) -> bool:
    """Calcula taxa por trade (cascata FI Analytics → B3). ANTES de filtrar."""
    return run_step("calc_taxa_negocios", "--date", X, resultados=resultados)

def filtrar(X, resultados=None) -> bool:
    """Classifica VALIDO / FUNDO / BROKER / PF."""
    return run_step("filtrar_trades", "--date", X, resultados=resultados)

def spread_anbima(d, resultados=None) -> bool:
    """Spread Anbima das indicativas na data d."""
    return run_step("calc_spread_anbima", "--date", d, resultados=resultados)

def match_ref(resultados=None) -> bool:
    """Preenche cdReferencia faltante (global, sem data). ANTES de spread_over."""
    return run_step("match_referencias", resultados=resultados)

def spread_over(X, resultados=None) -> bool:
    """Spread dos trades vs MtM (casado por dtNegocio)."""
    return run_step("calc_spread_over", "--date", X, resultados=resultados)

def relatorio(resultados=None) -> bool:
    """Regenera o relatório HTML (toda a base)."""
    return run_step("gerar_relatorio_credito", resultados=resultados)


# ---------------------------------------------------------------------------
# Cadeia diária — liquidação X
# ---------------------------------------------------------------------------

def run_dia(X: date | str, resultados: list | None = None,
            gerar_relatorio: bool = True) -> list:
    """Cadeia completa dos 13 passos para a liquidação X (ver [[11 - Pipeline de Execucao]]).
    Raspa Anbima deb/CRI/CRA de X E X-1u (Anbima casado por dtNegocio).
    Passe gerar_relatorio=False ao rodar em loop (relatório 1× no fim)."""
    res = resultados if resultados is not None else []
    Xant = dia_util_anterior(X)

    boletim(Xant, X, resultados=res)
    for d in (Xant, X):
        anbima_deb(d, resultados=res)
        anbima_cricra(d, resultados=res)
    fianalytics(resultados=res)
    anbima_data(Xant, X, resultados=res)
    ntnb(Xant, X, resultados=res)
    for d in (Xant, X):
        curva_di(d, resultados=res)

    calc_taxa(X, resultados=res)
    filtrar(X, resultados=res)
    for d in (Xant, X):
        spread_anbima(d, resultados=res)
    match_ref(resultados=res)
    spread_over(X, resultados=res)

    if gerar_relatorio:
        relatorio(resultados=res)
        _resumo(res)
    return res


def _run_cadeia_dias(dias: list[date], rotulo: str) -> list:
    """Roda a cadeia completa dos 13 passos para uma lista de liquidações `dias`
    (cronológica) e gera o relatório 1× no fim. Passos globais (fianalytics,
    anbima_data, match_ref) rodam uma vez sobre a janela inteira. Base das duas
    rotinas públicas: run_ultimos_n (padrão) e run_intervalo (range explícito)."""
    if not dias:
        raise SystemExit("Sem dias úteis para processar — confira as datas.")
    res: list[tuple[str, bool]] = []
    Xant0 = dia_util_anterior(dias[0])
    print(f"{rotulo} — liquidações {dias[0]} .. {dias[-1]} ({len(dias)} dias); X-1u da 1ª = {Xant0}")

    fianalytics(resultados=res)

    for X in dias:
        Xant = dia_util_anterior(X)
        boletim(Xant, X, resultados=res)
        anbima_deb(X, resultados=res)
        anbima_cricra(X, resultados=res)
        ntnb(Xant, X, resultados=res)
        curva_di(X, resultados=res)
    # a ponta X-1u da 1ª liquidação (fora do laço acima)
    anbima_deb(Xant0, resultados=res)
    anbima_cricra(Xant0, resultados=res)
    curva_di(Xant0, resultados=res)

    anbima_data(Xant0, dias[-1], resultados=res)

    for X in dias:
        calc_taxa(X, resultados=res)
        filtrar(X, resultados=res)
        spread_anbima(X, resultados=res)
    spread_anbima(Xant0, resultados=res)
    match_ref(resultados=res)
    for X in dias:
        spread_over(X, resultados=res)

    relatorio(resultados=res)
    _resumo(res)
    return res


def run_ultimos_n(n: int = 5) -> list:
    """MODO PADRÃO da rotina diária: reprocessa os últimos `n` dias úteis (pega
    alterações retroativas). É o que o run_diario.py roda sem argumentos."""
    return _run_cadeia_dias(ultimos_n_dias_uteis(n), f"Rotina diária (últimos {n})")


def run_intervalo(inicio: date | str, fim: date | str) -> list:
    """MODO INTERVALO: reprocessa TODOS os dias úteis de [inicio, fim] (inclusive).
    Use para refazer um período específico. Mesma cadeia da rotina diária."""
    return _run_cadeia_dias(dias_uteis_entre(inicio, fim), f"Intervalo {_iso(inicio)}..{_iso(fim)}")


# ---------------------------------------------------------------------------
# Setup inicial — bootstrap da base
# ---------------------------------------------------------------------------

def run_setup(inicio_boletim: date | str,
              dias_indicativas: int = 130,
              dias_curva_di: int = 20,
              dias_cricra: int = 5,
              rodar_outstanding: bool = False) -> list:
    """Bootstrap da base no banco (roda 1 vez). Raspa o histórico largo que cada
    fonte ainda entrega e roda a cadeia de cálculo sobre todos os pregões da
    janela do boletim. Tolerante a falha (segue em frente; resumo no fim).
    Ver [[13 - Migracao Banco]] §5.

      inicio_boletim   : 1º dia do boletim B3 (define a janela do relatório)
      dias_indicativas : janela (dias corridos) de deb/NTN-B — fonte guarda ~4 meses
      dias_curva_di    : nº de pregões da curva DI B3 (guarda ~20)
      dias_cricra      : nº de pregões de CRI/CRA (portal guarda ~5)
      rodar_outstanding: True só no banco (terminal Bloomberg)
    """
    res: list[tuple[str, bool]] = []
    hoje = date.today()
    ini_ind = hoje - timedelta(days=dias_indicativas)

    anbima_data(full=True, resultados=res)                 # universo completo (o mais pesado)
    fianalytics(resultados=res)
    anbima_deb(ini_ind, hoje, resultados=res)              # ~4 meses
    ntnb(ini_ind, hoje, resultados=res)                    # ~4 meses
    for d in ultimos_n_dias_uteis(dias_curva_di, ref=hoje):
        curva_di(d, resultados=res)                        # ~20 pregões
    for d in ultimos_n_dias_uteis(dias_cricra, ref=hoje):
        anbima_cricra(d, resultados=res)                   # ~5 pregões
    boletim(inicio_boletim, hoje, resultados=res)          # janela escolhida
    if rodar_outstanding:
        outstanding(inicio_boletim, hoje, resultados=res)  # só no banco

    dias = dias_uteis_entre(inicio_boletim, hoje)
    for X in dias:
        calc_taxa(X, resultados=res)
        filtrar(X, resultados=res)
        spread_anbima(X, resultados=res)
    match_ref(resultados=res)
    for X in dias:
        spread_over(X, resultados=res)

    relatorio(resultados=res)
    _resumo(res)
    return res
