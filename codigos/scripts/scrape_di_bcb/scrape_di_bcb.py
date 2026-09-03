"""
scrape_di_bcb.py
================
Taxa DI **realizada** (divulgada dia a dia pelo BCB) → a tabela `DiHistorico`.

É insumo da calculadora de renda fixa (%CDI e CDI+) — ver `codigos/helpers/calc.py`.
Migrado do `atualizar_di.py` do projeto `calculadora-renda-fixa` (handoff 12/07/2026).
A outra metade daquele script — a **curva** DI×pré (projeção) — vive no
`scrape_b3_curva_di.py`, que já baixava o mesmo CSV da B3 e agora arquiva a curva
inteira em `a tabela `CurvaDi``.

Fonte: API SGS do Banco Central, duas séries da MESMA taxa:
  - série 4389 → DI anualizado, base 252, % a.a. (2 casas; referência)
  - série 12   → DI ao dia, fator diário, % ao dia (6 casas)
O **acúmulo** do PU Par usa a série 12: o fator diário de 6 casas reproduz a
calculadora da B3 a ~1e-5; a 4389, com 2 casas no anualizado, perde precisão ao
ser convertida em taxa diária.

Idempotente (INSERT OR REPLACE). Sem argumentos, é incremental: retoma do último
dia gravado (re-processa esse dia, overlap seguro). Base vazia → backfill desde
ANO_INICIAL. `--start/--end` força uma janela específica.

CLI:
    python codigos/scripts/scrape_di_bcb.py
    python codigos/scripts/scrape_di_bcb.py --start 2020-01-01 --end 2020-12-31
"""

import argparse
import json
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers"))

import pandas as pd

import dados as D
from email_outlook import EnviarEmailConclusao
from relatorio_execucao import RelatorioExecucao
from logger import ObterLogger

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

NOME_SCRIPT = "scrape_di_bcb"

URL_SGS = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{serie}/dados"
SERIE_ANUAL = "4389"   # DI anualizado, base 252, % a.a.
SERIE_DIARIA = "12"    # DI ao dia (fator diário), % ao dia

ANO_INICIAL = 2000     # backfill quando a base está vazia (cobre papéis antigos)
JANELA_ANOS = 9        # a SGS devolve 406 para janelas > 10 anos

TIMEOUT = 30
HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}

# ---------------------------------------------------------------------------
# API SGS
# ---------------------------------------------------------------------------

def BaixarSerie(client: httpx.Client, serie: str, inicio: date, fim: date) -> dict[str, float]:
    """{dtIso: valor} de uma série SGS numa janela (só dias úteis, é o que a API devolve)."""
    url = (f"{URL_SGS.format(serie=serie)}?formato=json"
           f"&dataInicial={inicio.strftime('%d/%m/%Y')}"
           f"&dataFinal={fim.strftime('%d/%m/%Y')}")
    resp = client.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()

    valores: dict[str, float] = {}
    for item in json.loads(resp.content):
        d, m, a = item["data"].split("/")
        valores[f"{a}-{m}-{d}"] = float(item["valor"])
    return valores


def BuscarDiRealizado(client: httpx.Client, inicio: date, fim: date, log) -> list[tuple]:
    """[(dtIso, taxaAnual, taxaDiaria)] em ordem cronológica. Quebra a janela em
    pedaços de JANELA_ANOS anos (limite de 10 anos por request da SGS)."""
    anual: dict[str, float] = {}
    diaria: dict[str, float] = {}

    ini = inicio
    while ini <= fim:
        fimJanela = min(fim, date(ini.year + JANELA_ANOS, ini.month, ini.day))
        log.debug("di_bcb: janela %s .. %s", ini, fimJanela)
        anual.update(BaixarSerie(client, SERIE_ANUAL, ini, fimJanela))
        diaria.update(BaixarSerie(client, SERIE_DIARIA, ini, fimJanela))
        ini = fimJanela + timedelta(days=1)

    faltamDiaria = [d for d in anual if d not in diaria]
    if faltamDiaria:
        log.warning("di_bcb: %d dia(s) com a série 4389 mas sem a série 12 (fator diário)",
                    len(faltamDiaria))

    return [(d, anual[d], diaria.get(d)) for d in sorted(anual)]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def LerArgumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Taxa DI realizada (BCB/SGS series 4389 e 12) -> a tabela `DiHistorico`."
    )
    parser.add_argument("--start", metavar="YYYY-MM-DD", help="início da janela (default: último dia gravado)")
    parser.add_argument("--end", metavar="YYYY-MM-DD", help="fim da janela (default: hoje)")
    return parser.parse_args()


def ResolverJanela(args: argparse.Namespace, log) -> tuple[date, date]:
    fim = date.fromisoformat(args.end) if args.end else date.today()

    if args.start:
        return date.fromisoformat(args.start), fim

    ultima = D.Escalar('SELECT MAX(dtReferencia) FROM "DiHistorico"')
    if ultima:
        # Re-processa o último dia gravado: overlap barato que fecha buracos de borda.
        return date.fromisoformat(ultima), fim

    log.info("di_bcb: base vazia — backfill desde %d-01-01", ANO_INICIAL)
    return date(ANO_INICIAL, 1, 1), fim


def MontarResumo(registros: list[tuple], inicio: date, fim: date, novos: int) -> str:
    linhas = [
        "Resultado:",
        f"Janela          : {inicio} .. {fim}",
        f"Dias recebidos  : {len(registros)}",
        f"Dias novos      : {novos}",
    ]
    if registros:
        dt, anual, diaria = registros[-1]
        linhas += ["", f"Última Taxa DI: {dt} = {anual}% a.a. / {diaria}% ao dia"]
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Principal() -> None:
    log = ObterLogger(NOME_SCRIPT)
    args = LerArgumentos()
    rel     = RelatorioExecucao(NOME_SCRIPT, args=vars(args))
    erro    = None
    summary = ""
    success = True

    try:
        inicio, fim = ResolverJanela(args, log)
        log.info("di_bcb: buscando DI realizado de %s a %s", inicio, fim)

        with httpx.Client(trust_env=True, verify=False) as client:
            registros = BuscarDiRealizado(client, inicio, fim, log)

        # Zero dia devolvido = fonte quebrada (a SGS sempre cobre a janela,
        # nem que seja com 1 pregão). Falha alto — regra do [[98 - Backlog]].
        if not registros:
            raise RuntimeError(
                f"BCB/SGS não devolveu nenhum dia entre {inicio} e {fim} — fonte quebrada."
            )

        antes = D.Escalar('SELECT COUNT(*) FROM "DiHistorico"') or 0
        D.Upsert("DiHistorico", pd.DataFrame(
            [{"dtReferencia": d, "vrTaxaDiAnual": a, "vrTaxaDiDiaria": x}
             for d, a, x in registros]))
        depois = D.Escalar('SELECT COUNT(*) FROM "DiHistorico"') or 0

        summary = MontarResumo(registros, inicio, fim, depois - antes)
        log.info("di_bcb: concluído.\n%s", summary)

    except Exception:
        success = False
        erro = traceback.format_exc()
        rel.Erro("A rodada abortou — ver traceback.")
        log.exception("di_bcb: erro inesperado")

    finally:
        if summary:
            rel.Secao("Resumo", ["saida"], [[l] for l in summary.splitlines() if l.strip()])
        EnviarEmailConclusao(NOME_SCRIPT, success, rel, tracebackErro=erro, logger=log)

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    Principal()
