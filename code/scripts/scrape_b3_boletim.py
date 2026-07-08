"""
scrape_b3_boletim.py
====================
Baixa o Boletim Diario B3 de credito privado (DEB/CRI/CRA) via Playwright,
salva debug em data/debug/ e faz UPSERT em NegociosBrutos.

A pagina B3 usa um iframe em arquivos.b3.com.br/bdi/tabelas.
O fluxo:
  1. Navega diretamente para o iframe
  2. Clica na aba "Renda fixa"
  3. Seta a data no duet-date-picker via JS
  4. Seleciona a tabela "Negocio a negocio" (Trade@true)
  5. Intercepta a request de download do CSV ao clicar no botao CSV

CLI:
    python scripts/scrape_b3_boletim.py --date 2026-05-27
    python scripts/scrape_b3_boletim.py --start 2026-05-25 --end 2026-05-27
    python scripts/scrape_b3_boletim.py --date 2026-05-27 --headless
    python scripts/scrape_b3_boletim.py --date 2026-05-27 --debug-only
"""

import argparse
import asyncio
import csv
import io
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

# Garante que code/ esteja no sys.path ao rodar como script
sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright, Download, Page, BrowserContext

from lib.db import get_db
from lib.config import cfg, get_playwright_proxy
from lib.logger import get_logger
from lib.email_outlook import send_completion_email

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
BASE_URL     = cfg["scrape"]["b3"]["baseUrl"]
IFRAME_URL   = "https://arquivos.b3.com.br/bdi/tabelas?lang=pt-BR"
INSTRUMENTOS: list[str] = cfg["scrape"]["b3"]["instrumentosAceitos"]

DEBUG_DIR = Path("data/debug")

# ---------------------------------------------------------------------------
# Mapeamento de colunas CSV → colunas internas
#
# Nomes reais confirmados no CSV de "Negocio a negocio" da B3 (descobertos em 2026-05-30).
# O CSV usa delimitador ";" e tem 6 linhas de preamble descritivo antes do header.
# Os nomes à direita (valores) sao as colunas em NegociosBrutos — NAO altere.
# ---------------------------------------------------------------------------
COLUMN_MAP: dict[str, str] = {
    # Nome exato no CSV (strip aplicado)           →  coluna interna em NegociosBrutos
    "Instrumento financeiro"                       : "cdInstrumento",
    "Emissor"                                      : "cdEmissor",
    "Código IF"                                    : "cdTicker",
    "Quantidade negociada"                         : "vrQuantidade",
    "Preço negócio"                                : "vrPU",
    "Volume financeiro (R$)"                       : "vrVolume",
    "Taxa negócio"                                 : "vrTaxaNegocio",
    "Horário negócio"                              : "dtHorarioNegocio",
    "Data negócio"                                 : "dtNegocio",
    "Cód. identificador do negócio"                : "cdIdentificadorNegocio",
    "Código ISIN"                                  : "cdISIN",
    "Data liquidação"                              : "dtLiquidacao",
    "Situação negócio"                             : "cdSituacao",
    # Coluna ignorada: "Origem negócio" (sempre "Pre-registro - Voice")
}

# Numero de linhas de preamble (descricao textual) antes do header real no CSV.
# O header real comeca na linha 8 (linhas 1-7 sao descricao + linha em branco).
CSV_PREAMBLE_LINES = 7

# Colunas obrigatorias (nao podem estar ausentes no CSV apos mapeamento)
REQUIRED_COLS = {
    "cdIdentificadorNegocio", "cdInstrumento", "cdEmissor", "cdTicker",
    "vrQuantidade", "vrPU", "vrVolume", "dtHorarioNegocio",
    "dtNegocio", "dtLiquidacao", "cdSituacao",
}

UPSERT_SQL = """
INSERT INTO NegociosBrutos (
    cdIdentificadorNegocio, cdInstrumento, cdEmissor, cdTicker,
    vrQuantidade, vrPU, vrVolume, vrTaxaNegocio,
    dtHorarioNegocio, dtNegocio, cdISIN, dtLiquidacao, cdSituacao
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(cdIdentificadorNegocio) DO UPDATE SET
    vrTaxaNegocio = excluded.vrTaxaNegocio,
    cdSituacao    = excluded.cdSituacao,
    dtAtualizacao   = CURRENT_TIMESTAMP
"""

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _ParseArgs() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Baixa Boletim Diario B3 (DEB/CRI/CRA) e salva em NegociosBrutos."
    )
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--date",  metavar="YYYY-MM-DD", help="Data unica")
    grp.add_argument("--start", metavar="YYYY-MM-DD", help="Inicio do intervalo")
    p.add_argument("--end",     metavar="YYYY-MM-DD", help="Fim do intervalo (requer --start)")
    p.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Rodar Playwright em modo headless (padrao: True; use --no-headless para debug visual)",
    )
    p.add_argument(
        "--debug-only",
        action="store_true",
        default=False,
        help="So salva HTML/PNG de debug sem tentar baixar CSV ou gravar no DB",
    )
    args = p.parse_args()

    if args.start and not args.end:
        p.error("--start requer --end")
    if args.end and not args.start:
        p.error("--end requer --start")

    return args


