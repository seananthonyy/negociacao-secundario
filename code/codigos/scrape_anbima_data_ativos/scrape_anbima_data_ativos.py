"""Scrape Anbima Data: características e agenda de Debs/CRIs/CRAs."""

import argparse, asyncio, json, re, sqlite3, sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Helpers"))

from playwright.async_api import async_playwright

from config import cfg, ObterProxyPlaywright
from db import ObterBanco, SincronizarFluxoAtivos
from email_outlook import EnviarEmailConclusao
from relatorio_execucao import RelatorioExecucao
from logger import ObterLogger

# ── constantes ────────────────────────────────────────────────────────────────

# Usados no filtro da listagem (strings exatas da API de listagem)
INDEXADORES_VALIDOS_LISTING = {'DI+', 'IPCA', 'DI%', 'Pré-Fixado', 'Pre-Fixado'}

# Usados no filtro por-ticker (indexador.nome retorna tipo base, ex: "DI", "IPCA", "PRE")
# Lógica: bloquear inválidos conhecidos; aceitar todo o resto
INDEXADORES_INVALIDOS_TICKER = {'IGPM', 'TR', 'INPC', 'DÓLAR', 'DOLAR', 'USD'}

TIPO_API = {
    'DEB': 'debentures',
    'CRI': 'certificado-recebiveis',
    'CRA': 'certificado-recebiveis',
}
TIPO_URL_SITE = {
    'DEB': 'debentures',
    'CRI': 'certificado-de-recebiveis',
    'CRA': 'certificado-de-recebiveis',
}
LISTING_UI = {
    'DEB': 'https://data.anbima.com.br/busca/debentures',
    'CR':  'https://data.anbima.com.br/busca/certificado-de-recebiveis',
}

SIZE_LISTING = 5000
SIZE_AGENDA  = 100   # max aceito pela API de agenda (size maior e capado em 100)
WAIT_MS      = 3000
MAX_LISTING_PAGES = 30   # safety cap
MAX_AGENDA_PAGES  = 50   # safety cap (50 x 100 = 5000 eventos)


# ── CLI ───────────────────────────────────────────────────────────────────────

def LerArgumentos():
    ap = argparse.ArgumentParser(description='Scrape Anbima Data — características e agenda')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--mode', choices=['full'], help='Carga inicial: lista tickers do site')
    g.add_argument('--date', metavar='YYYY-MM-DD', help='Incremental: tickers de NegociosBrutos nessa data')
    g.add_argument('--start', metavar='YYYY-MM-DD', help='Incremental: data inicial (com --end)')
    g.add_argument('--ticker', help='Debug: um ticker específico')
    ap.add_argument('--end', metavar='YYYY-MM-DD')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--force', action='store_true', help='Re-scrappa mesmo com JSON em cache')
    ap.add_argument('--skip-scrape', action='store_true', dest='skipScrape',
                    help='Pula scraping; re-insere JSONs existentes no DB')
    ap.add_argument('--limit', type=int, metavar='N', help='Para após N tickers (teste)')
    return ap.parse_args()


# ── helpers ───────────────────────────────────────────────────────────────────

def Agora() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

def ParaFloat(v) -> float | None:
    if v is None or str(v).strip() in ('-', ''):
        return None
    try:
        return float(str(v).replace(',', '.'))
    except Exception:
        return None

def InferirTipo(ticker: str, tipoTitulo: str | None = None) -> str:
    # cdInstrumento autoritativo (NegociosBrutos/InfoAtivos) tem prioridade.
    if tipoTitulo in ('DEB', 'CRI', 'CRA'):
        return tipoTitulo
    # Convencao B3 (confirmado no banco 30/06): CRA comeca com 'CRA' (len 11);
    # CRI e codigo numerico (len 10, comeca com digito); DEB comeca com letra (len 6).
    t = ticker.upper()
    if t.startswith('CRA'):
        return 'CRA'
    if t[:1].isdigit():
        return 'CRI'
    return 'DEB'


# ── indexador normalização ────────────────────────────────────────────────────

