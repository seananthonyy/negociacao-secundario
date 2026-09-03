"""
scrape_fianalytics_planilha.py
==============================
Login via Playwright no FI Analytics, baixa os CSVs de debêntures e de CRI/CRA, e
faz UPSERT em InfoAtivos.

Layout NOVO do site (jul/2026): login (name=email/password, botão 'Entrar') cai na
lista de debêntures → botão 'Exportar' baixa o CSV; para CRI/CRA, clica no item de
menu 'Lista' (o de CRI/CRA é o ÚLTIMO dos dois) e no mesmo 'Exportar'. Seletores por
TEXTO/role — nunca por classe CSS (Tailwind com hash muda a cada build e já quebrou).

CSV: UTF-8 com BOM, separador ';', decimal vírgula. Colunas:
    Ticker | Indexador | Emissor | Vencimento | Duration | Preço | % PU Par |
    Taxa FIA (%) | Taxa Emissão (%) | Prêmio de Risco (%) | ...
Colunas usadas: Ticker, Indexador, Emissor, Vencimento, Duration, Taxa Emissão (%).
Duration já vem em anos (não dividir por 252).

CLI:
    python codigos/scripts/scrape_fianalytics_planilha.py
    python codigos/scripts/scrape_fianalytics_planilha.py --no-headless   # debug visual
"""

import argparse
import asyncio
import csv
import io
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers"))

import pandas as pd
from playwright.async_api import async_playwright

import dados as D
from config import cfg, ObterSegredo, ObterProxyPlaywright
from logger import ObterLogger
from email_outlook import EnviarEmailConclusao
from relatorio_execucao import RelatorioExecucao

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

NOME_SCRIPT = "scrape_fianalytics_planilha"

# O que era o `ON CONFLICT(cdTicker) DO UPDATE SET` de InfoAtivos.
#
# A FI Analytics reescreve sempre a identificacao do papel (instrumento, emissor,
# vencimento) e a duration quando ela vem. Ja cdIndexador e vrTaxaEmissao sao
# PREFERIR_ATUAL (era o COALESCE na ordem inversa, `COALESCE(cdIndexador,
# excluded.cdIndexador)`): a FI so preenche o buraco que a Anbima ou a B3 deixaram,
# nunca sobrescreve o que elas ja apuraram.
#
# dtAtualizacaoDuration era um `CASE WHEN excluded.vrDuration IS NOT NULL`: nao precisa
# de politica, o AnalisarCsv ja o deixa nulo quando nao ha duration.
#
# cdReferencia/cdFonteReferencia continuam saindo nulos daqui — a FI Analytics nao
# fornece referencia. Como sao PREFERIR_NOVO, entrar nulo nao apaga o que ja existe.
POLITICA_INFO = {
    "cdInstrumento": D.SOBRESCREVER,
    "cdEmissor":     D.SOBRESCREVER,
    "dtVencimento":  D.SOBRESCREVER,
    "dtAtualizacao": D.SOBRESCREVER,
    "cdIndexador":   D.PREFERIR_ATUAL,
    "vrTaxaEmissao": D.PREFERIR_ATUAL,
}

# ---------------------------------------------------------------------------
# Helpers de parsing
# ---------------------------------------------------------------------------

def NormalizarIndexador(raw) -> str | None:
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


def AnalisarVencimento(raw) -> str | None:
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


