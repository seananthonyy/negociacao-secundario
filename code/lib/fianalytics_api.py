import json
import logging
import threading

import httpx

from lib.config import cfg, get_secret

log = logging.getLogger(__name__)

# Proxy: httpx lê HTTPS_PROXY / HTTP_PROXY do ambiente automaticamente (trust_env=True).

# Sentinel para distinguir "não está no cache" de "está no cache como None".
_CACHE_MISS = object()

# Cache de resultados por (cdTicker, dtLiquidacao, vrPU) — match exato, sem tolerância no PU.
# Guarda float (% a.a.) ou None. Ambos são cacheados para evitar chamadas repetidas.
_rateCache: dict[tuple, object] = {}

# Lista de bonds do usuário — carregada uma única vez por processo via _GetUserBonds().
# None significa "ainda não buscado"; lista vazia/None pós-fetch significa "falhou ou sem bonds".
_userBondsFetched: bool = False
_userBonds: list[dict] | None = None
_bondsFetchLock = threading.Lock()


def _ParseResponse(resp: httpx.Response, url: str) -> dict | None:
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


def _GetHeaders() -> dict[str, str]:
    apiKey = get_secret("fianalyticsApiKey")
    if not apiKey:
        raise RuntimeError("API key do FI Analytics não configurada (ver [env].fianalyticsApiKey no config.toml)")
    # Content-Type com charset=utf-8 é exigido pelo servidor (502 sem ele).
    return {
        "Content-Type": "application/json; charset=utf-8",
        "x-api-key": apiKey,
    }


def _IsValidRate(value) -> bool:
    """m2mRate é válido apenas se for numérico, não-nulo, positivo e não-zero."""
    if value is None:
        return False
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return f > 0


def _CallPrimary(cdTicker: str, cdInstrumento: str, dtLiquidacao: str, vrPU: float) -> float | None:
    """Tenta calcular m2mRate no endpoint principal (deb ou cricra). Tentativa única, sem retry."""
    baseUrl: str = cfg["api"]["fianalytics"]["baseUrl"]
    timeout: int = cfg["calc"]["timeoutSeconds"]
    path: str = cfg["api"]["fianalytics"]["debPath"] if cdInstrumento == "DEB" else cfg["api"]["fianalytics"]["cricraPath"]
    url = f"{baseUrl}{path}"

    try:
        log.debug("fianalytics_api: POST %s ticker=%s date=%s", url, cdTicker, dtLiquidacao)
        resp = httpx.post(url, json={"ticker": cdTicker, "date": dtLiquidacao, "pu": vrPU}, headers=_GetHeaders(), timeout=timeout)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning("fianalytics_api: erro na chamada primária para %s: %s", cdTicker, exc)
        return None

    if not resp.is_success:
        log.warning("fianalytics_api: HTTP %d em %s ticker=%s", resp.status_code, url, cdTicker)
        return None

    data = _ParseResponse(resp, url)
    if data is None:
        return None

    rawRate = data.get("m2mRate")
    if not _IsValidRate(rawRate):
        log.warning("fianalytics_api: m2mRate inválido (%r) para %s/%s/%s", rawRate, cdTicker, dtLiquidacao, vrPU)
        return None

    # API retorna taxa em decimal (0.067); converter para % a.a. (6.7).
    rate = float(rawRate) * 100
    log.debug("fianalytics_api: m2mRate=%.6f%% para %s/%s/%s (endpoint primário)", rate, cdTicker, dtLiquidacao, vrPU)
    return rate


def _GetUserBonds() -> list[dict] | None:
    """
    Retorna a lista de bonds do usuário, buscando da API na primeira chamada
    e devolvendo do cache nas chamadas seguintes (uma única requisição por processo).
    Thread-safe: double-check lock garante que só uma thread faz o fetch.
    """
    global _userBondsFetched, _userBonds
    if _userBondsFetched:
        return _userBonds

    with _bondsFetchLock:
        if _userBondsFetched:
            return _userBonds

        baseUrl: str = cfg["api"]["fianalytics"]["baseUrl"]
        path: str = cfg["api"]["fianalytics"]["getUserBondsPath"]
        timeout: int = cfg["calc"]["timeoutSeconds"]
        url = f"{baseUrl}{path}"
        body = {"user_email": get_secret("fianalyticsUser", ""), "get_company_bonds": "1"}

        try:
            log.debug("fianalytics_api: POST %s (getUserBonds — único fetch da sessão)", url)
            resp = httpx.post(url, json=body, headers=_GetHeaders(), timeout=timeout)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            log.warning("fianalytics_api: erro ao buscar user bonds: %s", exc)
            _userBondsFetched = True
            return None

        if not resp.is_success:
            log.warning("fianalytics_api: HTTP %d em POST %s", resp.status_code, url)
            _userBondsFetched = True
            return None

        data = _ParseResponse(resp, url)
        _userBondsFetched = True

        if isinstance(data, list):
            log.info("fianalytics_api: %d bonds carregados do usuário (cache populado)", len(data))
            _userBonds = data
        else:
            log.warning("fianalytics_api: estrutura inesperada de getUserBonds: %s", type(data))
            _userBonds = None

        return _userBonds


