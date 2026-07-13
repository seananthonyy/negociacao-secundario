import logging
import threading

import httpx

from lib.config import cfg, ObterSegredo

log = logging.getLogger(__name__)

bearerToken: str | None = None
tokenLock = threading.Lock()

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

# Cache do getBondDetails por cdTicker (cadastro estático: não muda na rodagem).
cacheDetalhes: dict[str, object] = {}


def ResetarToken() -> None:
    """Descarta o token em memória, forçando novo login na próxima chamada."""
    global bearerToken
    bearerToken = None


def Autenticar() -> str:
    """
    Obtém token de sessão via POST /login.
    Levanta RuntimeError se falhar — o chamador decide o que fazer.
    """
    baseUrl = cfg["api"]["b3"]["baseUrl"]
    rawToken = ObterSegredo("b3CalcToken")
    if not rawToken:
        raise RuntimeError("Token da B3 Calculator não configurado (ver [env].b3CalcToken no config.toml)")

    timeout = cfg["calc"]["timeoutSeconds"]
    log.debug("b3_calc_api: obtendo token via POST /login")
    resp = ObterCliente().post(f"{baseUrl}/login", json={"token": rawToken}, timeout=timeout)
    resp.raise_for_status()

    data = resp.json()
    # A API retorna o token no campo "Authorization" — usado direto no header, sem "Bearer".
    if "Authorization" in data and data["Authorization"]:
        log.debug("b3_calc_api: token obtido")
        return str(data["Authorization"])

    raise RuntimeError(f"Resposta de /login não contém campo 'Authorization': {data}")


def GarantirToken() -> str:
    global bearerToken
    if bearerToken is not None:
        return bearerToken
    with tokenLock:
        if bearerToken is None:
            bearerToken = Autenticar()
        return bearerToken


def Requisitar(url: str, timeout: int) -> httpx.Response | None:
    """
    Faz GET com o token atual. Em 401, renova o token e tenta uma segunda vez.
    Retorna None em erro de rede/timeout.
    """
    token = GarantirToken()
    try:
        resp = ObterCliente().get(url, headers={"Authorization": token}, timeout=timeout)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning("b3_calc_api: erro na chamada para %s: %s", url, exc)
        return None

    if resp.status_code == 401:
        log.debug("b3_calc_api: 401 recebido, renovando token e tentando novamente")
        ResetarToken()
        token = GarantirToken()
        try:
            resp = ObterCliente().get(url, headers={"Authorization": token}, timeout=timeout)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            log.warning("b3_calc_api: erro após renovação de token para %s: %s", url, exc)
            return None

    return resp


def CalcularPuGov(cetip: str, dtRef: str, taxa: float) -> tuple[float | None, float | None]:
    """
    Calcula PU e duration de titulo governamental via B3 Calculator.

    GET /calcPU/{cetip}/{dtRef}/{taxa}

    cetip: codigo CETIP do titulo (ex: "76019920320815" para NTN-B 15/08/2032)
    dtRef: data de referencia no formato YYYY-MM-DD
    taxa:  taxa indicativa em % a.a. (ex: 6.75)

    Retorna (pu, duration) ou (None, None) em caso de falha.
    """
    baseUrl = cfg["api"]["b3"]["baseUrl"]
    timeout = cfg["calc"]["timeoutSeconds"]
    url = f"{baseUrl}/calcPU/{cetip}/{dtRef}/{taxa}"

    log.debug("b3_calc_api: GET %s", url)
    resp = Requisitar(url, timeout)

    if resp is None:
        return None, None

    if not resp.is_success:
        log.warning("b3_calc_api: HTTP %d em calcPU %s/%s/%s", resp.status_code, cetip, dtRef, taxa)
        return None, None

    try:
        data = resp.json()
    except Exception as exc:
        log.warning("b3_calc_api: resposta nao e JSON valido em calcPU %s: %s", url, exc)
        return None, None

    # A B3 devolve o PU em "PU" (maiúsculo) e a duration em "duration" (minúsculo) —
    # tanto para CETIP de governo quanto para ticker de debênture. Ler "pu" trazia
    # None sempre; passou despercebido porque o único chamador (scrape_anbima_ntnb)
    # só usa a duration.
    rawPu       = data.get("PU", data.get("pu"))
    rawDuration = data.get("duration")

    pu       = float(rawPu)       if rawPu       is not None else None
    duration = float(rawDuration) if rawDuration is not None else None

    log.debug("b3_calc_api: calcPU %s/%s/%s → pu=%s duration=%s", cetip, dtRef, taxa, pu, duration)
    return pu, duration


