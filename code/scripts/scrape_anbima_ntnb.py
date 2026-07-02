"""
scrape_anbima_ntnb.py
=====================
Baixa taxas indicativas de NTN-B do mercado secundario de titulos publicos
da Anbima e popula MtmAnbima com taxa e duration.

URL: https://www.anbima.com.br/informacoes/merc-sec/arqs/m{YY}{mmm_pt}{DD}.xls
Aba: NTN-B
Coluna C (idx 2): Data de Vencimento (DD/MM/YYYY)
Coluna F (idx 5): Tx. Indicativas (float, % a.a.)

Duration em cascata: FI Analytics → B3 Calculator → NULL

Duration em paralelo (ThreadPool, --workers, default 4) com skip do que já está
na base (--force ignora o skip). Retomada após falha é rápida.

CLI:
    python scripts/scrape_anbima_ntnb.py --date 2026-06-05
    python scripts/scrape_anbima_ntnb.py --start 2026-06-01 --end 2026-06-05 --workers 4
    python scripts/scrape_anbima_ntnb.py --date 2026-06-05 --force   # recalcula duration
"""

import argparse
import json
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import httpx
import xlrd

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.config import cfg, get_secret
from lib.db import get_db
from lib.logger import get_logger
from lib.email_outlook import send_completion_email
from lib.b3_calc_api import CalcPuGov

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

_SCRIPT_NAME = "scrape_anbima_ntnb"

_MONTHS_PT = {
    1: 'jan', 2: 'fev', 3: 'mar', 4: 'abr', 5: 'mai', 6: 'jun',
    7: 'jul', 8: 'ago', 9: 'set', 10: 'out', 11: 'nov', 12: 'dez',
}

_BASE_URL = "https://www.anbima.com.br/informacoes/merc-sec/arqs"

_COL_VENC = 2   # Data de Vencimento
_COL_TAXA = 5   # Tx. Indicativas

_DATA_START_ROW = 5  # linha 6 (0-based: 5) — headers nas linhas 4-5

_FIA_BASE_URL   = "https://endpoint.fi-analytics.com.br"
_FIA_ISIN_PATH  = "/financialutil/gov/getgovbondisin"
_FIA_CALC_PATH  = "/gov/govbondcalculator"