def _DateRange(start: str, end: str) -> list[date]:
    """Retorna lista de dates de start ate end inclusive."""
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    if e < s:
        raise ValueError(f"--end ({end}) e anterior a --start ({start})")
    result = []
    cur = s
    while cur <= e:
        result.append(cur)
        cur += timedelta(days=1)
    return result


# ---------------------------------------------------------------------------
# Helpers de debug
# ---------------------------------------------------------------------------

def _EnsureDebugDir() -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)


async def _SaveDebug(frame_or_page, prefix: str, log) -> None:
    """Salva HTML e screenshot PNG de debug."""
    _EnsureDebugDir()
    try:
        html = await frame_or_page.content()
        html_path = DEBUG_DIR / f"{prefix}.html"
        html_path.write_text(html, encoding="utf-8")
        log.debug(f"Debug HTML salvo: {html_path}")
    except Exception as e:
        log.warning(f"Falha ao salvar HTML de debug ({prefix}): {e}")

    try:
        png_path = DEBUG_DIR / f"{prefix}.png"
        await frame_or_page.screenshot(path=str(png_path), full_page=True)
        log.debug(f"Debug PNG salvo: {png_path}")
    except Exception as e:
        log.warning(f"Falha ao salvar screenshot de debug ({prefix}): {e}")


# ---------------------------------------------------------------------------
# Parsing do CSV
# ---------------------------------------------------------------------------