def ObterDetalhesAtivo(cdTicker: str) -> dict | None:
    """
    Cadastro + agenda de eventos de um ativo, via GET /getBondDetails/{cdTicker}.

    Estático: não leva data nem taxa. Devolve o dict cru da B3 ou None se ela não
    cobrir o ativo. Campos usados pelo validar_fluxos.py:
      startingdate, issuedate, expiredate, yield (taxa de emissão), method
      (IPCA-I | IPCA | DI-PERC | DI-SPREAD | PRE), anniversaryday, vne, issuer,
      events: [{date, eventType, yield}] — 'A' = %amortização, 'J' = cupom
      (no estilo IPCA-I o yield do 'J' é a %incorporação).

    Resultados (incluindo None) são cacheados por ticker — o cadastro não muda
    dentro de uma rodagem.
    """
    cached = cacheDetalhes.get(cdTicker, CACHE_MISS)
    if cached is not CACHE_MISS:
        return cached  # type: ignore[return-value]

    baseUrl = cfg["api"]["b3"]["baseUrl"]
    timeout = cfg["calc"]["timeoutSeconds"]
    url = f"{baseUrl}/getBondDetails/{cdTicker}"

    log.debug("b3_calc_api: GET %s", url)
    resp = Requisitar(url, timeout)

    if resp is None or not resp.is_success:
        if resp is not None:
            log.debug("b3_calc_api: HTTP %d em getBondDetails %s", resp.status_code, cdTicker)
        cacheDetalhes[cdTicker] = None
        return None

    try:
        data = resp.json()
    except Exception as exc:
        log.warning("b3_calc_api: resposta não é JSON válido em %s: %s", url, exc)
        cacheDetalhes[cdTicker] = None
        return None

    # Sem 'codbond' a B3 não conhece o papel (devolve corpo vazio/erro com HTTP 200).
    result = data if isinstance(data, dict) and data.get("codbond") else None
    if result is None:
        log.debug("b3_calc_api: getBondDetails sem cadastro para %s", cdTicker)

    cacheDetalhes[cdTicker] = result
    return result


def CalcularYield(cdTicker: str, dtLiquidacao: str, vrPU: float) -> float | None:
    """
    Retorna yield em % a.a. via B3 Calculator API, ou None se falhar.

    Resultados (incluindo None) são cacheados por (cdTicker, dtLiquidacao, vrPU) exatos —
    sem tolerância no PU. Cache evita chamadas duplicadas para o mesmo trade na mesma rodagem.

    dtLiquidacao: formato YYYY-MM-DD — sempre usar dtLiquidacao, não dtNegocio.
    vrPU: PU do negócio (float) — match exato, sem tolerância.
    """
    cacheKey = (cdTicker, dtLiquidacao, vrPU)
    cached = cacheTaxas.get(cacheKey, CACHE_MISS)
    if cached is not CACHE_MISS:
        log.debug("b3_calc_api: cache hit para %s/%s/%s → %s", cdTicker, dtLiquidacao, vrPU, cached)
        return cached  # type: ignore[return-value]

    baseUrl = cfg["api"]["b3"]["baseUrl"]
    timeout = cfg["calc"]["timeoutSeconds"]
    url = f"{baseUrl}/calcYield/{cdTicker}/{dtLiquidacao}/{vrPU}"

    log.debug("b3_calc_api: GET %s", url)
    resp = Requisitar(url, timeout)

    if resp is None:
        cacheTaxas[cacheKey] = None
        return None

    if not resp.is_success:
        log.warning("b3_calc_api: HTTP %d em %s", resp.status_code, url)
        cacheTaxas[cacheKey] = None
        return None

    try:
        data = resp.json()
    except Exception as exc:
        log.warning("b3_calc_api: resposta não é JSON válido em %s: %s", url, exc)
        cacheTaxas[cacheKey] = None
        return None

    rawYield = data.get("yield")
    if rawYield is None or rawYield <= 0:
        log.warning("b3_calc_api: campo 'yield' inválido (%r) para %s/%s/%s", rawYield, cdTicker, dtLiquidacao, vrPU)
        cacheTaxas[cacheKey] = None
        return None

    result = float(rawYield)
    log.debug("b3_calc_api: yield=%.6f para %s/%s/%s", result, cdTicker, dtLiquidacao, vrPU)
    cacheTaxas[cacheKey] = result
    return result
