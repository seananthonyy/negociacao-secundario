"""
scrape_anbima_debentures.py
===========================
Baixa o XLS de taxas indicativas de debêntures da Anbima e popula:
  - AnbimaIndicativos  (cdTicker, dtReferencia, vrTaxaAnbima = Taxa Indicativa)
  - InfoAtivos         (cdEmissor, cdInstrumento, dtVencimento, vrDuration,
                        dtAtualizacaoDuration, cdIndexador, cdReferencia)

Download direto via URL previsível — sem Playwright.
HTTP 404 = dia sem publicação (não é erro).

CLI:
    python scripts/scrape_anbima_debentures.py --date 2026-05-29
    python scripts/scrape_anbima_debentures.py --start 2026-05-01 --end 2026-05-29
"""

import argparse
import re
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

import httpx
import xlrd

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.config import cfg
from lib.db import get_db
from lib.logger import get_logger
from lib.email_outlook import send_completion_email

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

_MONTHS_PT = {
    1: 'jan', 2: 'fev', 3: 'mar', 4: 'abr', 5: 'mai', 6: 'jun',
    7: 'jul', 8: 'ago', 9: 'set', 10: 'out', 11: 'nov', 12: 'dez',
}

_SHEETS = ['DI_PERCENTUAL', 'DI_SPREAD', 'IPCA_SPREAD', 'PREFIXADO']

_COL_TICKER          = 0
_COL_EMISSOR         = 1
_COL_VENC            = 2
_COL_INDEXADOR       = 3
_COL_TAXA_INDICATIVA = 6   # col 5 = Taxa de Venda, col 6 = Taxa Indicativa (correta)
_COL_DURATION        = 12
_COL_REF_NTNB        = 14

_DATA_START_ROW = 9

_SQL_UPSERT_ANBIMA = """
INSERT INTO AnbimaIndicativos (cdTicker, dtReferencia, vrTaxaAnbima, vrSpreadAnbima)
VALUES (?, ?, ?, NULL)
ON CONFLICT(cdTicker, dtReferencia) DO UPDATE SET
    vrTaxaAnbima = excluded.vrTaxaAnbima
"""

_SQL_UPSERT_INFO = """
INSERT INTO InfoAtivos (
    cdTicker, cdInstrumento, cdEmissor, dtVencimento,
    vrDuration, dtAtualizacaoDuration, cdIndexador, cdReferencia, cdFonteReferencia, dtAtualizacao
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
ON CONFLICT(cdTicker) DO UPDATE SET
    cdInstrumento    = excluded.cdInstrumento,
    cdEmissor        = excluded.cdEmissor,
    dtVencimento     = excluded.dtVencimento,
    vrDuration       = COALESCE(excluded.vrDuration,       vrDuration),
    dtAtualizacaoDuration = CASE WHEN excluded.vrDuration IS NOT NULL
                            THEN excluded.dtAtualizacaoDuration
                            ELSE dtAtualizacaoDuration END,
    cdIndexador      = COALESCE(excluded.cdIndexador,      cdIndexador),
    cdReferencia            = COALESCE(excluded.cdReferencia,            cdReferencia),
    cdFonteReferencia      = CASE WHEN excluded.cdReferencia IS NOT NULL THEN 'Anbima' ELSE cdFonteReferencia END,
    dtAtualizacao      = excluded.dtAtualizacao
"""

# ---------------------------------------------------------------------------
# Helpers de parsing
# ---------------------------------------------------------------------------

def _BuildUrl(d: date) -> str:
    baseUrl = cfg["scrape"]["anbima"]["debXlsBaseUrl"]
    return f"{baseUrl}/d{d.strftime('%y')}{_MONTHS_PT[d.month]}{d.strftime('%d')}.xls"


def _CleanEmissor(raw) -> str | None:
    s = re.sub(r'\s*\(\*+\)', '', str(raw)).strip()
    return s or None


def _ParseFloat(raw) -> float | None:
    s = str(raw).strip()
    if s in ('--', '', 'N/D', 'N/A'):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _ParseDate(raw) -> str | None:
    s = str(raw).strip()
    if not s or s in ('--', 'N/D'):
        return None
    parts = s.split('/')
    if len(parts) == 3:
        try:
            return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
        except Exception:
            pass
    return None


def _NormalizeIndexador(raw) -> str | None:
    s = str(raw).strip().upper()
    if not s or s in ('--', 'N/D'):
        return None
    if 'DI +' in s or 'DI+' in s:
        return 'CDI+'
    if 'IPCA' in s:
        return 'IPCA'
    if '%' in s and ('DI' in s or 'CDI' in s):
        return '%CDI'
    if s.startswith('PR'):
        return 'PREFIXADO'
    return None


def _ParseRefNtnb(raw) -> str | None:
    s = str(raw).strip()
    if not s or s in ('--', 'N/D'):
        return None
    parts = s.split('/')
    if len(parts) == 3:
        year2 = parts[2].strip()[-2:]
        return f'NTN-B {year2}'
    return None


def _DeriveRef(cdIndexador: str | None, rawRefNtnb) -> str | None:
    if cdIndexador in ('CDI+', '%CDI'):
        return 'FUNDING'
    if cdIndexador == 'IPCA':
        return _ParseRefNtnb(rawRefNtnb)
    return None


# ---------------------------------------------------------------------------
# Parsing do XLS
# ---------------------------------------------------------------------------