def _ParseCsv(raw_text: str, log) -> list[dict]:
    """
    Parseia o CSV do boletim B3, aplica COLUMN_MAP, filtra por cdInstrumento
    e retorna lista de dicts com colunas internas.

    O CSV da B3 tem um preamble de linhas descritivas antes do header real.
    A funcao detecta automaticamente onde o header comeca buscando
    a coluna "Instrumento financeiro" (ou outra coluna do COLUMN_MAP).
    """
    # Detecta delimitador: B3 usa ";" no boletim de negocio-a-negocio
    sample = raw_text[:4096]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    log.debug(f"CSV delimiter detectado: '{delimiter}'")

    # Divide em linhas e localiza o header real
    lines = raw_text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        # O header real contem pelo menos uma coluna conhecida do COLUMN_MAP
        if any(col in line for col in COLUMN_MAP):
            header_idx = i
            log.info(f"Header CSV encontrado na linha {i+1}: {line[:120]}")
            break

    if header_idx is None:
        # Nenhum header encontrado — loga primeiras linhas para debug
        log.error(f"Header CSV nao encontrado. Primeiras 10 linhas:\n" +
                  "\n".join(lines[:10]))
        return []

    # Reconstroi o texto CSV a partir do header
    csv_from_header = "\n".join(lines[header_idx:])

    reader = csv.DictReader(io.StringIO(csv_from_header), delimiter=delimiter)

    # Descobre as colunas presentes e logga para debug
    fieldnames = reader.fieldnames or []
    log.info(f"Colunas no CSV: {fieldnames}")

    # Verifica se ha colunas sem match no COLUMN_MAP
    unmapped = [f for f in fieldnames if f.strip() not in COLUMN_MAP]
    if unmapped:
        log.warning(f"Colunas no CSV sem mapeamento (ignoradas): {unmapped}")

    rows_out = []
    rows_skipped = 0
    rows_wrong_instrument = 0

    for i, row in enumerate(reader, start=2):  # linha 1 = header
        # Aplica mapeamento com strip nas chaves
        mapped: dict = {}
        for csv_col, internal_col in COLUMN_MAP.items():
            val = row.get(csv_col) or row.get(csv_col.strip())
            if val is not None:
                mapped[internal_col] = val.strip() if isinstance(val, str) else val

        # Verifica colunas obrigatorias
        missing = REQUIRED_COLS - mapped.keys()
        if missing:
            # Linha pode ser rodape vazio — so loga em DEBUG
            log.debug(f"Linha {i}: colunas obrigatorias ausentes {missing} — pulando")
            rows_skipped += 1
            continue

        # Filtra por instrumento
        instrumento = mapped.get("cdInstrumento", "").upper().strip()
        if instrumento not in [x.upper() for x in INSTRUMENTOS]:
            rows_wrong_instrument += 1
            continue

        # Converte vrQuantidade
        try:
            mapped["vrQuantidade"] = int(
                str(mapped["vrQuantidade"]).replace(".", "").replace(",", "")
            )
        except (ValueError, KeyError):
            log.warning(f"Linha {i}: vrQuantidade invalido '{mapped.get('vrQuantidade')}' — pulando")
            rows_skipped += 1
            continue

        # Converte vrPU e vrVolume (formato BR: 1.234.567,89)
        skip_row = False
        for col in ("vrPU", "vrVolume"):
            try:
                val_str = str(mapped[col]).replace(".", "").replace(",", ".")
                mapped[col] = float(val_str)
            except (ValueError, KeyError):
                log.warning(f"Linha {i}: {col} invalido '{mapped.get(col)}' — pulando")
                rows_skipped += 1
                skip_row = True
                break
        if skip_row:
            continue

        # vrTaxaNegocio e opcional (pode ser vazio/-)
        taxa_raw = str(mapped.get("vrTaxaNegocio", "")).strip()
        if taxa_raw in ("", "-", "N/A", "n/a", "0"):
            mapped["vrTaxaNegocio"] = None
        else:
            try:
                taxa_str = taxa_raw.replace(".", "").replace(",", ".")
                mapped["vrTaxaNegocio"] = float(taxa_str)
            except ValueError:
                mapped["vrTaxaNegocio"] = None

        # cdISIN e opcional
        if "cdISIN" not in mapped or not mapped["cdISIN"]:
            mapped["cdISIN"] = None

        # Normaliza datas para ISO-8601 YYYY-MM-DD
        for col in ("dtNegocio", "dtLiquidacao"):
            if col in mapped:
                mapped[col] = _NormalizeDate(mapped[col], log)

        # Normaliza horario para HH:MM:SS
        if "dtHorarioNegocio" in mapped:
            mapped["dtHorarioNegocio"] = _NormalizeTime(mapped["dtHorarioNegocio"])

        rows_out.append(mapped)

    log.info(
        f"CSV parseado: {len(rows_out)} trades DEB/CRI/CRA, "
        f"{rows_wrong_instrument} outros instrumentos ignorados, "
        f"{rows_skipped} linhas com erro puladas"
    )
    return rows_out


def _NormalizeDate(val: str, log) -> str:
    """Converte DD/MM/YYYY ou YYYY-MM-DD para YYYY-MM-DD."""
    val = val.strip()
    if len(val) == 10 and val[2] == "/":
        try:
            d, m, y = val.split("/")
            return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
        except Exception:
            pass
    return val  # assume ja esta em YYYY-MM-DD


def _NormalizeTime(val: str) -> str:
    """Garante HH:MM:SS."""
    val = val.strip()
    if len(val) == 5 and val[2] == ":":
        return val + ":00"
    return val


# ---------------------------------------------------------------------------
# UPSERT no banco
# ---------------------------------------------------------------------------

def _UpsertRows(conn, rows: list[dict], log) -> tuple[int, int]:
    """Faz UPSERT de todos os rows em NegociosBrutos. Retorna (inseridos, atualizados)."""
    if not rows:
        return 0, 0

    ids = [r["cdIdentificadorNegocio"] for r in rows]
    existing_ids: set[str] = set()
    chunk_size = 500
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT cdIdentificadorNegocio FROM NegociosBrutos WHERE cdIdentificadorNegocio IN ({placeholders})",
            chunk,
        ):
            existing_ids.add(row[0])

    params = [
        (
            r["cdIdentificadorNegocio"],
            r["cdInstrumento"],
            r["cdEmissor"],
            r["cdTicker"],
            r["vrQuantidade"],
            r["vrPU"],
            r["vrVolume"],
            r.get("vrTaxaNegocio"),
            r["dtHorarioNegocio"],
            r["dtNegocio"],
            r.get("cdISIN"),
            r["dtLiquidacao"],
            r["cdSituacao"],
        )
        for r in rows
    ]

    conn.executemany(UPSERT_SQL, params)
    conn.commit()

    inserted = sum(1 for r in rows if r["cdIdentificadorNegocio"] not in existing_ids)
    updated  = sum(1 for r in rows if r["cdIdentificadorNegocio"] in existing_ids)
    log.info(f"UPSERT: {inserted} inseridos, {updated} atualizados")
    return inserted, updated


