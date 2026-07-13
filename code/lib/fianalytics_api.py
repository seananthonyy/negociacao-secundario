import json
import logging
import threading

import httpx

from lib.config import cfg, ObterSegredo

log = logging.getLogger(__name__)

# Proxy: httpx lê HTTPS_PROXY / HTTP_PROXY do ambiente automaticamente (trust_env=True).

# Client HTTP compartilhado por processo, com pool de conexões keepalive.
# Reusar a conexão evita re-handshake (TCP+TLS) a cada trade — ganho grande quando
# o tráfego passa por proxy (banco), onde cada CONNECT novo é caro. httpx.Client é
# thread-safe: suporta chamadas concorrentes dos workers do ThreadPoolExecutor.
clienteHttp: httpx.Client | None = None
clientLock = threading.Lock()


def ObterCliente() -> httpx.Client:
    global clienteHttp
    if clienteHttp is not None:
        return clienteHttp
    with clientLock:
        if clienteHttp is None:
            limits = httpx.Limits(max_connections=64, max_keepalive_connections=64, keepalive_expiry=30.0)
            clienteHttp = httpx.Client(limits=limits, trust_env=True)
    return clienteHttp


# Sentinel para distinguir "não está no cache" de "está no cache como None".
CACHE_MISS = object()

# Cache de resultados por (cdTicker, dtLiquidacao, vrPU) — match exato, sem tolerância no PU.
# Guarda float (% a.a.) ou None. Ambos são cacheados para evitar chamadas repetidas.
cacheTaxas: dict[tuple, object] = {}

# Lista de bonds do usuário — carregada uma única vez por processo via _GetUserBonds().
# None significa "ainda não buscado"; lista vazia/None pós-fetch significa "falhou ou sem bonds".
userBondsCarregados: bool = False
userBonds: list[dict] | None = None
bondsFetchLock = threading.Lock()


def AnalisarResposta(resp: httpx.Response, url: str) -> dict | None:
    """
    A API retorna o JSON como string dentro de outra string (double-encoded).
    resp.json() entrega a string; json.loads() faz o parse real.
    """
    try:
        raw = resp.json()
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        log.warning("fianalytics_api: resposta não é JSON válido em %s: %s", url, exc)
        return None


def ObterHeaders() -> dict[str, str]:
    apiKey = ObterSegredo("fianalyticsApiKey")
    if not apiKey:
        raise RuntimeError("API key do FI Analytics não configurada (ver [env].fianalyticsApiKey no config.toml)")
    # Content-Type com charset=utf-8 é exigido pelo servidor (502 sem ele).
    return {
        "Content-Type": "application/json; charset=utf-8",
        "x-api-key": apiKey,
    }


def TaxaValida(value) -> bool:
    """m2mRate é válido apenas se for numérico, não-nulo, positivo e não-zero."""
    if value is None:
        return False
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return f > 0


def ChamarPrimaria(cdTicker: str, cdInstrumento: str, dtLiquidacao: str, vrPU: float) -> float | None:
    """Tenta calcular m2mRate no endpoint principal (deb ou cricra). Tentativa única, sem retry."""
    baseUrl: str = cfg["api"]["fianalytics"]["baseUrl"]
    timeout: int = cfg["calc"]["timeoutSeconds"]
    path: str = cfg["api"]["fianalytics"]["debPath"] if cdInstrumento == "DEB" else cfg["api"]["fianalytics"]["cricraPath"]
    url = f"{baseUrl}{path}"

    try:
        log.debug("fianalytics_api: POST %s ticker=%s date=%s", url, cdTicker, dtLiquidacao)
        resp = ObterCliente().post(url, json={"ticker": cdTicker, "date": dtLiquidacao, "pu": vrPU}, headers=ObterHeaders(), timeout=timeout)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning("fianalytics_api: erro na chamada primária para %s: %s", cdTicker, exc)
        return None

    if not resp.is_success:
        log.warning("fianalytics_api: HTTP %d em %s ticker=%s", resp.status_code, url, cdTicker)
        return None

    data = AnalisarResposta(resp, url)
    if data is None:
        return None

    rawRate = data.get("m2mRate")
    if not TaxaValida(rawRate):
        log.warning("fianalytics_api: m2mRate inválido (%r) para %s/%s/%s", rawRate, cdTicker, dtLiquidacao, vrPU)
        return None

    # API retorna taxa em decimal (0.067); converter para % a.a. (6.7).
    rate = float(rawRate) * 100
    log.debug("fianalytics_api: m2mRate=%.6f%% para %s/%s/%s (endpoint primário)", rate, cdTicker, dtLiquidacao, vrPU)
    return rate


