"""
calc_taxa_negocios.py
=====================
Para cada trade em NegociosBrutos com dtLiquidacao na janela informada:
  - Se vrTaxaNegocio NOT NULL: copia para vrTaxaCalculada (cdFonteTaxa = NULL)
  - Se vrTaxaNegocio IS NULL:  cascata FI Analytics -> B3 Calculator

Grava resultado em NegociosProcessados via UPSERT.
Trades com cdSituacao = 'Cancelado' sao ignorados.

CLI:
    python scripts/calc_taxa_negocios.py --date 2026-05-29
    python scripts/calc_taxa_negocios.py --start 2026-05-01 --end 2026-05-29
"""

import argparse
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# Garante que code/ esteja no sys.path ao rodar como script
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.config import cfg
from lib.db import get_db
from lib.logger import get_logger
from lib.email_outlook import send_completion_email
from lib.fianalytics_api import CalcRate
from lib.b3_calc_api import CalcYield


# ---------------------------------------------------------------------------
# Tipos internos
# ---------------------------------------------------------------------------

@dataclass
class _TradeRaw:
    idTrade: int
    cdTicker: str
    cdEmissor: str
    cdInstrumento: str
    dtNegocio: str
    dtLiquidacao: str
    vrQuantidade: int
    vrPU: float
    vrVolume: float
    vrTaxaNegocio: Optional[float]


@dataclass
class _AlertaTrade:
    cdTicker:  str
    cdEmissor: str
    vrVolume:  float


@dataclass
class _DateStats:
    dtLiquidacao: str
    total: int = 0
    direta: int = 0       # vrTaxaNegocio copiado direto (sem chamada de API)
    fianalytics: int = 0  # calculada via FI Analytics
    b3: int = 0           # calculada via B3 Calculator
    semTaxa: int = 0      # vrTaxaCalculada = NULL (todas as calculadoras falharam)
    alertas: list[_AlertaTrade] = field(default_factory=list)  # sem taxa + volume >= threshold


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_SQL_FETCH_TRADES = """
SELECT idTrade, cdTicker, cdEmissor, cdInstrumento,
       dtNegocio, dtLiquidacao, vrQuantidade, vrPU, vrVolume, vrTaxaNegocio
FROM NegociosBrutos
WHERE dtLiquidacao = ?
  AND cdSituacao != 'Cancelado'
"""

_SQL_FETCH_EXISTING = """
SELECT idTrade, vrTaxaCalculada, cdFonteTaxa
FROM NegociosProcessados
WHERE dtLiquidacao = ?
  AND vrTaxaCalculada IS NOT NULL
"""

_SQL_UPSERT = """
INSERT INTO NegociosProcessados (
    idTrade, cdTicker, cdEmissor, dtNegocio, dtLiquidacao,
    vrQuantidade, vrPU, vrVolume, vrTaxaCalculada, cdFonteTaxa,
    vrDuration, vrSpreadOver, idGrupoNegocio, cdStatus, dtProcessamento
) VALUES (
    :idTrade, :cdTicker, :cdEmissor, :dtNegocio, :dtLiquidacao,
    :vrQuantidade, :vrPU, :vrVolume, :vrTaxaCalculada, :cdFonteTaxa,
    NULL, NULL, NULL, 'VALIDO', CURRENT_TIMESTAMP
)
ON CONFLICT(idTrade) DO UPDATE SET
    vrTaxaCalculada = excluded.vrTaxaCalculada,
    cdFonteTaxa    = excluded.cdFonteTaxa,
    dtProcessamento   = excluded.dtProcessamento
"""


# ---------------------------------------------------------------------------
# Logica de cascata
# ---------------------------------------------------------------------------

def _ApplyCascata(trade: _TradeRaw, log) -> tuple[Optional[float], Optional[str]]:
    """
    Retorna (vrTaxaCalculada, cdFonteTaxa).

    Cascata:
      1. vrTaxaNegocio nao nulo -> copia direto (source = None)
      2. FI Analytics           -> source = 'FiAnalytics'
      3. B3 Calculator          -> source = 'B3'
      4. Falhou tudo            -> (None, None)
    """
    if trade.vrTaxaNegocio is not None:
        return trade.vrTaxaNegocio, None

    log.debug(
        "calc_taxa: vrTaxaNegocio NULL para %s/%s/%s — tentando FI Analytics",
        trade.cdTicker, trade.dtLiquidacao, trade.vrPU,
    )

    taxa = CalcRate(trade.cdTicker, trade.cdInstrumento, trade.dtLiquidacao, trade.vrPU)
    if taxa is not None:
        return taxa, "FiAnalytics"

    log.debug(
        "calc_taxa: FI Analytics falhou para %s — tentando B3 Calculator",
        trade.cdTicker,
    )

    taxa = CalcYield(trade.cdTicker, trade.dtLiquidacao, trade.vrPU)
    if taxa is not None:
        return taxa, "B3"

    log.warning(
        "calc_taxa: todas as calculadoras falharam para idTrade=%d cdTicker=%s dtLiquidacao=%s",
        trade.idTrade, trade.cdTicker, trade.dtLiquidacao,
    )
    return None, None


