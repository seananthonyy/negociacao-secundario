"""
migrar_para_parquet.py
======================
Carga UNICA: leva o conteudo dos .db SQLite para o armazenamento Parquet descrito em
codigos/helpers/dados.py.

    <origem>/*.db   ->   <[dados] raiz>/<tabela>/[coluna=data/]dados.parquet

Nao assume layout de origem
---------------------------
Descobre as tabelas lendo o `sqlite_master` de cada `.db` da pasta e mapeia **por nome
de tabela**, nunca por qual arquivo ela esta. Assim funciona tanto com um `trades.db`
unico (o layout antigo) quanto com o par `ativos.db` + `trades.db` (o split de
26/08/2026), sem configuracao. Tabela que o ESQUEMA nao conhece e reportada e ignorada
-- nada entra na base sem esquema declarado.

A conferencia de coluna a coluna e o ponto do script
----------------------------------------------------
`dados.Conformar()` descarta coluna que nao esta no esquema e cria como NULL a que
falta -- SEM erro e SEM log. E o comportamento certo no uso diario e veneno numa
migracao: se a base de origem for anterior ao rename de 29/06/2026 (`TradesRaw` ->
`NegociosBrutos`, `vrRate` -> `vrTaxaCalculada`), a conversao gravaria a coluna de
taxa inteira vazia e o relatorio sairia em branco sem nada quebrar.

Por isso o dry-run compara os dois sentidos e ABORTA quando falta no SQLite uma coluna
que o ESQUEMA declara. Coluna a mais na origem so avisa (ela e descartada de proposito).

Mais duas conferencias, antes de escrever qualquer coisa:

  chave natural    `cdIdentificadorNegocio` presente e UNICO em NegociosBrutos. O
                   idTrade (AUTOINCREMENT) morreu com o SQLite, e fora dele ninguem
                   gera esse numero. Base anterior a 31/08/2026 pode ter negocio com
                   "-" nesse campo, e chave nula se anula em silencio no primeiro
                   Mesclar.
  destino vazio    a gravacao substitui arquivo/particao inteiros. Se a pasta Parquet
                   ja tem dado, aborta e exige --sobrescrever explicito -- senao uma
                   segunda tentativa apaga o que a primeira trouxe.

Traducao do idTrade
-------------------
Em `NegociosProcessados` o vinculo com `NegociosBrutos` era o `idTrade`. O script o
traduz para `cdIdentificadorNegocio` e aborta se sobrar linha orfa.

Conferencia final
-----------------
Contagem SQLite x Parquet, tabela a tabela (aborta se sumiu linha), e releitura pelo
DuckDB (prova que o que foi escrito volta a ser lido).

CLI:
    python codigos/scripts/migrar_para_parquet/migrar_para_parquet.py --origem D:/dbs
    python codigos/scripts/migrar_para_parquet/migrar_para_parquet.py --origem D:/dbs --executar
    python codigos/scripts/migrar_para_parquet/migrar_para_parquet.py --origem D:/dbs --executar --tabelas InfoAtivos,FluxoAtivos
"""

import argparse
import sqlite3
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers"))

import pandas as pd

import dados as D
from logger import ObterLogger

NOME_SCRIPT = "migrar_para_parquet"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def LerArgumentos() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Carga unica: leva os .db SQLite para o Parquet do projeto.")
    p.add_argument("--origem", required=True, metavar="PASTA",
                   help="Pasta com os arquivos .db. Todos sao varridos.")
    p.add_argument("--executar", action="store_true",
                   help="Grava de verdade. Sem a flag e dry-run: so descobre e confere.")
    p.add_argument("--tabelas", default=None,
                   help="Lista separada por virgula (default: tudo que for reconhecido).")
    p.add_argument("--sobrescrever", action="store_true",
                   help="Autoriza gravar por cima de um destino que JA TEM dados. Sem "
                        "esta flag o script aborta nesse caso.")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Descoberta
# ---------------------------------------------------------------------------

