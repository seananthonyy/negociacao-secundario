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
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd
import xlrd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Helpers"))

import dados as D
from config import cfg
from logger import ObterLogger
from email_outlook import EnviarEmailConclusao
from relatorio_execucao import RelatorioExecucao

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

MESES_PT = {
    1: 'jan', 2: 'fev', 3: 'mar', 4: 'abr', 5: 'mai', 6: 'jun',
    7: 'jul', 8: 'ago', 9: 'set', 10: 'out', 11: 'nov', 12: 'dez',
}

ABAS = ['DI_PERCENTUAL', 'DI_SPREAD', 'IPCA_SPREAD', 'PREFIXADO']

COL_TICKER          = 0
COL_EMISSOR         = 1
COL_VENC            = 2
COL_INDEXADOR       = 3
COL_TAXA_INDICATIVA = 6   # col 5 = Taxa de Venda, col 6 = Taxa Indicativa (correta)
COL_DURATION        = 12
COL_REF_NTNB        = 14

LINHA_INICIO_DADOS = 9

# Politicas de mesclagem — o que era o `ON CONFLICT DO UPDATE SET` de cada tabela.
#
# Em AnbimaIndicativos so a taxa e reescrita; vrSpreadAnbima nao entra no DataFrame
# (quem o calcula e o calc_spread_anbima) e por isso sobrevive intacto.
POLITICA_ANBIMA = {"vrTaxaAnbima": D.SOBRESCREVER, "dtCriacao": D.PREFERIR_ATUAL}

# Em InfoAtivos a Anbima e dona da identificacao do papel (instrumento, emissor,
# vencimento) e a reescreve sempre. Duration, indexador e referencia so entram quando
# vem preenchidos — dai o PREFERIR_NOVO, que e o COALESCE(excluded.x, x) do SQL.
#
# dtAtualizacaoDuration, cdFonteReferencia e dtAtualizacaoReferencia eram um
# `CASE WHEN excluded.<outra> IS NOT NULL`. Nao precisam de politica: AnalisarPlanilha
# ja os deixa NULOS quando a coluna de que dependem veio nula, e PREFERIR_NOVO da o
# mesmo resultado.
#
# dtAtualizacaoReferencia se renova mesmo repetindo o mesmo valor: e "a Anbima ainda
# cobre este papel", nao "a referencia mudou". Quando ela para de publicar, esta data
# congela e o match_referencias assume o papel apos DIAS_REVALIDAR_REFERENCIA.
POLITICA_INFO = {
    "cdInstrumento": D.SOBRESCREVER,
    "cdEmissor":     D.SOBRESCREVER,
    "dtVencimento":  D.SOBRESCREVER,
    "dtAtualizacao": D.SOBRESCREVER,
}

# ---------------------------------------------------------------------------
# Helpers de parsing
# ---------------------------------------------------------------------------

def MontarUrl(d: date) -> str:
    baseUrl = cfg["scrape"]["anbima"]["debXlsBaseUrl"]
    return f"{baseUrl}/d{d.strftime('%y')}{MESES_PT[d.month]}{d.strftime('%d')}.xls"


def LimparEmissor(raw) -> str | None:
    s = re.sub(r'\s*\(\*+\)', '', str(raw)).strip()
    return s or None


def AnalisarFloat(raw) -> float | None:
    s = str(raw).strip()
    if s in ('--', '', 'N/D', 'N/A'):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def AnalisarData(raw) -> str | None:
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


def NormalizarIndexador(raw) -> str | None:
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


def AnalisarRefNtnb(raw) -> str | None:
    s = str(raw).strip()
    if not s or s in ('--', 'N/D'):
        return None
    parts = s.split('/')
    if len(parts) == 3:
        year2 = parts[2].strip()[-2:]
        return f'NTN-B {year2}'
    return None


def DerivarRef(cdIndexador: str | None, rawRefNtnb) -> str | None:
    if cdIndexador in ('CDI+', '%CDI'):
        return 'FUNDING'
    if cdIndexador == 'IPCA':
        return AnalisarRefNtnb(rawRefNtnb)
    return None


# ---------------------------------------------------------------------------
# Parsing do XLS
# ---------------------------------------------------------------------------

