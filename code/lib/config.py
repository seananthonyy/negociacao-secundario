import os
import tomllib
from pathlib import Path
from dotenv import load_dotenv

# Raiz do projeto: code/lib/../  -> code/
RAIZ = Path(__file__).parent.parent

envCarregado = False
cfgCache: dict | None = None


def GarantirEnv() -> None:
    global envCarregado
    if not envCarregado:
        # No PC pessoal os segredos vêm do .env; no banco vêm de variáveis de
        # ambiente da conta. load_dotenv não sobrescreve variáveis já setadas
        # no ambiente (override=False por padrão), então ambos convivem.
        load_dotenv(RAIZ / ".env")
        envCarregado = True


# Chaves de [paths] que NAO devem ser ancoradas na raiz. A calculadoraDir tem
# resolucao propria em lib/calc.DirCalculadora (variavel de ambiente do banco tem
# precedencia sobre o config, e ela aceita caminho relativo ou absoluto).
PATHS_NAO_ANCORADOS = ("calculadoraDir",)


def AncorarPaths(conf: dict) -> None:
    """Torna cada [paths] absoluto, ancorado na RAIZ do projeto.

    Sem isto os caminhos sao relativos ao CWD, e um script rodado de outra pasta
    silenciosamente trabalha sobre outro lugar: o SQLite CRIA um banco vazio em
    `data/` ali e o script termina com sucesso, sobre nada. Ancorado, o script
    funciona rodado de qualquer diretorio. Caminho ja absoluto no config e
    respeitado (RAIZ / absoluto devolve o absoluto)."""
    for chave, valor in conf.get("paths", {}).items():
        if chave in PATHS_NAO_ANCORADOS or not isinstance(valor, str):
            continue
        conf["paths"][chave] = str((RAIZ / valor).resolve())


def GarantirCfg() -> None:
    global cfgCache
    if cfgCache is None:
        GarantirEnv()
        with open(RAIZ / "config.toml", "rb") as fh:
            cfgCache = tomllib.load(fh)
        AncorarPaths(cfgCache)
        AplicarProxyEnv()  # _cfg já setado — ObterSegredo pode ser usado aqui


def ObterEnv(key: str, default: str | None = None) -> str | None:
    GarantirEnv()
    return os.getenv(key, default)


def ObterCfg() -> dict:
    GarantirCfg()
    return cfgCache  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Resolução de segredos por nome lógico
# ---------------------------------------------------------------------------
# O bloco [env] do config.toml mapeia cada segredo para uma LISTA de nomes de
# variáveis de ambiente candidatas (apenas nomes — nunca valores; seguro para
# repositório público). O resolver tenta cada candidato na ordem e usa o
# primeiro preenchido. Assim o mesmo código roda no PC pessoal (nomes canônicos
# no .env) e no banco (nomes das variáveis da conta), sem edição.

def ObterSegredo(chaveLogica: str, default: str | None = None) -> str | None:
    """Resolve um segredo pelo nome lógico (chave de config [env])."""
    GarantirEnv()
    candidates = ObterCfg().get("env", {}).get(chaveLogica, [])
    if isinstance(candidates, str):
        candidates = [candidates]
    for name in candidates:
        val = os.getenv(name)
        if val:
            return val
    return default


def AplicarProxyEnv() -> None:
    """Copia o proxy resolvido (nomes do banco, ex.: proxy_https) para as
    variáveis padrão HTTP_PROXY/HTTPS_PROXY que o httpx lê nativamente.
    Respeita valores já presentes no ambiente.

    ATENÇÃO: o Chromium do Playwright NÃO lê HTTP_PROXY/HTTPS_PROXY do ambiente
    — para os scrapers Playwright use ObterProxyPlaywright() e passe o resultado
    em chromium.launch(proxy=...)."""
    for logical, target in (("httpProxy", "HTTP_PROXY"), ("httpsProxy", "HTTPS_PROXY")):
        if os.getenv(target):
            continue
        val = ObterSegredo(logical)
        if val:
            os.environ[target] = val


def ObterProxyPlaywright() -> dict | None:
    """Proxy no formato do Playwright ({'server': 'http://host:port', ...}) ou
    None se não houver proxy configurado.

    O Chromium do Playwright ignora HTTP_PROXY/HTTPS_PROXY do ambiente; o proxy
    precisa ser passado explicitamente em chromium.launch(proxy=...). No PC
    pessoal (sem proxy) retorna None → launch sem proxy (comportamento atual).
    No banco resolve proxy_https/proxy_http das variáveis da conta."""
    url = ObterSegredo("httpsProxy") or ObterSegredo("httpProxy")
    if not url:
        return None
    from urllib.parse import urlparse
    p = urlparse(url if "://" in url else f"http://{url}")
    server = p.scheme + "://" + (p.hostname or "")
    if p.port:
        server += f":{p.port}"
    proxy: dict = {"server": server}
    if p.username:
        proxy["username"] = p.username
    if p.password:
        proxy["password"] = p.password
    return proxy


def ObterListaEmails(which: str) -> list[str]:
    """Lista de destinatários de email. Ordem de resolução:
      1. code/destinatarios.py (não versionado)
      2. variável de ambiente mapeada em [env]
      3. [] (nenhum)
    `which`: 'destinatarios' (rascunho do relatório) ou 'outlook' (emails [OK]/[ERROR])."""
    attr = "EMAIL_DESTINATARIOS" if which == "destinatarios" else "OUTLOOK_TO"
    try:
        import destinatarios as destinos  # code/ está no sys.path em runtime
        vals = getattr(destinos, attr, None)
        if vals:
            return [str(v).strip() for v in vals if str(v).strip()]
    except ImportError:
        pass
    key = "emailDestinatarios" if which == "destinatarios" else "outlookTo"
    raw = ObterSegredo(key)
    if raw:
        return [e.strip() for e in raw.replace(";", ",").split(",") if e.strip()]
    return []


# Objeto global exposto para importação direta: from lib.config import cfg
class CfgProxy:
    """Proxy lazy: age como dict mas carrega config.toml apenas na primeira leitura."""

    def __getitem__(self, key):
        return ObterCfg()[key]

    def __contains__(self, key):
        return key in ObterCfg()

    def get(self, key, default=None):
        return ObterCfg().get(key, default)


cfg = CfgProxy()
