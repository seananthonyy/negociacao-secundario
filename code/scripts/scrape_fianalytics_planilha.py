"""
scrape_fianalytics_planilha.py
==============================
Login via Playwright no FI Analytics, baixa as planilhas Excel de debêntures e
de CRI/CRA, e faz UPSERT em InfoAtivos.

Planilhas têm as colunas:
    Ticker | Indexador | issuer | Vencimento | Duration |
    Preço | % Pu Par | Taxa FIA (%) | Taxa Emissão (%) | Prêmio de Risco (%)

Colunas usadas: Ticker, Indexador, issuer, Vencimento, Duration, Taxa Emissão (%).
Duration já vem em anos (não dividir por 252).

CLI:
    python scripts/scrape_fianalytics_planilha.py
    python scripts/scrape_fianalytics_planilha.py --headless
"""

import argparse
import asyncio
import sys
import tempfile
import traceback
from datetime import date
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright

from lib.config import cfg, get_secret, get_playwright_proxy
from lib.db import get_db
from lib.logger import get_logger
from lib.email_outlook import send_completion_email

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

_SCRIPT_NAME = "scrape_fianalytics_planilha"

# Planilhas a baixar: (tipo, URL, cdInstrumento padrão ou None para inferir)
# cdInstrumento None indica que deve ser inferido do ticker (CRI/CRA)
_PLANILHAS = [
    ("deb",     "https://fi-analytics.com.br/analytics-hub/hub?type=deb",     "DEB"),
    ("cri_cra", "https://fi-analytics.com.br/analytics-hub/hub?type=cri_cra", None),
]

_SQL_UPSERT_INFO = """
INSERT INTO InfoAtivos (
    cdTicker, cdInstrumento, cdEmissor, dtVencimento,
    vrDuration, dtAtualizacaoDuration, cdIndexador, cdReferencia, cdFonteReferencia, vrTaxaEmissao, dtAtualizacao
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
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
    cdFonteReferencia      = CASE WHEN excluded.cdReferencia IS NOT NULL THEN 'FiAnalytics' ELSE cdFonteReferencia END,
    vrTaxaEmissao   = COALESCE(excluded.vrTaxaEmissao,   vrTaxaEmissao),
    dtAtualizacao      = excluded.dtAtualizacao
"""

# ---------------------------------------------------------------------------
# Helpers de parsing
# ---------------------------------------------------------------------------

def _NormalizeIndexador(raw) -> str | None:
    """Normaliza string de indexador para o código canônico do projeto."""
    s = str(raw).strip().upper() if raw is not None else ''
    if not s or s in ('--', 'N/D', 'N/A', 'NONE'):
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


def _ParseVencimento(raw) -> str | None:
    """Converte valor de célula de vencimento para ISO YYYY-MM-DD."""
    if raw is None:
        return None
    # openpyxl pode retornar datetime.datetime ou datetime.date já parseados
    if hasattr(raw, 'strftime'):
        return raw.strftime('%Y-%m-%d')
    # String no formato DD/MM/YYYY
    s = str(raw).strip()
    if not s or s in ('--', 'N/D'):
        return None
    parts = s.split('/')
    if len(parts) == 3:
        try:
            return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
        except Exception:
            pass
    # Tenta interpretar como ISO
    if len(s) == 10 and s[4] == '-':
        return s
    return None


def _ParseDuration(raw) -> float | None:
    """Converte valor de duração para float (já em anos)."""
    if raw is None:
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def _ParseFloat(raw) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _InferInstrumento(cdTicker: str, defaultInstrumento: str | None) -> str | None:
    """Infere cdInstrumento a partir do ticker quando defaultInstrumento é None."""
    if defaultInstrumento is not None:
        return defaultInstrumento
    return 'CRA' if cdTicker.upper().startswith('CRA') else 'CRI'


# ---------------------------------------------------------------------------
# Playwright: login
# ---------------------------------------------------------------------------

async def _DismissSessionDialog(page, log) -> None:
    """Clica em 'Continuar' se o popup de sessão ativa aparecer após o login."""
    try:
        continuarBtn = page.locator('div[role="alertdialog"] button:has-text("Continuar")')
        await continuarBtn.wait_for(state="visible", timeout=4_000)
        await continuarBtn.click()
        log.info("fia_planilha: popup 'Sessão Ativa' detectado — clicado em Continuar")
        await page.wait_for_timeout(1500)
    except Exception:
        pass  # popup não apareceu, segue normalmente


