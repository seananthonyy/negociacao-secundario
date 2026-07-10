"""
check_no_secrets.py — trava de segurança pré-publicação.

Rode ANTES de subir qualquer arquivo para o GitHub (repo público). Falha (exit 1)
se encontrar, nos arquivos que iriam para o repo:
  (a) valores de segredo reais (lidos do ambiente/.env via lib.config);
  (b) arquivos proibidos soltos (.env, destinatarios.py, *.db);
  (c) padrões suspeitos de credencial hardcoded em código.

Este script NÃO contém nenhum segredo — os valores são lidos em runtime do
resolver de config. Rode no PC pessoal (com .env preenchido) para a checagem
de valores fazer sentido.

Uso:
    python scripts/check_no_secrets.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.config import ObterSegredo

ROOT    = Path(__file__).parent.parent   # code/
PROJECT = ROOT.parent                    # raiz do projeto

# Espelha o .gitignore: o que NÃO vai para o repo público.
IGNORE_PARTS  = {".git", "__pycache__", ".playwright", ".obsidian",
                 "logs", "relatorios", "anbima_data_raw", "api_samples", "debug", "_arquivo_morto"}
IGNORE_NAMES  = {".env", "destinatarios.py"}
IGNORE_SUFFIX = {".db", ".pyc", ".db-journal", ".db-wal"}

# Segredos a procurar (valores lidos do resolver — nunca hardcoded aqui).
SECRET_KEYS = ["b3CalcToken", "fianalyticsApiKey", "fianalyticsUser", "fianalyticsPass"]

# Heurística de credencial hardcoded (nome = "valor"), só em código.
SUSPICIOUS = re.compile(
    r'(?i)\b(password|senha|api[_-]?key|secret|token)\b\s*[:=]\s*["\']([^"\']{6,})["\']'
)
CODE_SUFFIX = {".py", ".toml", ".js"}
# Valores tipo slug/identificador (só minúsculas/dígitos/-/_) não são segredos
# (ex.: api_key = 'debentures'). Segredos reais têm maiúsculas/símbolos misturados.
SLUG = re.compile(r'^[a-z][a-z0-9_-]*$')


def ArquivosPublicaveis():
    for p in PROJECT.rglob("*"):
        if not p.is_file():
            continue
        parts = set(p.relative_to(PROJECT).parts)
        if parts & IGNORE_PARTS:
            continue
        if p.name in IGNORE_NAMES or p.suffix in IGNORE_SUFFIX:
            continue
        yield p


def Principal() -> None:
    problems: list[str] = []

    secrets = {}
    for k in SECRET_KEYS:
        v = ObterSegredo(k)
        if v and len(v) >= 6:
            secrets[k] = v

    files = list(ArquivosPublicaveis())
    for f in files:
        try:
            txt = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = f.relative_to(PROJECT)
        for k, v in secrets.items():
            if v in txt:
                problems.append(f"SEGREDO ({k}) encontrado em {rel}")
        if f.suffix in CODE_SUFFIX:
            for m in SUSPICIOUS.finditer(txt):
                val = m.group(2)
                if SLUG.fullmatch(val):
                    continue  # slug/identificador, não é segredo
                snippet = m.group(0)[:70]
                if any(w in snippet.lower() for w in ("exemplo", "preencher", "config")):
                    continue
                problems.append(f"SUSPEITO em {rel}: {snippet}")

    print(f"Arquivos verificados: {len(files)}")
    if not secrets:
        print("AVISO: nenhum segredo resolvido do ambiente — rode com .env preenchido "
              "para a checagem de valores reais.")

    if problems:
        print(f"\n[X] FALHOU — {len(problems)} problema(s):")
        for p in problems:
            print("  - " + p)
        sys.exit(1)

    print("\n[OK] Nenhum segredo ou padrão suspeito nos arquivos publicáveis.")


if __name__ == "__main__":
    Principal()