def NormalizarIndexador(info: dict) -> str | None:
    """Normaliza para CDI+, %CDI, IPCA ou PREFIXADO usando info['remuneracao']."""
    texto = (info.get('remuneracao') or '').strip().upper()
    if not texto:
        nome = ((info.get('indexador') or {}).get('nome') or '').strip().upper()
        texto = nome
    if not texto:
        return None
    if 'IPCA' in texto:
        return 'IPCA'
    if 'PRÉ' in texto or 'PRE' in texto or 'PREFIXADO' in texto:
        return 'PREFIXADO'
    if 'DI' in texto:
        if re.search(r'DI\s*\+', texto):
            return 'CDI+'
        if re.search(r'%\s*DI|\d.*DI', texto):
            return '%CDI'
        return 'CDI+'
    return None


# ── agenda processing ─────────────────────────────────────────────────────────

def EventoConhecido(evento: str) -> bool:
    n = evento.upper()
    return ('PAGAMENTO DE JUROS' in n or 'AMORTIZA' in n
            or 'VENCIMENTO' in n or 'RESGATE' in n or 'INCORPORA' in n)

def ProcessarAgenda(ticker: str, agenda: list, log) -> list | None:
    """Retorna rows para FluxoAtivos, ou None se evento desconhecido encontrado."""
    for item in agenda:
        evt = item.get('evento', '')
        if not EventoConhecido(evt):
            log.warning(f'{ticker}: evento desconhecido "{evt}" — FluxoAtivos descartado')
            return None

    porData = defaultdict(list)
    for item in agenda:
        porData[item['data_liquidacao']].append(item)

    now = Agora()
    rows = []
    for dtLiq, events in sorted(porData.items()):
        vrPctAmortizacao = None
        vrPctIncorporacao = None

        for e in events:
            n = e['evento'].upper()
            if 'AMORTIZA' in n or 'VENCIMENTO' in n or 'RESGATE' in n:
                vrPctAmortizacao = ParaFloat(e.get('taxa'))
                break

        incorp = [e for e in events if 'INCORPORA' in e['evento'].upper()]
        juros  = [e for e in events if 'PAGAMENTO DE JUROS' in e['evento'].upper()]
        if incorp:
            if not juros:
                vrPctIncorporacao = 100.0
            else:
                vi = [ParaFloat(e.get('valor')) for e in incorp]
                vj = [ParaFloat(e.get('valor')) for e in juros]
                if None in vi or None in vj:
                    vrPctIncorporacao = None
                else:
                    total = sum(vi) + sum(vj)
                    vrPctIncorporacao = (sum(vi) / total * 100) if total > 0 else None

        rows.append({
            'cdTicker': ticker, 'dtEvento': dtLiq,
            'vrPctAmortizacao': vrPctAmortizacao,
            'vrPctIncorporacao': vrPctIncorporacao,
            'dtAtualizacao': now,
        })
    return rows


# ── db ────────────────────────────────────────────────────────────────────────

def UpsertInfoAtivos(conn: sqlite3.Connection, ticker: str, cdInstrumento: str, info: dict):
    emissao = info.get('emissao') or {}
    cdEmissor = (emissao.get('emissor') or {}).get('nome') if cdInstrumento == 'DEB' else info.get('devedor')

    conn.execute("""
        INSERT INTO InfoAtivos
            (cdTicker, cdInstrumento, cdEmissor, dtVencimento, cdIndexador,
             vrTaxaEmissao, vrVNE, dtInicioRentabilidade,
             cdISIN, vrQuantidadeEmissao, dtEmissao, cdFonteCadastro, dtAtualizacao)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,'AnbimaData',?)
        ON CONFLICT(cdTicker) DO UPDATE SET
            -- COALESCE em tudo: a Anbima preenche buraco, nao sobrescreve. Num ativo de
            -- fonte B3, vrVNE/dtInicioRentabilidade ja vieram de la e ficam. A agenda e
            -- protegida em db.SincronizarFluxoAtivos.
            cdFonteCadastro      = COALESCE(cdFonteCadastro,      'AnbimaData'),
            cdInstrumento        = COALESCE(cdInstrumento,        excluded.cdInstrumento),
            cdEmissor            = COALESCE(cdEmissor,            excluded.cdEmissor),
            dtVencimento         = COALESCE(dtVencimento,         excluded.dtVencimento),
            cdIndexador          = COALESCE(cdIndexador,          excluded.cdIndexador),
            vrTaxaEmissao       = COALESCE(vrTaxaEmissao,       excluded.vrTaxaEmissao),
            vrVNE                = COALESCE(vrVNE,                excluded.vrVNE),
            dtInicioRentabilidade = COALESCE(dtInicioRentabilidade, excluded.dtInicioRentabilidade),
            cdISIN               = COALESCE(cdISIN,               excluded.cdISIN),
            vrQuantidadeEmissao  = COALESCE(vrQuantidadeEmissao,  excluded.vrQuantidadeEmissao),
            dtEmissao            = COALESCE(dtEmissao,            excluded.dtEmissao),
            dtAtualizacao          = excluded.dtAtualizacao
    """, (
        ticker, cdInstrumento, cdEmissor,
        info.get('data_vencimento'),
        NormalizarIndexador(info),
        ParaFloat(info.get('taxa_emissao')),
        ParaFloat(info.get('vne')),
        info.get('data_inicio_rentabilidade'),
        info.get('isin'),
        float(info['quantidade_emitida']) if info.get('quantidade_emitida') is not None else None,
        emissao.get('data_emissao'),
        Agora(),
    ))