async def _Login(page, log) -> None:
    """Realiza login no FI Analytics, tratando popup de sessão ativa se necessário."""
    signinUrl = cfg["scrape"]["fianalytics"]["signinUrl"]
    fiUser    = get_secret("fianalyticsUser")
    fiPass    = get_secret("fianalyticsPass")

    if not fiUser or not fiPass:
        raise ValueError(
            "Credenciais do FI Analytics não configuradas (ver [env].fianalyticsUser / fianalyticsPass no config.toml)"
        )

    log.info("fia_planilha: navegando para signin %s", signinUrl)
    await page.goto(signinUrl, wait_until="networkidle", timeout=45_000)
    await page.wait_for_timeout(1500)

    log.debug("fia_planilha: preenchendo credenciais")
    await page.fill('input[name="email"]', fiUser)
    await page.fill('input[name="password"]', fiPass)
    await page.click('button[type="submit"]')

    # Popup de sessão ativa pode aparecer antes da navegação completar
    await _DismissSessionDialog(page, log)

    # Aguarda navegação pós-login (URL muda saindo de /signin)
    await page.wait_for_url(lambda url: '/signin' not in url, timeout=30_000)
    await page.wait_for_timeout(2000)
    log.info("fia_planilha: login bem-sucedido (URL: %s)", page.url)


# ---------------------------------------------------------------------------
# Playwright: download da planilha
# ---------------------------------------------------------------------------

async def _DownloadPlanilha(page, tipo: str, url: str, log) -> bytes | None:
    """Navega para URL e baixa a planilha Excel via botão de download."""
    log.info("fia_planilha: navegando para %s (%s)", url, tipo)
    await page.goto(url, wait_until="networkidle", timeout=45_000)
    await page.wait_for_timeout(2500)

    # Localiza o botão de download Excel — SVG com classe hover:text-fia-500
    # O botão pai é um <button> ou <a> que contém o SVG com essa classe
    dlLocator = page.locator('svg.hover\\:text-fia-500').first
    try:
        # Confirma visibilidade
        await dlLocator.wait_for(state="visible", timeout=15_000)
    except Exception as exc:
        log.warning("fia_planilha: botão de download não encontrado para %s: %s", tipo, exc)
        # Tenta seletor alternativo: qualquer link/botão com título relacionado a download
        dlLocator = page.locator('[title*="download" i], [aria-label*="download" i]').first
        try:
            await dlLocator.wait_for(state="visible", timeout=10_000)
        except Exception:
            log.error("fia_planilha: nenhum botão de download encontrado para %s", tipo)
            return None

    log.debug("fia_planilha: interceptando download de %s", tipo)
    try:
        async with page.expect_download(timeout=60_000) as dlInfo:
            await dlLocator.click()
        dl     = await dlInfo.value
        dlPath = await dl.path()
        if dlPath is None:
            log.error("fia_planilha: download retornou path None para %s", tipo)
            return None
        with open(dlPath, 'rb') as fh:
            content = fh.read()
        log.info("fia_planilha: %s — xlsx baixado (%d bytes)", tipo, len(content))
        return content
    except Exception as exc:
        log.error("fia_planilha: erro ao baixar planilha de %s: %s", tipo, exc)
        return None


# ---------------------------------------------------------------------------
# Parsing do XLSX
# ---------------------------------------------------------------------------

def _ParseXlsx(
    content: bytes,
    tipo: str,
    defaultInstrumento: str | None,
    log,
) -> list[tuple]:
    """
    Lê o conteúdo xlsx e retorna lista de tuplas para UPSERT em InfoAtivos:
        (cdTicker, cdInstrumento, cdEmissor, dtVencimento,
         vrDuration, dtAtualizacaoDuration, cdIndexador, cdReferencia)
    """
    dtToday = date.today().isoformat()

    tmpPath = None
    try:
        # Salva em arquivo temporário pois openpyxl não lê de BytesIO com read_only=True
        # de forma confiável em todos os ambientes
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as tmp:
            tmp.write(content)
            tmpPath = tmp.name

        wb = openpyxl.load_workbook(tmpPath, read_only=True, data_only=True)
        ws = wb.active
    except Exception as exc:
        log.error("fia_planilha: erro ao abrir xlsx de %s: %s", tipo, exc)
        return []
    finally:
        if tmpPath:
            try:
                Path(tmpPath).unlink(missing_ok=True)
            except Exception:
                pass

    rows      = list(ws.iter_rows(values_only=True))
    wb.close()

    if not rows:
        log.warning("fia_planilha: planilha de %s está vazia", tipo)
        return []

    # Localiza a linha de cabeçalho (primeira linha com valor "Ticker")
    headerRowIdx = None
    for i, row in enumerate(rows):
        rowStripped = [str(c).strip() if c is not None else '' for c in row]
        if 'Ticker' in rowStripped:
            headerRowIdx = i
            break

    if headerRowIdx is None:
        log.error("fia_planilha: cabeçalho 'Ticker' não encontrado em %s", tipo)
        return []

    headers = [str(c).strip() if c is not None else '' for c in rows[headerRowIdx]]
    log.debug("fia_planilha: %s — headers: %s", tipo, headers)

    # Índices das colunas necessárias
    def _ColIdx(name: str) -> int | None:
        try:
            return headers.index(name)
        except ValueError:
            return None

    idxTicker      = _ColIdx('Ticker')
    idxIndexador   = _ColIdx('Indexador')
    idxIssuer      = _ColIdx('issuer')
    idxVenc        = _ColIdx('Vencimento')
    idxDuration    = _ColIdx('Duration')
    idxTaxaEmissao = _ColIdx('Taxa Emissão (%)')

    if idxTicker is None:
        log.error("fia_planilha: coluna 'Ticker' ausente em %s", tipo)
        return []

    infoRows: list[tuple] = []

    for row in rows[headerRowIdx + 1:]:
        if not row or all(c is None for c in row):
            continue

        cdTicker = str(row[idxTicker]).strip() if idxTicker is not None and row[idxTicker] is not None else ''
        if not cdTicker or cdTicker in ('None', '--'):
            continue

        cdEmissor    = str(row[idxIssuer]).strip()    if idxIssuer    is not None and row[idxIssuer]    is not None else None
        rawVenc      = row[idxVenc]                   if idxVenc      is not None else None
        rawDuration  = row[idxDuration]               if idxDuration  is not None else None
        rawIndexador = row[idxIndexador]               if idxIndexador is not None else None
        rawTaxaEm    = row[idxTaxaEmissao]             if idxTaxaEmissao is not None else None

        cdEmissor     = cdEmissor or None
        dtVencimento  = _ParseVencimento(rawVenc)
        vrDuration    = _ParseDuration(rawDuration)
        dtUpsertDur   = dtToday if vrDuration is not None else None
        cdIndexador   = _NormalizeIndexador(rawIndexador)
        cdInstrumento = _InferInstrumento(cdTicker, defaultInstrumento)
        vrTaxaEmissao = _ParseFloat(rawTaxaEm) if rawTaxaEm is not None else None

        # cdReferencia = None — FI Analytics não fornece esta informação
        infoRows.append((
            cdTicker, cdInstrumento, cdEmissor, dtVencimento,
            vrDuration, dtUpsertDur, cdIndexador, None, None, vrTaxaEmissao,
        ))

    log.info("fia_planilha: %s — %d tickers parseados", tipo, len(infoRows))
    return infoRows


