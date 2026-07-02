"""
pipeline_core.py
================
Lógica compartilhada de orquestração do pipeline. Usado por:
  - pipeline.ipynb        (interativo, 1 bloco por fluxo)
  - scripts/run_diario.py (entrypoint agendável no Task Scheduler)

Cada passo é executado como subprocesso do CLI que já existe em scripts/ — não
duplica lógica e mantém cada script independente (com seu próprio log/email).

Datas seguem a lógica de liquidação X / X-1u de [[11 - Pipeline de Execucao]]:
para a liquidação X, os negócios vêm das pontas X-1u (D+1) e X (D+0).

Funções principais:
  ultimos_n_dias_uteis(n)   -> últimos n dias úteis (cronológico)
  dia_util_anterior(d)      -> X-1u
  run_dia(X)                -> cadeia dos 13 passos para a liquidação X
  run_ultimos_n(n)          -> rotina diária (n dias úteis) + relatório no fim
  run_setup(...)            -> bootstrap inicial (histórico largo por fonte)

Uso típico:
    from pipeline_core import run_ultimos_n, run_setup
    run_ultimos_n(5)
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


# ---------------------------------------------------------------------------
# Cadeia diária — liquidação X
# ---------------------------------------------------------------------------

def run_dia(X: date | str, resultados: list | None = None,
            gerar_relatorio: bool = True) -> list:
    """Cadeia completa dos 13 passos para a liquidação X (ver [[11 - Pipeline de Execucao]]).
    Raspa Anbima deb/CRI/CRA de X E X-1u (forward-compatible com Anbima por dtNegocio).
    Passe gerar_relatorio=False ao rodar em loop (rodar o relatório 1× no fim)."""
    res = resultados if resultados is not None else []
    X = date.fromisoformat(X) if isinstance(X, str) else X
    Xant = dia_util_anterior(X)

    # --- scraping (rede) ---
    run_step("scrape_b3_boletim", "--start", Xant, "--end", X, resultados=res)
    for d in (Xant, X):
        run_step("scrape_anbima_debentures", "--date", d, resultados=res)
        run_step("scrape_anbima_cri_cra", "--date", d, resultados=res)
    run_step("scrape_fianalytics_planilha", resultados=res)
    run_step("scrape_anbima_data_ativos", "--start", Xant, "--end", X, resultados=res)
    run_step("scrape_anbima_ntnb", "--start", Xant, "--end", X, resultados=res)
    for d in (Xant, X):
        run_step("scrape_b3_curva_di", "--date", d, resultados=res)

    # --- cálculo (local) ---
    run_step("calc_taxa_negocios", "--date", X, resultados=res)
    run_step("filtrar_trades", "--date", X, resultados=res)
    for d in (Xant, X):
        run_step("calc_spread_anbima", "--date", d, resultados=res)
    run_step("match_referencias", resultados=res)
    run_step("calc_spread_over", "--date", X, resultados=res)

    if gerar_relatorio:
        run_step("gerar_relatorio_credito", resultados=res)
        _resumo(res)
    return res


def run_ultimos_n(n: int = 5) -> list:
    """Rotina diária: roda a cadeia para os últimos n dias úteis (para pegar
    alterações retroativas) e gera o relatório 1× no fim.

    Passos globais (fianalytics, anbima_data_ativos, match_referencias) rodam
    uma vez sobre a janela inteira, não por dia."""
    res: list[tuple[str, bool]] = []
    dias = ultimos_n_dias_uteis(n)
    Xant0 = dia_util_anterior(dias[0])
    print(f"Rotina diária — liquidações {dias[0]} .. {dias[-1]} (n={n}); X-1u da 1ª = {Xant0}")

    # 1. Global (uma vez)
    run_step("scrape_fianalytics_planilha", resultados=res)

    # 2. Scraping per-date (boletim, Anbima indicativas, MtM)
    for X in dias:
        Xant = dia_util_anterior(X)
        run_step("scrape_b3_boletim", "--start", Xant, "--end", X, resultados=res)
        run_step("scrape_anbima_debentures", "--date", X, resultados=res)
        run_step("scrape_anbima_cri_cra", "--date", X, resultados=res)
        run_step("scrape_anbima_ntnb", "--start", Xant, "--end", X, resultados=res)
        run_step("scrape_b3_curva_di", "--date", X, resultados=res)
    # a ponta X-1u da 1ª liquidação (fora do laço acima)
    run_step("scrape_anbima_debentures", "--date", Xant0, resultados=res)
    run_step("scrape_anbima_cri_cra", "--date", Xant0, resultados=res)
    run_step("scrape_b3_curva_di", "--date", Xant0, resultados=res)

    # 3. Características/fluxo dos ativos da janela (incremental)
    run_step("scrape_anbima_data_ativos", "--start", Xant0, "--end", dias[-1], resultados=res)

    # 4. Cálculo por dia
    for X in dias:
        run_step("calc_taxa_negocios", "--date", X, resultados=res)
        run_step("filtrar_trades", "--date", X, resultados=res)
        run_step("calc_spread_anbima", "--date", X, resultados=res)
    run_step("calc_spread_anbima", "--date", Xant0, resultados=res)
    run_step("match_referencias", resultados=res)
    for X in dias:
        run_step("calc_spread_over", "--date", X, resultados=res)

    # 5. Relatório (uma vez, toda a base)
    run_step("gerar_relatorio_credito", resultados=res)
    _resumo(res)
    return res


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
    janela do boletim. Ver [[13 - Migracao Banco]] §5.

      inicio_boletim   : 1º dia do boletim B3 a raspar (define a janela do relatório)
      dias_indicativas : janela (dias corridos) de deb/NTN-B — fonte guarda ~4 meses
      dias_curva_di    : nº de pregões da curva DI B3 (guarda ~20)
      dias_cricra      : nº de pregões de CRI/CRA (portal guarda ~5)
      rodar_outstanding: True só no banco (terminal Bloomberg)
    """
    res: list[tuple[str, bool]] = []
    hoje = date.today()
    ini_ind = hoje - timedelta(days=dias_indicativas)

    # 1. Anbima Data — universo completo (características + fluxo). O passo mais pesado.
    run_step("scrape_anbima_data_ativos", "--mode", "full", resultados=res)
    # 2. FI Analytics — snapshot de características
    run_step("scrape_fianalytics_planilha", resultados=res)
    # 3. Indicativas deb + NTN-B (~4 meses)
    run_step("scrape_anbima_debentures", "--start", ini_ind, "--end", hoje, resultados=res)
    run_step("scrape_anbima_ntnb", "--start", ini_ind, "--end", hoje, resultados=res)
    # 4. Curva DI (~20 pregões) e CRI/CRA (~5 pregões) — por pregão
    for d in ultimos_n_dias_uteis(dias_curva_di, ref=hoje):
        run_step("scrape_b3_curva_di", "--date", d, resultados=res)
    for d in ultimos_n_dias_uteis(dias_cricra, ref=hoje):
        run_step("scrape_anbima_cri_cra", "--date", d, resultados=res)
    # 5. Boletim B3 (janela escolhida)
    run_step("scrape_b3_boletim", "--start", inicio_boletim, "--end", hoje, resultados=res)
    # 6. Outstanding (só no banco)
    if rodar_outstanding:
        run_step("scrape_outstanding_bloomberg", "--start", inicio_boletim, "--end", hoje, resultados=res)

    # 7. Cadeia de cálculo sobre cada liquidação da janela
    dias = dias_uteis_entre(inicio_boletim, hoje)
    for X in dias:
        run_step("calc_taxa_negocios", "--date", X, resultados=res)
        run_step("filtrar_trades", "--date", X, resultados=res)
        run_step("calc_spread_anbima", "--date", X, resultados=res)
    run_step("match_referencias", resultados=res)
    for X in dias:
        run_step("calc_spread_over", "--date", X, resultados=res)

    run_step("gerar_relatorio_credito", resultados=res)
    _resumo(res)
    return res
