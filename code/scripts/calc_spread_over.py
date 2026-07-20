"""
calc_spread_over.py
===================
Calcula vrSpreadOver para cada trade em NegociosProcessados com vrTaxaCalculada
preenchida, usando a taxa de referencia em MtmAnbima na dtNegocio do trade.

Logica por trade:
  1. Se vrTaxaCalculada IS NULL  -> vrSpreadOver = NULL (sem taxa, pula)
  2. Se cdReferencia IS NULL            -> vrSpreadOver = NULL (ativo sem referencia)
  3. Se cdReferencia = 'FUNDING'        -> vrSpreadOver = vrTaxaCalculada
  4. Senao (NTN-B ou DI1):
       vrTaxa = MtmAnbima WHERE cdTicker = cdReferencia AND dtReferencia = dtNegocio
       - Se nao encontrar        -> vrSpreadOver = NULL (sem curva na data)
       - Se encontrar            -> ((1 + vrTaxaCalculada/100) / (1 + vrTaxa/100) - 1) * 100

CLI:
    python scripts/calc_spread_over.py --date 2026-06-03
    python scripts/calc_spread_over.py --start 2026-06-01 --end 2026-06-05
"""

import argparse
import sys
import traceback
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.db import ObterBanco
from lib.logger import ObterLogger
from lib.email_outlook import EnviarEmailConclusao
from lib.relatorio_execucao import RelatorioExecucao


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

NOME_SCRIPT = "calc_spread_over"

SQL_BUSCAR_NEGOCIOS = """
    SELECT tp.idTrade,
           tp.cdTicker,
           tp.dtNegocio,
           tp.dtLiquidacao,
           tp.vrTaxaCalculada,
           ia.cdReferencia
    FROM   NegociosProcessados tp
    LEFT JOIN InfoAtivos ia ON ia.cdTicker = tp.cdTicker
    WHERE  tp.dtLiquidacao = ?
      AND  tp.vrTaxaCalculada IS NOT NULL
      AND  tp.cdStatus != 'BROKER'
"""

SQL_BUSCAR_TAXA = """
    SELECT vrTaxa
    FROM   MtmAnbima
    WHERE  cdTicker    = ?
      AND  dtReferencia = ?
"""

SQL_ATUALIZAR_SPREAD = """
    UPDATE NegociosProcessados
    SET    vrSpreadOver = ?
    WHERE  idTrade = ?
"""


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@dataclass
class EstatisticasData:
    dtLiquidacao: str
    total: int = 0
    calculado: int = 0
    funding: int = 0
    nullSemRef: int = 0
    nullSemMtm: int = 0


# ---------------------------------------------------------------------------
# Logica de spread por trade
# ---------------------------------------------------------------------------

def CalcularSpread(
    cdTicker: str,
    dtNegocio: str,
    vrTaxaCalculada: float,
    cdReferencia: Optional[str],
    conn,
    log,
    stats: EstatisticasData,
) -> Optional[float]:
    if cdReferencia is None:
        log.debug("spread_over: %s/%s — cdReferencia NULL, spread=NULL", cdTicker, dtNegocio)
        stats.nullSemRef += 1
        return None

    if cdReferencia == "FUNDING":
        log.debug("spread_over: %s/%s — FUNDING, spread=%.4f%%", cdTicker, dtNegocio, vrTaxaCalculada)
        stats.calculado += 1
        stats.funding += 1
        return vrTaxaCalculada

    row = conn.execute(SQL_BUSCAR_TAXA, (cdReferencia, dtNegocio)).fetchone()
    if row is None:
        log.debug(
            "spread_over: %s/%s — cdReferencia=%s sem taxa em MtmAnbima, spread=NULL",
            cdTicker, dtNegocio, cdReferencia,
        )
        stats.nullSemMtm += 1
        return None

    vrTaxa   = row["vrTaxa"]
    vrSpread = ((1.0 + vrTaxaCalculada / 100.0) / (1.0 + vrTaxa / 100.0) - 1.0) * 100.0

    log.debug(
        "spread_over: %s/%s — cdReferencia=%s vrTaxa=%.4f%% vrTaxa=%.4f%% spread=%.4f%%",
        cdTicker, dtNegocio, cdReferencia, vrTaxa, vrTaxaCalculada, vrSpread,
    )
    stats.calculado += 1
    return vrSpread


# ---------------------------------------------------------------------------
# Processamento por data
# ---------------------------------------------------------------------------

