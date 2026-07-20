"""
scrape_outstanding_bloomberg.py
===============================
Puxa o outstanding (saldo em circulacao / AMT_OUTSTANDING) dos ativos da
Bloomberg e popula a tabela Outstanding.

Para cada dia do intervalo informado, monta o conjunto de tickers a buscar a
partir de duas fontes:
  1. Negociados  — tickers com dtNegocio naquele dia em NegociosBrutos
                   (exclui cdSituacao = 'Cancelado').
  2. Anbima      — tickers com dtReferencia naquele dia em AnbimaIndicativos
                   (debentures / CRI / CRA divulgados pela Anbima).

A uniao por (cdTicker, data) define quais outstandings buscar e em qual data.
Um ticker tradado em 03/06 e divulgado em 04/06 gera dois pares — busca o
outstanding em cada data. NTN-B e DI1 nao entram (vivem em MtmAnbima, nao
nessas tabelas); filtrados por seguranca.

A busca na Bloomberg agrupa os pares por data e faz UMA chamada bdp por data
(override AMOUNT_OUTSTANDING_AS_OF_DT vale para todos os tickers da chamada).

IMPORTANTE: so roda em ambiente com terminal Bloomberg logado (xbbg). O import
do xbbg e feito sob demanda, dentro do fetch, para o resto do script (montagem
das listas via SQL) ser inspecionavel sem o pacote instalado.

CLI:
    python scripts/scrape_outstanding_bloomberg.py --date 2026-06-03
    python scripts/scrape_outstanding_bloomberg.py --start 2026-06-01 --end 2026-06-05
    python scripts/scrape_outstanding_bloomberg.py --start ... --end ... --force
"""

import argparse
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.db import ObterBanco
from lib.logger import ObterLogger
from lib.email_outlook import EnviarEmailConclusao
from lib.relatorio_execucao import RelatorioExecucao

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

NOME_SCRIPT = "scrape_outstanding_bloomberg"

# Prefixos de tickers que nao sao ativos de credito privado (vem de MtmAnbima).
PREFIXOS_EXCLUIDOS = ("NTN-B", "DI1")

SQL_TICKERS_NEGOCIADOS = """
SELECT DISTINCT cdTicker
FROM NegociosBrutos
WHERE dtNegocio = ?
  AND cdSituacao != 'Cancelado'
"""

SQL_TICKERS_ANBIMA = """
SELECT DISTINCT cdTicker
FROM AnbimaIndicativos
WHERE dtReferencia = ?
"""

# Pares (cdTicker, data) ja resolvidos — pulados sem --force.
SQL_JA_GRAVADOS = """
SELECT cdTicker
FROM Outstanding
WHERE dtOutstanding = ?
  AND vrOutstanding IS NOT NULL
"""

SQL_UPSERT = """
INSERT INTO Outstanding (cdTicker, dtOutstanding, vrOutstanding)
VALUES (?, ?, ?)
ON CONFLICT(cdTicker, dtOutstanding) DO UPDATE SET
    vrOutstanding = excluded.vrOutstanding
"""


# ---------------------------------------------------------------------------
# Selecao de tickers
# ---------------------------------------------------------------------------

def ExcluiTicker(cdTicker: str) -> bool:
    """True se o ticker deve ser ignorado (NTN-B / DI1)."""
    return cdTicker.upper().startswith(PREFIXOS_EXCLUIDOS)


def TickersDaData(conn, d: date, log) -> set[str]:
    """Tickers negociados + divulgados pela Anbima na data d (NTN-B/DI1 fora)."""
    dStr = d.isoformat()

    negociados = {r[0] for r in conn.execute(SQL_TICKERS_NEGOCIADOS, (dStr,))}
    anbima     = {r[0] for r in conn.execute(SQL_TICKERS_ANBIMA, (dStr,))}

    tickers = {t for t in (negociados | anbima) if t and not ExcluiTicker(t)}

    log.info(
        "outstanding: %s — %d negociados, %d Anbima, %d unicos (apos filtro)",
        dStr, len(negociados), len(anbima), len(tickers),
    )
    return tickers


def FiltrarJaGravados(conn, d: date, tickers: set[str], log) -> set[str]:
    """Remove tickers que ja tem outstanding gravado nessa data."""
    gravados = {r[0] for r in conn.execute(SQL_JA_GRAVADOS, (d.isoformat(),))}
    pendentes = tickers - gravados
    if gravados:
        log.info(
            "outstanding: %s — %d ja gravados (cache), %d a buscar",
            d.isoformat(), len(tickers & gravados), len(pendentes),
        )
    return pendentes


# ---------------------------------------------------------------------------
# Bloomberg
# ---------------------------------------------------------------------------

