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
from datetime import date, datetime, timedelta
from pathlib import Path

# Garante que code/ esteja no sys.path ao rodar como script
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Helpers"))

from playwright.async_api import async_playwright, Download, Page, BrowserContext

import pandas as pd

import dados as D
from config import cfg, ObterProxyPlaywright
from logger import ObterLogger
from email_outlook import EnviarEmailConclusao
from relatorio_execucao import RelatorioExecucao

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
BASE_URL     = cfg["scrape"]["b3"]["baseUrl"]
IFRAME_URL   = "https://arquivos.b3.com.br/bdi/tabelas?lang=pt-BR"
INSTRUMENTOS: list[str] = cfg["scrape"]["b3"]["instrumentosAceitos"]

DEBUG_DIR = Path(cfg["paths"]["dadosDir"]) / "debug"

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

COLS_NEGOCIO = ("cdIdentificadorNegocio", "cdInstrumento", "cdEmissor", "cdTicker",
                "vrQuantidade", "vrPU", "vrVolume", "vrTaxaNegocio",
                "dtHorarioNegocio", "dtNegocio", "cdISIN", "dtLiquidacao", "cdSituacao")

# O `ON CONFLICT(cdIdentificadorNegocio) DO UPDATE` daqui reescrevia so tres colunas:
# vrTaxaNegocio, cdSituacao e dtAtualizacao. Um negocio ja gravado nao muda de ticker,
# de PU nem de volume — o que a B3 revisa depois e a taxa e a situacao.
#
# Como Mesclar aplica a politica a toda coluna PRESENTE no DataFrame, o lote e partido em
# dois, como no calc_taxa_negocios: linha nova entra inteira, linha existente leva so as
# tres do DO UPDATE.
COLS_ATUALIZAVEIS = ("vrTaxaNegocio", "cdSituacao", "dtAtualizacao")
POLITICA_UPSERT = {c: D.SOBRESCREVER for c in COLS_ATUALIZAVEIS}

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def LerArgumentos() -> argparse.Namespace:
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
        dest="debugOnly",
        help="So salva HTML/PNG de debug sem tentar baixar CSV ou gravar no DB",
    )
    args = p.parse_args()

    if args.start and not args.end:
        p.error("--start requer --end")
    if args.end and not args.start:
        p.error("--end requer --start")

    return args


def MontarIntervaloDatas(start: str, end: str) -> list[date]:
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

def GarantirDirDebug() -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)


async def SalvarDebug(frameOuPagina, prefix: str, log) -> None:
    """Salva HTML e screenshot PNG de debug."""
    GarantirDirDebug()
    try:
        html = await frameOuPagina.content()
        caminhoHtml = DEBUG_DIR / f"{prefix}.html"
        caminhoHtml.write_text(html, encoding="utf-8")
        log.debug(f"Debug HTML salvo: {caminhoHtml}")
    except Exception as e:
        log.warning(f"Falha ao salvar HTML de debug ({prefix}): {e}")

    try:
        caminhoPng = DEBUG_DIR / f"{prefix}.png"
        await frameOuPagina.screenshot(path=str(caminhoPng), full_page=True)
        log.debug(f"Debug PNG salvo: {caminhoPng}")
    except Exception as e:
        log.warning(f"Falha ao salvar screenshot de debug ({prefix}): {e}")


# ---------------------------------------------------------------------------
# Parsing do CSV
# ---------------------------------------------------------------------------

