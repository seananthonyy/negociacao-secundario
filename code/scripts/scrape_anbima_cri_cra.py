"""
scrape_anbima_cri_cra.py
========================
Baixa o CSV de taxas indicativas de CRIs e CRAs da Anbima via Playwright
(o link de download é um blob URL — não é possível download direto) e popula:
  - AnbimaIndicativos  (cdTicker, dtReferencia, vrTaxaAnbima = Taxa Indicativa)
  - InfoAtivos         (cdEmissor, cdInstrumento, dtVencimento, vrDuration,
                        dtAtualizacaoDuration, cdIndexador, cdReferencia)

CRI ou CRA é determinado pelo ticker: começa com 'CRA' → CRA, senão → CRI.

CLI:
    python scripts/scrape_anbima_cri_cra.py --date 2026-05-29
    python scripts/scrape_anbima_cri_cra.py --start 2026-05-01 --end 2026-05-29
    python scripts/scrape_anbima_cri_cra.py --date 2026-05-29 --headless
"""

import argparse
import asyncio
import csv
import io
import re
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright, Page

from lib.config import cfg
from lib.db import get_db
from lib.logger import get_logger
from lib.email_outlook import send_completion_email

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

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
# Helpers de parsing (compartilhados com scrape_anbima_debentures)
# ---------------------------------------------------------------------------

def _CleanEmissor(raw) -> str | None:
    s = re.sub(r'\s*\(\*+\)', '', str(raw)).strip()
    return s or None


def _ParseFloat(raw) -> float | None:
    s = str(raw).strip()
    if s in ('--', '', 'N/D', 'N/A'):
        return None
    try:
        return float(s.replace(',', '.'))
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
# Parsing do CSV
# ---------------------------------------------------------------------------

def _ParseCsv(content: bytes, dtRef: str, log) -> tuple[list, list]:
    textDecoded = None
    for enc in ('utf-8-sig', 'utf-8', 'latin-1', 'cp1252'):
        try:
            textDecoded = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if textDecoded is None:
        log.warning("anbima_cricra: não foi possível decodificar CSV de %s", dtRef)
        return [], []

    # Detecta delimitador
    sample = textDecoded[:2048]
    delimiter = ';' if sample.count(';') >= sample.count(',') else ','

    reader = csv.DictReader(io.StringIO(textDecoded), delimiter=delimiter)
    if not reader.fieldnames:
        log.warning("anbima_cricra: CSV de %s sem headers", dtRef)
        return [], []

    # Header usa " ; " (com espaços) — strip para normalizar os nomes
    reader.fieldnames = [f.strip() for f in reader.fieldnames]
    log.debug("anbima_cricra: colunas CSV: %s", reader.fieldnames)

    anbimaRows: list[tuple] = []
    infoRows:   list[tuple] = []

    for row in reader:
        cdTicker = (row.get('Código') or row.get('Codigo') or '').strip()
        if not cdTicker or ' ' in cdTicker:
            continue

        # Filtra linhas cuja data de referência não bate com dtRef
        rawDataRef  = row.get('Data Referência') or row.get('Data de Referência') or row.get('Data Referencia') or ''
        dtLinhaRef  = _ParseDate(rawDataRef.strip())
        if dtLinhaRef and dtLinhaRef != dtRef:
            continue

        cdInstrumento = 'CRA' if cdTicker.upper().startswith('CRA') else 'CRI'
        # 'Risco de Crédito' = empresa originadora; 'Emissor' = securitizadora
        cdEmissor     = _CleanEmissor(row.get('Risco de Crédito') or row.get('Risco de Credito') or '')
        dtVencimento  = _ParseDate(row.get('Vencimento') or '')
        rawIndexador  = row.get('Índice / Correção') or row.get('Indice / Correcao') or ''
        cdIndexador   = _NormalizeIndexador(rawIndexador) or 'PREFIXADO'
        vrTaxaAnbima  = _ParseFloat(row.get('Taxa Indicativa') or '')
        rawDuration   = _ParseFloat(row.get('Duration') or '')
        vrDuration    = round(rawDuration / 252, 6) if rawDuration is not None else None
        dtUpsertDur   = dtRef if vrDuration is not None else None
        rawRefNtnb    = row.get('Referência NTNB') or row.get('Referencia NTNB') or ''
        cdReferencia         = _DeriveRef(cdIndexador, rawRefNtnb)
        cdFonteReferencia   = 'Anbima' if cdReferencia is not None else None

        anbimaRows.append((cdTicker, dtRef, vrTaxaAnbima))
        infoRows.append((cdTicker, cdInstrumento, cdEmissor, dtVencimento,
                         vrDuration, dtUpsertDur, cdIndexador, cdReferencia, cdFonteReferencia))

    return anbimaRows, infoRows