_SQL_UPSERT = """
INSERT INTO MtmAnbima (cdTicker, dtReferencia, vrTaxa, vrDuration)
VALUES (?, ?, ?, ?)
ON CONFLICT(cdTicker, dtReferencia) DO UPDATE SET
    vrTaxa     = excluded.vrTaxa,
    vrDuration = COALESCE(excluded.vrDuration, vrDuration)
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _BuildUrl(d: date) -> str:
    yy = d.strftime('%y')
    mm = _MONTHS_PT[d.month]
    dd = d.strftime('%d')
    return f"{_BASE_URL}/m{yy}{mm}{dd}.xls"


def _ParseFloat(raw) -> float | None:
    s = str(raw).strip()
    if s in ('--', '', 'N/D', 'N/A', 'None'):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _ParseVencimento(raw) -> date | None:
    """Converte 'DD/MM/YYYY' em objeto date. Retorna None se inválido."""
    s = str(raw).strip()
    if not s or s in ('--', 'N/D'):
        return None
    parts = s.split('/')
    if len(parts) == 3:
        try:
            return date(int(parts[2]), int(parts[1]), int(parts[0]))
        except (ValueError, IndexError):
            pass
    return None


def _NormalizeTicker(dtVenc: date) -> str:
    """NTN-B vencendo em 2032 → 'NTN-B 32'."""
    yy = str(dtVenc.year)[-2:]
    return f"NTN-B {yy}"


def _FiaHeaders() -> dict[str, str]:
    apiKey = get_secret("fianalyticsApiKey")
    if not apiKey:
        raise RuntimeError("API key do FI Analytics nao configurada (ver [env].fianalyticsApiKey no config.toml)")
    return {
        "Content-Type": "application/json; charset=utf-8",
        "x-api-key": apiKey,
    }


def _ParseFiaResponse(resp: httpx.Response, url: str, log) -> dict | None:
    """FI Analytics retorna JSON double-encoded em alguns endpoints."""
    try:
        raw = resp.json()
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        log.warning("ntnb: resposta nao e JSON valido em %s: %s", url, exc)
        return None


def _GetDurationFia(dtVenc: date, dtRef: date, vrTaxa: float, log) -> float | None:
    """
    Busca duration via FI Analytics em dois passos:
      1. POST /financialutil/gov/getgovbondisin → obtem ISIN
      2. POST /gov/govbondcalculator            → obtem maculayDuration
    """
    timeout = cfg["calc"]["timeoutSeconds"]
    headers = _FiaHeaders()
    dtVencStr = dtVenc.strftime("%d/%m/%Y")
    dtRefStr  = dtRef.isoformat()

    # Passo 1: obter ISIN
    urlIsin = f"{_FIA_BASE_URL}{_FIA_ISIN_PATH}"
    try:
        log.debug("ntnb: FIA getgovbondisin venc=%s", dtVencStr)
        respIsin = httpx.post(
            urlIsin,
            json={"instrument_type": "NTN-B", "maturity_date": dtVencStr},
            headers=headers,
            timeout=timeout,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning("ntnb: FIA getgovbondisin erro de rede para %s: %s", dtVencStr, exc)
        return None

    if not respIsin.is_success:
        log.warning("ntnb: FIA getgovbondisin HTTP %d para %s", respIsin.status_code, dtVencStr)
        return None

    dataIsin = _ParseFiaResponse(respIsin, urlIsin, log)
    if dataIsin is None:
        return None

    isin = dataIsin.get("isin")
    if not isin:
        log.warning("ntnb: FIA getgovbondisin sem campo 'isin' para %s: %s", dtVencStr, dataIsin)
        return None

    # Passo 2: calcular duration
    urlCalc = f"{_FIA_BASE_URL}{_FIA_CALC_PATH}"
    try:
        log.debug("ntnb: FIA govbondcalculator isin=%s date=%s rate=%.4f", isin, dtRefStr, vrTaxa)
        respCalc = httpx.post(
            urlCalc,
            json={"isin": isin, "date": dtRefStr, "rate": vrTaxa},
            headers=headers,
            timeout=timeout,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning("ntnb: FIA govbondcalculator erro de rede para %s: %s", isin, exc)
        return None

    if not respCalc.is_success:
        log.warning("ntnb: FIA govbondcalculator HTTP %d para isin=%s", respCalc.status_code, isin)
        return None

    dataCalc = _ParseFiaResponse(respCalc, urlCalc, log)
    if dataCalc is None:
        return None

    rawDur = dataCalc.get("maculayDuration")
    if rawDur is None:
        log.warning("ntnb: FIA govbondcalculator sem 'maculayDuration' para isin=%s: %s", isin, dataCalc)
        return None

    try:
        dur = float(rawDur)
    except (TypeError, ValueError):
        log.warning("ntnb: FIA govbondcalculator 'maculayDuration' nao e float: %r", rawDur)
        return None

    if dur <= 0:
        log.warning("ntnb: FIA govbondcalculator duration invalida (%.4f) para isin=%s", dur, isin)
        return None

    log.debug("ntnb: FIA duration=%.4f para isin=%s", dur, isin)
    return dur


def _GetDurationB3(dtVenc: date, dtRef: date, vrTaxa: float, log) -> float | None:
    """
    Busca duration via B3 Calculator usando codigo CETIP derivado do vencimento.
    CETIP = "760199" + YYYY + MM + DD
    """
    cetip = f"760199{dtVenc.year}{dtVenc.month:02d}{dtVenc.day:02d}"
    dtRefStr = dtRef.isoformat()

    log.debug("ntnb: B3 calcPU cetip=%s dtRef=%s taxa=%.4f", cetip, dtRefStr, vrTaxa)
    _, duration = CalcPuGov(cetip, dtRefStr, vrTaxa)

    if duration is None:
        log.warning("ntnb: B3 calcPU nao retornou duration para cetip=%s", cetip)
        return None

    log.debug("ntnb: B3 duration=%.4f para cetip=%s", duration, cetip)
    return duration


def _GetDuration(dtVenc: date, dtRef: date, vrTaxa: float, log) -> float | None:
    """Cascata: FI Analytics → B3 Calculator → None."""
    dur = _GetDurationFia(dtVenc, dtRef, vrTaxa, log)
    if dur is not None:
        return dur

    log.info("ntnb: FIA falhou para %s, tentando B3 Calculator", dtVenc.strftime("%d/%m/%Y"))
    dur = _GetDurationB3(dtVenc, dtRef, vrTaxa, log)
    if dur is not None:
        return dur

    log.warning("ntnb: duration indisponivel para %s (ambos FIA e B3 falharam)", dtVenc.strftime("%d/%m/%Y"))
    return None


# ---------------------------------------------------------------------------
# Download e processamento do XLS
# ---------------------------------------------------------------------------

def _DownloadXls(d: date, log) -> bytes | None:
    url = _BuildUrl(d)
    log.debug("ntnb: GET %s", url)
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=30)
    except Exception as exc:
        log.warning("ntnb: erro de rede para %s: %s", d, exc)
        return None

    if resp.status_code == 404:
        log.info("ntnb: %s sem publicacao (404) — dia nao util", d)
        return None
    if not resp.is_success:
        log.warning("ntnb: HTTP %d para %s", resp.status_code, d)
        return None

    return resp.content


def _ParseSheet(sh, wb, dtRef: date, log) -> list[tuple]:
    rows: list[tuple] = []
    dtRefStr = dtRef.isoformat()

    for rowIdx in range(_DATA_START_ROW, sh.nrows):
        cellVenc = sh.cell(rowIdx, _COL_VENC)
        rawTaxa  = sh.cell_value(rowIdx, _COL_TAXA)

        # Vencimento vem como serial Excel (ctype=3=XL_CELL_DATE)
        if cellVenc.ctype == xlrd.XL_CELL_DATE:
            try:
                dtVenc = xlrd.xldate_as_datetime(cellVenc.value, wb.datemode).date()
            except Exception:
                continue
        else:
            dtVenc = _ParseVencimento(cellVenc.value)

        if dtVenc is None:
            continue

        vrTaxa = _ParseFloat(rawTaxa)
        if vrTaxa is None or vrTaxa <= 0:
            continue

        cdTicker = _NormalizeTicker(dtVenc)
        rows.append((cdTicker, dtVenc, dtRefStr, vrTaxa))

    return rows


def _ProcessDate(conn, d: date, log, workers: int, force: bool = False) -> tuple[int, int]:
    content = _DownloadXls(d, log)
    if content is None:
        return 0, 0

    try:
        wb = xlrd.open_workbook(file_contents=content)
    except Exception as exc:
        log.warning("ntnb: erro ao abrir XLS de %s: %s", d, exc)
        return 0, 0

    sheetName = "NTN-B"
    if sheetName not in wb.sheet_names():
        log.warning("ntnb: aba '%s' ausente no XLS de %s (abas: %s)",
                    sheetName, d, wb.sheet_names())
        return 0, 0

    sh = wb.sheet_by_name(sheetName)
    rows = _ParseSheet(sh, wb, d, log)

    if not rows:
        log.warning("ntnb: %s — nenhuma NTN-B extraida da aba", d)
        return 0, 0

    # Skip do já-calculado: tickers que já têm vrDuration nesta data não chamam
    # a API de novo (retomada rápida após falha). --force ignora o skip.
    dtRefStr0 = d.isoformat()
    jaFeitos: set = set()
    if not force:
        jaFeitos = {r[0] for r in conn.execute(
            "SELECT cdTicker FROM MtmAnbima WHERE dtReferencia = ? AND vrDuration IS NOT NULL",
            (dtRefStr0,)).fetchall()}

    log.info("ntnb: %s — %d NTN-Bs (%d ja c/ duration, %d workers)...",
             d, len(rows), len(jaFeitos), workers)

    # Duration via API é I/O-bound: paraleliza com concorrência limitada por
    # `workers` (não sobrecarregar/bloquear a API). O UPSERT (COALESCE) preserva
    # a duration existente quando o valor vem None. UPSERT sequencial após o pool.
    def _ComputeRow(item: tuple) -> tuple:
        cdTicker, dtVenc, dtRefStr, vrTaxa = item
        if cdTicker in jaFeitos:
            return (cdTicker, dtRefStr, vrTaxa, None, True)      # pulado
        return (cdTicker, dtRefStr, vrTaxa, _GetDuration(dtVenc, d, vrTaxa, log), False)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(_ComputeRow, rows))

    upsertRows   = [(r[0], r[1], r[2], r[3]) for r in results]
    nSkip        = sum(1 for r in results if r[4])
    nSemDuration = sum(1 for r in results if not r[4] and r[3] is None)

    conn.executemany(_SQL_UPSERT, upsertRows)
    conn.commit()

    log.info("ntnb: %s — %d upserts (%d pulados, %d sem duration)",
             d, len(upsertRows), nSkip, nSemDuration)
    return len(upsertRows), nSemDuration


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _ParseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Baixa taxas indicativas de NTN-B da Anbima e popula MtmAnbima."
    )
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--date",  metavar="YYYY-MM-DD", help="Data unica de publicacao Anbima.")
    grp.add_argument("--start", metavar="YYYY-MM-DD", help="Inicio do intervalo.")
    parser.add_argument("--end", metavar="YYYY-MM-DD", help="Fim do intervalo (requer --start).")
    parser.add_argument("--workers", type=int, default=4,
                        help="Chamadas de duration em paralelo por data (default 4; "
                             "reduza p/ 1-2 se a API bloquear, aumente se estiver de boa).")
    parser.add_argument("--force", action="store_true",
                        help="Recalcula a duration mesmo se ja existir na base (ignora o skip).")
    args = parser.parse_args()
    if args.start and not args.end:
        parser.error("--end e obrigatorio com --start.")
    if args.end and not args.start:
        parser.error("--start e obrigatorio com --end.")
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
    lines.append(f"{'Data':<12}  {'Upserts':>8}  {'Sem duration':>13}")
    lines.append("-" * 38)
    totalUpserts = totalSemDur = 0
    for dtStr, nUp, nSem in results:
        lines.append(f"{dtStr:<12}  {nUp:>8}  {nSem:>13}")
        totalUpserts += nUp
        totalSemDur  += nSem
    lines.append("-" * 38)
    lines.append(f"{'TOTAL':<12}  {totalUpserts:>8}  {totalSemDur:>13}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Main() -> None:
    log     = get_logger(_SCRIPT_NAME)
    args    = _ParseArgs()
    conn    = get_db()
    summary = ""
    success = True

    try:
        datas   = _BuildDateRange(args)
        results: list[tuple[str, int, int]] = []

        log.info("ntnb: processando %d data(s): %s ... %s", len(datas), datas[0], datas[-1])

        for d in datas:
            nUp, nSem = _ProcessDate(conn, d, log, args.workers, args.force)
            results.append((d.isoformat(), nUp, nSem))

        summary = _BuildSummary(results)
        log.info("ntnb: concluido.\n%s", summary)

    except Exception:
        success = False
        summary = traceback.format_exc()
        log.exception("ntnb: erro inesperado")

    finally:
        conn.close()
        send_completion_email(_SCRIPT_NAME, success, summary, logger=log)


if __name__ == "__main__":
    Main()
