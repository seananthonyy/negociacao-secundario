import os
import tomllib
from pathlib import Path
from dotenv import load_dotenv

# Raiz do projeto: code/lib/../  -> code/
_ROOT = Path(__file__).parent.parent

_env_loaded = False
_cfg: dict | None = None


def _ensure_env() -> None:
    global _env_loaded
    if not _env_loaded:
        # No PC pessoal os segredos vêm do .env; no banco vêm de variáveis de
        # ambiente da conta. load_dotenv não sobrescreve variáveis já setadas
        # no ambiente (override=False por padrão), então ambos convivem.
        load_dotenv(_ROOT / ".env")
        _env_loaded = True


def _ensure_cfg() -> None:
    global _cfg
    if _cfg is None:
        _ensure_env()
        with open(_ROOT / "config.toml", "rb") as fh:
            _cfg = tomllib.load(fh)
        _apply_proxy_env()  # _cfg já setado — get_secret pode ser usado aqui


def get_env(key: str, default: str | None = None) -> str | None:
    _ensure_env()
    return os.getenv(key, default)


def _get_cfg() -> dict:
    _ensure_cfg()
    return _cfg  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Resolução de segredos por nome lógico
# ---------------------------------------------------------------------------
# O bloco [env] do config.toml mapeia cada segredo para uma LISTA de nomes de
# variáveis de ambiente candidatas (apenas nomes — nunca valores; seguro para
# repositório público). O resolver tenta cada candidato na ordem e usa o
# primeiro preenchido. Assim o mesmo código roda no PC pessoal (nomes canônicos
# no .env) e no banco (nomes das variáveis da conta), sem edição.

def get_secret(logical_key: str, default: str | None = None) -> str | None:
    """Resolve um segredo pelo nome lógico (chave de config [env])."""
    _ensure_env()
    candidates = _get_cfg().get("env", {}).get(logical_key, [])
    if isinstance(candidates, str):
        candidates = [candidates]
    for name in candidates:
        val = os.getenv(name)
        if val:
            return val
    return default


def _apply_proxy_env() -> None:
    """Copia o proxy resolvido (nomes do banco, ex.: proxy_https) para as
    variáveis padrão HTTP_PROXY/HTTPS_PROXY que httpx e Playwright leem
    nativamente. Respeita valores já presentes no ambiente."""
    for logical, target in (("httpProxy", "HTTP_PROXY"), ("httpsProxy", "HTTPS_PROXY")):
        if os.getenv(target):
            continue
        val = get_secret(logical)
        if val:
            os.environ[target] = val


def get_email_list(which: str) -> list[str]:
    """Lista de destinatários de email. Ordem de resolução:
      1. code/destinatarios.py (não versionado)
      2. variável de ambiente mapeada em [env]
      3. [] (nenhum)
    `which`: 'destinatarios' (rascunho do relatório) ou 'outlook' (emails [OK]/[ERROR])."""
    attr = "EMAIL_DESTINATARIOS" if which == "destinatarios" else "OUTLOOK_TO"
    try:
        import destinatarios as _dest  # code/ está no sys.path em runtime
        vals = getattr(_dest, attr, None)
        if vals:
            return [str(v).strip() for v in vals if str(v).strip()]
    except ImportError:
        pass
    key = "emailDestinatarios" if which == "destinatarios" else "outlookTo"
    raw = get_secret(key)
    if raw:
        return [e.strip() for e in raw.replace(";", ",").split(",") if e.strip()]
    return []


# Objeto global exposto para importação direta: from lib.config import cfg
class _CfgProxy:
    """Proxy lazy: age como dict mas carrega config.toml apenas na primeira leitura."""

    def __getitem__(self, key):
        return _get_cfg()[key]

    def __contains__(self, key):
        return key in _get_cfg()

    def get(self, key, default=None):
        return _get_cfg().get(key, default)


cfg = _CfgProxy()