def _SoftCancelMissing(conn, date_str: str, downloaded_ids: set, log) -> int:
    """
    Trades que estavam em NegociosBrutos para date_str mas não aparecem em downloaded_ids
    são marcados cdSituacao='Cancelado' (soft delete).
    Os correspondentes em NegociosProcessados são deletados (hard delete).
    Retorna count de trades cancelados.
    """
    cursor = conn.execute(
        "SELECT idTrade, cdIdentificadorNegocio FROM NegociosBrutos "
        "WHERE dtNegocio = ? AND cdSituacao != 'Cancelado'",
        (date_str,),
    )
    to_cancel = [(row[0], row[1]) for row in cursor if row[1] not in downloaded_ids]

    if not to_cancel:
        return 0

    cancel_trade_ids  = [r[0] for r in to_cancel]
    cancel_negoc_ids  = [r[1] for r in to_cancel]
    ph = ",".join("?" * len(cancel_trade_ids))

    conn.execute(f"DELETE FROM NegociosProcessados WHERE idTrade IN ({ph})", cancel_trade_ids)
    conn.execute(
        f"UPDATE NegociosBrutos SET cdSituacao = 'Cancelado', dtAtualizacao = CURRENT_TIMESTAMP "
        f"WHERE cdIdentificadorNegocio IN ({ph})",
        cancel_negoc_ids,
    )
    conn.commit()

    log.warning(
        f"Soft-cancel {date_str}: {len(to_cancel)} trade(s) marcados Cancelado e removidos de "
        f"NegociosProcessados — reprocessar calc_taxa → filtrar_trades para essa data. "
        f"IDs: {cancel_negoc_ids}"
    )
    return len(to_cancel)


# ---------------------------------------------------------------------------
# Download via POST /bdi/table/export/csv (endpoint real descoberto por inspecao)
# ---------------------------------------------------------------------------

async def _DownloadCsvViaPost(
    page: Page,
    target_date: date,
    export_post_body: dict | None,
    log,
) -> str | None:
    """
    Tenta baixar o CSV via POST para o endpoint real do BDI:
      POST https://arquivos.b3.com.br/bdi/table/export/csv?lang=pt-BR

    O body e descoberto por interceptacao durante o click no botao CSV.
    Se export_post_body for None, usa bodies candidatos conhecidos.
    Retorna o texto CSV ou None.
    """
    date_str = target_date.strftime("%Y-%m-%d")
    export_url = "https://arquivos.b3.com.br/bdi/table/export/csv?lang=pt-BR"

    # Bodies candidatos para o POST de export.
    # Body real descoberto por inspecao em 2026-05-30:
    #   {"Name":"Trade","Date":"2026-05-29","FinalDate":"2026-05-29","ClientId":"","Filters":{}}
    # O body e enviado como JSON (Content-Type: application/json).
    candidate_bodies = []
    if export_post_body:
        # Sobrescreve Date/FinalDate com a data correta — o body capturado pode ter D-1
        corrected = {**export_post_body, "Date": date_str, "FinalDate": date_str}
        candidate_bodies.append(corrected)

    # Bodies padrao baseados no formato real da API BDI
    candidate_bodies.extend([
        {"Name": "Trade", "Date": date_str, "FinalDate": date_str, "ClientId": "", "Filters": {}},
        {"Name": "Trade", "Date": date_str, "FinalDate": date_str, "ClientId": ""},
        {"Name": "Trade@true", "Date": date_str, "FinalDate": date_str, "ClientId": "", "Filters": {}},
    ])

    for body in candidate_bodies:
        try:
            log.info(f"Tentando POST export/csv com body: {body}")
            # O endpoint espera JSON (Content-Type: application/json)
            resp = await page.request.post(
                export_url,
                data=body,          # Playwright serializa dict como JSON automaticamente
                headers={"Content-Type": "application/json"},
                timeout=30_000,
            )
            log.info(f"  Status: {resp.status}, Content-Type: {resp.headers.get('content-type', 'N/A')}")

            if resp.status == 200:
                content_type = resp.headers.get("content-type", "")
                if "text/html" in content_type.lower():
                    body_text = await resp.text()
                    log.warning(f"  Retornou HTML (nao e CSV): {body_text[:100]}")
                    continue

                body_bytes = await resp.body()
                log.info(f"  Tamanho: {len(body_bytes)} bytes")

                if len(body_bytes) < 50:
                    log.warning(f"  Conteudo muito pequeno — pulando")
                    continue

                csv_text = None
                for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
                    try:
                        csv_text = body_bytes.decode(enc, errors="strict")
                        log.info(f"  Decodificado com encoding: {enc}")
                        break
                    except UnicodeDecodeError:
                        continue
                if csv_text is None:
                    csv_text = body_bytes.decode("latin-1", errors="replace")

                return csv_text

            else:
                body_text = await resp.text()
                log.warning(f"  Status {resp.status}: {body_text[:200]}")

        except Exception as e:
            log.warning(f"  Erro no POST: {e}")

    return None