# A escrita do fluxo passa por db.SincronizarFluxoAtivos: ele so grava se a
# agenda mudou de verdade e, nesse caso, invalida a validacao do fluxo do ativo
# (contrato com a calculadora). Re-scrape que devolve a mesma agenda nao escreve.

# Colunas de InfoAtivos que precisam estar preenchidas para o ticker ser
# considerado "completo" (não precisa de re-scrape no modo incremental).
INFO_REQUIRED_COLS = [
    'cdInstrumento', 'cdEmissor', 'dtVencimento', 'cdIndexador',
    'vrTaxaEmissao', 'vrVNE', 'dtInicioRentabilidade',
    'cdISIN', 'vrQuantidadeEmissao', 'dtEmissao',
]

# Colunas reportadas no email como "faltantes" quando NULL após scrape.
INFO_FALTANTE_COLS = [
    'cdEmissor', 'dtVencimento', 'cdIndexador', 'vrTaxaEmissao', 'vrVNE',
    'dtInicioRentabilidade', 'cdISIN', 'vrQuantidadeEmissao', 'dtEmissao',
]


def InfoEhNula(v) -> bool:
    """NULL para fins de reporte: None, string vazia ou '-'."""
    return v is None or str(v).strip() in ('', '-')


def CalcularInfoFaltante(info: dict, cdInstrumento: str) -> list[str]:
    """Colunas de INFO_FALTANTE_COLS que ficaram NULL no dado capturado.

    Usa a MESMA lógica de extração de `_UpsertInfoAtivos`."""
    emissao = info.get('emissao') or {}
    cdEmissor = ((emissao.get('emissor') or {}).get('nome')
                 if cdInstrumento == 'DEB' else info.get('devedor'))
    valores = {
        'cdEmissor':            cdEmissor,
        'dtVencimento':         info.get('data_vencimento'),
        'cdIndexador':          NormalizarIndexador(info),
        'vrTaxaEmissao':       ParaFloat(info.get('taxa_emissao')),
        'vrVNE':                ParaFloat(info.get('vne')),
        'dtInicioRentabilidade': info.get('data_inicio_rentabilidade'),
        'cdISIN':               info.get('isin'),
        'vrQuantidadeEmissao':  info.get('quantidade_emitida'),
        'dtEmissao':            emissao.get('data_emissao'),
    }
    return [c for c in INFO_FALTANTE_COLS if InfoEhNula(valores[c])]


def CarregarSkipTickers() -> set[str]:
    """Tickers a pular, de data/anbima_skip_tickers.csv.

    Um ticker por linha; linhas em branco e iniciadas por '#' são ignoradas;
    texto após a primeira vírgula (motivo) é descartado. Arquivo opcional —
    se não existir, retorna set vazio."""
    skipPath = Path(cfg['paths']['dadosDir']) / 'anbima_skip_tickers.csv'
    skip: set[str] = set()
    if not skipPath.exists():
        return skip
    for line in skipPath.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        tk = line.split(',')[0].strip().upper()
        if tk:
            skip.add(tk)
    return skip

SQL_VAR_CHUNK = 500  # limite seguro abaixo do teto de ~999 variáveis do SQLite


def TickersDeNegociosBrutos(conn: sqlite3.Connection, dates: list[str]) -> list[tuple[str, str | None]]:
    """Tickers com trade (não cancelado) nas datas. cdInstrumento cru (pode ser NULL)."""
    ph = ','.join('?' * len(dates))
    rows = conn.execute(f"""
        SELECT DISTINCT cdTicker, cdInstrumento FROM NegociosBrutos
        WHERE dtNegocio IN ({ph}) AND cdSituacao != 'Cancelado'
    """, dates).fetchall()
    return [(r['cdTicker'], r['cdInstrumento']) for r in rows]


