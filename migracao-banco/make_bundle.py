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
        "Recria a árvore do projeto (codigos/, docs/, config/) na pasta atual.",
        "Seguro rodar por cima de uma base existente: só empacota arquivos",
        "versionados (código/vault), NUNCA o trades.db nem segredos. Além disso",
        "o extrator se recusa a sobrescrever *.db / .env / destinatarios.py / a",
        "skip-list — seus dados e credenciais ficam intactos ao importar nova versão.",
        "O config.toml existente também é preservado: a versão nova sai ao lado como",
        "config.toml.novo, para você comparar e mesclar (ele guarda customização local).",
        "Script que sumiu do projeto é APAGADO em codigos/, senão a",
        "versão velha ficaria rodando ao lado da nova, em silêncio.",
        "Gerado por make_bundle.py — não edite à mão.",
        '"""',
        "import base64, os",
        "",
        "# Arquivos que NUNCA devem ser sobrescritos se ja existirem (dados/segredos).",
        "# A skip-list e mantida A MAO no banco: sobrescreve-la apaga trabalho manual.",
        "# Casam por SUFIXO, entao seguem valendo onde quer que o arquivo esteja.",
        "# O '.db' saiu em 03/09/2026: o projeto nao usa mais SQLite. A base Parquet",
        "# dispensa guarda — ela nao e versionada, entao o bundle nunca a contem.",
        "PROTEGIDOS = ('.env', 'destinatarios.py', 'anbima_skip_tickers.csv')",
        "",
        "# Escritos AO LADO (com sufixo) quando ja existem, em vez de sobrescritos: sao",
        "# versionados (precisam receber chave nova) mas carregam ajuste local.",
        "MESCLAR = ('config/config.toml',)",
        "",
        "# Pastas onde um arquivo que sumiu do projeto deve ser APAGADO do destino.",
        "# So codigo — nunca data/, nunca segredo. Sem isto, um script removido no repo",
        "# sobrevive no banco e continua executavel (foi o caso do validar_fluxos).",
        "VARRER = ('codigos/scripts', 'codigos/helpers')",
        "",
        "def _protegido(path):",
        "    return any(path == p or path.endswith('/' + p) or path.endswith(p)",
        "               for p in PROTEGIDOS)",
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
        "    escritos, preservados, mesclar, apagados = 0, [], [], []",
        "    for path, b64 in FILES.items():",
        "        if _protegido(path) and os.path.exists(path):",
        "            preservados.append(path)",
        "            continue",
        "        destino = path",
        "        if path in MESCLAR and os.path.exists(path):",
        "            destino = path + '.novo'",
        "            mesclar.append(path)",
        "        d = os.path.dirname(destino)",
        "        if d:",
        "            os.makedirs(d, exist_ok=True)",
        "        with open(destino, 'wb') as f:",
        "            f.write(base64.b64decode(b64))",
        "        escritos += 1",
        "    # Varredura de orfaos: .py em VARRER que o bundle nao traz mais.",
        "    for pasta in VARRER:",
        "        if not os.path.isdir(pasta):",
        "            continue",
        "        for raiz, _dirs, arqs in os.walk(pasta):",
        "            for nome in arqs:",
        "                if not nome.endswith('.py'):",
        "                    continue",
        "                rel = os.path.join(raiz, nome).replace(os.sep, '/')",
        "                if rel not in FILES:",
        "                    os.remove(rel)",
        "                    apagados.append(rel)",
        "    print(f'Extraidos {escritos} arquivos na pasta atual.')",
        "    if preservados:",
        "        print(f'Preservados (nao sobrescritos): {preservados}')",
        "    if mesclar:",
        "        print(f'ATENCAO — ja existiam, versao nova salva como .novo: {mesclar}')",
        "        print('  Compare e mescle a mao (guardam customizacao local).')",
        "    if apagados:",
        "        print(f'Removidos (sairam do projeto): {apagados}')",
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