# ---------------------------------------------------------------------------
# Scraping via Playwright (interacao com a pagina)
# ---------------------------------------------------------------------------

async def _ScrapeDate(
    page: Page,
    context: BrowserContext,
    target_date: date,
    conn,
    log,
    debug_only: bool = False,
) -> tuple[int, int, int]:
    """
    Tenta baixar o CSV do boletim B3 para target_date.
    Retorna (inseridos, atualizados, cancelados).
    """
    date_str   = target_date.strftime("%Y-%m-%d")
    date_br    = target_date.strftime("%d/%m/%Y")
    log.info(f"=== Processando data: {date_str} ===")

    # Captura o body do POST de export CSV (interceptado nas requests)
    export_post_body: dict | None = None
    intercepted_csv_content: list[bytes] = []

    # Intercepta responses que parecem ser CSV (fallback)
    async def _handle_response(response):
        url = response.url
        content_type = response.headers.get("content-type", "")
        if (
            "arquivos.b3" in url
            and response.status == 200
            and ("csv" in content_type.lower() or "octet" in content_type.lower())
        ):
            try:
                body = await response.body()
                if len(body) > 100:
                    log.info(f"CSV interceptado via response: {url} ({len(body)} bytes)")
                    intercepted_csv_content.append(body)
            except Exception:
                pass

    # Intercepta TODAS as requests de rede para logging e captura de body do export
    all_net_requests: list[str] = []

    async def _handle_request(req):
        nonlocal export_post_body
        url = req.url
        if "arquivos.b3" in url:
            log.info(f"NET REQ: {req.method} {url}")
            all_net_requests.append(f"{req.method} {url}")
            # Captura o body do POST de export para reusar depois
            if "export/csv" in url and req.method == "POST":
                try:
                    body = req.post_data
                    if body:
                        log.info(f"POST export/csv body capturado: {body[:200]}")
                        # Tenta parsear como JSON ou form data
                        import json as _json
                        try:
                            export_post_body = _json.loads(body)
                        except Exception:
                            # Pode ser form-urlencoded
                            from urllib.parse import parse_qs
                            parsed = parse_qs(body)
                            export_post_body = {k: v[0] for k, v in parsed.items()}
                        log.info(f"POST body parseado: {export_post_body}")
                except Exception as e:
                    log.debug(f"Erro ao capturar body do POST export: {e}")

    page.on("request",  lambda req: asyncio.ensure_future(_handle_request(req)))
    page.on("response", lambda res: asyncio.ensure_future(_handle_response(res)))

    # ------- PASSO 1: Navega diretamente para o iframe do BDI -------
    # (mais confiavel que tentar atraves do outer page da B3)
    log.info(f"Navegando para iframe: {IFRAME_URL}")
    try:
        await page.goto(IFRAME_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(3000)
    except Exception as e:
        log.error(f"Erro ao navegar para o iframe: {e}")
        raise

    await _SaveDebug(page, f"01_bdi_inicial_{date_str}", log)
    log.info("Passo 1 concluido: iframe BDI carregado.")

    if debug_only:
        log.info("--debug-only ativo: encerrando apos passo 1.")
        return 0, 0, 0

    # ------- PASSO 2: Clica na aba "Renda fixa" -------
    log.info("Clicando na aba 'Renda fixa'...")
    try:
        # Tenta pelo texto exato
        renda_fixa_tab = page.get_by_text("Renda fixa", exact=True)
        count = await renda_fixa_tab.count()
        if count > 0:
            await renda_fixa_tab.first.click()
            log.info("Clicado em 'Renda fixa' via get_by_text")
        else:
            # Fallback: busca pelo aria-label ou wai-aria
            rf_link = await page.query_selector("a[wai-aria*='Renda fixa']")
            if rf_link:
                await rf_link.click()
                log.info("Clicado em 'Renda fixa' via wai-aria")
            else:
                log.warning("Link 'Renda fixa' nao encontrado — continuando mesmo assim")
    except Exception as e:
        log.warning(f"Erro ao clicar em 'Renda fixa': {e}")

    await page.wait_for_timeout(2000)
    await _SaveDebug(page, f"02_apos_renda_fixa_{date_str}", log)
    log.info("Passo 2 concluido: aba Renda fixa selecionada.")

    # ------- PASSO 3: Seta a data no duet-date-picker -------
    log.info(f"Setando data {date_br} no duet-date-picker...")

    date_set_ok = False

    # Estrategia 1: API JS do duet-date-picker (setValue)
    try:
        js_result = await page.evaluate(f"""
            () => {{
                // Tenta duet-date-picker API (componente Web Component)
                const pickers = document.querySelectorAll('duet-date-picker');
                // Usa o primeiro picker visivel (nao hidden)
                for (const dp of pickers) {{
                    const parent = dp.closest('[hidden]');
                    if (!parent) {{
                        if (dp.setValue) {{
                            dp.setValue('{date_str}');
                            // Dispara evento de change para a pagina reagir
                            dp.dispatchEvent(new CustomEvent('duetChange', {{
                                bubbles: true,
                                detail: {{ value: '{date_str}', valueAsDate: new Date('{date_str}') }}
                            }}));
                            return 'duet-api-setValue-ok';
                        }}
                    }}
                }}

                // Fallback: seta o input hidden diretamente
                const hiddenInputs = document.querySelectorAll('input[type="hidden"][name="date"]');
                for (const inp of hiddenInputs) {{
                    const parent = inp.closest('[hidden]');
                    if (!parent) {{
                        inp.value = '{date_str}';
                        inp.dispatchEvent(new Event('input',  {{ bubbles: true }}));
                        inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        return 'hidden-input-set-ok';
                    }}
                }}

                return 'no-picker-found';
            }}
        """)
        log.info(f"JS setValue result: {js_result}")
        if js_result in ("duet-api-setValue-ok", "hidden-input-set-ok"):
            date_set_ok = True
            await page.wait_for_timeout(2000)
    except Exception as e:
        log.warning(f"Erro ao setar data via JS: {e}")

    # Estrategia 2: Digitar no input visivel
    if not date_set_ok:
        try:
            # Localiza o input visivel do duet-date que nao esta em container hidden
            visible_input = await page.evaluate_handle("""
                () => {
                    const inputs = document.querySelectorAll('input.duet-date__input');
                    for (const inp of inputs) {
                        const parent = inp.closest('[hidden]');
                        if (!parent) return inp;
                    }
                    return null;
                }
            """)
            if visible_input:
                log.info(f"Input duet-date visivel encontrado — tentando fill + Enter")
                await visible_input.as_element().triple_click()
                await page.wait_for_timeout(100)
                await visible_input.as_element().type(date_br, delay=50)
                await page.wait_for_timeout(200)
                await visible_input.as_element().press("Enter")
                await page.wait_for_timeout(2000)
                date_set_ok = True
                log.info(f"Data digitada via type: {date_br}")
        except Exception as e:
            log.warning(f"Falha ao digitar no input duet-date: {e}")

    if not date_set_ok:
        log.warning("Nao foi possivel setar a data — continuando com data atual do picker")

    await _SaveDebug(page, f"03_apos_data_{date_str}", log)
    log.info("Passo 3 concluido: data setada no picker.")

    # ------- PASSO 4: Seleciona tabela "Negocio a negocio" -------
    log.info("Selecionando tabela 'Negocio a negocio' (Trade@true)...")
    try:
        # O select pode ter id "selectTabelas" ou similar
        select_el = await page.query_selector("select#selectTabelas")
        if not select_el:
            # Tenta outros selects na pagina
            selects = await page.query_selector_all("select")
            log.info(f"Selects encontrados na pagina: {len(selects)}")
            for sel in selects:
                sel_id = await sel.get_attribute("id") or ""
                sel_name = await sel.get_attribute("name") or ""
                log.info(f"  select id='{sel_id}' name='{sel_name}'")

            # Procura pelo select que tem opcao "Trade" ou "Negocio a negocio"
            for sel in selects:
                options_text = await sel.evaluate("el => Array.from(el.options).map(o => o.text + '|' + o.value).join(',')")
                if "Trade" in options_text or "negocio" in options_text.lower():
                    select_el = sel
                    log.info(f"Select com opcao Trade encontrado: {options_text[:200]}")
                    break

        if select_el:
            # Lista opcoes disponíveis para log
            options = await select_el.evaluate(
                "el => Array.from(el.options).map(o => ({text: o.text, value: o.value}))"
            )
            log.info(f"Opcoes do select de tabelas: {options}")

            # Tenta selecionar "Trade@true" primeiro, depois variações
            for val_to_try in ("Trade@true", "Trade", "trade", "TRADE"):
                try:
                    await select_el.select_option(value=val_to_try)
                    log.info(f"Tabela selecionada com value='{val_to_try}'")
                    break
                except Exception:
                    # Tenta pelo texto
                    try:
                        await select_el.select_option(label="Negócio a negócio")
                        log.info("Tabela selecionada pelo label 'Negocio a negocio'")
                        break
                    except Exception:
                        continue
        else:
            log.warning("Select de tabelas nao encontrado — continuando")
    except Exception as e:
        log.warning(f"Erro ao selecionar tabela: {e}")

    await page.wait_for_timeout(1500)
    await _SaveDebug(page, f"04_apos_select_tabela_{date_str}", log)
    log.info("Passo 4 concluido: tabela selecionada.")

    # ------- PASSO 5: Clica no botao CSV e captura download (estrategia primaria) -------
    log.info("Tentando click no botao CSV para capturar download...")
    csv_content = await _ClickCsvButton(page, target_date, log)

    # ------- PASSO 6: Fallback — tenta POST direto no endpoint export/csv -------
    if csv_content is None:
        log.info("Click CSV falhou — tentando POST direto no endpoint export/csv...")
        csv_content = await _DownloadCsvViaPost(page, target_date, export_post_body, log)

    # ------- PASSO 7: Fallback 2 — usa CSV interceptado nas responses -------
    if csv_content is None and intercepted_csv_content:
        log.info(f"Usando CSV interceptado nas responses ({len(intercepted_csv_content)} capturados)...")
        body_bytes = intercepted_csv_content[-1]  # Ultimo capturado
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                csv_content = body_bytes.decode(enc)
                log.info(f"CSV interceptado decodificado com {enc}")
                break
            except UnicodeDecodeError:
                continue

    # ------- PASSO 8: Loga requests para debug -------
    if all_net_requests:
        req_path = DEBUG_DIR / f"network_requests_{date_str}.txt"
        req_path.write_text("\n".join(all_net_requests), encoding="utf-8")
        log.info(f"Requests de rede salvas em: {req_path}")

    await _SaveDebug(page, f"05_apos_download_{date_str}", log)

    # ------- PASSO 9: Parseia e grava no banco -------
    if csv_content is None:
        log.error(
            f"NAO foi possivel obter CSV para {date_str}. "
            f"Verifique data/debug/ para analise da estrutura."
        )
        return 0, 0, 0

    rows = _ParseCsv(csv_content, log)
    if not rows:
        log.warning(f"Nenhum trade DEB/CRI/CRA encontrado para {date_str}")
        return 0, 0, 0

    ins, upd = _UpsertRows(conn, rows, log)
    downloaded_ids = {r["cdIdentificadorNegocio"] for r in rows}
    cancelled = _SoftCancelMissing(conn, date_str, downloaded_ids, log)
    return ins, upd, cancelled


async def _ClickCsvButton(page: Page, target_date: date, log) -> str | None:
    """
    Tenta clicar no botao CSV da pagina e capturar o download.
    Tenta varios seletores em sequencia.
    """
    date_str = target_date.strftime("%Y-%m-%d")

    # Loga todos os botoes/links da pagina para debug
    try:
        elements = await page.eval_on_selector_all(
            "a, button",
            """els => els.slice(0, 30).map(el => ({
                tag: el.tagName,
                id: el.id || '',
                text: (el.innerText || '').trim().substring(0, 60),
                class: (el.className || '').substring(0, 80),
                href: el.href || ''
            }))"""
        )
        log.info(f"Botoes/links na pagina ({len(elements)} total, mostrando ate 30):")
        for el in elements:
            log.info(f"  {el}")
    except Exception as e:
        log.warning(f"Erro ao listar elementos: {e}")

    # Seletores para o botao de download CSV
    csv_selectors = [
        ("button:has(span.b3__ico--csv)",  "button com span.b3__ico--csv"),
        ("button.b3__ico--csv",            "button.b3__ico--csv"),
        ("[class*='ico--csv']",            "qualquer com ico--csv"),
        ("button:has-text('CSV')",         "button CSV"),
        ("a:has-text('CSV')",              "link CSV"),
        ("button:has-text('Exportar')",    "button Exportar"),
        ("[id*='export']",                 "id~export"),
        ("[class*='export']",              "class~export"),
        ("a[href*='.csv']",                "href .csv"),
        ("a[download]",                    "download attr"),
    ]

    for selector, desc in csv_selectors:
        try:
            el = await page.query_selector(selector)
            if el:
                log.info(f"Elemento CSV encontrado: {desc} ({selector})")
                try:
                    async with page.expect_download(timeout=20_000) as dl_info:
                        await el.click()
                        log.info(f"Clicado: {desc}")
                    dl: Download = await dl_info.value
                    log.info(f"Download: {dl.suggested_filename}")

                    body_bytes = await dl.read()
                    for enc in ("utf-8-sig", "latin-1", "utf-8", "cp1252"):
                        try:
                            text = body_bytes.decode(enc, errors="strict")
                            log.info(f"CSV decodificado com encoding: {enc}")
                            return text
                        except UnicodeDecodeError:
                            continue
                    return body_bytes.decode("latin-1", errors="replace")

                except Exception as e:
                    log.warning(f"Clique em '{desc}' nao gerou download: {e}")
                    await page.wait_for_timeout(500)
        except Exception as e:
            log.warning(f"Erro ao tentar selector '{selector}': {e}")

    return None


# ---------------------------------------------------------------------------
# Main async
# ---------------------------------------------------------------------------

async def _MainAsync(args: argparse.Namespace) -> str:
    log = get_logger("scrape_b3_boletim")

    if args.date:
        dates = [date.fromisoformat(args.date)]
    else:
        dates = _DateRange(args.start, args.end)

    log.info(f"Datas a processar: {[str(d) for d in dates]}")
    log.info(f"headless={args.headless}, debug_only={args.debug_only}")
    log.info(f"DEBUG_DIR: {DEBUG_DIR.resolve()}")

    _EnsureDebugDir()
    conn = get_db()
    total_inserted   = 0
    total_updated    = 0
    total_cancelled  = 0
    results: list[str] = []

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=args.headless,
                slow_mo=200,
                proxy=get_playwright_proxy(),  # None no PC pessoal; proxy da conta no banco
            )

            # Loop data por data. (Antes havia um atalho de POST-range único para
            # --start/--end, mas a API BDI da B3 só retornava as duas PONTAS do
            # intervalo — não os pregões do meio — e ainda com status 200, então o
            # fallback nunca disparava e dias sumiam silenciosamente. Removido: cada
            # pregão é baixado individualmente, idempotente via UPSERT.)
            for target_date in dates:
                date_str = str(target_date)
                context = await browser.new_context(
                    accept_downloads=True,
                    viewport={"width": 1400, "height": 900},
                )
                page = await context.new_page()

                try:
                    ins, upd, cnl = await _ScrapeDate(
                        page, context, target_date, conn, log, args.debug_only
                    )
                    total_inserted  += ins
                    total_updated   += upd
                    total_cancelled += cnl
                    results.append(f"{date_str}: {ins} inseridos, {upd} atualizados, {cnl} cancelados")
                    log.info(f"{date_str}: {ins} inseridos, {upd} atualizados, {cnl} cancelados")
                except Exception as e:
                    log.error(f"{date_str}: ERRO — {e}")
                    log.debug(traceback.format_exc())
                    results.append(f"{date_str}: ERRO — {e}")
                    try:
                        await _SaveDebug(page, f"ERRO_{date_str}", log)
                    except Exception:
                        pass
                finally:
                    await context.close()

            await browser.close()

    finally:
        conn.close()

    summary = (
        f"Operações inseridas:   {total_inserted}\n"
        f"Operações atualizadas: {total_updated}\n"
        f"Operações canceladas:  {total_cancelled}\n\n"
        f"Datas processadas:\n" + "\n".join(f"  {r}" for r in results)
    )
    log.info(summary)
    return summary


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Main() -> str:
    args = _ParseArgs()
    return asyncio.run(_MainAsync(args))


if __name__ == "__main__":
    _summary, _ok, _tb = "", True, None
    _log = get_logger("scrape_b3_boletim")
    try:
        _summary = Main()
    except Exception:
        _ok = False
        _tb = traceback.format_exc()
        print(_tb, file=sys.stderr)
        raise
    finally:
        send_completion_email("scrape_b3_boletim", _ok, _summary or "", _tb, logger=_log)
