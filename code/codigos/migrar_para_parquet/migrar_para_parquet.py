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
    p.add_argument("--sobrescrever", action="store_true",
                   help="autoriza gravar por cima de um destino que JA TEM dados. Sem "
                        "esta flag o script aborta nesse caso — ver ConferirDestinoVazio.")
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


def ConferirChaveNatural(conn: sqlite3.Connection, log) -> bool:
    """A chave que substitui o idTrade tem de existir e ser unica ANTES de migrar.

    O idTrade era AUTOINCREMENT: o SQLite garantia a unicidade de graca. Fora dele a
    chave e o `cdIdentificadorNegocio` que a B3 manda, e ninguem garante nada — a propria
    B3 ja mandou negocio SEM identificador (foi o que o fix de 31/08/2026 passou a
    descartar na entrada). Uma base montada ANTES desse fix pode ter essas linhas.

    Se elas passarem, o estrago e silencioso: linha com chave NULL nao casa com nada, e
    duas linhas com a mesma chave se anulam no primeiro Upsert/Mesclar — sem erro, sem
    log, com o total de volume mudando sozinho semanas depois. Por isso a conferencia e
    ANTES e ABORTA, em vez de virar um aviso no fim.
    """
    nulos = conn.execute(
        "SELECT COUNT(*) FROM NegociosBrutos "
        "WHERE cdIdentificadorNegocio IS NULL OR TRIM(cdIdentificadorNegocio) = ''"
    ).fetchone()[0]
    dups = conn.execute(
        "SELECT COUNT(*) FROM (SELECT cdIdentificadorNegocio FROM NegociosBrutos "
        " WHERE cdIdentificadorNegocio IS NOT NULL "
        " GROUP BY cdIdentificadorNegocio HAVING COUNT(*) > 1)"
    ).fetchone()[0]

    if not nulos and not dups:
        log.info("   chave natural OK: cdIdentificadorNegocio presente e unico")
        return True

    if nulos:
        log.error("   %d negocio(s) SEM cdIdentificadorNegocio. Eles nao tem chave no "
                  "Parquet — o fix de 31/08/2026 descarta esses na entrada, mas os que "
                  "ja estao na base precisam sair antes:", nulos)
        log.error("       DELETE FROM NegociosBrutos WHERE cdIdentificadorNegocio IS NULL "
                  "OR TRIM(cdIdentificadorNegocio) = '';")
    if dups:
        log.error("   %d cdIdentificadorNegocio REPETIDO. Duas linhas com a mesma chave "
                  "se anulam no primeiro Mesclar, sem erro nenhum. Investigar antes de "
                  "migrar:", dups)
        log.error("       SELECT cdIdentificadorNegocio, COUNT(*) FROM NegociosBrutos "
                  "GROUP BY 1 HAVING COUNT(*) > 1;")
    return False


def ConferirDestinoVazio(alvo: list[str], log, sobrescrever: bool) -> bool:
    """O destino tem de estar vazio — ou o usuario tem de dizer que sabe.

    `Gravar` usa GravarTudo/GravarDia, que SUBSTITUEM o arquivo (ou a particao) inteiro:
    e o certo para uma migracao unica sobre destino limpo, e destrutivo sobre um destino
    que ja tem dados. No banco os dois casos existem — a primeira carga e sobre vazio, e
    uma segunda tentativa depois de meio caminho andado nao e. Sem esta trava, a segunda
    apaga o que a primeira trouxe e o que o pipeline gravou entre as duas.

    Para MESCLAR em vez de substituir, o caminho e outro (D.Mesclar, com politica por
    coluna) — nao esta flag.
    """
    ocupadas = [(t, int(D.Escalar(f'SELECT COUNT(*) FROM "{t}"') or 0))
                for t in alvo if D.TemDados(t)]
    ocupadas = [(t, n) for t, n in ocupadas if n]
    if not ocupadas:
        log.info("   destino vazio nas %d tabela(s) — carga limpa", len(alvo))
        return True

    detalhe = ", ".join(f"{t} ({n} linhas)" for t, n in ocupadas)
    if sobrescrever:
        log.warning("   destino JA TEM dados e --sobrescrever foi passado: %s", detalhe)
        log.warning("   essas linhas serao SUBSTITUIDAS pelo conteudo do SQLite.")
        return True
    log.error("   destino JA TEM dados: %s", detalhe)
    log.error("   a gravacao substitui arquivo/particao inteiros e apagaria isso.")
    log.error("   Se e mesmo para sobrepor, repita com --sobrescrever.")
    return False


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

        log.info("")
        log.info("conferencias previas:")
        okChave = ("NegociosBrutos" not in alvo
                   or ConferirChaveNatural(conns["dbTrades"], log))
        okDestino = ConferirDestinoVazio(alvo, log, args.sobrescrever)

        if not args.executar:
            log.info("")
            log.info("DRY-RUN -- nada foi escrito. Rode com --executar para valer.")
            return okChave and okDestino

        if not (okChave and okDestino):
            log.error("ABORTADO -- conferencia previa reprovou (acima). Nada foi escrito.")
            return False

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
