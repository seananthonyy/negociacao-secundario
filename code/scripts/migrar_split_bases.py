"""
migrar_split_bases.py
=====================
Migracao unica: quebra o trades.db em DOIS arquivos.

    trades.db (antes)  ->  ativos.db  cadastro, fluxo, indicativas Anbima, MtM, outstanding
                           trades.db  NegociosBrutos + NegociosProcessados

O que faz, nesta ordem:
  1. backup do trades.db inteiro (API de backup do SQLite, segura com WAL);
  2. cria/abre o ativos.db e roda o Bootstrap do dominio 'ativos' (schema completo);
  3. copia as linhas das 5 tabelas de ativos + a trilha de SchemaVersao;
  4. confere a contagem tabela a tabela -- ABORTA se qualquer uma nao bater;
  5. so entao apaga as tabelas de ativos do trades.db e roda VACUUM.

Idempotente: rodar de novo com o split ja feito nao faz nada (as tabelas de ativos
nao existem mais no trades.db).

Sem --executar e um DRY-RUN: mostra o que faria e nao escreve nada.

CLI:
    python scripts/migrar_split_bases.py                # dry-run
    python scripts/migrar_split_bases.py --executar
    python scripts/migrar_split_bases.py --executar --sem-vacuum
"""

import argparse
import sqlite3
import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.config import cfg
from lib.db import (COLUNAS_REMOVIDAS, SCHEMA_VERSION, TABELAS_POR_DOMINIO,
                    Bootstrap, ObterConexao)
from lib.logger import ObterLogger

NOME_SCRIPT = "migrar_split_bases"

# As tabelas que MUDAM de arquivo. SchemaVersao e tratada a parte (vive nos dois).
TABELAS_A_MUDAR = tuple(t for t in TABELAS_POR_DOMINIO["ativos"] if t != "SchemaVersao")


def ColunasDe(conn: sqlite3.Connection, tabela: str, schema: str = "main") -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({tabela})")]


