"""
scrape_anbima_cri_cra.py
========================
Baixa o CSV de taxas indicativas de CRIs e CRAs da Anbima via Playwright
(o link de download é um blob URL — não é possível download direto) e popula:
  - AnbimaIndicativos  (cdTicker, dtReferencia, vrTaxaAnbima = Taxa Indicativa)
  - InfoAtivos         (cdEmissor, cdInstrumento, dtVencimento, vrDuration,
                        dtAtualizacaoDuration, cdIndexador, cdReferencia)

CRI ou CRA é determinado pelo ticker: começa com 'CRA' → CRA, senão → CRI.

**O CSV do portal já traz os ~5 últimos pregões de uma vez** (coluna
"Data de Referência"). Por isso o script faz UM download por rodada e distribui
as linhas pelas datas pedidas — o seletor de data da página é irrelevante e não
é tocado. Datas fora da janela de ~5 pregões simplesmente não existem no CSV.

CLI:
    python scripts/scrape_anbima_cri_cra.py --date 2026-05-29
    python scripts/scrape_anbima_cri_cra.py --start 2026-05-01 --end 2026-05-29
    python scripts/scrape_anbima_cri_cra.py --date 2026-05-29 --no-headless
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

from lib.config import cfg, ObterProxyPlaywright
from lib.db import ObterBanco
from lib.logger import ObterLogger
from lib.email_outlook import EnviarEmailConclusao
from lib.relatorio_execucao import RelatorioExecucao

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

SQL_UPSERT_ANBIMA = """
INSERT INTO AnbimaIndicativos (cdTicker, dtReferencia, vrTaxaAnbima, vrSpreadAnbima)
VALUES (?, ?, ?, NULL)
ON CONFLICT(cdTicker, dtReferencia) DO UPDATE SET
    vrTaxaAnbima = excluded.vrTaxaAnbima
"""

SQL_UPSERT_INFO = """
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

def LimparEmissor(raw) -> str | None:
    s = re.sub(r'\s*\(\*+\)', '', str(raw)).strip()
    return s or None


def AnalisarFloat(raw) -> float | None:
    s = str(raw).strip()
    if s in ('--', '', 'N/D', 'N/A'):
        return None
    try:
        return float(s.replace(',', '.'))
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
# Parsing do CSV
# ---------------------------------------------------------------------------

def AnalisarLinha(row: dict, dtRef: str) -> tuple[tuple, tuple] | None:
    """Converte uma linha do CSV em (anbimaRow, infoRow). None se o ticker for inválido."""
    cdTicker = (row.get('Código') or row.get('Codigo') or '').strip()
    if not cdTicker or ' ' in cdTicker:
        return None

    cdInstrumento = 'CRA' if cdTicker.upper().startswith('CRA') else 'CRI'
    # 'Risco de Crédito' = empresa originadora; 'Emissor' = securitizadora
    cdEmissor     = LimparEmissor(row.get('Risco de Crédito') or row.get('Risco de Credito') or '')
    dtVencimento  = AnalisarData(row.get('Vencimento') or '')
    rawIndexador  = row.get('Índice / Correção') or row.get('Indice / Correcao') or ''
    cdIndexador   = NormalizarIndexador(rawIndexador) or 'PREFIXADO'
    vrTaxaAnbima  = AnalisarFloat(row.get('Taxa Indicativa') or '')
    rawDuration   = AnalisarFloat(row.get('Duration') or '')
    vrDuration    = round(rawDuration / 252, 6) if rawDuration is not None else None
    dtUpsertDur   = dtRef if vrDuration is not None else None
    rawRefNtnb    = row.get('Referência NTNB') or row.get('Referencia NTNB') or ''
    cdReferencia      = DerivarRef(cdIndexador, rawRefNtnb)
    cdFonteReferencia = 'Anbima' if cdReferencia is not None else None

    return (
        (cdTicker, dtRef, vrTaxaAnbima),
        (cdTicker, cdInstrumento, cdEmissor, dtVencimento,
         vrDuration, dtUpsertDur, cdIndexador, cdReferencia, cdFonteReferencia),
    )


