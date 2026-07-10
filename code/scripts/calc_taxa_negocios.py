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
    python scripts/calc_taxa_negocios.py --date 2026-05-29 --limit 40   # smoke test
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
from lib.db import ObterBanco
from lib.logger import ObterLogger
from lib.email_outlook import EnviarEmailConclusao
from lib.fianalytics_api import CalcularTaxa
from lib.b3_calc_api import CalcularYield


# ---------------------------------------------------------------------------
# Tipos internos
# ---------------------------------------------------------------------------

@dataclass
class NegocioBruto:
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
class AlertaNegocio:
    cdTicker:  str
    cdEmissor: str
    vrVolume:  float


@dataclass
class EstatisticasData:
    dtLiquidacao: str
    total: int = 0
    direta: int = 0       # vrTaxaNegocio copiado direto (sem chamada de API)
    fianalytics: int = 0  # calculada via FI Analytics
    b3: int = 0           # calculada via B3 Calculator
    semTaxa: int = 0      # vrTaxaCalculada = NULL (todas as calculadoras falharam)
    alertas: list[AlertaNegocio] = field(default_factory=list)  # sem taxa + volume >= threshold


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

SQL_BUSCAR_NEGOCIOS = """
SELECT idTrade, cdTicker, cdEmissor, cdInstrumento,
       dtNegocio, dtLiquidacao, vrQuantidade, vrPU, vrVolume, vrTaxaNegocio
FROM NegociosBrutos
WHERE dtLiquidacao = ?
  AND cdSituacao != 'Cancelado'
"""

SQL_BUSCAR_EXISTENTES = """
SELECT idTrade, vrTaxaCalculada, cdFonteTaxa
FROM NegociosProcessados
WHERE dtLiquidacao = ?
  AND vrTaxaCalculada IS NOT NULL
"""