def ChamarCompleto(cdTicker: str, dtIso: str, vrTaxa: float) -> dict | None:
    """
    Resposta CRUA do calculador da FI, dado (ticker, data, taxa) — modo `rate`.

    Enquanto ChamarPrimaria() extrai só o m2mRate, aqui devolvemos o dict inteiro:
    `cashFlowEvents` (a agenda), `issueRate`, `maturityDate`, `adjustedFaceValue`
    (VNA) e `accruedInterest`. É o que o validar_fluxos.py precisa para conferir a
    agenda quando a B3 não cobre o ativo.

    Não recebe cdInstrumento: tenta o endpoint de debênture e, se não for, o de
    CRI/CRA (os dois devolvem os mesmos campos). Retorna None se nenhum responder.
    """
    baseUrl: str = cfg["api"]["fianalytics"]["baseUrl"]
    timeout: int = cfg["calc"]["timeoutSeconds"]
    corpo = {"ticker": cdTicker, "date": dtIso, "rate": float(vrTaxa)}

    for chave in ("debPath", "cricraPath"):
        url = f"{baseUrl}{cfg['api']['fianalytics'][chave]}"
        try:
            resp = ObterCliente().post(url, json=corpo, headers=ObterHeaders(), timeout=timeout)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            log.warning("fianalytics_api: erro em %s para %s: %s", chave, cdTicker, exc)
            continue

        if not resp.is_success:
            log.debug("fianalytics_api: HTTP %d em %s ticker=%s", resp.status_code, url, cdTicker)
            continue

        data = AnalisarResposta(resp, url)
        # Resposta válida = a FI reconheceu o papel e precificou.
        if isinstance(data, dict) and (data.get("m2m") is not None or data.get("m2mRate") is not None):
            return data

    log.debug("fianalytics_api: sem resposta completa para %s em %s", cdTicker, dtIso)
    return None


def ObterBondsUsuario() -> list[dict] | None:
    """
    Retorna a lista de bonds do usuário, buscando da API na primeira chamada
    e devolvendo do cache nas chamadas seguintes (uma única requisição por processo).
    Thread-safe: double-check lock garante que só uma thread faz o fetch.
    """
    global userBondsCarregados, userBonds
    if userBondsCarregados:
        return userBonds

    with bondsFetchLock:
        if userBondsCarregados:
            return userBonds

        baseUrl: str = cfg["api"]["fianalytics"]["baseUrl"]
        path: str = cfg["api"]["fianalytics"]["getUserBondsPath"]
        timeout: int = cfg["calc"]["timeoutSeconds"]
        url = f"{baseUrl}{path}"
        body = {"user_email": ObterSegredo("fianalyticsUser", ""), "get_company_bonds": "1"}

        try:
            log.debug("fianalytics_api: POST %s (getUserBonds — único fetch da sessão)", url)
            resp = ObterCliente().post(url, json=body, headers=ObterHeaders(), timeout=timeout)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            log.warning("fianalytics_api: erro ao buscar user bonds: %s", exc)
            userBondsCarregados = True
            return None

        if not resp.is_success:
            log.warning("fianalytics_api: HTTP %d em POST %s", resp.status_code, url)
            userBondsCarregados = True
            return None

        data = AnalisarResposta(resp, url)
        userBondsCarregados = True

        if isinstance(data, list):
            log.info("fianalytics_api: %d bonds carregados do usuário (cache populado)", len(data))
            userBonds = data
        else:
            log.warning("fianalytics_api: estrutura inesperada de getUserBonds: %s", type(data))
            userBonds = None

        return userBonds


def AcharBond(bonds: list[dict], cdTicker: str) -> dict | None:
    """Localiza o bond pelo campo bond_name (case-insensitive)."""
    tickerLower = cdTicker.lower()
    for bond in bonds:
        if str(bond.get("bond_name", "")).lower() == tickerLower:
            return bond
    return None


