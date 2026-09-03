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
    python codigos/scripts/scrape_anbima_ntnb.py --date 2026-06-05
    python codigos/scripts/scrape_anbima_ntnb.py --start 2026-06-01 --end 2026-06-05 --workers 4
    python codigos/scripts/scrape_anbima_ntnb.py --date 2026-06-05 --force   # recalcula duration
"""

import argparse
import json
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import httpx
import pandas as pd
import xlrd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers"))

import dados as D
from config import cfg, ObterSegredo
from logger import ObterLogger
from email_outlook import EnviarEmailConclusao
from relatorio_execucao import RelatorioExecucao
from b3_calc_api import CalcularPuGov

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

NOME_SCRIPT = "scrape_anbima_ntnb"

MESES_PT = {
    1: 'jan', 2: 'fev', 3: 'mar', 4: 'abr', 5: 'mai', 6: 'jun',
    7: 'jul', 8: 'ago', 9: 'set', 10: 'out', 11: 'nov', 12: 'dez',
}

BASE_URL = "https://www.anbima.com.br/informacoes/merc-sec/arqs"

COL_VENC = 2   # Data de Vencimento
COL_TAXA = 5   # Tx. Indicativas

LINHA_INICIO_DADOS = 5  # linha 6 (0-based: 5) — headers nas linhas 4-5

FIA_BASE_URL   = "https://endpoint.fi-analytics.com.br"
FIA_ISIN_PATH  = "/financialutil/gov/getgovbondisin"
FIA_CALC_PATH  = "/gov/govbondcalculator"

# O que era `ON CONFLICT(cdTicker, dtReferencia) DO UPDATE SET vrTaxa = excluded.vrTaxa,
# vrDuration = COALESCE(excluded.vrDuration, vrDuration)`: a taxa do dia sempre manda;
# a duration so e trocada quando vem preenchida (a cascata FI->B3 pode devolver None, e
# nesse caso a que ja estava na base tem de sobreviver).
POLITICA_MTM = {"vrTaxa": D.SOBRESCREVER, "vrDuration": D.PREFERIR_NOVO}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def MontarUrl(d: date) -> str:
    yy = d.strftime('%y')
    mm = MESES_PT[d.month]
    dd = d.strftime('%d')
    return f"{BASE_URL}/m{yy}{mm}{dd}.xls"


def AnalisarFloat(raw) -> float | None:
    s = str(raw).strip()
    if s in ('--', '', 'N/D', 'N/A', 'None'):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def AnalisarVencimento(raw) -> date | None:
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


def NormalizarTicker(dtVenc: date) -> str:
    """NTN-B vencendo em 2032 → 'NTN-B 32'."""
    yy = str(dtVenc.year)[-2:]
    return f"NTN-B {yy}"


def HeadersFia() -> dict[str, str]:
    apiKey = ObterSegredo("fianalyticsApiKey")
    if not apiKey:
        raise RuntimeError("API key do FI Analytics nao configurada (ver [env].fianalyticsApiKey no config.toml)")
    return {
        "Content-Type": "application/json; charset=utf-8",
        "x-api-key": apiKey,
    }


def AnalisarRespostaFia(resp: httpx.Response, url: str, log) -> dict | None:
    """FI Analytics retorna JSON double-encoded em alguns endpoints."""
    try:
        raw = resp.json()
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        log.warning("ntnb: resposta nao e JSON valido em %s: %s", url, exc)
        return None


def DurationViaFia(dtVenc: date, dtRef: date, vrTaxa: float, log) -> float | None:
    """
    Busca duration via FI Analytics em dois passos:
      1. POST /financialutil/gov/getgovbondisin → obtem ISIN
      2. POST /gov/govbondcalculator            → obtem maculayDuration
    """
    timeout = cfg["calc"]["timeoutSeconds"]
    headers = HeadersFia()
    dtVencStr = dtVenc.strftime("%d/%m/%Y")
    dtRefStr  = dtRef.isoformat()

    # Passo 1: obter ISIN
    urlIsin = f"{FIA_BASE_URL}{FIA_ISIN_PATH}"
    try:
        log.debug("ntnb: FIA getgovbondisin venc=%s", dtVencStr)
        respIsin = httpx.post(
            urlIsin,
            json={"instrument_type": "NTN-B", "maturity_date": dtVencStr},
            headers=headers,
            timeout=timeout,
            verify=False,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning("ntnb: FIA getgovbondisin erro de rede para %s: %s", dtVencStr, exc)
        return None

    if not respIsin.is_success:
        log.warning("ntnb: FIA getgovbondisin HTTP %d para %s", respIsin.status_code, dtVencStr)
        return None

    dataIsin = AnalisarRespostaFia(respIsin, urlIsin, log)
    if dataIsin is None:
        return None

    isin = dataIsin.get("isin")
    if not isin:
        log.warning("ntnb: FIA getgovbondisin sem campo 'isin' para %s: %s", dtVencStr, dataIsin)
        return None

    # Passo 2: calcular duration
    urlCalc = f"{FIA_BASE_URL}{FIA_CALC_PATH}"
    try:
        log.debug("ntnb: FIA govbondcalculator isin=%s date=%s rate=%.4f", isin, dtRefStr, vrTaxa)
        respCalc = httpx.post(
            urlCalc,
            json={"isin": isin, "date": dtRefStr, "rate": vrTaxa},
            headers=headers,
            timeout=timeout,
            verify=False,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning("ntnb: FIA govbondcalculator erro de rede para %s: %s", isin, exc)
        return None

    if not respCalc.is_success:
        log.warning("ntnb: FIA govbondcalculator HTTP %d para isin=%s", respCalc.status_code, isin)
        return None

    dataCalc = AnalisarRespostaFia(respCalc, urlCalc, log)
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


def DurationViaB3(dtVenc: date, dtRef: date, vrTaxa: float, log) -> float | None:
    """
    Busca duration via B3 Calculator usando codigo CETIP derivado do vencimento.
    CETIP = "760199" + YYYY + MM + DD
    """
    cetip = f"760199{dtVenc.year}{dtVenc.month:02d}{dtVenc.day:02d}"
    dtRefStr = dtRef.isoformat()

    log.debug("ntnb: B3 calcPU cetip=%s dtRef=%s taxa=%.4f", cetip, dtRefStr, vrTaxa)
    _, duration = CalcularPuGov(cetip, dtRefStr, vrTaxa)

    if duration is None:
        log.warning("ntnb: B3 calcPU nao retornou duration para cetip=%s", cetip)
        return None

    log.debug("ntnb: B3 duration=%.4f para cetip=%s", duration, cetip)
    return duration


def ObterDuration(dtVenc: date, dtRef: date, vrTaxa: float, log) -> float | None:
    """Cascata: FI Analytics → B3 Calculator → None."""
    dur = DurationViaFia(dtVenc, dtRef, vrTaxa, log)
    if dur is not None:
        return dur

    log.info("ntnb: FIA falhou para %s, tentando B3 Calculator", dtVenc.strftime("%d/%m/%Y"))
    dur = DurationViaB3(dtVenc, dtRef, vrTaxa, log)
    if dur is not None:
        return dur

    log.warning("ntnb: duration indisponivel para %s (ambos FIA e B3 falharam)", dtVenc.strftime("%d/%m/%Y"))
    return None


# ---------------------------------------------------------------------------
# Download e processamento do XLS
# ---------------------------------------------------------------------------

def BaixarXls(d: date, log) -> bytes | None:
    url = MontarUrl(d)
    log.debug("ntnb: GET %s", url)
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=30, verify=False)
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


def AnalisarPlanilha(sh, wb, dtRef: date, log) -> list[tuple]:
    rows: list[tuple] = []
    dtRefStr = dtRef.isoformat()

    for rowIdx in range(LINHA_INICIO_DADOS, sh.nrows):
        cellVenc = sh.cell(rowIdx, COL_VENC)
        rawTaxa  = sh.cell_value(rowIdx, COL_TAXA)

        # Vencimento vem como serial Excel (ctype=3=XL_CELL_DATE)
        if cellVenc.ctype == xlrd.XL_CELL_DATE:
            try:
                dtVenc = xlrd.xldate_as_datetime(cellVenc.value, wb.datemode).date()
            except Exception:
                continue
        else:
            dtVenc = AnalisarVencimento(cellVenc.value)

        if dtVenc is None:
            continue

        vrTaxa = AnalisarFloat(rawTaxa)
        if vrTaxa is None or vrTaxa <= 0:
            continue

        cdTicker = NormalizarTicker(dtVenc)
        rows.append((cdTicker, dtVenc, dtRefStr, vrTaxa))

    return rows


def ProcessarData(d: date, log, workers: int, force: bool = False) -> tuple[int, int]:
    content = BaixarXls(d, log)
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
    rows = AnalisarPlanilha(sh, wb, d, log)

    if not rows:
        log.warning("ntnb: %s — nenhuma NTN-B extraida da aba", d)
        return 0, 0

    # Skip do já-calculado: tickers que já têm vrDuration nesta data não chamam
    # a API de novo (retomada rápida após falha). --force ignora o skip.
    dtRefStr0 = d.isoformat()
    jaFeitos: set = set()
    if not force:
        jaFeitos = set(D.Consultar(
            "SELECT cdTicker FROM MtmAnbima WHERE dtReferencia = ? AND vrDuration IS NOT NULL",
            (dtRefStr0,))["cdTicker"])

    log.info("ntnb: %s — %d NTN-Bs (%d ja c/ duration, %d workers)...",
             d, len(rows), len(jaFeitos), workers)

    # Duration via API é I/O-bound: paraleliza com concorrência limitada por
    # `workers` (não sobrecarregar/bloquear a API). O UPSERT (COALESCE) preserva
    # a duration existente quando o valor vem None. UPSERT sequencial após o pool.
    def CalcularLinha(item: tuple) -> tuple:
        cdTicker, dtVenc, dtRefStr, vrTaxa = item
        if cdTicker in jaFeitos:
            return (cdTicker, dtRefStr, vrTaxa, None, True)      # pulado
        return (cdTicker, dtRefStr, vrTaxa, ObterDuration(dtVenc, d, vrTaxa, log), False)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(CalcularLinha, rows))

    upsertRows   = [{"cdTicker": r[0], "dtReferencia": r[1],
                     "vrTaxa": r[2], "vrDuration": r[3]} for r in results]
    nSkip        = sum(1 for r in results if r[4])
    nSemDuration = sum(1 for r in results if not r[4] and r[3] is None)

    D.Mesclar("MtmAnbima", pd.DataFrame(upsertRows), politica=POLITICA_MTM)

    log.info("ntnb: %s — %d upserts (%d pulados, %d sem duration)",
             d, len(upsertRows), nSkip, nSemDuration)
    return len(upsertRows), nSemDuration


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def LerArgumentos() -> argparse.Namespace:
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


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Principal() -> None:
    log     = ObterLogger(NOME_SCRIPT)
    args    = LerArgumentos()
    rel     = RelatorioExecucao("scrape_anbima_ntnb", args=vars(args))
    erro    = None
    success = True

    try:
        datas   = MontarIntervaloDatas(args)
        results: list[tuple[str, int, int]] = []

        log.info("ntnb: processando %d data(s): %s ... %s", len(datas), datas[0], datas[-1])

        for d in datas:
            nUp, nSem = ProcessarData(d, log, args.workers, args.force)
            results.append((d.isoformat(), nUp, nSem))

        rel.PorData("Resultado por data", ['data', 'MtmAnbima', 'duration'], results)
        log.info("ntnb: concluido.\n%s", rel.Texto())

    except Exception:
        success = False
        erro = traceback.format_exc()
        rel.Erro("A rodada abortou — ver traceback.")
        log.exception("ntnb: erro inesperado")

    finally:
        EnviarEmailConclusao(NOME_SCRIPT, success, rel, tracebackErro=erro, logger=log)

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    Principal()