def TickersDaAnbima(conn: sqlite3.Connection, dates: list[str]) -> list[tuple[str, str | None]]:
    """Tickers com taxa Anbima divulgada nas datas (AnbimaIndicativos.dtReferencia).

    AnbimaIndicativos não tem coluna de instrumento — derivamos via LEFT JOIN
    com InfoAtivos (cdInstrumento cru, pode ser NULL)."""
    ph = ','.join('?' * len(dates))
    rows = conn.execute(f"""
        SELECT DISTINCT a.cdTicker, i.cdInstrumento
        FROM AnbimaIndicativos a
        LEFT JOIN InfoAtivos i ON i.cdTicker = a.cdTicker
        WHERE a.dtReferencia IN ({ph})
    """, dates).fetchall()
    return [(r['cdTicker'], r['cdInstrumento']) for r in rows]


def TickersCompletos(conn: sqlite3.Connection, tickers: list[str]) -> set[str]:
    """Set de tickers já completos: existem em InfoAtivos com todas as colunas
    de INFO_REQUIRED_COLS não-nulas E têm pelo menos uma linha em FluxoAtivos.

    Uma query por chunk de SQL_VAR_CHUNK tickers (evita o teto de variáveis)."""
    if not tickers:
        return set()
    naoNulo = ' AND '.join(f'i.{c} IS NOT NULL' for c in INFO_REQUIRED_COLS)
    completos: set[str] = set()
    for i in range(0, len(tickers), SQL_VAR_CHUNK):
        part = tickers[i:i + SQL_VAR_CHUNK]
        ph = ','.join('?' * len(part))
        rows = conn.execute(f"""
            SELECT i.cdTicker FROM InfoAtivos i
            WHERE i.cdTicker IN ({ph})
              AND {naoNulo}
              AND EXISTS (SELECT 1 FROM FluxoAtivos f WHERE f.cdTicker = i.cdTicker)
        """, part).fetchall()
        completos.update(r['cdTicker'] for r in rows)
    return completos


# skip_reasons TERMINAIS: o ticker ja foi resolvido o quanto a Anbima permite e nao deve
# voltar a fila. 'indexador_invalido' (descartado do DB) e 'sem_agenda' (cadastro gravado,
# mas a fonte nao tem fluxo — ficaria eternamente "incompleto" por falta de FluxoAtivos).
SKIP_REASONS_TERMINAIS = frozenset({'indexador_invalido', 'sem_agenda'})


def TickersSkipTerminal(dirJson: Path, tickers: list[str]) -> set[str]:
    """Set de tickers cujo checkpoint JSON marca um skip_reason TERMINAL
    (SKIP_REASONS_TERMINAIS). Guarda contra re-scrape infinito: sem isto eles sempre
    apareceriam como 'faltando' (sem FluxoAtivos / descartados do DB)."""
    terminais: set[str] = set()
    for tk in tickers:
        jp = dirJson / f'{tk}.json'
        if not jp.exists():
            continue
        try:
            payload = json.loads(jp.read_text(encoding='utf-8'))
        except Exception:
            continue
        if SKIP_REASONS_TERMINAIS & set(payload.get('skip_reasons') or []):
            terminais.add(tk)
    return terminais


# ── playwright: listagem ──────────────────────────────────────────────────────

async def PasseListagem(browser, apiKey: str, uiBase: str, order: str, log) -> tuple[dict, int]:
    """Um passe de listagem com a ordem dada. Retorna (collected, total_elements)."""
    ctx  = await browser.new_context(ignore_https_errors=True)
    page = await ctx.new_page()

    collected: dict[str, dict] = {}
    state = {'total': 0}

    async def RotaListagem(route):
        url = re.sub(r'size=\d+', f'size={SIZE_LISTING}', route.request.url)
        await route.continue_(url=url)

    await page.route(re.compile(rf'web-bff/v1/{apiKey}\?'), RotaListagem)

    async def AoResponder(resp):
        if f'web-bff/v1/{apiKey}?' in resp.url and 'view=caracteristicas' in resp.url:
            try:
                d = await resp.json()
                state['total'] = d.get('total_elements', state['total'])
                for item in d.get('content', []):
                    code = item.get('codigo_b3')
                    if code and code not in collected:
                        collected[code] = item
            except Exception:
                pass

    page.on('response', AoResponder)

    for apiPage in range(MAX_LISTING_PAGES):
        prev = len(collected)
        try:
            await page.goto(
                f'{uiBase}?page={apiPage}&field=codigo_b3&order={order}',
                wait_until='domcontentloaded', timeout=30000
            )
            await page.wait_for_timeout(WAIT_MS)
        except Exception as e:
            log.warning(f'Listagem pag {apiPage} ({order}): {e}')
            break

        if state['total'] and len(collected) >= state['total']:
            break
        if len(collected) == prev and apiPage > 0:
            break

    await ctx.close()
    return collected, state['total']


