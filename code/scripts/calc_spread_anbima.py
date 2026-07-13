"""
calc_spread_anbima.py
=====================
Calcula vrSpreadAnbima para cada ticker/data em AnbimaIndicativos.

O spread e uma propriedade do ticker na data (nao de cada negocio individual).
Fonte da taxa do ativo: AnbimaIndicativos.vrTaxaAnbima.
Fonte da taxa de referencia: MtmAnbima.vrTaxa (cdReferencia de InfoAtivos).

Logica por row em AnbimaIndicativos:
  1. Se vrTaxaAnbima IS NULL  -> vrSpreadAnbima = NULL (sem taxa Anbima)
  2. Se cdReferencia IS NULL         -> vrSpreadAnbima = NULL (ativo sem referencia em InfoAtivos)
  3. Se cdReferencia = 'FUNDING'     -> vrSpreadAnbima = vrTaxaAnbima (CDI+/%CDI: spread e a propria taxa)
  4. Senao:
       vrTaxa = SELECT vrTaxa FROM MtmAnbima WHERE cdTicker = cdReferencia AND dtReferencia = data
       - Se nao encontrar     -> vrSpreadAnbima = NULL (sem curva na data)
       - Se encontrar         -> ((1 + vrTaxaAnbima/100) / (1 + vrTaxa/100) - 1) * 100

O cdReferencia aponta para o ticker certo em MtmAnbima (ex: 'NTN-B 35' ou 'DI1F27').
Transparente para o script — apenas usa o valor de cdReferencia.

No relatorio: join NegociosProcessados JOIN AnbimaIndicativos ON cdTicker + dtLiquidacao.

CLI:
    python scripts/calc_spread_anbima.py --date 2026-06-03
    python scripts/calc_spread_anbima.py --start 2026-06-01 --end 2026-06-05
    python scripts/calc_spread_anbima.py --date 2026-06-03 --force
"""

import argparse
import sys
import traceback
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# Garante que code/ esteja no sys.path ao rodar como script
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.db import ObterBanco
from lib.logger import ObterLogger
from lib.email_outlook import EnviarEmailConclusao


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

SQL_BUSCAR_TICKERS = """
SELECT ai.cdTicker,
       ai.dtReferencia,
       ai.vrTaxaAnbima,
       ai.vrSpreadAnbima,
       ia.cdReferencia
FROM AnbimaIndicativos ai
LEFT JOIN InfoAtivos ia ON ia.cdTicker = ai.cdTicker
WHERE ai.dtReferencia = ?
"""

SQL_BUSCAR_TAXA = """
SELECT vrTaxa
FROM MtmAnbima
WHERE cdTicker = ?
  AND dtReferencia = ?
"""

SQL_ATUALIZAR_SPREAD = """
UPDATE AnbimaIndicativos
SET vrSpreadAnbima = ?
WHERE cdTicker = ?
  AND dtReferencia = ?
"""


# ---------------------------------------------------------------------------
# Tipos internos
# ---------------------------------------------------------------------------

@dataclass
class EstatisticasData:
    dtReferencia: str
    total: int = 0
    calculado: int = 0
    funding: int = 0       # subconjunto de calculado: cdReferencia = 'FUNDING'
    nullSemTaxa: int = 0   # vrTaxaAnbima IS NULL
    nullSemRef: int = 0    # cdReferencia IS NULL em InfoAtivos
    nullSemMtm: int = 0    # cdReferencia nao encontrado em MtmAnbima
    jaCalculado: int = 0   # ja tinha vrSpreadAnbima e --force nao foi passado


# ---------------------------------------------------------------------------
# Logica de spread para um ticker/data
# ---------------------------------------------------------------------------