def AnalisarCsv(textoBruto: str, log) -> list[dict]:
    """
    Parseia o CSV do boletim B3, aplica COLUMN_MAP, filtra por cdInstrumento
    e retorna lista de dicts com colunas internas.

    O CSV da B3 tem um preamble de linhas descritivas antes do header real.
    A funcao detecta automaticamente onde o header comeca buscando
    a coluna "Instrumento financeiro" (ou outra coluna do COLUMN_MAP).
    """
    # Detecta delimitador: B3 usa ";" no boletim de negocio-a-negocio
    sample = textoBruto[:4096]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    log.debug(f"CSV delimiter detectado: '{delimiter}'")

    # Divide em linhas e localiza o header real
    lines = textoBruto.splitlines()
    idxHeader = None
    for i, line in enumerate(lines):
        # O header real contem pelo menos uma coluna conhecida do COLUMN_MAP
        if any(col in line for col in COLUMN_MAP):
            idxHeader = i
            log.info(f"Header CSV encontrado na linha {i+1}: {line[:120]}")
            break

    if idxHeader is None:
        # Nenhum header encontrado — loga primeiras linhas para debug
        log.error(f"Header CSV nao encontrado. Primeiras 10 linhas:\n" +
                  "\n".join(lines[:10]))
        return []

    # Reconstroi o texto CSV a partir do header
    csvDoHeader = "\n".join(lines[idxHeader:])

    reader = csv.DictReader(io.StringIO(csvDoHeader), delimiter=delimiter)

    # Descobre as colunas presentes e logga para debug
    fieldnames = reader.fieldnames or []
    log.info(f"Colunas no CSV: {fieldnames}")

    # Verifica se ha colunas sem match no COLUMN_MAP
    unmapped = [f for f in fieldnames if f.strip() not in COLUMN_MAP]
    if unmapped:
        log.warning(f"Colunas no CSV sem mapeamento (ignoradas): {unmapped}")

    linhasSaida = []
    linhasPuladas = 0
    linhasInstrumentoErrado = 0

    for i, row in enumerate(reader, start=2):  # linha 1 = header
        # Aplica mapeamento com strip nas chaves
        mapped: dict = {}
        for colCsv, colInterna in COLUMN_MAP.items():
            val = row.get(colCsv) or row.get(colCsv.strip())
            if val is not None:
                mapped[colInterna] = val.strip() if isinstance(val, str) else val

        # Verifica colunas obrigatorias
        missing = REQUIRED_COLS - mapped.keys()
        if missing:
            # Linha pode ser rodape vazio — so loga em DEBUG
            log.debug(f"Linha {i}: colunas obrigatorias ausentes {missing} — pulando")
            linhasPuladas += 1
            continue

        # Filtra por instrumento
        instrumento = mapped.get("cdInstrumento", "").upper().strip()
        if instrumento not in [x.upper() for x in INSTRUMENTOS]:
            linhasInstrumentoErrado += 1
            continue

        # Converte vrQuantidade
        try:
            mapped["vrQuantidade"] = int(
                str(mapped["vrQuantidade"]).replace(".", "").replace(",", "")
            )
        except (ValueError, KeyError):
            log.warning(f"Linha {i}: vrQuantidade invalido '{mapped.get('vrQuantidade')}' — pulando")
            linhasPuladas += 1
            continue

        # Converte vrPU e vrVolume (formato BR: 1.234.567,89)
        pularLinha = False
        for col in ("vrPU", "vrVolume"):
            try:
                valStr = str(mapped[col]).replace(".", "").replace(",", ".")
                mapped[col] = float(valStr)
            except (ValueError, KeyError):
                log.warning(f"Linha {i}: {col} invalido '{mapped.get(col)}' — pulando")
                linhasPuladas += 1
                pularLinha = True
                break
        if pularLinha:
            continue

        # vrTaxaNegocio e opcional (pode ser vazio/-)
        taxaBruta = str(mapped.get("vrTaxaNegocio", "")).strip()
        if taxaBruta in ("", "-", "N/A", "n/a", "0"):
            mapped["vrTaxaNegocio"] = None
        else:
            try:
                taxaStr = taxaBruta.replace(".", "").replace(",", ".")
                mapped["vrTaxaNegocio"] = float(taxaStr)
            except ValueError:
                mapped["vrTaxaNegocio"] = None

        # cdISIN e opcional
        if "cdISIN" not in mapped or not mapped["cdISIN"]:
            mapped["cdISIN"] = None

        # Normaliza datas para ISO-8601 YYYY-MM-DD
        for col in ("dtNegocio", "dtLiquidacao"):
            if col in mapped:
                mapped[col] = NormalizarData(mapped[col], log)

        # Normaliza horario para HH:MM:SS
        if "dtHorarioNegocio" in mapped:
            mapped["dtHorarioNegocio"] = NormalizarHora(mapped["dtHorarioNegocio"])

        linhasSaida.append(mapped)

    log.info(
        f"CSV parseado: {len(linhasSaida)} trades DEB/CRI/CRA, "
        f"{linhasInstrumentoErrado} outros instrumentos ignorados, "
        f"{linhasPuladas} linhas com erro puladas"
    )
    return linhasSaida