async def TickersListagemComFiltro(browser, tipoGrupo: str, log) -> list[tuple[str, str]]:
    """Captura listagem completa. Para DEB: dois passes (asc+desc) para superar cap ~3000/sessão."""
    apiKey = 'debentures' if tipoGrupo == 'DEB' else 'certificado-recebiveis'
    uiBase = LISTING_UI[tipoGrupo]

    collected: dict[str, dict] = {}
    totalElementos = 0

    orders = ['asc', 'desc'] if tipoGrupo == 'DEB' else ['asc']
    for order in orders:
        colPasse, totalPasse = await PasseListagem(browser, apiKey, uiBase, order, log)
        totalElementos = totalPasse or totalElementos
        prev = len(collected)
        collected.update(colPasse)
        log.info(f'Listagem {tipoGrupo} ({order}): {len(colPasse)}/{totalPasse} — acumulado {len(collected)}/{totalElementos}')

    result = []
    descartados = 0
    for code, item in collected.items():
        idx = item.get('indexador', '')
        if idx not in INDEXADORES_VALIDOS_LISTING:
            descartados += 1
            continue
        result.append((code, InferirTipo(code, item.get('tipo_titulo'))))

    log.info(f'Listagem {tipoGrupo}: {len(result)} válidos, {descartados} descartados por indexador')
    return result


# ── playwright: por ticker ────────────────────────────────────────────────────

async def RasparCaracteristicas(page, ticker: str, cdInstrumento: str, log) -> dict | None:
    api  = TIPO_API[cdInstrumento]
    site = TIPO_URL_SITE[cdInstrumento]
    captured = {}

    async def AoResponder(resp):
        url = resp.url
        if f'web-bff/v1/{api}/{ticker}' in url and 'agenda' not in url:
            try:
                d = await resp.json()
                if isinstance(d, dict) and d.get('codigo_b3'):
                    captured.update(d)
            except Exception:
                pass

    page.on('response', AoResponder)
    try:
        await page.goto(
            f'https://data.anbima.com.br/{site}/{ticker}/caracteristicas',
            wait_until='domcontentloaded', timeout=30000
        )
        await page.wait_for_timeout(WAIT_MS)
    except Exception as e:
        log.warning(f'{ticker}: características timeout: {e}')
    finally:
        page.remove_listener('response', AoResponder)

    return captured if captured.get('codigo_b3') else None


async def RasparAgenda(page, ticker: str, cdInstrumento: str, log) -> list | None:
    api  = TIPO_API[cdInstrumento]
    site = TIPO_URL_SITE[cdInstrumento]
    items: list = []
    totalElementos = 0
    seen: set = set()

    async def RotaAgenda(route):
        # Apenas eleva o size ao maximo aceito (100); NAO forcar page (precisamos paginar).
        url = re.sub(r'size=\d+', f'size={SIZE_AGENDA}', route.request.url)
        await route.continue_(url=url)

    async def AoResponder(resp):
        nonlocal totalElementos
        if f'web-bff/v1/{api}/{ticker}/agenda' in resp.url:
            try:
                d = await resp.json()
                totalElementos = d.get('total_elements', totalElementos)
                for item in d.get('content', []):
                    key = (item['data_base'], item['evento'])
                    if key not in seen:
                        seen.add(key)
                        items.append(item)
            except Exception:
                pass

    pattern = re.compile(rf'web-bff/v1/{api}/{re.escape(ticker)}/agenda')
    await page.route(pattern, RotaAgenda)
    page.on('response', AoResponder)
    try:
        # A API de agenda pagina (size capado em 100). Navega ?page=0,1,2... ate
        # juntar total_elements; para por progresso (2 paginas seguidas sem novidade).
        maxPg = MAX_AGENDA_PAGES
        pg = 0
        stale = 0
        while pg < maxPg:
            before = len(items)
            await page.goto(
                f'https://data.anbima.com.br/{site}/{ticker}/agenda?page={pg}',
                wait_until='domcontentloaded', timeout=30000
            )
            await page.wait_for_timeout(WAIT_MS)
            if totalElementos:
                needed = (totalElementos + SIZE_AGENDA - 1) // SIZE_AGENDA
                maxPg = min(MAX_AGENDA_PAGES, needed + 1)
                if len(items) >= totalElementos:
                    break
            stale = stale + 1 if len(items) == before else 0
            if stale >= 2:   # 2 navegacoes seguidas sem nada novo -> para
                break
            pg += 1
    except Exception as e:
        log.warning(f'{ticker}: agenda timeout: {e}')
    finally:
        page.remove_listener('response', AoResponder)
        await page.unroute(pattern)

    if totalElementos and len(items) < totalElementos:
        log.warning(f'{ticker}: agenda incompleta {len(items)}/{totalElementos}')

    return items if items else None