# ---------------------------------------------------------------------------
# Persistência
# ---------------------------------------------------------------------------

def _SaveToDb(conn, infoRows: list[tuple], tipo: str, log) -> int:
    """Executa UPSERT em InfoAtivos e retorna número de linhas processadas."""
    if not infoRows:
        log.warning("fia_planilha: %s — nenhum ticker para salvar", tipo)
        return 0
    conn.executemany(_SQL_UPSERT_INFO, infoRows)
    conn.commit()
    log.info("fia_planilha: %s — %d registros em InfoAtivos", tipo, len(infoRows))
    return len(infoRows)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def _BuildSummary(results: list[tuple[str, int]]) -> str:
    """Monta tabela de resumo por planilha."""
    col1 = max(len(r[0]) for r in results) if results else 7
    col1 = max(col1, len("Planilha"))

    lines = [
        f"{'Planilha':<{col1}}    {'Tickers':>7}",
        "-" * (col1 + 12),
    ]
    total = 0
    for planilha, nTickers in results:
        lines.append(f"{planilha:<{col1}}    {nTickers:>7}")
        total += nTickers
    lines.append("-" * (col1 + 12))
    lines.append(f"{'TOTAL':<{col1}}    {total:>7}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main assíncrono
# ---------------------------------------------------------------------------

async def _MainAsync(args: argparse.Namespace, log) -> list[tuple[str, int]]:
    """Orquestra login, downloads e UPSERTs. Retorna lista (planilha, nTickers)."""
    conn    = get_db()
    results: list[tuple[str, int]] = []

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=args.headless, proxy=get_playwright_proxy())
            context = await browser.new_context(
                accept_downloads=True,
                viewport={"width": 1400, "height": 900},
            )
            page = await context.new_page()

            # Login único — sessão reutilizada para ambas as planilhas
            await _Login(page, log)

            for tipo, url, defaultInstrumento in _PLANILHAS:
                content = await _DownloadPlanilha(page, tipo, url, log)
                if content is None:
                    results.append((tipo, 0))
                    continue

                infoRows = _ParseXlsx(content, tipo, defaultInstrumento, log)
                nSaved   = _SaveToDb(conn, infoRows, tipo, log)
                results.append((tipo, nSaved))

            await browser.close()

    finally:
        conn.close()

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _ParseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Login FI Analytics via Playwright, baixa planilhas Excel de "
            "debêntures e CRI/CRA, e faz UPSERT em InfoAtivos."
        )
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="Rodar Playwright em modo headless (padrão: False para debug visual).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Main() -> None:
    log     = get_logger(_SCRIPT_NAME)
    args    = _ParseArgs()
    summary = ""
    success = True

    try:
        results = asyncio.run(_MainAsync(args, log))
        summary = _BuildSummary(results)
        log.info("fia_planilha: concluído.\n%s", summary)

    except Exception:
        success = False
        summary = traceback.format_exc()
        log.exception("fia_planilha: erro inesperado")

    finally:
        send_completion_email(_SCRIPT_NAME, success, summary, logger=log)


if __name__ == "__main__":
    Main()