# ---------------------------------------------------------------------------
# Processamento por data
# ---------------------------------------------------------------------------

def _ProcessDate(conn, dtLiquidacao: str, log, workers: int, force: bool) -> _DateStats:
    """Processa todos os trades de uma dtLiquidacao e faz UPSERT em NegociosProcessados."""
    stats = _DateStats(dtLiquidacao=dtLiquidacao)

    rows = conn.execute(_SQL_FETCH_TRADES, (dtLiquidacao,)).fetchall()
    stats.total = len(rows)

    if stats.total == 0:
        log.info("calc_taxa: dtLiquidacao=%s — nenhum trade encontrado em NegociosBrutos", dtLiquidacao)
        return stats

    trades = [
        _TradeRaw(
            idTrade=row["idTrade"],
            cdTicker=row["cdTicker"],
            cdEmissor=row["cdEmissor"],
            cdInstrumento=row["cdInstrumento"],
            dtNegocio=row["dtNegocio"],
            dtLiquidacao=row["dtLiquidacao"],
            vrQuantidade=row["vrQuantidade"],
            vrPU=row["vrPU"],
            vrVolume=row["vrVolume"],
            vrTaxaNegocio=row["vrTaxaNegocio"],
        )
        for row in rows
    ]

    # Carrega taxa já calculada (exceto se --force)
    existing: dict[int, tuple[Optional[float], Optional[str]]] = {}
    if not force:
        for row in conn.execute(_SQL_FETCH_EXISTING, (dtLiquidacao,)).fetchall():
            existing[row["idTrade"]] = (row["vrTaxaCalculada"], row["cdFonteTaxa"])

    # Separa trades que já têm taxa (cached) dos que precisam de API
    cached:     list[tuple[_TradeRaw, Optional[float], Optional[str]]] = []
    to_process: list[_TradeRaw] = []

    for trade in trades:
        if trade.idTrade in existing:
            cached.append((trade, *existing[trade.idTrade]))
        else:
            to_process.append(trade)

    n_cached = len(cached)
    n_api    = len(to_process)

    if force:
        log.info(
            "calc_taxa: dtLiquidacao=%s — %d trade(s), --force ativo, recalculando tudo (workers=%d)",
            dtLiquidacao, stats.total, workers,
        )
    else:
        log.info(
            "calc_taxa: dtLiquidacao=%s — %d trade(s): %d via API, %d já calculados (mantidos)",
            dtLiquidacao, stats.total, n_api, n_cached,
        )

    # Fase paralela: API calls apenas para to_process
    api_results: list[tuple[_TradeRaw, Optional[float], Optional[str]]] = []

    if to_process:
        done_api   = 0
        start_time = time.monotonic()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_trade = {executor.submit(_ApplyCascata, t, log): t for t in to_process}
            for future in as_completed(future_to_trade):
                trade = future_to_trade[future]
                try:
                    taxa, source = future.result()
                except Exception:
                    log.exception(
                        "calc_taxa: erro inesperado para idTrade=%d cdTicker=%s",
                        trade.idTrade, trade.cdTicker,
                    )
                    taxa, source = None, None

                done_api += 1
                api_results.append((trade, taxa, source))

                elapsed  = time.monotonic() - start_time
                taxa_str = f"{taxa:.4f}%" if taxa is not None else "sem taxa"
                origem   = source if source else ("direta" if trade.vrTaxaNegocio is not None else "sem taxa")
                if done_api >= 2:
                    eta     = elapsed / done_api * (n_api - done_api)
                    eta_str = f" | ~{eta:.0f}s restante"
                else:
                    eta_str = ""
                log.info(
                    "[%d/%d] %s → %s %s | %.0fs elapsed%s",
                    done_api, n_api, trade.cdTicker, origem, taxa_str, elapsed, eta_str,
                )

    # Fase sequencial: contagem de stats + UPSERTs (só para to_process)
    # Trades cached: apenas contagem, sem UPSERT (já estão corretos no banco)
    volumeMin: float = cfg["alerta"]["volumeMinSemTaxa"]

    def _CountStats(trade: _TradeRaw, vrTaxaCalculada: Optional[float], cdFonteTaxa: Optional[str]) -> None:
        if trade.vrTaxaNegocio is not None:
            stats.direta += 1
        elif cdFonteTaxa == "FiAnalytics":
            stats.fianalytics += 1
        elif cdFonteTaxa == "B3":
            stats.b3 += 1
        else:
            stats.semTaxa += 1
            if trade.vrVolume >= volumeMin:
                stats.alertas.append(_AlertaTrade(trade.cdTicker, trade.cdEmissor, trade.vrVolume))

    for trade, vrTaxaCalculada, cdFonteTaxa in cached:
        _CountStats(trade, vrTaxaCalculada, cdFonteTaxa)

    for trade, vrTaxaCalculada, cdFonteTaxa in api_results:
        _CountStats(trade, vrTaxaCalculada, cdFonteTaxa)
        conn.execute(_SQL_UPSERT, {
            "idTrade":         trade.idTrade,
            "cdTicker":        trade.cdTicker,
            "cdEmissor":       trade.cdEmissor,
            "dtNegocio":       trade.dtNegocio,
            "dtLiquidacao":    trade.dtLiquidacao,
            "vrQuantidade":    trade.vrQuantidade,
            "vrPU":            trade.vrPU,
            "vrVolume":        trade.vrVolume,
            "vrTaxaCalculada": vrTaxaCalculada,
            "cdFonteTaxa":    cdFonteTaxa,
        })

    conn.commit()

    log.info(
        "calc_taxa: dtLiquidacao=%s — direta=%d fianalytics=%d b3=%d semTaxa=%d",
        dtLiquidacao, stats.direta, stats.fianalytics, stats.b3, stats.semTaxa,
    )
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _ParseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Calcula vrTaxaCalculada para trades de crédito privado (cascata FI Analytics -> B3)."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="Processa uma única dtLiquidacao.",
    )
    group.add_argument(
        "--start",
        metavar="YYYY-MM-DD",
        help="Início do intervalo de dtLiquidacao (usar com --end).",
    )
    parser.add_argument(
        "--end",
        metavar="YYYY-MM-DD",
        help="Fim do intervalo de dtLiquidacao (obrigatório com --start).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        metavar="N",
        help="Número de threads paralelas para chamadas de API (padrão: 8).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Recalcula taxa mesmo para trades que já têm vrTaxaCalculada no banco.",
    )
    args = parser.parse_args()

    if args.start and not args.end:
        parser.error("--end é obrigatório quando --start é informado.")
    if args.end and not args.start:
        parser.error("--start é obrigatório quando --end é informado.")

    return args