# ── worker ────────────────────────────────────────────────────────────────────

async def Trabalhador(wid: int, queue: asyncio.Queue, browser, dirJson: Path,
                  conn: sqlite3.Connection, lock: asyncio.Lock, stats: dict, args, log):
    ctx  = await browser.new_context(ignore_https_errors=True)
    page = await ctx.new_page()

    while True:
        try:
            ticker, cdInstrumento = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        caminhoJson = dirJson / f'{ticker}.json'
        try:
            motivosSkip: list[str] = []
            linhasFluxo = None
            info = None
            agenda = None

            # ── 1. obter dados (cache ou scrape) ─────────────────────────────
            if args.skipScrape or (caminhoJson.exists() and not args.force):
                if not caminhoJson.exists():
                    log.warning(f'{ticker}: JSON não encontrado (--skip-scrape)')
                    stats['erros'] += 1
                    continue
                payload       = json.loads(caminhoJson.read_text(encoding='utf-8'))
                info          = payload['info']
                agenda        = payload.get('agenda')
                motivosSkip  = payload.get('skip_reasons', [])
                cdInstrumento = payload.get('cdInstrumento', cdInstrumento)
                doCache    = True

            else:
                # NAO apagar o checkpoint antes de raspar: se o scrape falhar,
                # preservamos o JSON antigo. O write_text() abaixo sobrescreve so no sucesso.
                info = await RasparCaracteristicas(page, ticker, cdInstrumento, log)
                if not info:
                    log.warning(f'{ticker}: características não capturadas')
                    stats['erros'] += 1
                    continue

                idx = (info.get('indexador') or {}).get('nome', '')
                if idx and idx.upper() in INDEXADORES_INVALIDOS_TICKER:
                    log.info(f'{ticker}: indexador "{idx}" inválido — descartado')
                    motivosSkip = ['indexador_invalido']
                else:
                    agenda = await RasparAgenda(page, ticker, cdInstrumento, log)
                    if agenda is None:
                        # A Anbima nao tem agenda para este ativo (ex.: RAIZ12 — confirmado
                        # sem fluxo na fonte). Persistimos mesmo assim o cadastro capturado
                        # (emissor, indexador, etc.) — so nao grava FluxoAtivos. Sem fluxo o
                        # ativo nao e precificado pela calc (cai na cascata de API), mas o
                        # emissor passa a aparecer no relatorio. Mesmo tratamento de
                        # 'evento_desconhecido' (linhasFluxo=None); 'sem_agenda' exclui da
                        # fila para nao re-raspar eternamente.
                        log.warning(f'{ticker}: agenda não capturada — grava só o cadastro')
                        motivosSkip = ['sem_agenda']
                        linhasFluxo = None
                    else:
                        linhasFluxo = ProcessarAgenda(ticker, agenda, log)
                        if linhasFluxo is None:
                            motivosSkip = ['evento_desconhecido']

                caminhoJson.write_text(
                    json.dumps({'ticker': ticker, 'cdInstrumento': cdInstrumento,
                                'info': info, 'agenda': agenda,
                                'skip_reasons': motivosSkip},
                               ensure_ascii=False, indent=2),
                    encoding='utf-8'
                )
                stats['scrappados'] += 1
                stats['novos_tickers'].append(ticker)
                doCache = False

            # ── 2. indexador inválido: não persiste nada no DB ────────────────
            if 'indexador_invalido' in motivosSkip:
                stats['descartados'] += 1
                continue

            # ── 3. processar agenda em cache hit ─────────────────────────────
            if doCache and 'evento_desconhecido' not in motivosSkip:
                linhasFluxo = ProcessarAgenda(ticker, agenda, log)
                if linhasFluxo is None:
                    motivosSkip = ['evento_desconhecido']
                    payload['skip_reasons'] = motivosSkip
                    caminhoJson.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

            # ── 4. persistir no DB ────────────────────────────────────────────
            async with lock:
                UpsertInfoAtivos(conn, ticker, cdInstrumento, info)
                if linhasFluxo is not None:
                    if SincronizarFluxoAtivos(conn, ticker, linhasFluxo):
                        stats['fluxo_mudou'] += 1
                    stats['fluxo_ok'] += 1
                else:
                    stats['fluxo_skip'] += 1
                    stats['anomalias'].append((ticker, motivosSkip[:]))
                # reporta info faltante só para tickers EFETIVAMENTE raspados
                # nesta run (não os que vieram só de cache)
                if not doCache:
                    faltantes = CalcularInfoFaltante(info, cdInstrumento)
                    if faltantes:
                        stats['info_faltante'].append((ticker, cdInstrumento, faltantes))
                conn.commit()
                stats['inseridos'] += 1

            log.info(f'[W{wid}] {ticker} OK ({stats["inseridos"]} inseridos)'
                     + (f' [skip: {motivosSkip}]' if motivosSkip else ''))

        except Exception as e:
            log.error(f'{ticker}: erro inesperado: {e}', exc_info=True)
            stats['erros'] += 1
        finally:
            queue.task_done()

    await ctx.close()