def ProcessarData(conn, dtLiquidacao: str, log) -> EstatisticasData:
    stats = EstatisticasData(dtLiquidacao=dtLiquidacao)

    trades = conn.execute(SQL_BUSCAR_NEGOCIOS, (dtLiquidacao,)).fetchall()
    stats.total = len(trades)

    if stats.total == 0:
        log.info("spread_over: dtLiquidacao=%s — nenhum trade com taxa", dtLiquidacao)
        return stats

    log.info("spread_over: dtLiquidacao=%s — %d trade(s)", dtLiquidacao, stats.total)

    updates: list[tuple] = []
    for trade in trades:
        vrSpread = CalcularSpread(
            cdTicker        = trade["cdTicker"],
            dtNegocio       = trade["dtNegocio"],
            vrTaxaCalculada = trade["vrTaxaCalculada"],
            cdReferencia           = trade["cdReferencia"],
            conn            = conn,
            log             = log,
            stats           = stats,
        )
        updates.append((vrSpread, trade["idTrade"]))

    conn.executemany(SQL_ATUALIZAR_SPREAD, updates)
    conn.commit()

    log.info(
        "spread_over: dtLiquidacao=%s — calculado=%d (funding=%d) nullSemRef=%d nullSemMtm=%d",
        dtLiquidacao, stats.calculado, stats.funding, stats.nullSemRef, stats.nullSemMtm,
    )
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def LerArgumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Calcula vrSpreadOver em NegociosProcessados usando MtmAnbima como referencia."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--date",  metavar="YYYY-MM-DD", help="dtLiquidacao unica.")
    group.add_argument("--start", metavar="YYYY-MM-DD", help="Inicio do intervalo.")
    parser.add_argument("--end",  metavar="YYYY-MM-DD", help="Fim do intervalo (requer --start).")
    args = parser.parse_args()
    if args.start and not args.end:
        parser.error("--end e obrigatorio com --start.")
    if args.end and not args.start:
        parser.error("--start e obrigatorio com --end.")
    return args


def MontarIntervaloDatas(args: argparse.Namespace) -> list[str]:
    if args.date:
        date.fromisoformat(args.date)   # valida: data inválida aborta em vez de sair mudo (0 linhas, exit 0)
        return [args.date]
    startDate = date.fromisoformat(args.start)
    endDate   = date.fromisoformat(args.end)
    if endDate < startDate:
        raise ValueError(f"--end ({args.end}) anterior a --start ({args.start})")
    datas, cur = [], startDate
    while cur <= endDate:
        datas.append(cur.isoformat())
        cur += timedelta(days=1)
    return datas


def MontarResumo(statsList: list[EstatisticasData]) -> str:
    header = f"{'Data':<12}  {'Total':>6}  {'Calculado':>9}  {'Funding':>7}  {'SemRef':>6}  {'SemMtm':>6}"
    sep    = "-" * len(header)
    lines  = ["Resultado por dtLiquidacao (NegociosProcessados):", "", header, sep]

    totTotal = totCalc = totFunding = totSemRef = totSemMtm = 0
    for s in statsList:
        lines.append(
            f"{s.dtLiquidacao:<12}  {s.total:>6}  {s.calculado:>9}  {s.funding:>7}  "
            f"{s.nullSemRef:>6}  {s.nullSemMtm:>6}"
        )
        totTotal   += s.total
        totCalc    += s.calculado
        totFunding += s.funding
        totSemRef  += s.nullSemRef
        totSemMtm  += s.nullSemMtm

    lines.append(sep)
    lines.append(
        f"{'TOTAL':<12}  {totTotal:>6}  {totCalc:>9}  {totFunding:>7}  "
        f"{totSemRef:>6}  {totSemMtm:>6}"
    )

    totNull = totSemRef + totSemMtm
    if totNull > 0:
        lines.append("")
        lines.append(f"ATENCAO: {totNull} trade(s) com vrSpreadOver = NULL:")
        if totSemRef > 0:
            lines.append(f"  - {totSemRef} sem cdReferencia em InfoAtivos")
        if totSemMtm > 0:
            lines.append(f"  - {totSemMtm} sem taxa em MtmAnbima para cdReferencia/dtLiquidacao")

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
        log.info("spread_over: processando %d data(s): %s ... %s", len(datas), datas[0], datas[-1])

        statsList: list[EstatisticasData] = []
        for dtLiquidacao in datas:
            s = ProcessarData(conn, dtLiquidacao, log)
            statsList.append(s)

        summary = MontarResumo(statsList)
        log.info("spread_over: concluido.\n%s", summary)

    except Exception:
        success = False
        erro = traceback.format_exc()
        rel.Erro("A rodada abortou — ver traceback.")
        log.exception("spread_over: erro inesperado")

    finally:
        conn.close()
        if summary:
            rel.Secao("Resumo", ["saida"], [[l] for l in summary.splitlines() if l.strip()])
        EnviarEmailConclusao(NOME_SCRIPT, success, rel, tracebackErro=erro, logger=log)

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    Principal()