SQL_UPSERT = """
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

def AplicarCascata(trade: NegocioBruto, log) -> tuple[Optional[float], Optional[str]]:
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

    taxa = CalcularTaxa(trade.cdTicker, trade.cdInstrumento, trade.dtLiquidacao, trade.vrPU)
    if taxa is not None:
        return taxa, "FiAnalytics"

    log.debug(
        "calc_taxa: FI Analytics falhou para %s — tentando B3 Calculator",
        trade.cdTicker,
    )

    taxa = CalcularYield(trade.cdTicker, trade.dtLiquidacao, trade.vrPU)
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

def ProcessarData(conn, dtLiquidacao: str, log, workers: int, force: bool,
                 limit: Optional[int] = None) -> EstatisticasData:
    """Processa todos os trades de uma dtLiquidacao e faz UPSERT em NegociosProcessados.

    `limit` (só para smoke test) processa apenas N trades, priorizando os que
    precisam de chamada de API (vrTaxaNegocio IS NULL) — assim a cascata
    FI Analytics -> B3 e o caminho 'direta' são ambos exercitados.
    """
    stats = EstatisticasData(dtLiquidacao=dtLiquidacao)

    rows = conn.execute(SQL_BUSCAR_NEGOCIOS, (dtLiquidacao,)).fetchall()

    if limit is not None and len(rows) > limit:
        # trades sem taxa (precisam de API) primeiro — o resto completa o lote
        rows = sorted(rows, key=lambda r: r["vrTaxaNegocio"] is not None)[:limit]
        log.warning("calc_taxa: --limit %d ativo — processando apenas %d trade(s) de %s (SMOKE TEST)",
                    limit, len(rows), dtLiquidacao)

    stats.total = len(rows)

    if stats.total == 0:
        log.info("calc_taxa: dtLiquidacao=%s — nenhum trade encontrado em NegociosBrutos", dtLiquidacao)
        return stats

    trades = [
        NegocioBruto(
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
        for row in conn.execute(SQL_BUSCAR_EXISTENTES, (dtLiquidacao,)).fetchall():
            existing[row["idTrade"]] = (row["vrTaxaCalculada"], row["cdFonteTaxa"])

    # Separa trades que já têm taxa (cached) dos que precisam de API
    cached:     list[tuple[NegocioBruto, Optional[float], Optional[str]]] = []
    aProcessar: list[NegocioBruto] = []

    for trade in trades:
        if trade.idTrade in existing:
            cached.append((trade, *existing[trade.idTrade]))
        else:
            aProcessar.append(trade)

    nCache = len(cached)
    nApi    = len(aProcessar)

    if force:
        log.info(
            "calc_taxa: dtLiquidacao=%s — %d trade(s), --force ativo, recalculando tudo (workers=%d)",
            dtLiquidacao, stats.total, workers,
        )
    else:
        log.info(
            "calc_taxa: dtLiquidacao=%s — %d trade(s): %d via API, %d já calculados (mantidos)",
            dtLiquidacao, stats.total, nApi, nCache,
        )

    # Fase paralela: API calls apenas para to_process
    resultadosApi: list[tuple[NegocioBruto, Optional[float], Optional[str]]] = []

    if aProcessar:
        feitosApi   = 0
        tempoInicio = time.monotonic()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futureParaNegocio = {executor.submit(AplicarCascata, t, log): t for t in aProcessar}
            for future in as_completed(futureParaNegocio):
                trade = futureParaNegocio[future]
                try:
                    taxa, source = future.result()
                except Exception:
                    log.exception(
                        "calc_taxa: erro inesperado para idTrade=%d cdTicker=%s",
                        trade.idTrade, trade.cdTicker,
                    )
                    taxa, source = None, None

                feitosApi += 1
                resultadosApi.append((trade, taxa, source))

                elapsed  = time.monotonic() - tempoInicio
                taxaStr = f"{taxa:.4f}%" if taxa is not None else "sem taxa"
                origem   = source if source else ("direta" if trade.vrTaxaNegocio is not None else "sem taxa")
                if feitosApi >= 2:
                    eta     = elapsed / feitosApi * (nApi - feitosApi)
                    etaStr = f" | ~{eta:.0f}s restante"
                else:
                    etaStr = ""
                log.info(
                    "[%d/%d] %s → %s %s | %.0fs elapsed%s",
                    feitosApi, nApi, trade.cdTicker, origem, taxaStr, elapsed, etaStr,
                )

    # Fase sequencial: contagem de stats + UPSERTs (só para to_process)
    # Trades cached: apenas contagem, sem UPSERT (já estão corretos no banco)
    volumeMin: float = cfg["alerta"]["volumeMinSemTaxa"]

    def ContarEstatisticas(trade: NegocioBruto, vrTaxaCalculada: Optional[float], cdFonteTaxa: Optional[str]) -> None:
        if trade.vrTaxaNegocio is not None:
            stats.direta += 1
        elif cdFonteTaxa == "FiAnalytics":
            stats.fianalytics += 1
        elif cdFonteTaxa == "B3":
            stats.b3 += 1
        else:
            stats.semTaxa += 1
            if trade.vrVolume >= volumeMin:
                stats.alertas.append(AlertaNegocio(trade.cdTicker, trade.cdEmissor, trade.vrVolume))

    for trade, vrTaxaCalculada, cdFonteTaxa in cached:
        ContarEstatisticas(trade, vrTaxaCalculada, cdFonteTaxa)

    for trade, vrTaxaCalculada, cdFonteTaxa in resultadosApi:
        ContarEstatisticas(trade, vrTaxaCalculada, cdFonteTaxa)
        conn.execute(SQL_UPSERT, {
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

def LerArgumentos() -> argparse.Namespace:
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
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="SMOKE TEST: processa só N trades por data (prioriza os que precisam de API).",
    )
    args = parser.parse_args()

    if args.start and not args.end:
        parser.error("--end é obrigatório quando --start é informado.")
    if args.end and not args.start:
        parser.error("--start é obrigatório quando --end é informado.")

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

def Principal() -> None:
    log     = ObterLogger("calc_taxa_negocios")
    args    = LerArgumentos()
    conn    = ObterBanco()
    summary = ""
    success = True

    try:
        datas = MontarIntervaloDatas(args)
        log.info("calc_taxa: processando %d data(s): %s ... %s", len(datas), datas[0], datas[-1])

        statsList: list[EstatisticasData] = []
        for dtLiquidacao in datas:
            s = ProcessarData(conn, dtLiquidacao, log, args.workers, args.force, args.limit)
            statsList.append(s)

        summary = MontarResumo(statsList)
        log.info("calc_taxa: concluido.\n%s", summary)

    except Exception:
        success = False
        summary = traceback.format_exc()
        log.exception("calc_taxa: erro inesperado")

    finally:
        conn.close()
        EnviarEmailConclusao(
            "calc_taxa_negocios",
            success,
            summary,
            logger=log,
        )


if __name__ == "__main__":
    Principal()