# ── main ──────────────────────────────────────────────────────────────────────

async def PrincipalAsync():
    args = LerArgumentos()
    log  = ObterLogger('scrape_anbima_data_ativos')
    conn = ObterBanco()

    dirJson = Path(cfg['paths']['anbimaDataRaw'])
    dirJson.mkdir(parents=True, exist_ok=True)

    stats = {'scrappados': 0, 'inseridos': 0, 'erros': 0,
             'fluxo_ok': 0, 'fluxo_skip': 0, 'fluxo_mudou': 0, 'descartados': 0,
             'skip_list': 0,        # tickers excluídos pela skip-list manual
             'anomalias': [],       # [(ticker, [skip_reasons])]
             'info_faltante': [],   # [(ticker, cdInstrumento, [campos_null])]
             'novos_tickers': []}   # tickers novos scrappados nessa run

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, proxy=ObterProxyPlaywright())

        # ── fase 1: coleta de tickers ─────────────────────────────────────────
        tickers: list[tuple[str, str]] = []

        if args.ticker:
            tickers = [(args.ticker, InferirTipo(args.ticker))]

        elif args.skipScrape:
            for jp in sorted(dirJson.glob('*.json')):
                p = json.loads(jp.read_text(encoding='utf-8'))
                tickers.append((p['ticker'], p['cdInstrumento']))

        elif args.mode == 'full':
            log.info('Modo full: coletando listagem Anbima Data...')
            deb    = await TickersListagemComFiltro(browser, 'DEB', log)
            cr     = await TickersListagemComFiltro(browser, 'CR',  log)
            tickers = deb + cr

        else:
            dates = []
            if args.date:
                dates = [args.date]
            elif args.start:
                d   = date.fromisoformat(args.start)
                end = date.fromisoformat(args.end) if args.end else d
                while d <= end:
                    dates.append(d.isoformat())
                    d += timedelta(days=1)
            else:
                log.error('Especifique --mode full, --date, --start/--end ou --ticker')
                await browser.close()
                conn.close()
                return

            # ── 1. união de fontes: quem teve trade + quem teve taxa Anbima ────
            dosNegocios = TickersDeNegociosBrutos(conn, dates)
            daAnbima = TickersDaAnbima(conn, dates)
            log.info(f'Candidatos brutos — NegociosBrutos: {len(dosNegocios)}, '
                     f'AnbimaIndicativos: {len(daAnbima)}')

            # dedup por ticker, preferindo cdInstrumento não-nulo
            # (trades costuma ter; senão InfoAtivos; senão InferirTipo no final)
            merged: dict[str, str | None] = {}
            for tk, inst in dosNegocios:
                if tk not in merged or (merged[tk] is None and inst is not None):
                    merged[tk] = inst
            for tk, inst in daAnbima:
                if tk not in merged or (merged[tk] is None and inst is not None):
                    merged[tk] = inst
            candidatos = [(tk, inst or InferirTipo(tk)) for tk, inst in merged.items()]
            log.info(f'Candidatos após dedup por ticker: {len(candidatos)}')

            # ── 2. guarda contra re-scrape infinito: skip_reasons terminais ───
            terminais = TickersSkipTerminal(dirJson, [tk for tk, _ in candidatos])
            if terminais:
                candidatos = [(tk, inst) for tk, inst in candidatos if tk not in terminais]
                log.info(f'Excluídos por skip_reason terminal (indexador_invalido/sem_agenda): '
                         f'{len(terminais)} — restam {len(candidatos)}')

            # ── 3. filtro de completude (a menos que --force) ─────────────────
            if args.force:
                tickers = candidatos
                log.info(f'--force: ignora completude — fila: {len(tickers)}')
            else:
                completos = TickersCompletos(conn, [tk for tk, _ in candidatos])
                tickers = [(tk, inst) for tk, inst in candidatos if tk not in completos]
                log.info(f'Completos na base: {len(completos)} — '
                         f'fila (info faltando): {len(tickers)}')

        # ── exclusão pela skip-list manual (INCONDICIONAL, mesmo com --force) ──
        skipTickers = CarregarSkipTickers()
        if skipTickers:
            antes   = len(tickers)
            tickers = [(tk, inst) for tk, inst in tickers if tk.upper() not in skipTickers]
            excluidos = antes - len(tickers)
            stats['skip_list'] = excluidos
            if excluidos:
                log.info(f'Excluídos pela skip-list ({len(skipTickers)} tickers no arquivo): '
                         f'{excluidos} — restam {len(tickers)}')

        if args.limit:
            tickers = tickers[:args.limit]

        log.info(f'Tickers a processar: {len(tickers)}')
        if not tickers:
            log.info('Nada a fazer.')
            await browser.close()
            conn.close()
            return

        # ── fase 2: scraping paralelo ─────────────────────────────────────────
        queue = asyncio.Queue()
        for t in tickers:
            queue.put_nowait(t)

        nWorkers = 1 if args.skipScrape else args.workers
        lock      = asyncio.Lock()

        await asyncio.gather(*[
            Trabalhador(i, queue, browser, dirJson, conn, lock, stats, args, log)
            for i in range(nWorkers)
        ])

        await browser.close()

    conn.close()
    log.info(f'Concluído: {stats}')

    lines = [
        f"Tickers inseridos/atualizados : {stats['inseridos']}",
        f"Scrappados (novos)            : {stats['scrappados']}",
        f"Descartados (indexador)       : {stats['descartados']}",
        f"Pulados (skip-list)           : {stats['skip_list']}",
        f"FluxoAtivos OK                : {stats['fluxo_ok']}",
        f"FluxoAtivos mudou (invalidado): {stats['fluxo_mudou']}",
        f"FluxoAtivos skip (evento desc): {stats['fluxo_skip']}",
        f"Erros                         : {stats['erros']}",
    ]

    if stats['info_faltante']:
        lines.append(f"\nAtivos com info faltante após scrape "
                     f"({len(stats['info_faltante'])} tickers):")
        for tk, inst, campos in stats['info_faltante']:
            lines.append(f"  {tk} ({inst}): {', '.join(campos)}")

    if stats['anomalias']:
        lines.append(f"\nAnomalias — FluxoAtivos descartado ({len(stats['anomalias'])} tickers):")
        for tk, reasons in stats['anomalias']:
            lines.append(f"  {tk}: {', '.join(reasons)}")

    if stats['novos_tickers']:
        lines.append(f"\nNovos tickers scrappados ({len(stats['novos_tickers'])}):")
        lines.append('  ' + ', '.join(stats['novos_tickers']))

    body = '\n'.join(lines)
    EnviarEmailConclusao('scrape_anbima_data_ativos', stats['erros'] == 0, body, log)


def Principal():
    asyncio.run(PrincipalAsync())


if __name__ == '__main__':
    Principal()