def NormalizarData(val: str, log) -> str:
    """Converte DD/MM/YYYY ou YYYY-MM-DD para YYYY-MM-DD."""
    val = val.strip()
    if len(val) == 10 and val[2] == "/":
        try:
            d, m, y = val.split("/")
            return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
        except Exception:
            pass
    return val  # assume ja esta em YYYY-MM-DD


def NormalizarHora(val: str) -> str:
    """Garante HH:MM:SS."""
    val = val.strip()
    if len(val) == 5 and val[2] == ":":
        return val + ":00"
    return val


# ---------------------------------------------------------------------------
# UPSERT no banco
# ---------------------------------------------------------------------------

def UpsertLinhas(rows: list[dict], log) -> tuple[int, int]:
    """Faz UPSERT de todos os rows em NegociosBrutos. Retorna (inseridos, atualizados)."""
    if not rows:
        return 0, 0

    agora = datetime.now().isoformat(sep=" ", timespec="seconds")
    inserted = updated = 0

    # NegociosBrutos e particionada por dtNegocio, e cada Mesclar toca UM dia. Na pratica
    # o CSV de um pregao so traz aquele dtNegocio, mas agrupar aqui torna isso explicito
    # em vez de suposto.
    porDia: dict[str, list[dict]] = {}
    for r in rows:
        porDia.setdefault(r["dtNegocio"], []).append(r)

    for dtNegocio, doDia in porDia.items():
        marcas = ",".join("?" * len(doDia))
        idsExistentes = {r[0] for r in D.Tuplas(
            f"SELECT cdIdentificadorNegocio FROM NegociosBrutos "
            f"WHERE dtNegocio = ? AND cdIdentificadorNegocio IN ({marcas})",
            [dtNegocio] + [r["cdIdentificadorNegocio"] for r in doDia])}

        novos = [{**{c: r.get(c) for c in COLS_NEGOCIO},
                  "dtCriacao": agora, "dtAtualizacao": agora}
                 for r in doDia if r["cdIdentificadorNegocio"] not in idsExistentes]
        atualizados = [{"cdIdentificadorNegocio": r["cdIdentificadorNegocio"],
                        "dtNegocio": r["dtNegocio"],
                        "vrTaxaNegocio": r.get("vrTaxaNegocio"),
                        "cdSituacao": r["cdSituacao"], "dtAtualizacao": agora}
                       for r in doDia if r["cdIdentificadorNegocio"] in idsExistentes]

        if novos:
            D.Mesclar("NegociosBrutos", pd.DataFrame(novos),
                      padrao=D.SOBRESCREVER, data=dtNegocio)
        if atualizados:
            D.Mesclar("NegociosBrutos", pd.DataFrame(atualizados),
                      politica=POLITICA_UPSERT, data=dtNegocio)

        # Conta IDENTIFICADORES, nao linhas do CSV. O CSV traz negocios cujo
        # "Cod. identificador do negocio" vem como "-" (a B3 nao o preenche em toda
        # operacao), e todos eles colapsam numa linha so — em 28/07/2026 foram 25 linhas
        # virando uma. Contar linhas dizia "25 inseridos" onde entrou 1. Ver o aviso de
        # SemIdentificador.
        inserted += len({r["cdIdentificadorNegocio"] for r in novos})
        updated  += len({r["cdIdentificadorNegocio"] for r in atualizados})

    log.info(f"UPSERT: {inserted} inseridos, {updated} atualizados")
    return inserted, updated


def SemIdentificador(rows: list[dict]) -> int:
    """Quantos negocios do CSV vieram sem `cdIdentificadorNegocio` (a B3 manda "-").

    Importa porque esse campo e a CHAVE da base desde que o idTrade AUTOINCREMENT do
    SQLite foi aposentado: todos os "-" de um pregao colapsam numa unica linha, e os
    demais somem. Nao e regressao do Parquet — o SQLite tinha UNIQUE nessa coluna e
    fazia o mesmo —, mas so agora ficou visivel. Ver [[98 - Backlog]]."""
    return sum(1 for r in rows if not (r.get("cdIdentificadorNegocio") or "").strip("-").strip())


