"""
check_no_secrets.py — trava de segurança pré-publicação.

Rode ANTES de subir qualquer arquivo para o GitHub (repo público). Falha (exit 1)
se encontrar, nos arquivos que iriam para o repo:
  (a) valores de segredo reais (lidos do ambiente/.env via codigos/helpers/config.py);
  (b) arquivos proibidos soltos (.env, destinatarios.py, *.db);
  (c) padrões suspeitos de credencial hardcoded em código.

Este script NÃO contém nenhum segredo — os valores são lidos em runtime do
resolver de config. Rode no PC pessoal (com .env preenchido) para a checagem
de valores fazer sentido.

Uso:
    python codigos/scripts/check_no_secrets.py
"""
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers"))
from config import ObterSegredo, cfg

ROOT = Path(__file__).resolve().parents[3]   # a raiz do projeto

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


def Repositorios() -> list[Path]:
    """Os DOIS repos que esta trava protege: o projeto e a calculadora que ele consome.

    Nomeados, nao descobertos por glob. A pasta irma tem outros projetos do usuario, e
    varrer todos faria a trava reprovar por causa de codigo que nao tem nada a ver com
    esta publicacao — o mesmo cry-wolf que a lista IGNORE_* causava."""
    repos = [ROOT]
    calc = Path(cfg["paths"]["calculadoraDir"])
    calc = calc if calc.is_absolute() else (ROOT / calc)
    calc = calc.resolve()
    if (calc / ".git").exists():
        repos.append(calc)
    return repos


def ArquivosPublicaveis() -> list[Path]:
    """Exatamente o que o git COMMITARIA: rastreado + nao-rastreado nao-ignorado.

    Antes esta funcao mantinha uma lista `IGNORE_PARTS` escrita a mao, com o comentario
    'espelha o .gitignore'. Toda fonte de verdade duplicada deriva, e esta derivou: em
    03/09/2026 a trava reprovou um HTML de documentacao com a chave da FI Analytics que
    o git ja ignorava havia meses. Alarme que dispara no caso legitimo ensina a ignorar
    o alarme — e uma trava de seguranca ignorada nao e trava.

    Perguntar ao git elimina a classe inteira de erro: ele e a autoridade sobre o que
    seria publicado, e nunca fica dessincronizado de si mesmo."""
    arquivos: list[Path] = []
    for repo in Repositorios():
        for args in (["ls-files"], ["ls-files", "--others", "--exclude-standard"]):
            saida = subprocess.run(["git", "-C", str(repo), *args],
                                   capture_output=True, text=True, check=True)
            arquivos += [repo / linha for linha in saida.stdout.splitlines() if linha]
    return [p for p in arquivos if p.is_file()]


def Principal() -> None:
    problems: list[str] = []

    secrets = {}
    for k in SECRET_KEYS:
        v = ObterSegredo(k)
        if v and len(v) >= 6:
            secrets[k] = v

    files = ArquivosPublicaveis()
    for f in files:
        try:
            txt = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = f.relative_to(ROOT.parent)
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