def AnalisarFloat(raw) -> float | None:
    """Float tolerante ao formato BR (decimal vírgula, milhar ponto) do CSV da FIA."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in ('--', 'N/D', 'N/A', 'None', ''):
        return None
    if ',' in s:
        s = s.replace('.', '').replace(',', '.')   # BR: '.' milhar, ',' decimal
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def AnalisarDuration(raw) -> float | None:
    """Converte valor de duração para float (já em anos)."""
    v = AnalisarFloat(raw)
    return v if (v is not None and v > 0) else None


def InferirInstrumento(cdTicker: str, defaultInstrumento: str | None) -> str | None:
    """Infere cdInstrumento a partir do ticker quando defaultInstrumento é None."""
    if defaultInstrumento is not None:
        return defaultInstrumento
    return 'CRA' if cdTicker.upper().startswith('CRA') else 'CRI'


# ---------------------------------------------------------------------------
# Playwright: login
# ---------------------------------------------------------------------------

async def FecharDialogoSessao(page, log) -> None:
    """Clica em 'Continuar' se o popup de sessão ativa aparecer após o login."""
    try:
        continuarBtn = page.locator('div[role="alertdialog"] button:has-text("Continuar")')
        await continuarBtn.wait_for(state="visible", timeout=4_000)
        await continuarBtn.click()
        log.info("fia_planilha: popup 'Sessão Ativa' detectado — clicado em Continuar")
        await page.wait_for_timeout(1500)
    except Exception:
        pass  # popup não apareceu, segue normalmente


async def Autenticar(page, log) -> None:
    """Realiza login no FI Analytics, tratando popup de sessão ativa se necessário."""
    signinUrl = cfg["scrape"]["fianalytics"]["signinUrl"]
    fiUser    = ObterSegredo("fianalyticsUser")
    fiPass    = ObterSegredo("fianalyticsPass")

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
    await FecharDialogoSessao(page, log)

    # Aguarda navegação pós-login (URL muda saindo de /signin)
    await page.wait_for_url(lambda url: '/signin' not in url, timeout=30_000)
    await page.wait_for_timeout(2000)
    log.info("fia_planilha: login bem-sucedido (URL: %s)", page.url)


# ---------------------------------------------------------------------------
# Playwright: download da planilha
# ---------------------------------------------------------------------------
#
# Layout novo do site (jul/2026): não há mais URLs `?type=deb`. Após o login a
# página cai na LISTA DE DEBÊNTURES; um botão "Exportar" (SVG lucide-download)
# baixa o xlsx. Para CRI/CRA, primeiro clica no item de menu "Lista" e depois no
# mesmo "Exportar". Selecionamos por TEXTO/role (estável), nunca por classe CSS
# (as classes Tailwind com hash mudam a cada build — foi o que quebrou antes).

async def BaixarViaExportar(page, tipo: str, log) -> bytes | None:
    """Clica no botão 'Exportar' visível e captura o download do xlsx."""
    exportBtn = page.get_by_role("button", name="Exportar").first
    try:
        await exportBtn.wait_for(state="visible", timeout=20_000)
    except Exception as exc:
        log.error("fia_planilha: botão 'Exportar' não encontrado para %s: %s", tipo, exc)
        return None

    log.debug("fia_planilha: interceptando download de %s", tipo)
    try:
        async with page.expect_download(timeout=60_000) as dlInfo:
            await exportBtn.click()
        dl     = await dlInfo.value
        dlPath = await dl.path()
        if dlPath is None:
            log.error("fia_planilha: download retornou path None para %s", tipo)
            return None
        content = Path(dlPath).read_bytes()
        log.info("fia_planilha: %s — download '%s' (%d bytes)", tipo, dl.suggested_filename, len(content))
        return content
    except Exception as exc:
        log.error("fia_planilha: erro ao baixar planilha de %s: %s", tipo, exc)
        return None


async def IrParaCriCra(page, log) -> bool:
    """Navega da lista de debêntures para a de CRI/CRA pelo item de menu 'Lista'.
    Há dois itens 'Lista' na sidebar (Debêntures ~1.5k e CRI/CRA ~640); o de CRI/CRA
    é o ÚLTIMO (a sidebar lista Debêntures antes)."""
    listaBtn = page.locator('button:has-text("Lista")').last
    try:
        await listaBtn.wait_for(state="visible", timeout=15_000)
        await listaBtn.click()
        await page.wait_for_timeout(2_500)   # aguarda a lista de CRI/CRA carregar
        log.info("fia_planilha: navegou para a lista de CRI/CRA (menu 'Lista')")
        return True
    except Exception as exc:
        log.error("fia_planilha: não consegui abrir a lista de CRI/CRA ('Lista'): %s", exc)
        return False


# ---------------------------------------------------------------------------
# Parsing do XLSX
# ---------------------------------------------------------------------------

def AnalisarCsv(
    content: bytes,
    tipo: str,
    defaultInstrumento: str | None,
    log,
) -> list[tuple]:
    """
    Lê o CSV da FIA (UTF-8 com BOM, separador ';', decimal vírgula) e retorna as tuplas
    para UPSERT em InfoAtivos. Colunas usadas: Ticker, Indexador, Emissor, Vencimento,
    Duration, Taxa Emissão (%). (O layout novo trocou xlsx→CSV e 'issuer'→'Emissor'.)
    """
    dtToday = date.today().isoformat()

    txt  = content.decode('utf-8-sig', errors='replace')   # utf-8-sig remove o BOM
    rows = list(csv.reader(io.StringIO(txt), delimiter=';'))
    if not rows:
        log.warning("fia_planilha: CSV de %s está vazio", tipo)
        return []

    # Linha de cabeçalho = primeira linha que contém 'Ticker'
    headerRowIdx = next(
        (i for i, row in enumerate(rows) if 'Ticker' in [c.strip() for c in row]), None)
    if headerRowIdx is None:
        log.error("fia_planilha: cabeçalho 'Ticker' não encontrado em %s", tipo)
        return []

    headers = [c.strip() for c in rows[headerRowIdx]]
    log.debug("fia_planilha: %s — headers: %s", tipo, headers)

    def ColIdx(name: str) -> int | None:
        try:
            return headers.index(name)
        except ValueError:
            return None

    idxTicker      = ColIdx('Ticker')
    idxIndexador   = ColIdx('Indexador')
    idxEmissor     = ColIdx('Emissor')            # era 'issuer' no xlsx antigo
    idxVenc        = ColIdx('Vencimento')
    idxDuration    = ColIdx('Duration')
    idxTaxaEmissao = ColIdx('Taxa Emissão (%)')

    if idxTicker is None:
        log.error("fia_planilha: coluna 'Ticker' ausente em %s", tipo)
        return []

    def Cell(row, idx):
        return row[idx] if (idx is not None and idx < len(row)) else None

    infoRows: list[dict] = []
    for row in rows[headerRowIdx + 1:]:
        if not row or all((c or '').strip() == '' for c in row):
            continue

        cdTicker = (Cell(row, idxTicker) or '').strip()
        if not cdTicker or cdTicker in ('None', '--'):
            continue

        cdEmissor     = (Cell(row, idxEmissor) or '').strip() or None
        dtVencimento  = AnalisarVencimento(Cell(row, idxVenc))
        vrDuration    = AnalisarDuration(Cell(row, idxDuration))
        dtUpsertDur   = dtToday if vrDuration is not None else None
        cdIndexador   = NormalizarIndexador(Cell(row, idxIndexador))
        cdInstrumento = InferirInstrumento(cdTicker, defaultInstrumento)
        vrTaxaEmissao = AnalisarFloat(Cell(row, idxTaxaEmissao))

        # cdReferencia = None — FI Analytics não fornece esta informação
        infoRows.append({
            'cdTicker': cdTicker, 'cdInstrumento': cdInstrumento,
            'cdEmissor': cdEmissor, 'dtVencimento': dtVencimento,
            'vrDuration': vrDuration, 'dtAtualizacaoDuration': dtUpsertDur,
            'cdIndexador': cdIndexador,
            'cdReferencia': None, 'cdFonteReferencia': None,
            'vrTaxaEmissao': vrTaxaEmissao,
            'dtAtualizacao': datetime.now().isoformat(sep=' ', timespec='seconds'),
        })

    log.info("fia_planilha: %s — %d tickers parseados", tipo, len(infoRows))
    return infoRows


# ---------------------------------------------------------------------------
# Persistência
# ---------------------------------------------------------------------------

def Gravar(infoRows: list[dict], tipo: str, log) -> int:
    """Mescla em InfoAtivos e retorna número de linhas processadas."""
    if not infoRows:
        log.warning("fia_planilha: %s — nenhum ticker para salvar", tipo)
        return 0
    D.Mesclar("InfoAtivos", pd.DataFrame(infoRows), politica=POLITICA_INFO)
    log.info("fia_planilha: %s — %d registros em InfoAtivos", tipo, len(infoRows))
    return len(infoRows)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def MontarResumo(results: list[tuple[str, int]]) -> str:
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

async def PrincipalAsync(args: argparse.Namespace, log) -> list[tuple[str, int]]:
    """Orquestra login, downloads e UPSERTs. Retorna lista (planilha, nTickers).
    Fluxo novo: login → lista de debêntures (Exportar) → menu 'Lista' → CRI/CRA (Exportar)."""
    results: list[tuple[str, int]] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=args.headless, proxy=ObterProxyPlaywright())
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1400, "height": 900},
            ignore_https_errors=True,
        )
        page = await context.new_page()

        await Autenticar(page, log)          # o login cai na lista de debêntures
        await page.wait_for_timeout(2_500)

        # 1) DEBÊNTURES — exporta direto da lista onde o login caiu.
        content = await BaixarViaExportar(page, "deb", log)
        if content:
            infoRows = AnalisarCsv(content, "deb", "DEB", log)
            results.append(("deb", Gravar(infoRows, "deb", log)))
        else:
            results.append(("deb", 0))

        # 2) CRI/CRA — abre a lista pelo menu 'Lista' e exporta.
        if await IrParaCriCra(page, log):
            content = await BaixarViaExportar(page, "cri_cra", log)
            if content:
                infoRows = AnalisarCsv(content, "cri_cra", None, log)
                results.append(("cri_cra", Gravar(infoRows, "cri_cra", log)))
            else:
                results.append(("cri_cra", 0))
        else:
            results.append(("cri_cra", 0))

        await browser.close()

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def LerArgumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Login FI Analytics via Playwright, baixa planilhas Excel de "
            "debêntures e CRI/CRA, e faz UPSERT em InfoAtivos."
        )
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Rodar Playwright em modo headless (padrão: True; use --no-headless para debug visual).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Principal() -> None:
    log     = ObterLogger(NOME_SCRIPT)
    args    = LerArgumentos()
    rel     = RelatorioExecucao(NOME_SCRIPT)
    erro    = None
    summary = ""
    success = True

    try:
        results = asyncio.run(PrincipalAsync(args, log))
        summary = MontarResumo(results)
        log.info("fia_planilha: concluído.\n%s", summary)

        # Falha EXPLÍCITA se nenhuma planilha gravou tickers — o scraper quebrou
        # (não é "sem dado"). Antes saía exit 0 gravando 0 = falha silenciosa.
        if sum(n for _, n in results) == 0:
            success = False
            rel.Erro("Nenhuma planilha gravou tickers — o download provavelmente quebrou "
                     "(seletor 'Exportar'/'Lista' mudou?). Ver logs.")
            log.error("fia_planilha: 0 tickers em TODAS as planilhas — falha (exit 1).")

    except Exception:
        success = False
        erro = traceback.format_exc()
        rel.Erro("A rodada abortou — ver traceback.")
        log.exception("fia_planilha: erro inesperado")

    finally:
        if summary:
            rel.Secao("Resumo", ["saida"], [[l] for l in summary.splitlines() if l.strip()])
        EnviarEmailConclusao(NOME_SCRIPT, success, rel, tracebackErro=erro, logger=log)

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    Principal()