# ---------------------------------------------------------------------------
# Playwright: download por data
# ---------------------------------------------------------------------------

async def _SetDate(page: Page, dtStr: str, log) -> None:
    d       = date.fromisoformat(dtStr)
    dtInput = d.strftime('%d/%m/%Y')

    # Tenta preencher o input de data (selector do componente Anbima)
    dateLocator = page.locator('input.anbima-ui-input__input').first
    try:
        await dateLocator.click(click_count=3)
        await dateLocator.fill(dtInput)
        await dateLocator.press('Enter')
        await page.wait_for_timeout(2500)
        log.debug("anbima_cricra: data setada para %s", dtInput)
    except Exception as exc:
        log.warning("anbima_cricra: erro ao setar data %s: %s", dtInput, exc)


async def _DownloadCsv(page: Page, dtStr: str, log) -> bytes | None:
    await _SetDate(page, dtStr, log)

    # Tenta intercept de download via botão CSV
    csvLocator = page.locator('ul.anbima-ui-toolbar__menu-files a').filter(has_text='CSV')
    try:
        async with page.expect_download(timeout=20_000) as dlInfo:
            await csvLocator.click()
        dl      = await dlInfo.value
        dlPath  = await dl.path()
        with open(dlPath, 'rb') as fh:
            content = fh.read()
        log.info("anbima_cricra: %s — CSV baixado (%d bytes)", dtStr, len(content))
        return content
    except Exception as exc:
        log.warning("anbima_cricra: %s — erro ao baixar CSV: %s", dtStr, exc)
        return None


# ---------------------------------------------------------------------------
# Processamento por data
# ---------------------------------------------------------------------------

def _SaveToDb(conn, anbimaRows: list, infoRows: list, dtStr: str, log) -> tuple[int, int]:
    if not anbimaRows:
        log.warning("anbima_cricra: %s — nenhum ticker extraído do CSV", dtStr)
        return 0, 0
    conn.executemany(_SQL_UPSERT_ANBIMA, anbimaRows)
    conn.executemany(_SQL_UPSERT_INFO,   infoRows)
    conn.commit()
    log.info("anbima_cricra: %s — %d AnbimaIndicativos, %d InfoAtivos",
             dtStr, len(anbimaRows), len(infoRows))
    return len(anbimaRows), len(infoRows)


# ---------------------------------------------------------------------------
# Main assíncrono
# ---------------------------------------------------------------------------

async def _MainAsync(args: argparse.Namespace, log) -> list[tuple[str, int, int]]:
    datas   = _BuildDateRange(args)
    pageUrl = cfg["scrape"]["anbima"]["cricraUrl"]
    results: list[tuple[str, int, int]] = []
    conn    = get_db()

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=args.headless)
            context = await browser.new_context(
                accept_downloads=True,
                viewport={"width": 1400, "height": 900},
            )
            page = await context.new_page()

            log.info("anbima_cricra: navegando para %s", pageUrl)
            await page.goto(pageUrl, wait_until="networkidle", timeout=45_000)
            await page.wait_for_timeout(2000)

            for d in datas:
                dtStr   = d.isoformat()
                content = await _DownloadCsv(page, dtStr, log)
                if content is None:
                    results.append((dtStr, 0, 0))
                    continue
                anbimaRows, infoRows = _ParseCsv(content, dtStr, log)
                nAnbima, nInfo       = _SaveToDb(conn, anbimaRows, infoRows, dtStr, log)
                results.append((dtStr, nAnbima, nInfo))

            await browser.close()
    finally:
        conn.close()

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _ParseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Baixa taxas indicativas de CRI/CRA da Anbima via Playwright."
    )
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--date",  metavar="YYYY-MM-DD", help="Data única de publicação Anbima.")
    grp.add_argument("--start", metavar="YYYY-MM-DD", help="Início do intervalo.")
    parser.add_argument("--end", metavar="YYYY-MM-DD", help="Fim do intervalo (requer --start).")
    parser.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="Rodar Playwright em modo headless (padrão: False para debug visual).",
    )
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
    log     = get_logger("scrape_anbima_cri_cra")
    args    = _ParseArgs()
    summary = ""
    success = True

    try:
        results = asyncio.run(_MainAsync(args, log))
        summary = _BuildSummary(results)
        log.info("anbima_cricra: concluído.\n%s", summary)

    except Exception:
        success = False
        summary = traceback.format_exc()
        log.exception("anbima_cricra: erro inesperado")

    finally:
        send_completion_email("scrape_anbima_cri_cra", success, summary, logger=log)


if __name__ == "__main__":
    Main()