def SoftCancelAusentes(dataStr: str, idsBaixados: set, log) -> int:
    """
    Trades que estavam em NegociosBrutos para date_str mas não aparecem em downloaded_ids
    são marcados cdSituacao='Cancelado' (soft delete).
    Os correspondentes em NegociosProcessados são deletados (hard delete).
    Retorna count de trades cancelados.
    """
    # A chave e uma so desde a migracao para Parquet (era idTrade + identificador).
    aCancelar = [r[0] for r in D.Tuplas(
        "SELECT cdIdentificadorNegocio FROM NegociosBrutos "
        "WHERE dtNegocio = ? AND cdSituacao != 'Cancelado'", (dataStr,))
        if r[0] not in idsBaixados]

    if not aCancelar:
        return 0

    idsNegocioCancelar = aCancelar
    agora = datetime.now().isoformat(sep=" ", timespec="seconds")

    D.Apagar("NegociosProcessados", "cdIdentificadorNegocio", aCancelar)
    D.Mesclar("NegociosBrutos", pd.DataFrame(
        [{"cdIdentificadorNegocio": i, "dtNegocio": dataStr,
          "cdSituacao": "Cancelado", "dtAtualizacao": agora} for i in aCancelar]),
        politica={"cdSituacao": D.SOBRESCREVER, "dtAtualizacao": D.SOBRESCREVER},
        data=dataStr)

    log.warning(
        f"Soft-cancel {dataStr}: {len(aCancelar)} trade(s) marcados Cancelado e removidos de "
        f"NegociosProcessados — reprocessar calc_taxa → filtrar_trades para essa data. "
        f"IDs: {idsNegocioCancelar}"
    )
    return len(aCancelar)


# ---------------------------------------------------------------------------
# Download via POST /bdi/table/export/csv (endpoint real descoberto por inspecao)
# ---------------------------------------------------------------------------

async def BaixarCsvViaPost(
    page: Page,
    dataAlvo: date,
    corpoPostExport: dict | None,
    log,
) -> str | None:
    """
    Tenta baixar o CSV via POST para o endpoint real do BDI:
      POST https://arquivos.b3.com.br/bdi/table/export/csv?lang=pt-BR

    O body e descoberto por interceptacao durante o click no botao CSV.
    Se export_post_body for None, usa bodies candidatos conhecidos.
    Retorna o texto CSV ou None.
    """
    dataStr = dataAlvo.strftime("%Y-%m-%d")
    urlExport = "https://arquivos.b3.com.br/bdi/table/export/csv?lang=pt-BR"

    # Bodies candidatos para o POST de export.
    # Body real descoberto por inspecao em 2026-05-30:
    #   {"Name":"Trade","Date":"2026-05-29","FinalDate":"2026-05-29","ClientId":"","Filters":{}}
    # O body e enviado como JSON (Content-Type: application/json).
    corposCandidatos = []
    if corpoPostExport:
        # Sobrescreve Date/FinalDate com a data correta — o body capturado pode ter D-1
        corrected = {**corpoPostExport, "Date": dataStr, "FinalDate": dataStr}
        corposCandidatos.append(corrected)

    # Bodies padrao baseados no formato real da API BDI
    corposCandidatos.extend([
        {"Name": "Trade", "Date": dataStr, "FinalDate": dataStr, "ClientId": "", "Filters": {}},
        {"Name": "Trade", "Date": dataStr, "FinalDate": dataStr, "ClientId": ""},
        {"Name": "Trade@true", "Date": dataStr, "FinalDate": dataStr, "ClientId": "", "Filters": {}},
    ])

    for body in corposCandidatos:
        try:
            log.info(f"Tentando POST export/csv com body: {body}")
            # O endpoint espera JSON (Content-Type: application/json)
            resp = await page.request.post(
                urlExport,
                data=body,          # Playwright serializa dict como JSON automaticamente
                headers={"Content-Type": "application/json"},
                timeout=30_000,
            )
            log.info(f"  Status: {resp.status}, Content-Type: {resp.headers.get('content-type', 'N/A')}")

            if resp.status == 200:
                contentType = resp.headers.get("content-type", "")
                if "text/html" in contentType.lower():
                    corpoTexto = await resp.text()
                    log.warning(f"  Retornou HTML (nao e CSV): {corpoTexto[:100]}")
                    continue

                corpoBytes = await resp.body()
                log.info(f"  Tamanho: {len(corpoBytes)} bytes")

                if len(corpoBytes) < 50:
                    log.warning(f"  Conteudo muito pequeno — pulando")
                    continue

                textoCsv = None
                for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
                    try:
                        textoCsv = corpoBytes.decode(enc, errors="strict")
                        log.info(f"  Decodificado com encoding: {enc}")
                        break
                    except UnicodeDecodeError:
                        continue
                if textoCsv is None:
                    textoCsv = corpoBytes.decode("latin-1", errors="replace")

                return textoCsv

            else:
                corpoTexto = await resp.text()
                log.warning(f"  Status {resp.status}: {corpoTexto[:200]}")

        except Exception as e:
            log.warning(f"  Erro no POST: {e}")

    return None