def CalcularSpread(
    cdTicker: str,
    dtReferencia: str,
    vrTaxaAnbima: Optional[float],
    cdReferencia: Optional[str],
    conn,
    log,
    stats: "EstatisticasData",
) -> Optional[float]:
    """
    Calcula vrSpreadAnbima para um ticker/data.
    Retorna o valor calculado (ou None) e atualiza stats.
    Nao grava no banco — o caller e responsavel pelo UPDATE.
    """
    # 1. Sem taxa Anbima
    if vrTaxaAnbima is None:
        log.debug(
            "spread: %s/%s — vrTaxaAnbima NULL, spread=NULL",
            cdTicker, dtReferencia,
        )
        stats.nullSemTaxa += 1
        return None

    # 2. Sem referencia cadastrada em InfoAtivos
    if cdReferencia is None:
        log.debug(
            "spread: %s/%s — cdReferencia NULL em InfoAtivos, spread=NULL",
            cdTicker, dtReferencia,
        )
        stats.nullSemRef += 1
        return None

    # 3. FUNDING: spread = taxa propria (CDI+ / %CDI nao tem benchmark externo)
    if cdReferencia == "FUNDING":
        log.debug(
            "spread: %s/%s — FUNDING, spread=%.4f%%",
            cdTicker, dtReferencia, vrTaxaAnbima,
        )
        stats.calculado += 1
        stats.funding += 1
        return vrTaxaAnbima

    # 4. Busca taxa de referencia em MtmAnbima
    row = conn.execute(SQL_BUSCAR_TAXA, (cdReferencia, dtReferencia)).fetchone()

    if row is None:
        log.debug(
            "spread: %s/%s — cdReferencia=%s nao encontrado em MtmAnbima, spread=NULL",
            cdTicker, dtReferencia, cdReferencia,
        )
        stats.nullSemMtm += 1
        return None

    vrTaxa = row["vrTaxa"]

    # Spread over: ((1 + taxa_ativo/100) / (1 + taxa_ref/100) - 1) * 100
    vrSpread = ((1.0 + vrTaxaAnbima / 100.0) / (1.0 + vrTaxa / 100.0) - 1.0) * 100.0

    log.debug(
        "spread: %s/%s — cdReferencia=%s vrTaxa=%.4f%% vrTaxaAnbima=%.4f%% spread=%.4f%%",
        cdTicker, dtReferencia, cdReferencia, vrTaxa, vrTaxaAnbima, vrSpread,
    )
    stats.calculado += 1
    return vrSpread


# ---------------------------------------------------------------------------
# Processamento por data
# ---------------------------------------------------------------------------

def ProcessarData(conn, dtReferencia: str, log, force: bool) -> EstatisticasData:
    """
    Processa todos os tickers de uma dtReferencia em AnbimaIndicativos
    e atualiza vrSpreadAnbima.
    """
    stats = EstatisticasData(dtReferencia=dtReferencia)

    rows = conn.execute(SQL_BUSCAR_TICKERS, (dtReferencia,)).fetchall()
    stats.total = len(rows)

    if stats.total == 0:
        log.info(
            "spread: dtReferencia=%s — nenhum ticker em AnbimaIndicativos",
            dtReferencia,
        )
        return stats

    log.info(
        "spread: dtReferencia=%s — %d ticker(s) para processar",
        dtReferencia, stats.total,
    )

    updates: list[tuple[Optional[float], str, str]] = []

    for row in rows:
        cdTicker        = row["cdTicker"]
        vrTaxaAnbima    = row["vrTaxaAnbima"]
        vrSpreadAnterior = row["vrSpreadAnbima"]
        cdReferencia           = row["cdReferencia"]

        # Sem --force, pula tickers que ja tem spread calculado
        if not force and vrSpreadAnterior is not None:
            stats.jaCalculado += 1
            stats.calculado += 1
            continue

        vrSpread = CalcularSpread(
            cdTicker=cdTicker,
            dtReferencia=dtReferencia,
            vrTaxaAnbima=vrTaxaAnbima,
            cdReferencia=cdReferencia,
            conn=conn,
            log=log,
            stats=stats,
        )

        updates.append((vrSpread, cdTicker, dtReferencia))

    # Executa UPDATEs em batch
    if updates:
        conn.executemany(SQL_ATUALIZAR_SPREAD, updates)
        conn.commit()

    log.info(
        "spread: dtReferencia=%s — calculado=%d (funding=%d) "
        "nullSemTaxa=%d nullSemRef=%d nullSemMtm=%d jaCalculado=%d",
        dtReferencia,
        stats.calculado,
        stats.funding,
        stats.nullSemTaxa,
        stats.nullSemRef,
        stats.nullSemMtm,
        stats.jaCalculado,
    )
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def LerArgumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Calcula vrSpreadAnbima em AnbimaIndicativos usando taxas de referencia "
            "em MtmAnbima (NTN-B via Anbima / curva DI via B3)."
        )
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="Processa uma unica dtReferencia.",
    )
    group.add_argument(
        "--start",
        metavar="YYYY-MM-DD",
        help="Inicio do intervalo de dtReferencia (usar com --end).",
    )
    parser.add_argument(
        "--end",
        metavar="YYYY-MM-DD",
        help="Fim do intervalo de dtReferencia (obrigatorio com --start).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Recalcula spread mesmo para tickers que ja tem vrSpreadAnbima no banco.",
    )
    args = parser.parse_args()

    if args.start and not args.end:
        parser.error("--end e obrigatorio quando --start e informado.")
    if args.end and not args.start:
        parser.error("--start e obrigatorio quando --end e informado.")

    return args