def _BuildDateRange(args: argparse.Namespace) -> list[str]:
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


def _BuildSummary(statsList: list[_DateStats]) -> str:
    lines = ["Resultado por dtLiquidacao:", ""]
    lines.append(
        f"{'Data':<12}  {'Total':>6}  {'Direta':>7}  {'FIAnaly':>7}  {'B3':>5}  {'SemTaxa':>8}"
    )
    lines.append("-" * 56)

    totalGeral    = 0
    diretaGeral   = 0
    fiGeral       = 0
    b3Geral       = 0
    semTaxaGeral  = 0

    for s in statsList:
        lines.append(
            f"{s.dtLiquidacao:<12}  {s.total:>6}  {s.direta:>7}  "
            f"{s.fianalytics:>7}  {s.b3:>5}  {s.semTaxa:>8}"
        )
        totalGeral   += s.total
        diretaGeral  += s.direta
        fiGeral      += s.fianalytics
        b3Geral      += s.b3
        semTaxaGeral += s.semTaxa

    lines.append("-" * 56)
    lines.append(
        f"{'TOTAL':<12}  {totalGeral:>6}  {diretaGeral:>7}  "
        f"{fiGeral:>7}  {b3Geral:>5}  {semTaxaGeral:>8}"
    )

    if semTaxaGeral > 0:
        lines.append(f"\nATENCAO: {semTaxaGeral} trade(s) sem taxa calculada (vrTaxaCalculada = NULL).")

    # Alertas: sem taxa + volume acima do threshold
    todoAlertas = [
        (s.dtLiquidacao, a)
        for s in statsList
        for a in s.alertas
    ]
    if todoAlertas:
        volumeMin: float = cfg["alerta"]["volumeMinSemTaxa"]
        lines.append(f"\n{'='*56}")
        lines.append(f"ALERTAS — sem taxa calculada, volume >= R$ {volumeMin:,.0f}:")
        lines.append(f"  {'Data':<12}  {'Ticker':<12}  {'Emissor':<22}  {'Volume (R$)':>16}")
        lines.append("  " + "-" * 66)
        for dtLiq, a in sorted(todoAlertas, key=lambda x: (-x[1].vrVolume, x[0])):
            lines.append(f"  {dtLiq:<12}  {a.cdTicker:<12}  {a.cdEmissor:<22}  {a.vrVolume:>16,.2f}")
        lines.append(f"{'='*56}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Main() -> None:
    log     = get_logger("calc_taxa_negocios")
    args    = _ParseArgs()
    conn    = get_db()
    summary = ""
    success = True

    try:
        datas = _BuildDateRange(args)
        log.info("calc_taxa: processando %d data(s): %s ... %s", len(datas), datas[0], datas[-1])

        statsList: list[_DateStats] = []
        for dtLiquidacao in datas:
            s = _ProcessDate(conn, dtLiquidacao, log, args.workers, args.force)
            statsList.append(s)

        summary = _BuildSummary(statsList)
        log.info("calc_taxa: concluido.\n%s", summary)

    except Exception:
        success = False
        summary = traceback.format_exc()
        log.exception("calc_taxa: erro inesperado")

    finally:
        conn.close()
        send_completion_email(
            "calc_taxa_negocios",
            success,
            summary,
            logger=log,
        )


if __name__ == "__main__":
    Main()