def _FindBond(bonds: list[dict], cdTicker: str) -> dict | None:
    """Localiza o bond pelo campo bond_name (case-insensitive)."""
    tickerLower = cdTicker.lower()
    for bond in bonds:
        if str(bond.get("bond_name", "")).lower() == tickerLower:
            return bond
    return None


def _CallBondbuilder(docId: str, cdTicker: str, dtLiquidacao: str, vrPU: float) -> float | None:
    """POST /bb/bondbuildercalculator. Tentativa única, sem retry."""
    baseUrl: str = cfg["api"]["fianalytics"]["baseUrl"]
    path: str = cfg["api"]["fianalytics"]["bondbuilderPath"]
    timeout: int = cfg["calc"]["timeoutSeconds"]
    url = f"{baseUrl}{path}"

    try:
        log.debug("fianalytics_api: POST %s (bondbuilder) ticker=%s date=%s", url, cdTicker, dtLiquidacao)
        resp = httpx.post(url, json={"doc_id": docId, "date": dtLiquidacao, "pu": vrPU}, headers=_GetHeaders(), timeout=timeout)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning("fianalytics_api: erro no bondbuilder para %s: %s", cdTicker, exc)
        return None

    if not resp.is_success:
        log.warning("fianalytics_api: HTTP %d no bondbuilder para %s", resp.status_code, cdTicker)
        return None

    data = _ParseResponse(resp, url)
    if data is None:
        return None

    rawRate = data.get("m2mRate")
    if not _IsValidRate(rawRate):
        log.warning("fianalytics_api: m2mRate inválido (%r) no bondbuilder para %s/%s/%s", rawRate, cdTicker, dtLiquidacao, vrPU)
        return None

    rate = float(rawRate) * 100
    log.debug("fianalytics_api: m2mRate=%.6f%% para %s/%s/%s (bondbuilder)", rate, cdTicker, dtLiquidacao, vrPU)
    return rate


def _CallBondbuilderFallback(cdTicker: str, dtLiquidacao: str, vrPU: float) -> float | None:
    """Fluxo bondbuilder: getUserBonds (cached) → FindBond → CallBondbuilder."""
    bonds = _GetUserBonds()
    if not bonds:
        log.warning("fianalytics_api: getUserBonds não retornou bonds para fallback de %s", cdTicker)
        return None

    bond = _FindBond(bonds, cdTicker)
    if bond is None:
        log.warning("fianalytics_api: ticker %s não encontrado nos %d bonds do usuário", cdTicker, len(bonds))
        return None

    docId = bond.get("_id")
    if not docId:
        log.warning("fianalytics_api: bond de %s não tem campo '_id': %s", cdTicker, bond)
        return None

    log.debug("fianalytics_api: bond encontrado para %s (doc_id=%s), chamando bondbuilder", cdTicker, docId)
    return _CallBondbuilder(docId, cdTicker, dtLiquidacao, vrPU)


def CalcRate(
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
    cached = _rateCache.get(cacheKey, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        log.debug("fianalytics_api: cache hit para %s/%s/%s → %s", cdTicker, dtLiquidacao, vrPU, cached)
        return cached  # type: ignore[return-value]

    rate = _CallPrimary(cdTicker, cdInstrumento, dtLiquidacao, vrPU)
    if rate is None:
        log.info("fianalytics_api: endpoint primário falhou para %s, tentando bondbuilder", cdTicker)
        rate = _CallBondbuilderFallback(cdTicker, dtLiquidacao, vrPU)

    if rate is None:
        log.warning("fianalytics_api: todos os níveis FI Analytics falharam para %s/%s/%s", cdTicker, dtLiquidacao, vrPU)

    _rateCache[cacheKey] = rate
    return rate