def ChamarBondbuilder(docId: str, cdTicker: str, dtLiquidacao: str, vrPU: float) -> float | None:
    """POST /bb/bondbuildercalculator. Tentativa única, sem retry."""
    baseUrl: str = cfg["api"]["fianalytics"]["baseUrl"]
    path: str = cfg["api"]["fianalytics"]["bondbuilderPath"]
    timeout: int = cfg["calc"]["timeoutSeconds"]
    url = f"{baseUrl}{path}"

    try:
        log.debug("fianalytics_api: POST %s (bondbuilder) ticker=%s date=%s", url, cdTicker, dtLiquidacao)
        resp = ObterCliente().post(url, json={"doc_id": docId, "date": dtLiquidacao, "pu": vrPU}, headers=ObterHeaders(), timeout=timeout)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning("fianalytics_api: erro no bondbuilder para %s: %s", cdTicker, exc)
        return None

    if not resp.is_success:
        log.warning("fianalytics_api: HTTP %d no bondbuilder para %s", resp.status_code, cdTicker)
        return None

    data = AnalisarResposta(resp, url)
    if data is None:
        return None

    rawRate = data.get("m2mRate")
    if not TaxaValida(rawRate):
        log.warning("fianalytics_api: m2mRate inválido (%r) no bondbuilder para %s/%s/%s", rawRate, cdTicker, dtLiquidacao, vrPU)
        return None

    rate = float(rawRate) * 100
    log.debug("fianalytics_api: m2mRate=%.6f%% para %s/%s/%s (bondbuilder)", rate, cdTicker, dtLiquidacao, vrPU)
    return rate


def ChamarBondbuilderFallback(cdTicker: str, dtLiquidacao: str, vrPU: float) -> float | None:
    """Fluxo bondbuilder: getUserBonds (cached) → FindBond → CallBondbuilder."""
    bonds = ObterBondsUsuario()
    if not bonds:
        log.warning("fianalytics_api: getUserBonds não retornou bonds para fallback de %s", cdTicker)
        return None

    bond = AcharBond(bonds, cdTicker)
    if bond is None:
        log.warning("fianalytics_api: ticker %s não encontrado nos %d bonds do usuário", cdTicker, len(bonds))
        return None

    docId = bond.get("_id")
    if not docId:
        log.warning("fianalytics_api: bond de %s não tem campo '_id': %s", cdTicker, bond)
        return None

    log.debug("fianalytics_api: bond encontrado para %s (doc_id=%s), chamando bondbuilder", cdTicker, docId)
    return ChamarBondbuilder(docId, cdTicker, dtLiquidacao, vrPU)


def CalcularTaxa(
    cdTicker: str,
    cdInstrumento: str,
    dtLiquidacao: str,
    vrPU: float,
) -> float | None:
    """
    Retorna m2mRate em % a.a. ou None se todos os níveis falharem.

    Nível 1: endpoint principal (deb ou cricra) conforme cdInstrumento.
    Nível 2: bondbuilder (getUserBonds cached + bondbuildercalculator).

    Resultados (incluindo None) são cacheados por (cdTicker, dtLiquidacao, vrPU) exatos —
    sem tolerância no PU. Cache evita chamadas duplicadas para o mesmo trade na mesma rodagem.

    cdInstrumento deve ser 'DEB', 'CRI' ou 'CRA'.
    dtLiquidacao: formato YYYY-MM-DD — sempre usar dtLiquidacao, não dtNegocio.
    vrPU: PU do negócio (float) — match exato, sem tolerância.
    """
    cacheKey = (cdTicker, dtLiquidacao, vrPU)
    cached = cacheTaxas.get(cacheKey, CACHE_MISS)
    if cached is not CACHE_MISS:
        log.debug("fianalytics_api: cache hit para %s/%s/%s → %s", cdTicker, dtLiquidacao, vrPU, cached)
        return cached  # type: ignore[return-value]

    rate = ChamarPrimaria(cdTicker, cdInstrumento, dtLiquidacao, vrPU)
    if rate is None:
        log.info("fianalytics_api: endpoint primário falhou para %s, tentando bondbuilder", cdTicker)
        rate = ChamarBondbuilderFallback(cdTicker, dtLiquidacao, vrPU)

    if rate is None:
        log.warning("fianalytics_api: todos os níveis FI Analytics falharam para %s/%s/%s", cdTicker, dtLiquidacao, vrPU)

    cacheTaxas[cacheKey] = rate
    return rate
