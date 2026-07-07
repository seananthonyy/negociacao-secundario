"""
make_bundle.py — gera o bundle_banco.py (arquivo único auto-extraível).

Empacota TODOS os arquivos versionados (git ls-files) em base64 dentro de um
único script Python. No banco, baixa-se só o bundle_banco.py pela web do GitHub
e roda `python bundle_banco.py` — ele recria a árvore de pastas do projeto.

Não inclui .env / destinatarios.py / *.db (não são versionados). Regere após
mudar arquivos:  python make_bundle.py
"""

import base64
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "bundle_banco.py"
EXCLUIR = {"bundle_banco.py"}  # nunca empacota a si mesmo


def _arquivos_versionados() -> list[str]:
    saida = subprocess.run(
        ["git", "ls-files"], cwd=str(ROOT), capture_output=True, text=True, check=True
    ).stdout
    return [p for p in saida.splitlines() if p and p not in EXCLUIR]


def main() -> None:
    arquivos = _arquivos_versionados()
    linhas = [
        '"""',
        "bundle_banco.py — AUTO-EXTRAÍVEL. Baixe só este arquivo e rode:",
        "    python bundle_banco.py",
        "Recria a árvore do projeto (code/, vault/, docs) na pasta atual.",
        "Seguro rodar por cima de uma base existente: só empacota arquivos",
        "versionados (código/vault), NUNCA o trades.db nem segredos. Além disso",
        "o extrator se recusa a sobrescrever *.db / .env / destinatarios.py caso",
        "existam — seus dados e credenciais ficam intactos ao importar nova versão.",
        "Gerado por make_bundle.py — não edite à mão.",
        '"""',
        "import base64, os",
        "",
        "# Arquivos que NUNCA devem ser sobrescritos se ja existirem (dados/segredos).",
        "PROTEGIDOS = ('.db', '.env', 'destinatarios.py')",
        "",
        "def _protegido(path):",
        "    return any(path == p or path.endswith('/' + p) or path.endswith(p)",
        "               for p in PROTEGIDOS) or path.endswith('.db')",
        "",
        "FILES = {",
    ]
    total_bytes = 0
    for rel in arquivos:
        data = (ROOT / rel).read_bytes()
        total_bytes += len(data)
        b64 = base64.b64encode(data).decode("ascii")
        linhas.append(f"    {rel!r}: {b64!r},")
    linhas += [
        "}",
        "",
        "def main():",
        "    escritos, preservados = 0, []",
        "    for path, b64 in FILES.items():",
        "        if _protegido(path) and os.path.exists(path):",
        "            preservados.append(path)",
        "            continue",
        "        d = os.path.dirname(path)",
        "        if d:",
        "            os.makedirs(d, exist_ok=True)",
        "        with open(path, 'wb') as f:",
        "            f.write(base64.b64decode(b64))",
        "        escritos += 1",
        "    print(f'Extraidos {escritos} arquivos na pasta atual.')",
        "    if preservados:",
        "        print(f'Preservados (nao sobrescritos): {preservados}')",
        "    print('Seu trades.db (se existir) NAO foi tocado.')",
        "    print('Proximo passo: leia INSTALACAO_BANCO.md (ou peca ao Claude).')",
        "",
        "if __name__ == '__main__':",
        "    main()",
        "",
    ]
    OUT.write_text("\n".join(linhas), encoding="utf-8")
    print(f"bundle_banco.py gerado: {len(arquivos)} arquivos, "
          f"{total_bytes/1024:.0f} KB de conteudo, {OUT.stat().st_size/1024:.0f} KB no bundle.")


if __name__ == "__main__":
    main()