def BuscarOutstanding(tickers: list[str], d: date, log) -> dict[str, float | None]:
    """
    Busca AMT_OUTSTANDING de varios tickers numa unica chamada bdp, com o
    override de data AMOUNT_OUTSTANDING_AS_OF_DT. Retorna {ticker: valor|None}.

    O import do xbbg e local: o pacote so existe no ambiente Bloomberg do banco.
    """
    from xbbg import blp

    bbgTickers = [f"{t} Corp" for t in tickers]

    log.info("outstanding: bdp %s — %d tickers", d.isoformat(), len(bbgTickers))
    df = blp.bdp(
        tickers=bbgTickers,
        flds="AMT_OUTSTANDING",
        AMOUNT_OUTSTANDING_AS_OF_DT=d.strftime("%Y%m%d"),
    )

    resultado: dict[str, float | None] = {t: None for t in tickers}

    if df is None or df.empty:
        log.warning("outstanding: bdp %s nao retornou linhas", d.isoformat())
        return resultado

    # df vem indexado por '<TICKER> Corp', coluna 'amt_outstanding'.
    col = "amt_outstanding"
    if col not in df.columns:
        log.warning("outstanding: bdp %s sem coluna '%s' (cols=%s)",
                    d.isoformat(), col, list(df.columns))
        return resultado

    for t in tickers:
        idx = f"{t} Corp"
        if idx not in df.index:
            continue
        raw = df.loc[idx, col]
        try:
            val = float(raw)
        except (TypeError, ValueError):
            val = None
        if val is not None and val == val:  # descarta NaN
            resultado[t] = val

    return resultado


# ---------------------------------------------------------------------------
# Processamento por data
# ---------------------------------------------------------------------------

def ProcessarData(conn, d: date, force: bool, log) -> tuple[int, int, int]:
    """
    Processa uma data: monta tickers, busca outstanding, grava.
    Retorna (tickers_alvo, gravados_com_valor, sem_valor).
    """
    tickers = TickersDaData(conn, d, log)
    nAlvo = len(tickers)
    if not tickers:
        return 0, 0, 0

    if not force:
        tickers = FiltrarJaGravados(conn, d, tickers, log)
    if not tickers:
        return nAlvo, 0, 0

    valores = BuscarOutstanding(sorted(tickers), d, log)

    dStr = d.isoformat()
    upsertRows = [(t, dStr, v) for t, v in valores.items()]
    nComValor = sum(1 for v in valores.values() if v is not None)
    nSemValor = len(valores) - nComValor

    conn.executemany(SQL_UPSERT, upsertRows)
    conn.commit()

    log.info("outstanding: %s — %d gravados (%d com valor, %d sem)",
             dStr, len(upsertRows), nComValor, nSemValor)
    return nAlvo, nComValor, nSemValor


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def LerArgumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Puxa outstanding (AMT_OUTSTANDING) da Bloomberg e popula Outstanding."
    )
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--date",  metavar="YYYY-MM-DD", help="Data unica.")
    grp.add_argument("--start", metavar="YYYY-MM-DD", help="Inicio do intervalo.")
    parser.add_argument("--end", metavar="YYYY-MM-DD", help="Fim do intervalo (requer --start).")
    parser.add_argument("--force", action="store_true",
                        help="Rebusca mesmo os pares (ticker, data) ja gravados.")
    args = parser.parse_args()
    if args.start and not args.end:
        parser.error("--end e obrigatorio com --start.")
    if args.end and not args.start:
        parser.error("--start e obrigatorio com --end.")
    return args


def MontarIntervaloDatas(args: argparse.Namespace) -> list[date]:
    if args.date:
        return [date.fromisoformat(args.date)]
    startDate = date.fromisoformat(args.start)
    endDate   = date.fromisoformat(args.end)
    if endDate < startDate:
        raise ValueError(f"--end ({args.end}) anterior a --start ({args.start})")
    datas, cur = [], startDate
    while cur <= endDate:
        datas.append(cur)
        cur += timedelta(days=1)
    return datas


def MontarResumo(results: list[tuple[str, int, int, int]]) -> str:
    lines = ["Resultado por data:", ""]
    lines.append(f"{'Data':<12}  {'Alvo':>6}  {'Com valor':>10}  {'Sem valor':>10}")
    lines.append("-" * 44)
    totAlvo = totCom = totSem = 0
    for dtStr, nAlvo, nCom, nSem in results:
        lines.append(f"{dtStr:<12}  {nAlvo:>6}  {nCom:>10}  {nSem:>10}")
        totAlvo += nAlvo
        totCom  += nCom
        totSem  += nSem
    lines.append("-" * 44)
    lines.append(f"{'TOTAL':<12}  {totAlvo:>6}  {totCom:>10}  {totSem:>10}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Principal() -> None:
    log     = ObterLogger(NOME_SCRIPT)
    args    = LerArgumentos()
    conn    = ObterBanco()
    rel     = RelatorioExecucao(NOME_SCRIPT)
    erro    = None
    summary = ""
    success = True

    try:
        datas = MontarIntervaloDatas(args)
        results: list[tuple[str, int, int, int]] = []

        log.info("outstanding: processando %d data(s): %s ... %s",
                 len(datas), datas[0], datas[-1])

        for d in datas:
            nAlvo, nCom, nSem = ProcessarData(conn, d, args.force, log)
            results.append((d.isoformat(), nAlvo, nCom, nSem))

        summary = MontarResumo(results)
        log.info("outstanding: concluido.\n%s", summary)

    except Exception:
        success = False
        erro = traceback.format_exc()
        rel.Erro("A rodada abortou — ver traceback.")
        log.exception("outstanding: erro inesperado")

    finally:
        conn.close()
        if summary:
            rel.Secao("Resumo", ["saida"], [[l] for l in summary.splitlines() if l.strip()])
        EnviarEmailConclusao(NOME_SCRIPT, success, rel, tracebackErro=erro, logger=log)

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    Principal()