def MontarIntervaloDatas(args: argparse.Namespace) -> list[str]:
    """Retorna lista de datas YYYY-MM-DD a processar."""
    if args.date:
        return [args.date]

    startDate = date.fromisoformat(args.start)
    endDate   = date.fromisoformat(args.end)

    if endDate < startDate:
        raise ValueError(f"--end ({args.end}) anterior a --start ({args.start})")

    datas: list[str] = []
    current = startDate
    while current <= endDate:
        datas.append(current.isoformat())
        current += timedelta(days=1)
    return datas


def MontarResumo(statsList: list[EstatisticasData]) -> str:
    lines = ["Resultado por dtReferencia (AnbimaIndicativos):", ""]
    header = (
        f"{'Data':<12}  {'Total':>6}  {'Calculado':>9}  {'Funding':>7}  "
        f"{'NullSemTaxa':>11}  {'NullSemRef':>10}  {'NullSemMtm':>10}  {'JaCalc':>6}"
    )
    separator = "-" * len(header)
    lines.append(header)
    lines.append(separator)

    totTotal     = 0
    totCalculado = 0
    totFunding   = 0
    totSemTaxa   = 0
    totSemRef    = 0
    totSemMtm    = 0
    totJaCalc    = 0

    for s in statsList:
        lines.append(
            f"{s.dtReferencia:<12}  {s.total:>6}  {s.calculado:>9}  {s.funding:>7}  "
            f"{s.nullSemTaxa:>11}  {s.nullSemRef:>10}  {s.nullSemMtm:>10}  {s.jaCalculado:>6}"
        )
        totTotal     += s.total
        totCalculado += s.calculado
        totFunding   += s.funding
        totSemTaxa   += s.nullSemTaxa
        totSemRef    += s.nullSemRef
        totSemMtm    += s.nullSemMtm
        totJaCalc    += s.jaCalculado

    lines.append(separator)
    lines.append(
        f"{'TOTAL':<12}  {totTotal:>6}  {totCalculado:>9}  {totFunding:>7}  "
        f"{totSemTaxa:>11}  {totSemRef:>10}  {totSemMtm:>10}  {totJaCalc:>6}"
    )

    totNull = totSemTaxa + totSemRef + totSemMtm
    if totNull > 0:
        lines.append("")
        lines.append(f"ATENCAO: {totNull} ticker(s) com vrSpreadAnbima = NULL:")
        if totSemTaxa > 0:
            lines.append(f"  - {totSemTaxa} sem vrTaxaAnbima em AnbimaIndicativos")
        if totSemRef > 0:
            lines.append(f"  - {totSemRef} sem cdReferencia em InfoAtivos (ativo sem referencia cadastrada)")
        if totSemMtm > 0:
            lines.append(f"  - {totSemMtm} sem taxa em MtmAnbima para o cdReferencia/dtReferencia")

    lines.append("")
    lines.append(
        "Legenda: Calculado=spread gravado em AnbimaIndicativos; "
        "Funding=spread=vrTaxaAnbima (CDI+/%CDI); "
        "JaCalc=ja existia (ignorado sem --force)."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Principal() -> None:
    log     = ObterLogger("calc_spread_anbima")
    args    = LerArgumentos()
    conn    = ObterBanco()
    summary = ""
    success = True

    try:
        datas = MontarIntervaloDatas(args)
        log.info(
            "spread: processando %d data(s): %s ... %s (force=%s)",
            len(datas), datas[0], datas[-1], args.force,
        )

        statsList: list[EstatisticasData] = []
        for dtReferencia in datas:
            s = ProcessarData(conn, dtReferencia, log, args.force)
            statsList.append(s)

        summary = MontarResumo(statsList)
        log.info("spread: concluido.\n%s", summary)

    except Exception:
        success = False
        summary = traceback.format_exc()
        log.exception("spread: erro inesperado")

    finally:
        conn.close()
        EnviarEmailConclusao(
            "calc_spread_anbima",
            success,
            summary,
            logger=log,
        )


if __name__ == "__main__":
    Principal()