def Descobrir(pastaOrigem: Path, log) -> dict:
    """{nomeTabela: (caminhoDoDb, nrLinhas)} para tudo que o ESQUEMA reconhece.

    Mapeia por NOME DE TABELA, nunca por arquivo: o mesmo script serve ao layout
    antigo (um trades.db) e ao split em ativos.db + trades.db."""
    encontradas, ignoradas = {}, []
    arquivos = sorted(pastaOrigem.glob("*.db"))
    if not arquivos:
        raise FileNotFoundError(f"nenhum .db em {pastaOrigem}")

    for db in arquivos:
        con = sqlite3.connect(db)
        try:
            nomes = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1")]
            for tabela in nomes:
                n = con.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
                if tabela in D.TABELAS:
                    if tabela in encontradas:
                        log.warning("   %s aparece em mais de um .db — fico com %s",
                                    tabela, encontradas[tabela][0].name)
                        continue
                    encontradas[tabela] = (db, n)
                else:
                    ignoradas.append((db.name, tabela, n))
        finally:
            con.close()

    log.info("origem: %s", pastaOrigem)
    for db in arquivos:
        log.info("   %s  (%d MB)", db.name, db.stat().st_size // 1024 // 1024)
    log.info("")
    log.info("tabelas reconhecidas:")
    for tabela, (db, n) in sorted(encontradas.items()):
        log.info("   %-22s %9d linhas   de %s", tabela, n, db.name)
    if ignoradas:
        log.warning("tabelas IGNORADAS (sem esquema declarado):")
        for arq, tabela, n in ignoradas:
            log.warning("   %-22s %9d linhas   de %s", tabela, n, arq)
    return encontradas


# ---------------------------------------------------------------------------
# Conferencias previas
# ---------------------------------------------------------------------------

def ConferirColunas(tabela: str, db: Path, log) -> bool:
    """Compara as colunas do SQLite com o ESQUEMA, nos DOIS sentidos.

    Falta no SQLite uma coluna do esquema  -> ABORTA (viraria coluna vazia)
    Sobra no SQLite uma coluna desconhecida -> avisa (e descartada de proposito)"""
    con = sqlite3.connect(db)
    try:
        naOrigem = {r[1] for r in con.execute(f"PRAGMA table_info({tabela})")}
    finally:
        con.close()

    noEsquema = {c.name for c in D.ESQUEMA[tabela]}
    # O idTrade e traduzido, nao migrado: nao conta como coluna a mais.
    sobrando = naOrigem - noEsquema - {"idTrade"}
    faltando = noEsquema - naOrigem
    # cdIdentificadorNegocio nasce da traducao do idTrade, entao pode faltar na origem.
    if tabela == "NegociosProcessados" and "idTrade" in naOrigem:
        faltando -= {"cdIdentificadorNegocio"}

    if sobrando:
        log.warning("   %s: colunas a mais na origem, serao DESCARTADAS: %s",
                    tabela, sorted(sobrando))
    if faltando:
        log.error("   %s: FALTAM na origem colunas do esquema: %s", tabela, sorted(faltando))
        log.error("      Migrar assim gravaria essas colunas VAZIAS, sem erro. Verifique "
                  "se a base de origem e de uma versao anterior do schema.")
        return False
    log.info("   %s: %d colunas conferidas", tabela, len(noEsquema))
    return True


def ConferirChaveNatural(db: Path, log) -> bool:
    """A chave que substitui o idTrade tem de existir e ser unica ANTES de migrar."""
    con = sqlite3.connect(db)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(NegociosBrutos)")}
        if "cdIdentificadorNegocio" not in cols:
            log.error("   NegociosBrutos nao tem cdIdentificadorNegocio — sem chave "
                      "natural nao ha como migrar.")
            return False
        nulos = con.execute(
            "SELECT COUNT(*) FROM NegociosBrutos WHERE cdIdentificadorNegocio IS NULL "
            "OR TRIM(cdIdentificadorNegocio) = ''").fetchone()[0]
        dups = con.execute(
            "SELECT COUNT(*) FROM (SELECT cdIdentificadorNegocio FROM NegociosBrutos "
            " WHERE cdIdentificadorNegocio IS NOT NULL "
            " GROUP BY cdIdentificadorNegocio HAVING COUNT(*) > 1)").fetchone()[0]
    finally:
        con.close()

    if not nulos and not dups:
        log.info("   chave natural OK: cdIdentificadorNegocio presente e unico")
        return True
    if nulos:
        log.error("   %d negocio(s) SEM cdIdentificadorNegocio. Limpe antes:", nulos)
        log.error("      DELETE FROM NegociosBrutos WHERE cdIdentificadorNegocio IS NULL "
                  "OR TRIM(cdIdentificadorNegocio) = '';")
    if dups:
        log.error("   %d cdIdentificadorNegocio REPETIDO. Duas linhas com a mesma chave "
                  "se anulam no primeiro Mesclar, sem erro:", dups)
        log.error("      SELECT cdIdentificadorNegocio, COUNT(*) FROM NegociosBrutos "
                  "GROUP BY 1 HAVING COUNT(*) > 1;")
    return False


def ConferirDestinoVazio(alvo: list, log, sobrescrever: bool) -> bool:
    """A gravacao substitui arquivo/particao inteiros — o destino tem de estar vazio,
    ou o usuario tem de dizer que sabe."""
    ocupadas = [(t, int(D.Escalar(f'SELECT COUNT(*) FROM "{t}"') or 0))
                for t in alvo if D.TemDados(t)]
    ocupadas = [(t, n) for t, n in ocupadas if n]
    if not ocupadas:
        log.info("   destino vazio nas %d tabela(s) — carga limpa", len(alvo))
        return True

    detalhe = ", ".join(f"{t} ({n} linhas)" for t, n in ocupadas)
    if sobrescrever:
        log.warning("   destino JA TEM dados e --sobrescrever foi passado: %s", detalhe)
        return True
    log.error("   destino JA TEM dados: %s", detalhe)
    log.error("   a gravacao substitui arquivo/particao inteiros e apagaria isso.")
    log.error("   Se e mesmo para sobrepor, repita com --sobrescrever.")
    return False


# ---------------------------------------------------------------------------
# Leitura e gravacao
# ---------------------------------------------------------------------------

def Ler(tabela: str, db: Path, dbNegocios: Path | None) -> pd.DataFrame:
    df = pd.read_sql(f"SELECT * FROM {tabela}", sqlite3.connect(db))

    if tabela == "NegociosProcessados" and "idTrade" in df.columns:
        # Traduz o vinculo: no SQLite era o idTrade, que nao existe mais.
        mapa = pd.read_sql("SELECT idTrade, cdIdentificadorNegocio FROM NegociosBrutos",
                           sqlite3.connect(dbNegocios or db))
        antes = len(df)
        df = df.merge(mapa, on="idTrade", how="left")
        orfaos = int(df["cdIdentificadorNegocio"].isna().sum())
        if orfaos:
            raise RuntimeError(
                f"NegociosProcessados: {orfaos} linha(s) sem NegociosBrutos "
                f"correspondente — a traducao do idTrade perderia esses dados.")
        assert len(df) == antes, "o merge do idTrade duplicou linhas"

    return df.drop(columns=[c for c in ("idTrade",) if c in df.columns])


def Gravar(tabela: str, df: pd.DataFrame, log) -> int:
    coluna = D.PARTICAO[tabela]
    if not coluna:
        n = D.GravarTudo(tabela, df)
        log.info("   %-22s %8d linhas -> arquivo unico", tabela, n)
        return n

    total = 0
    datas = sorted(x for x in df[coluna].dropna().unique())
    for data in datas:
        total += D.GravarDia(tabela, df[df[coluna] == data], str(data))
    log.info("   %-22s %8d linhas -> %d particao(oes) por %s",
             tabela, total, len(datas), coluna)
    return total


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Principal() -> bool:
    args = LerArgumentos()
    log = ObterLogger(NOME_SCRIPT)

    origem = Path(args.origem).resolve()
    encontradas = Descobrir(origem, log)
    if not encontradas:
        log.error("nenhuma tabela reconhecida em %s", origem)
        return False

    alvo = ([t.strip() for t in args.tabelas.split(",")] if args.tabelas
            else sorted(encontradas))
    desconhecidas = [t for t in alvo if t not in encontradas]
    if desconhecidas:
        log.error("tabela(s) pedida(s) mas nao encontrada(s): %s", desconhecidas)
        return False

    log.info("")
    log.info("destino: %s", D.Raiz())
    log.info("")
    log.info("conferencias previas:")

    okColunas = all(ConferirColunas(t, encontradas[t][0], log) for t in alvo)
    dbNegocios = encontradas.get("NegociosBrutos", (None,))[0]
    okChave = ("NegociosBrutos" not in alvo
               or ConferirChaveNatural(dbNegocios, log))
    okDestino = ConferirDestinoVazio(alvo, log, args.sobrescrever)

    if not args.executar:
        log.info("")
        log.info("DRY-RUN — nada foi escrito. Rode com --executar para valer.")
        return okColunas and okChave and okDestino

    if not (okColunas and okChave and okDestino):
        log.error("ABORTADO — conferencia previa reprovou (acima). Nada foi escrito.")
        return False

    log.info("")
    log.info("gravando...")
    gravadas = {t: Gravar(t, Ler(t, encontradas[t][0], dbNegocios), log) for t in alvo}

    divergentes = {t: (encontradas[t][1], gravadas[t])
                   for t in alvo if encontradas[t][1] != gravadas[t]}
    if divergentes:
        log.error("ABORTADO — contagem nao bate (SQLite/Parquet): %s", divergentes)
        return False
    log.info("")
    log.info("conferencia OK — %d tabelas, %d linhas", len(alvo), sum(gravadas.values()))

    # Releitura pelo DuckDB: prova que o que foi escrito e legivel de volta.
    D.Invalidar()
    for tabela in alvo:
        lido = int(D.Escalar(f'SELECT COUNT(*) FROM "{tabela}"') or 0)
        if lido != gravadas[tabela]:
            log.error("%s: gravadas %d, mas o DuckDB le %d", tabela, gravadas[tabela], lido)
            return False
    log.info("releitura pelo DuckDB OK nas %d tabelas", len(alvo))
    return True


if __name__ == "__main__":
    try:
        ok = Principal()
    except Exception:
        print(traceback.format_exc())
        ok = False
    if not ok:
        sys.exit(1)
