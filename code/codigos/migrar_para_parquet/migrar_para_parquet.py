"""
migrar_para_parquet.py
======================
Migracao unica: leva o conteudo dos .db SQLite para o armazenamento Parquet
descrito em Helpers/dados.py.

    ativos.db + trades.db  ->  <[dados].raiz>/<tabela>/[coluna=data/]dados.parquet

O destino sai do config ([dados] raiz): uma pasta local hoje, um s3://bucket
quando o acesso estiver liberado. O codigo e o mesmo.

O que acontece com o idTrade
----------------------------
Ele NAO vai junto. Era INTEGER PRIMARY KEY AUTOINCREMENT -- existia so porque o
SQLite o dava de graca, e fora dele ninguem gera esse numero. A ligacao entre
NegociosBrutos e NegociosProcessados passa a ser a chave natural que a propria B3
manda, `cdIdentificadorNegocio`, conferida unica nas 680.651 linhas da base. O
script traduz o vinculo na hora da migracao.

Confere no fim que nenhuma linha se perdeu, tabela a tabela, e ABORTA se perdeu.

CLI:
    python migrar_para_parquet.py                 # dry-run: so mostra o plano
    python migrar_para_parquet.py --executar
    python migrar_para_parquet.py --executar --tabelas InfoAtivos,FluxoAtivos
"""

import argparse
import sqlite3
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Helpers"))

import pandas as pd

import dados as D
from config import cfg
from logger import ObterLogger

NOME_SCRIPT = "migrar_para_parquet"

# De qual .db vem cada tabela (o split de 26/08).
ORIGEM = {
    "InfoAtivos": "dbAtivos", "FluxoAtivos": "dbAtivos",
    "AnbimaIndicativos": "dbAtivos", "MtmAnbima": "dbAtivos",
    "Outstanding": "dbAtivos",
    "NegociosBrutos": "dbTrades", "NegociosProcessados": "dbTrades",
}


def LerArgumentos() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Leva os .db SQLite para Parquet.")
    p.add_argument("--executar", action="store_true",
                   help="grava de verdade (sem a flag e dry-run)")
    p.add_argument("--tabelas", dest="tabelas", default=None,
                   help="lista separada por virgula (default: todas)")
    return p.parse_args()


def Conectar(chave: str) -> sqlite3.Connection:
    caminho = cfg["paths"][chave]
    if not Path(caminho).exists():
        raise FileNotFoundError(f"{chave} nao encontrado: {caminho}")
    return sqlite3.connect(caminho)


def MapaIdTrade(conn: sqlite3.Connection) -> pd.DataFrame:
    """idTrade -> cdIdentificadorNegocio, para traduzir o vinculo de
    NegociosProcessados (que hoje aponta pelo id do SQLite)."""
    return pd.read_sql(
        "SELECT idTrade, cdIdentificadorNegocio FROM NegociosBrutos", conn)


def Ler(tabela: str, conns: dict) -> pd.DataFrame:
    conn = conns[ORIGEM[tabela]]
    df = pd.read_sql(f"SELECT * FROM {tabela}", conn)

    if tabela == "NegociosProcessados":
        # Traduz idTrade -> cdIdentificadorNegocio e descarta a coluna antiga.
        mapa = MapaIdTrade(conns["dbTrades"])
        antes = len(df)
        df = df.merge(mapa, on="idTrade", how="left")
        orfaos = int(df["cdIdentificadorNegocio"].isna().sum())
        if orfaos:
            raise RuntimeError(
                f"NegociosProcessados: {orfaos} linha(s) sem NegociosBrutos "
                f"correspondente — a traducao do idTrade perderia esses dados.")
        assert len(df) == antes, "o merge do idTrade duplicou linhas"
    df = df.drop(columns=[c for c in ("idTrade",) if c in df.columns])
    return df


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


def Principal() -> bool:
    args = LerArgumentos()
    log = ObterLogger(NOME_SCRIPT)

    alvo = ([t.strip() for t in args.tabelas.split(",")] if args.tabelas
            else list(D.TABELAS))
    desconhecidas = [t for t in alvo if t not in D.TABELAS]
    if desconhecidas:
        log.error("tabela(s) desconhecida(s): %s", desconhecidas)
        return False

    conns = {c: Conectar(c) for c in ("dbAtivos", "dbTrades")}
    try:
        log.info("origem : %s", cfg["paths"]["dbAtivos"])
        log.info("         %s", cfg["paths"]["dbTrades"])
        log.info("destino: %s", D.Raiz())
        log.info("")

        contagens = {}
        for tabela in alvo:
            n = conns[ORIGEM[tabela]].execute(
                f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
            contagens[tabela] = n
            log.info("   %-22s %8d linhas  (particao: %s)",
                     tabela, n, D.PARTICAO[tabela] or "nenhuma")

        if not args.executar:
            log.info("")
            log.info("DRY-RUN -- nada foi escrito. Rode com --executar para valer.")
            return True

        log.info("")
        log.info("gravando...")
        gravadas = {}
        for tabela in alvo:
            gravadas[tabela] = Gravar(tabela, Ler(tabela, conns), log)

        # Conferencia: nenhuma linha pode ter sumido no caminho.
        divergentes = {t: (contagens[t], gravadas[t])
                       for t in alvo if contagens[t] != gravadas[t]}
        if divergentes:
            log.error("ABORTADO -- contagem nao bate (SQLite/Parquet): %s", divergentes)
            return False

        log.info("")
        log.info("conferencia OK -- %d tabelas, %d linhas",
                 len(alvo), sum(gravadas.values()))

        # Releitura pelo DuckDB: prova que o que foi escrito e legivel de volta.
        D.Invalidar()
        for tabela in alvo:
            lido = int(D.Escalar(f'SELECT COUNT(*) FROM "{tabela}"') or 0)
            if lido != gravadas[tabela]:
                log.error("%s: gravadas %d, mas o DuckDB le %d",
                          tabela, gravadas[tabela], lido)
                return False
        log.info("releitura pelo DuckDB OK nas %d tabelas", len(alvo))
        return True
    finally:
        for c in conns.values():
            c.close()


if __name__ == "__main__":
    try:
        ok = Principal()
    except Exception:
        print(traceback.format_exc())
        ok = False
    if not ok:
        sys.exit(1)