def TabelasExistentes(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def Contar(conn: sqlite3.Connection, tabela: str, schema: str = "main") -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {schema}.{tabela}").fetchone()[0]


def Backup(origem: Path, log) -> Path:
    """Copia integral via API de backup do SQLite (consistente mesmo com WAL)."""
    destinoDir = origem.parent / "backups"
    destinoDir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    destino = destinoDir / f"{origem.stem}_pre_split_{ts}.db"
    src = sqlite3.connect(str(origem))
    dst = sqlite3.connect(str(destino))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    log.info("backup: %s (%.1f MB)", destino, destino.stat().st_size / 1e6)
    return destino


def Copiar(conn: sqlite3.Connection, tabela: str, log) -> int:
    """Copia origem.<tabela> para main.<tabela>, so as colunas que as duas pontas tem
    em comum: a origem pode ter coluna aposentada e o destino coluna nova (nasce NULL)."""
    colsDestino = ColunasDe(conn, tabela, "main")
    colsOrigem = ColunasDe(conn, tabela, "origem")
    comuns = [c for c in colsDestino if c in colsOrigem]
    somenteDestino = [c for c in colsDestino if c not in colsOrigem]

    # Coluna que existe na origem e nao no destino seria DADO PERDIDO em silencio.
    # So e aceitavel se for coluna aposentada de proposito (COLUNAS_REMOVIDAS); qualquer
    # outra significa que a DDL esta atras do banco vivo -- e ai o destino e que esta
    # errado, nao o banco. Foi assim que apareceram InfoAtivos.cdISIN/vrQuantidadeEmissao/
    # dtEmissao, fora da DDL desde 23/07 e com ~4.500 linhas preenchidas cada.
    aposentadas = set(COLUNAS_REMOVIDAS.get(tabela, ()))
    somenteOrigem = [c for c in colsOrigem if c not in colsDestino]
    inesperadas = [c for c in somenteOrigem if c not in aposentadas]
    if inesperadas:
        raise RuntimeError(
            f"{tabela}: coluna(s) no banco vivo que a DDL nao preve: {inesperadas}. "
            f"Copiar assim PERDERIA esses dados. Acrescente-as a DDL (lib/db.py) "
            f"ou declare-as em COLUNAS_REMOVIDAS se forem para aposentar.")
    if somenteOrigem:
        log.info("%s: coluna(s) aposentada(s), nao copiada(s): %s", tabela, somenteOrigem)
    if somenteDestino:
        log.info("%s: coluna(s) nova(s) no destino, nascem NULL: %s", tabela, somenteDestino)

    lista = ", ".join(comuns)
    conn.execute(f"INSERT OR REPLACE INTO main.{tabela} ({lista}) "
                 f"SELECT {lista} FROM origem.{tabela}")
    return Contar(conn, tabela)


def Principal() -> bool:
    ap = argparse.ArgumentParser(description="Quebra o trades.db em ativos.db + trades.db")
    ap.add_argument("--executar", action="store_true",
                    help="grava de verdade (sem a flag e dry-run)")
    ap.add_argument("--sem-vacuum", dest="semVacuum", action="store_true",
                    help="nao roda VACUUM no trades.db no fim (o arquivo nao encolhe)")
    args = ap.parse_args()

    log = ObterLogger(NOME_SCRIPT)
    caminhoTrades = Path(cfg["paths"]["dbTrades"])
    caminhoAtivos = Path(cfg["paths"]["dbAtivos"])

    if not caminhoTrades.exists():
        log.error("trades.db nao encontrado em %s", caminhoTrades)
        return False

    # --- diagnostico: o split ja foi feito? ------------------------------------
    connOrigem = sqlite3.connect(str(caminhoTrades))
    existentes = TabelasExistentes(connOrigem)
    aMudar = [t for t in TABELAS_A_MUDAR if t in existentes]
    contagensAntes = {t: Contar(connOrigem, t) for t in aMudar}
    connOrigem.close()

    if not aMudar:
        log.info("nada a fazer: o trades.db nao tem mais tabela de ativos (split ja feito)")
        return True

    log.info("tabelas a mover (%d):", len(aMudar))
    for t in aMudar:
        log.info("   %-20s %10d linhas", t, contagensAntes[t])
    log.info("destino: %s", caminhoAtivos)

    if not args.executar:
        log.info("DRY-RUN -- nada foi escrito. Rode com --executar para valer.")
        return True

    # --- 1. backup --------------------------------------------------------------
    Backup(caminhoTrades, log)

    # --- 2. ativos.db com o schema completo do dominio -------------------------
    conn = ObterConexao(str(caminhoAtivos))
    Bootstrap(conn, "ativos")
    log.info("ativos.db criado/atualizado no schema v%d", SCHEMA_VERSION)

    # --- 3. copia ---------------------------------------------------------------
    conn.execute("ATTACH DATABASE ? AS origem", (str(caminhoTrades),))
    contagensDepois = {}
    for t in aMudar:
        contagensDepois[t] = Copiar(conn, t, log)
        log.info("copiado %-20s %10d linhas", t, contagensDepois[t])

    # SchemaVersao: a trilha ja percorrida vale para os dois arquivos.
    conn.execute(
        "INSERT OR IGNORE INTO main.SchemaVersao (vrVersao, dtAtualizacao, cdNota) "
        "SELECT vrVersao, dtAtualizacao, cdNota FROM origem.SchemaVersao")
    conn.commit()

    # --- 4. conferencia ---------------------------------------------------------
    divergentes = {t: (contagensAntes[t], contagensDepois[t])
                   for t in aMudar if contagensAntes[t] != contagensDepois[t]}
    if divergentes:
        conn.rollback()
        conn.close()
        log.error("ABORTADO -- contagem nao bate (antes/depois): %s", divergentes)
        log.error("o trades.db NAO foi tocado; apague o ativos.db e investigue")
        return False
    log.info("conferencia OK -- %d tabelas, %d linhas no total",
             len(aMudar), sum(contagensDepois.values()))

    conn.execute("DETACH DATABASE origem")
    conn.close()

    # --- 5. so agora tira as tabelas de ativos do trades.db --------------------
    connTrades = sqlite3.connect(str(caminhoTrades))
    connTrades.execute("PRAGMA foreign_keys=OFF")
    connTrades.execute("DROP TRIGGER IF EXISTS trgInfoAtivosInvalidaFluxo")
    for t in aMudar:
        connTrades.execute(f"DROP TABLE IF EXISTS {t}")
        log.info("removido do trades.db: %s", t)
    connTrades.commit()
    tamanhoAntes = caminhoTrades.stat().st_size
    if not args.semVacuum:
        log.info("VACUUM no trades.db (pode demorar)...")
        connTrades.execute("VACUUM")
        connTrades.commit()
    connTrades.close()

    log.info("trades.db: %.1f MB -> %.1f MB", tamanhoAntes / 1e6,
             caminhoTrades.stat().st_size / 1e6)
    log.info("ativos.db: %.1f MB", caminhoAtivos.stat().st_size / 1e6)
    log.info("SPLIT CONCLUIDO")
    return True


if __name__ == "__main__":
    try:
        ok = Principal()
    except Exception:
        print(traceback.format_exc())
        ok = False
    if not ok:
        sys.exit(1)