def AnalisarCsv(content: bytes, log) -> dict[str, tuple[list, list]]:
    """Parseia o CSV inteiro e agrupa as linhas por 'Data de Referência' (ISO).

    Retorna {dtRef: (anbimaRows, infoRows)}. O CSV do portal traz ~5 pregões.
    Levanta RuntimeError se o CSV for indecifrável, sem header ou sem linha útil —
    silêncio aqui foi a causa do bug de 01–06/07/2026 (ver [[09 - Progresso]]).
    """
    textDecoded = None
    for enc in ('utf-8-sig', 'utf-8', 'latin-1', 'cp1252'):
        try:
            textDecoded = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if textDecoded is None:
        raise RuntimeError("não foi possível decodificar o CSV (nenhum encoding serviu)")

    # Detecta delimitador
    sample = textDecoded[:2048]
    delimiter = ';' if sample.count(';') >= sample.count(',') else ','

    reader = csv.DictReader(io.StringIO(textDecoded), delimiter=delimiter)
    if not reader.fieldnames:
        raise RuntimeError("CSV sem headers")

    # Header usa " ; " (com espaços) — strip para normalizar os nomes
    reader.fieldnames = [f.strip() for f in reader.fieldnames]
    log.debug("anbima_cricra: colunas CSV: %s", reader.fieldnames)

    porData: dict[str, tuple[list, list]] = {}
    semData = 0

    for row in reader:
        rawDataRef = (row.get('Data Referência') or row.get('Data de Referência')
                      or row.get('Data Referencia') or '')
        dtLinhaRef = AnalisarData(rawDataRef.strip())
        if not dtLinhaRef:
            semData += 1
            continue

        parsed = AnalisarLinha(row, dtLinhaRef)
        if parsed is None:
            continue

        anbimaRow, infoRow = parsed
        anbimaRows, infoRows = porData.setdefault(dtLinhaRef, ([], []))
        anbimaRows.append(anbimaRow)
        infoRows.append(infoRow)

    if semData:
        log.warning("anbima_cricra: %d linha(s) sem 'Data de Referência' parseável — ignoradas", semData)
    if not porData:
        raise RuntimeError(f"CSV baixado ({len(content)} bytes) não produziu nenhuma linha útil "
                           f"— colunas: {reader.fieldnames}")

    log.info("anbima_cricra: CSV cobre %d data(s): %s",
             len(porData), ", ".join(sorted(porData)))
    return porData


# ---------------------------------------------------------------------------
# Playwright: um único download (o CSV já traz os ~5 últimos pregões)
# ---------------------------------------------------------------------------

# Seletores estáveis (data-testid). As classes do portal são CSS-modules com hash
# (ex.: '_menuFiles_727cw_116') e mudam a cada build — nunca usar classe aqui.
SEL_LINK_CSV = 'ul[data-testid="toolbar-file-list"] a'


async def BaixarCsv(page: Page, log) -> bytes:
    """Baixa o CSV único do portal. Levanta se o link sumir ou o download falhar."""
    csvLocator = page.locator(SEL_LINK_CSV).filter(has_text='CSV')
    try:
        await csvLocator.wait_for(state='visible', timeout=30_000)
    except Exception as exc:
        raise RuntimeError(
            f"link de CSV não encontrado ({SEL_LINK_CSV!r}) — o portal Anbima mudou de layout: {exc}"
        ) from exc

    async with page.expect_download(timeout=20_000) as dlInfo:
        await csvLocator.click()
    dl      = await dlInfo.value
    dlPath  = await dl.path()
    with open(dlPath, 'rb') as fh:
        content = fh.read()

    if not content:
        raise RuntimeError("CSV baixado veio vazio (0 bytes)")

    log.info("anbima_cricra: CSV baixado (%d bytes)", len(content))
    return content


# ---------------------------------------------------------------------------
# Processamento por data
# ---------------------------------------------------------------------------

def GravarNoBanco(conn, anbimaRows: list, infoRows: list, dtStr: str, log) -> tuple[int, int]:
    conn.executemany(SQL_UPSERT_ANBIMA, anbimaRows)
    conn.executemany(SQL_UPSERT_INFO,   infoRows)
    conn.commit()
    log.info("anbima_cricra: %s — %d AnbimaIndicativos, %d InfoAtivos",
             dtStr, len(anbimaRows), len(infoRows))
    return len(anbimaRows), len(infoRows)