# ---------------------------------------------------------------------------
# Scraping via Playwright (interacao com a pagina)
# ---------------------------------------------------------------------------

async def RasparData(
    page: Page,
    context: BrowserContext,
    dataAlvo: date,
    log,
    debugOnly: bool = False,
) -> tuple[int, int, int]:
    """
    Tenta baixar o CSV do boletim B3 para target_date.
    Retorna (inseridos, atualizados, cancelados).
    """
    dataStr   = dataAlvo.strftime("%Y-%m-%d")
    dataBr    = dataAlvo.strftime("%d/%m/%Y")
    log.info(f"=== Processando data: {dataStr} ===")

    # Captura o body do POST de export CSV (interceptado nas requests)
    corpoPostExport: dict | None = None
    conteudoCsvInterceptado: list[bytes] = []

    # Intercepta responses que parecem ser CSV (fallback)
    async def AoResponder(response):
        url = response.url
        contentType = response.headers.get("content-type", "")
        if (
            "arquivos.b3" in url
            and response.status == 200
            and ("csv" in contentType.lower() or "octet" in contentType.lower())
        ):
            try:
                body = await response.body()
                if len(body) > 100:
                    log.info(f"CSV interceptado via response: {url} ({len(body)} bytes)")
                    conteudoCsvInterceptado.append(body)
            except Exception:
                pass

    # Intercepta TODAS as requests de rede para logging e captura de body do export
    todasRequisicoes: list[str] = []

    async def AoRequisitar(req):
        nonlocal corpoPostExport
        url = req.url
        if "arquivos.b3" in url:
            log.info(f"NET REQ: {req.method} {url}")
            todasRequisicoes.append(f"{req.method} {url}")
            # Captura o body do POST de export para reusar depois
            if "export/csv" in url and req.method == "POST":
                try:
                    body = req.post_data
                    if body:
                        log.info(f"POST export/csv body capturado: {body[:200]}")
                        # Tenta parsear como JSON ou form data
                        import json as jsonMod
                        try:
                            corpoPostExport = jsonMod.loads(body)
                        except Exception:
                            # Pode ser form-urlencoded
                            from urllib.parse import parse_qs
                            parsed = parse_qs(body)
                            corpoPostExport = {k: v[0] for k, v in parsed.items()}
                        log.info(f"POST body parseado: {corpoPostExport}")
                except Exception as e:
                    log.debug(f"Erro ao capturar body do POST export: {e}")

    page.on("request",  lambda req: asyncio.ensure_future(AoRequisitar(req)))
    page.on("response", lambda res: asyncio.ensure_future(AoResponder(res)))

    # ------- PASSO 1: Navega diretamente para o iframe do BDI -------
    # (mais confiavel que tentar atraves do outer page da B3)
    log.info(f"Navegando para iframe: {IFRAME_URL}")
    try:
        await page.goto(IFRAME_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(3000)
    except Exception as e:
        log.error(f"Erro ao navegar para o iframe: {e}")
        raise

    await SalvarDebug(page, f"01_bdi_inicial_{dataStr}", log)
    log.info("Passo 1 concluido: iframe BDI carregado.")

    if debugOnly:
        log.info("--debug-only ativo: encerrando apos passo 1.")
        return 0, 0, 0

    # ------- PASSO 2: Clica na aba "Renda fixa" -------
    log.info("Clicando na aba 'Renda fixa'...")
    try:
        # Tenta pelo texto exato
        abaRendaFixa = page.get_by_text("Renda fixa", exact=True)
        count = await abaRendaFixa.count()
        if count > 0:
            await abaRendaFixa.first.click()
            log.info("Clicado em 'Renda fixa' via get_by_text")
        else:
            # Fallback: busca pelo aria-label ou wai-aria
            linkRf = await page.query_selector("a[wai-aria*='Renda fixa']")
            if linkRf:
                await linkRf.click()
                log.info("Clicado em 'Renda fixa' via wai-aria")
            else:
                log.warning("Link 'Renda fixa' nao encontrado — continuando mesmo assim")
    except Exception as e:
        log.warning(f"Erro ao clicar em 'Renda fixa': {e}")

    await page.wait_for_timeout(2000)
    await SalvarDebug(page, f"02_apos_renda_fixa_{dataStr}", log)
    log.info("Passo 2 concluido: aba Renda fixa selecionada.")

    # ------- PASSO 3: Seta a data no duet-date-picker -------
    log.info(f"Setando data {dataBr} no duet-date-picker...")

    dataSetadaOk = False

    # Estrategia 1: API JS do duet-date-picker (setValue)
    try:
        resultadoJs = await page.evaluate(f"""
            () => {{
                // Tenta duet-date-picker API (componente Web Component)
                const pickers = document.querySelectorAll('duet-date-picker');
                // Usa o primeiro picker visivel (nao hidden)
                for (const dp of pickers) {{
                    const parent = dp.closest('[hidden]');
                    if (!parent) {{
                        if (dp.setValue) {{
                            dp.setValue('{dataStr}');
                            // Dispara evento de change para a pagina reagir
                            dp.dispatchEvent(new CustomEvent('duetChange', {{
                                bubbles: true,
                                detail: {{ value: '{dataStr}', valueAsDate: new Date('{dataStr}') }}
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
                        inp.value = '{dataStr}';
                        inp.dispatchEvent(new Event('input',  {{ bubbles: true }}));
                        inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        return 'hidden-input-set-ok';
                    }}
                }}

                return 'no-picker-found';
            }}
        """)
        log.info(f"JS setValue result: {resultadoJs}")
        if resultadoJs in ("duet-api-setValue-ok", "hidden-input-set-ok"):
            dataSetadaOk = True
            await page.wait_for_timeout(2000)
    except Exception as e:
        log.warning(f"Erro ao setar data via JS: {e}")

    # Estrategia 2: Digitar no input visivel
    if not dataSetadaOk:
        try:
            # Localiza o input visivel do duet-date que nao esta em container hidden
            inputVisivel = await page.evaluate_handle("""
                () => {
                    const inputs = document.querySelectorAll('input.duet-date__input');
                    for (const inp of inputs) {
                        const parent = inp.closest('[hidden]');
                        if (!parent) return inp;
                    }
                    return null;
                }
            """)
            if inputVisivel:
                log.info(f"Input duet-date visivel encontrado — tentando fill + Enter")
                await inputVisivel.as_element().triple_click()
                await page.wait_for_timeout(100)
                await inputVisivel.as_element().type(dataBr, delay=50)
                await page.wait_for_timeout(200)
                await inputVisivel.as_element().press("Enter")
                await page.wait_for_timeout(2000)
                dataSetadaOk = True
                log.info(f"Data digitada via type: {dataBr}")
        except Exception as e:
            log.warning(f"Falha ao digitar no input duet-date: {e}")

    if not dataSetadaOk:
        log.warning("Nao foi possivel setar a data — continuando com data atual do picker")

    await SalvarDebug(page, f"03_apos_data_{dataStr}", log)
    log.info("Passo 3 concluido: data setada no picker.")

    # ------- PASSO 4: Seleciona tabela "Negocio a negocio" -------
    log.info("Selecionando tabela 'Negocio a negocio' (Trade@true)...")
    try:
        # O select pode ter id "selectTabelas" ou similar
        elSelect = await page.query_selector("select#selectTabelas")
        if not elSelect:
            # Tenta outros selects na pagina
            selects = await page.query_selector_all("select")
            log.info(f"Selects encontrados na pagina: {len(selects)}")
            for sel in selects:
                selId = await sel.get_attribute("id") or ""
                selName = await sel.get_attribute("name") or ""
                log.info(f"  select id='{selId}' name='{selName}'")

            # Procura pelo select que tem opcao "Trade" ou "Negocio a negocio"
            for sel in selects:
                textoOpcoes = await sel.evaluate("el => Array.from(el.options).map(o => o.text + '|' + o.value).join(',')")
                if "Trade" in textoOpcoes or "negocio" in textoOpcoes.lower():
                    elSelect = sel
                    log.info(f"Select com opcao Trade encontrado: {textoOpcoes[:200]}")
                    break

        if elSelect:
            # Lista opcoes disponíveis para log
            options = await elSelect.evaluate(
                "el => Array.from(el.options).map(o => ({text: o.text, value: o.value}))"
            )
            log.info(f"Opcoes do select de tabelas: {options}")

            # Tenta selecionar "Trade@true" primeiro, depois variações
            for valTentar in ("Trade@true", "Trade", "trade", "TRADE"):
                try:
                    await elSelect.select_option(value=valTentar)
                    log.info(f"Tabela selecionada com value='{valTentar}'")
                    break
                except Exception:
                    # Tenta pelo texto
                    try:
                        await elSelect.select_option(label="Negócio a negócio")
                        log.info("Tabela selecionada pelo label 'Negocio a negocio'")
                        break
                    except Exception:
                        continue
        else:
            log.warning("Select de tabelas nao encontrado — continuando")
    except Exception as e:
        log.warning(f"Erro ao selecionar tabela: {e}")

    await page.wait_for_timeout(1500)
    await SalvarDebug(page, f"04_apos_select_tabela_{dataStr}", log)
    log.info("Passo 4 concluido: tabela selecionada.")

    # ------- PASSO 5: Clica no botao CSV e captura download (estrategia primaria) -------
    log.info("Tentando click no botao CSV para capturar download...")
    conteudoCsv = await ClicarBotaoCsv(page, dataAlvo, log)

    # ------- PASSO 6: Fallback — tenta POST direto no endpoint export/csv -------
    if conteudoCsv is None:
        log.info("Click CSV falhou — tentando POST direto no endpoint export/csv...")
        conteudoCsv = await BaixarCsvViaPost(page, dataAlvo, corpoPostExport, log)

    # ------- PASSO 7: Fallback 2 — usa CSV interceptado nas responses -------
    if conteudoCsv is None and conteudoCsvInterceptado:
        log.info(f"Usando CSV interceptado nas responses ({len(conteudoCsvInterceptado)} capturados)...")
        corpoBytes = conteudoCsvInterceptado[-1]  # Ultimo capturado
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                conteudoCsv = corpoBytes.decode(enc)
                log.info(f"CSV interceptado decodificado com {enc}")
                break
            except UnicodeDecodeError:
                continue

    # ------- PASSO 8: Loga requests para debug -------
    if todasRequisicoes:
        caminhoReq = DEBUG_DIR / f"network_requests_{dataStr}.txt"
        caminhoReq.write_text("\n".join(todasRequisicoes), encoding="utf-8")
        log.info(f"Requests de rede salvas em: {caminhoReq}")

    await SalvarDebug(page, f"05_apos_download_{dataStr}", log)

    # ------- PASSO 9: Parseia e grava no banco -------
    if conteudoCsv is None:
        log.error(
            f"NAO foi possivel obter CSV para {dataStr}. "
            f"Verifique data/debug/ para analise da estrutura."
        )
        return 0, 0, 0

    rows = AnalisarCsv(conteudoCsv, log)
    if not rows:
        log.warning(f"Nenhum trade DEB/CRI/CRA encontrado para {dataStr}")
        return 0, 0, 0

    semId = SemIdentificador(rows)
    if semId:
        log.warning(
            f"{dataStr}: {semId} negocio(s) vieram SEM identificador da B3 ('-'). "
            f"Como esse campo e a chave da base, todos colapsam numa linha so e os "
            f"demais nao entram. Ver [[98 - Backlog]]."
        )

    ins, upd = UpsertLinhas(rows, log)
    idsBaixados = {r["cdIdentificadorNegocio"] for r in rows}
    cancelled = SoftCancelAusentes(dataStr, idsBaixados, log)
    return ins, upd, cancelled


async def ClicarBotaoCsv(page: Page, dataAlvo: date, log) -> str | None:
    """
    Tenta clicar no botao CSV da pagina e capturar o download.
    Tenta varios seletores em sequencia.
    """
    dataStr = dataAlvo.strftime("%Y-%m-%d")

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
    seletoresCsv = [
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

    for selector, desc in seletoresCsv:
        try:
            el = await page.query_selector(selector)
            if el:
                log.info(f"Elemento CSV encontrado: {desc} ({selector})")
                try:
                    async with page.expect_download(timeout=20_000) as dlInfo:
                        await el.click()
                        log.info(f"Clicado: {desc}")
                    dl: Download = await dlInfo.value
                    log.info(f"Download: {dl.suggested_filename}")

                    corpoBytes = await dl.read()
                    for enc in ("utf-8-sig", "latin-1", "utf-8", "cp1252"):
                        try:
                            text = corpoBytes.decode(enc, errors="strict")
                            log.info(f"CSV decodificado com encoding: {enc}")
                            return text
                        except UnicodeDecodeError:
                            continue
                    return corpoBytes.decode("latin-1", errors="replace")

                except Exception as e:
                    log.warning(f"Clique em '{desc}' nao gerou download: {e}")
                    await page.wait_for_timeout(500)
        except Exception as e:
            log.warning(f"Erro ao tentar selector '{selector}': {e}")

    return None


# ---------------------------------------------------------------------------
# Main async
# ---------------------------------------------------------------------------

async def PrincipalAsync(args: argparse.Namespace) -> RelatorioExecucao:
    log = ObterLogger("scrape_b3_boletim")

    if args.date:
        dates = [date.fromisoformat(args.date)]
    else:
        dates = MontarIntervaloDatas(args.start, args.end)

    log.info(f"Datas a processar: {[str(d) for d in dates]}")
    log.info(f"headless={args.headless}, debug_only={args.debugOnly}")
    log.info(f"DEBUG_DIR: {DEBUG_DIR.resolve()}")

    GarantirDirDebug()
    totalInseridos   = 0
    totalAtualizados    = 0
    totalCancelados  = 0
    results: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=args.headless,
            slow_mo=200,
            proxy=ObterProxyPlaywright(),  # None no PC pessoal; proxy da conta no banco
        )

        # Loop data por data. (Antes havia um atalho de POST-range único para
        # --start/--end, mas a API BDI da B3 só retornava as duas PONTAS do
        # intervalo — não os pregões do meio — e ainda com status 200, então o
        # fallback nunca disparava e dias sumiam silenciosamente. Removido: cada
        # pregão é baixado individualmente, idempotente via UPSERT.)
        for dataAlvo in dates:
            dataStr = str(dataAlvo)
            context = await browser.new_context(
                accept_downloads=True,
                viewport={"width": 1400, "height": 900},
                ignore_https_errors=True,
            )
            page = await context.new_page()

            try:
                ins, upd, cnl = await RasparData(
                    page, context, dataAlvo, log, args.debugOnly
                )
                totalInseridos  += ins
                totalAtualizados   += upd
                totalCancelados += cnl
                results.append([dataStr, ins, upd, cnl])
                log.info(f"{dataStr}: {ins} inseridos, {upd} atualizados, {cnl} cancelados")
            except Exception as e:
                log.error(f"{dataStr}: ERRO — {e}")
                log.debug(traceback.format_exc())
                results.append([dataStr, "ERRO", str(e)[:60], ""])
                try:
                    await SalvarDebug(page, f"ERRO_{dataStr}", log)
                except Exception:
                    pass
            finally:
                await context.close()

        await browser.close()

    rel = RelatorioExecucao("scrape_b3_boletim", args=vars(args))
    rel.Datas([r[0] for r in results])
    rel.Contar("inseridos", totalInseridos)
    rel.Contar("atualizados", totalAtualizados)
    rel.Contar("cancelados", totalCancelados)
    rel.Secao("Resultado por pregão", ["data", "inseridos", "atualizados", "cancelados"], results)

    # Pregão que não trouxe negócio nenhum quase sempre é falha silenciosa (a B3 sempre
    # tem negócio em dia útil) — foi assim que o bug do proxy no Playwright passou batido.
    semDado = [r[0] for r in results if r[1] == 0 and r[2] == 0]
    if semDado:
        rel.Aviso(f"{len(semDado)} pregão(ões) sem nenhum negócio: {', '.join(semDado[:6])}. "
                  f"Em dia útil isso quase sempre é falha de download, não ausência de dado.")

    log.info(rel.Texto())
    return rel


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Principal() -> RelatorioExecucao:
    args = LerArgumentos()
    return asyncio.run(PrincipalAsync(args))


if __name__ == "__main__":
    log = ObterLogger("scrape_b3_boletim")
    rel, ok, tb = RelatorioExecucao("scrape_b3_boletim"), True, None
    try:
        rel = Principal()
    except Exception:
        ok = False
        tb = traceback.format_exc()
        rel.Erro("A rodada abortou — ver traceback.")
        print(tb, file=sys.stderr)
        raise
    finally:
        EnviarEmailConclusao("scrape_b3_boletim", ok, rel, tb, logger=log)