def _ParseSheet(sh, dtRef: str) -> tuple[list, list]:
    anbimaRows: list[tuple] = []
    infoRows:   list[tuple] = []

    for rowIdx in range(_DATA_START_ROW, sh.nrows):
        cdTicker = str(sh.cell_value(rowIdx, _COL_TICKER)).strip()
        if not cdTicker or ' ' in cdTicker or cdTicker.startswith('Obs') or cdTicker == '1.0':
            continue

        cdEmissor    = _CleanEmissor(sh.cell_value(rowIdx, _COL_EMISSOR))
        dtVencimento = _ParseDate(sh.cell_value(rowIdx, _COL_VENC))
        cdIndexador  = _NormalizeIndexador(sh.cell_value(rowIdx, _COL_INDEXADOR))
        vrTaxaAnbima = _ParseFloat(sh.cell_value(rowIdx, _COL_TAXA_INDICATIVA))
        rawDuration  = _ParseFloat(sh.cell_value(rowIdx, _COL_DURATION))
        vrDuration   = round(rawDuration / 252, 6) if rawDuration is not None else None
        dtUpsertDur  = dtRef if vrDuration is not None else None
        cdReferencia        = _DeriveRef(cdIndexador, sh.cell_value(rowIdx, _COL_REF_NTNB))
        cdFonteReferencia  = 'Anbima' if cdReferencia is not None else None

        anbimaRows.append((cdTicker, dtRef, vrTaxaAnbima))
        infoRows.append((cdTicker, 'DEB', cdEmissor, dtVencimento,
                         vrDuration, dtUpsertDur, cdIndexador, cdReferencia, cdFonteReferencia))

    return anbimaRows, infoRows


# ---------------------------------------------------------------------------
# Download + processamento por data
# ---------------------------------------------------------------------------

def _DownloadXls(d: date, log) -> bytes | None:
    url = _BuildUrl(d)
    log.debug("anbima_deb: GET %s", url)
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=30)
    except Exception as exc:
        log.warning("anbima_deb: erro de rede para %s: %s", d, exc)
        return None
    if resp.status_code == 404:
        log.info("anbima_deb: %s sem publicação (404) — dia não útil", d)
        return None
    if not resp.is_success:
        log.warning("anbima_deb: HTTP %d para %s", resp.status_code, d)
        return None
    return resp.content


def _ProcessDate(conn, d: date, log) -> tuple[int, int]:
    dtRef   = d.isoformat()
    content = _DownloadXls(d, log)
    if content is None:
        return 0, 0

    try:
        wb = xlrd.open_workbook(file_contents=content)
    except Exception as exc:
        log.warning("anbima_deb: erro ao abrir XLS de %s: %s", dtRef, exc)
        return 0, 0

    allAnbima: list[tuple] = []
    allInfo:   list[tuple] = []

    for sheetName in _SHEETS:
        if sheetName not in wb.sheet_names():
            log.debug("anbima_deb: aba '%s' ausente em %s", sheetName, dtRef)
            continue
        sh = wb.sheet_by_name(sheetName)
        anbimaRows, infoRows = _ParseSheet(sh, dtRef)
        allAnbima.extend(anbimaRows)
        allInfo.extend(infoRows)
        log.debug("anbima_deb: %s — %s: %d tickers", dtRef, sheetName, len(anbimaRows))

    if not allAnbima:
        log.warning("anbima_deb: %s — nenhum ticker extraído", dtRef)
        return 0, 0

    conn.executemany(_SQL_UPSERT_ANBIMA, allAnbima)
    conn.executemany(_SQL_UPSERT_INFO,   allInfo)
    conn.commit()

    log.info("anbima_deb: %s — %d AnbimaIndicativos, %d InfoAtivos", dtRef, len(allAnbima), len(allInfo))
    return len(allAnbima), len(allInfo)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _ParseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Baixa taxas indicativas de debêntures da Anbima (XLS direto)."
    )
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--date",  metavar="YYYY-MM-DD", help="Data única de publicação Anbima.")
    grp.add_argument("--start", metavar="YYYY-MM-DD", help="Início do intervalo.")
    parser.add_argument("--end", metavar="YYYY-MM-DD", help="Fim do intervalo (requer --start).")
    args = parser.parse_args()
    if args.start and not args.end:
        parser.error("--end é obrigatório com --start.")
    if args.end and not args.start:
        parser.error("--start é obrigatório com --end.")
    return args


def _BuildDateRange(args: argparse.Namespace) -> list[date]:
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


def _BuildSummary(results: list[tuple[str, int, int]]) -> str:
    lines = ["Resultado por data:", ""]
    lines.append(f"{'Data':<12}  {'Anbima':>7}  {'InfoAtivos':>10}")
    lines.append("-" * 34)
    totalAnbima = totalInfo = 0
    for dtStr, nAnbima, nInfo in results:
        lines.append(f"{dtStr:<12}  {nAnbima:>7}  {nInfo:>10}")
        totalAnbima += nAnbima
        totalInfo   += nInfo
    lines.append("-" * 34)
    lines.append(f"{'TOTAL':<12}  {totalAnbima:>7}  {totalInfo:>10}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Main() -> None:
    log     = get_logger("scrape_anbima_debentures")
    args    = _ParseArgs()
    conn    = get_db()
    summary = ""
    success = True

    try:
        datas   = _BuildDateRange(args)
        results: list[tuple[str, int, int]] = []

        log.info("anbima_deb: processando %d data(s): %s … %s",
                 len(datas), datas[0], datas[-1])

        for d in datas:
            nAnbima, nInfo = _ProcessDate(conn, d, log)
            results.append((d.isoformat(), nAnbima, nInfo))

        summary = _BuildSummary(results)
        log.info("anbima_deb: concluído.\n%s", summary)

    except Exception:
        success = False
        summary = traceback.format_exc()
        log.exception("anbima_deb: erro inesperado")

    finally:
        conn.close()
        send_completion_email("scrape_anbima_debentures", success, summary, logger=log)


if __name__ == "__main__":
    Main()