# ---------------------------------------------------------------------------
# Main assíncrono
# ---------------------------------------------------------------------------

async def PrincipalAsync(args: argparse.Namespace, log) -> tuple[list[tuple[str, int, int]], list[str]]:
    """Baixa o CSV uma vez e distribui as linhas pelas datas pedidas.
    Retorna (resultados por data, datas disponíveis no CSV)."""
    datas   = MontarIntervaloDatas(args)
    pageUrl = cfg["scrape"]["anbima"]["cricraUrl"]
    results: list[tuple[str, int, int]] = []
    conn    = ObterBanco()

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=args.headless, proxy=ObterProxyPlaywright())
            context = await browser.new_context(
                accept_downloads=True,
                viewport={"width": 1400, "height": 900},
            )
            page = await context.new_page()
            try:
                log.info("anbima_cricra: navegando para %s", pageUrl)
                await page.goto(pageUrl, wait_until="domcontentloaded", timeout=45_000)
                content = await BaixarCsv(page, log)
            finally:
                await browser.close()

        porData = AnalisarCsv(content, log)   # {dtRef: (anbimaRows, infoRows)}

        for d in datas:
            dtStr = d.isoformat()
            if dtStr not in porData:
                log.warning("anbima_cricra: %s — não está no CSV (o portal só publica os "
                            "~5 últimos pregões; disponíveis: %s)", dtStr, ", ".join(sorted(porData)))
                results.append((dtStr, 0, 0))
                continue
            anbimaRows, infoRows = porData[dtStr]
            nAnbima, nInfo       = GravarNoBanco(conn, anbimaRows, infoRows, dtStr, log)
            results.append((dtStr, nAnbima, nInfo))
    finally:
        conn.close()

    return results, sorted(porData)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def LerArgumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Baixa taxas indicativas de CRI/CRA da Anbima via Playwright."
    )
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--date",  metavar="YYYY-MM-DD", help="Data única de publicação Anbima.")
    grp.add_argument("--start", metavar="YYYY-MM-DD", help="Início do intervalo.")
    parser.add_argument("--end", metavar="YYYY-MM-DD", help="Fim do intervalo (requer --start).")
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Rodar Playwright em modo headless (padrão: True; use --no-headless para debug visual).",
    )
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


def MontarResumo(results: list[tuple[str, int, int]], disponiveis: list[str]) -> str:
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
    lines.append("")
    lines.append(f"Datas publicadas no CSV do portal: {', '.join(disponiveis)}")

    faltando = [d for d, n, _ in results if n == 0]
    if faltando:
        lines.append("")
        lines.append(f"ATENCAO: {len(faltando)} data(s) pedida(s) sem linha no CSV: {', '.join(faltando)}")
        lines.append("(o portal Anbima só publica os ~5 últimos pregões de CRI/CRA)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Principal() -> None:
    log     = ObterLogger("scrape_anbima_cri_cra")
    args    = LerArgumentos()
    rel     = RelatorioExecucao("scrape_anbima_cri_cra")
    erro    = None
    summary = ""
    success = True

    try:
        results, disponiveis = asyncio.run(PrincipalAsync(args, log))
        summary = MontarResumo(results, disponiveis)
        log.info("anbima_cricra: concluído.\n%s", summary)

        # Nenhuma linha gravada em NENHUMA data pedida = scraper quebrado, não
        # "não tem dado". Falha alto — foi exatamente isso que passou batido em
        # 01-06/07/2026 (exit 0 com 0 linhas). Data individual ausente é só WARNING.
        if not any(n > 0 for _, n, _ in results):
            success = False
            summary = ("NENHUMA linha gravada para as datas pedidas.\n\n" + summary)
            log.error("anbima_cricra: 0 linhas gravadas em todas as datas pedidas — falhando")

    except Exception:
        success = False
        erro = traceback.format_exc()
        rel.Erro("A rodada abortou — ver traceback.")
        log.exception("anbima_cricra: erro inesperado")

    finally:
        if summary:
            rel.Secao("Resumo", ["saida"], [[l] for l in summary.splitlines() if l.strip()])
        EnviarEmailConclusao("scrape_anbima_cri_cra", success, rel, tracebackErro=erro, logger=log)

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    Principal()