def AnalisarPlanilha(sh, dtRef: str) -> tuple[list, list]:
    anbimaRows: list[dict] = []
    infoRows:   list[dict] = []
    agora = datetime.now().isoformat(sep=' ', timespec='seconds')

    for rowIdx in range(LINHA_INICIO_DADOS, sh.nrows):
        cdTicker = str(sh.cell_value(rowIdx, COL_TICKER)).strip()
        if not cdTicker or ' ' in cdTicker or cdTicker.startswith('Obs') or cdTicker == '1.0':
            continue

        cdEmissor    = LimparEmissor(sh.cell_value(rowIdx, COL_EMISSOR))
        dtVencimento = AnalisarData(sh.cell_value(rowIdx, COL_VENC))
        cdIndexador  = NormalizarIndexador(sh.cell_value(rowIdx, COL_INDEXADOR))
        vrTaxaAnbima = AnalisarFloat(sh.cell_value(rowIdx, COL_TAXA_INDICATIVA))
        rawDuration  = AnalisarFloat(sh.cell_value(rowIdx, COL_DURATION))
        vrDuration   = round(rawDuration / 252, 6) if rawDuration is not None else None
        dtUpsertDur  = dtRef if vrDuration is not None else None
        cdReferencia        = DerivarRef(cdIndexador, sh.cell_value(rowIdx, COL_REF_NTNB))
        cdFonteReferencia  = 'Anbima' if cdReferencia is not None else None

        anbimaRows.append({'cdTicker': cdTicker, 'dtReferencia': dtRef,
                           'vrTaxaAnbima': vrTaxaAnbima, 'dtCriacao': agora})
        dtUpsertRef = dtRef if cdReferencia is not None else None
        infoRows.append({
            'cdTicker': cdTicker, 'cdInstrumento': 'DEB', 'cdEmissor': cdEmissor,
            'dtVencimento': dtVencimento,
            'vrDuration': vrDuration, 'dtAtualizacaoDuration': dtUpsertDur,
            'cdIndexador': cdIndexador,
            'cdReferencia': cdReferencia, 'cdFonteReferencia': cdFonteReferencia,
            'dtAtualizacaoReferencia': dtUpsertRef, 'dtAtualizacao': agora,
        })

    return anbimaRows, infoRows


# ---------------------------------------------------------------------------
# Download + processamento por data
# ---------------------------------------------------------------------------

def BaixarXls(d: date, log) -> bytes | None:
    url = MontarUrl(d)
    log.debug("anbima_deb: GET %s", url)
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=30, verify=False)
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


def ProcessarData(d: date, log) -> tuple[int, int]:
    dtRef   = d.isoformat()
    content = BaixarXls(d, log)
    if content is None:
        return 0, 0

    try:
        wb = xlrd.open_workbook(file_contents=content)
    except Exception as exc:
        log.warning("anbima_deb: erro ao abrir XLS de %s: %s", dtRef, exc)
        return 0, 0

    allAnbima: list[tuple] = []
    allInfo:   list[tuple] = []

    for sheetName in ABAS:
        if sheetName not in wb.sheet_names():
            log.debug("anbima_deb: aba '%s' ausente em %s", sheetName, dtRef)
            continue
        sh = wb.sheet_by_name(sheetName)
        anbimaRows, infoRows = AnalisarPlanilha(sh, dtRef)
        allAnbima.extend(anbimaRows)
        allInfo.extend(infoRows)
        log.debug("anbima_deb: %s — %s: %d tickers", dtRef, sheetName, len(anbimaRows))

    if not allAnbima:
        log.warning("anbima_deb: %s — nenhum ticker extraído", dtRef)
        return 0, 0

    D.Mesclar("AnbimaIndicativos", pd.DataFrame(allAnbima),
              politica=POLITICA_ANBIMA, data=dtRef)
    D.Mesclar("InfoAtivos", pd.DataFrame(allInfo), politica=POLITICA_INFO)

    log.info("anbima_deb: %s — %d AnbimaIndicativos, %d InfoAtivos", dtRef, len(allAnbima), len(allInfo))
    return len(allAnbima), len(allInfo)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def LerArgumentos() -> argparse.Namespace:
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
    log     = ObterLogger("scrape_anbima_debentures")
    args    = LerArgumentos()
    rel     = RelatorioExecucao("scrape_anbima_debentures", args=vars(args))
    erro    = None
    success = True

    try:
        datas   = MontarIntervaloDatas(args)
        results: list[tuple[str, int, int]] = []

        log.info("anbima_deb: processando %d data(s): %s … %s",
                 len(datas), datas[0], datas[-1])

        for d in datas:
            nAnbima, nInfo = ProcessarData(d, log)
            results.append((d.isoformat(), nAnbima, nInfo))

        rel.PorData("Resultado por data", ['data', 'Anbima', 'InfoAtivos'], results)
        log.info("anbima_deb: concluído.\n%s", rel.Texto())

    except Exception:
        success = False
        erro = traceback.format_exc()
        rel.Erro("A rodada abortou — ver traceback.")
        log.exception("anbima_deb: erro inesperado")

    finally:
        EnviarEmailConclusao("scrape_anbima_debentures", success, rel, tracebackErro=erro, logger=log)

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    Principal()
