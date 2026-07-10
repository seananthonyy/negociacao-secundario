import os
import sqlite3

from lib.config import cfg


DDL = """
-- ===== NegociosBrutos =====
CREATE TABLE IF NOT EXISTS NegociosBrutos (
    idTrade                 INTEGER PRIMARY KEY AUTOINCREMENT,
    cdIdentificadorNegocio  TEXT    NOT NULL UNIQUE,
    cdInstrumento           TEXT    NOT NULL,
    cdEmissor               TEXT    NOT NULL,
    cdTicker                TEXT    NOT NULL,
    vrQuantidade            INTEGER NOT NULL,
    vrPU                    REAL    NOT NULL,
    vrVolume                REAL    NOT NULL,
    vrTaxaNegocio           REAL    NULL,
    dtHorarioNegocio        TEXT    NOT NULL,
    dtNegocio               TEXT    NOT NULL,
    cdISIN                  TEXT    NULL,
    dtLiquidacao            TEXT    NOT NULL,
    cdSituacao              TEXT    NOT NULL,
    dtCriacao               TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    dtAtualizacao           TEXT    NULL
);
CREATE INDEX IF NOT EXISTS idxNegociosBrutosDtNegocio    ON NegociosBrutos(dtNegocio);
CREATE INDEX IF NOT EXISTS idxNegociosBrutosDtLiquidacao ON NegociosBrutos(dtLiquidacao);
CREATE INDEX IF NOT EXISTS idxNegociosBrutosCdTicker     ON NegociosBrutos(cdTicker);

-- ===== NegociosProcessados =====
CREATE TABLE IF NOT EXISTS NegociosProcessados (
    idTrade           INTEGER PRIMARY KEY,
    cdTicker          TEXT    NOT NULL,
    cdEmissor         TEXT    NOT NULL,
    dtNegocio         TEXT    NOT NULL,
    dtLiquidacao      TEXT    NOT NULL,
    vrQuantidade      INTEGER NOT NULL,
    vrPU              REAL    NOT NULL,
    vrVolume          REAL    NOT NULL,
    vrTaxaCalculada   REAL    NULL,
    cdFonteTaxa       TEXT    NULL,
    vrDuration        REAL    NULL,
    vrSpreadOver      REAL    NULL,
    idGrupoNegocio    TEXT    NULL,
    cdStatus          TEXT    NOT NULL,
    dtProcessamento   TEXT    NOT NULL,
    FOREIGN KEY (idTrade) REFERENCES NegociosBrutos(idTrade)
);
CREATE INDEX IF NOT EXISTS idxNegociosProcessadosDtLiquidacao ON NegociosProcessados(dtLiquidacao);
CREATE INDEX IF NOT EXISTS idxNegociosProcessadosDtNegocio    ON NegociosProcessados(dtNegocio);
CREATE INDEX IF NOT EXISTS idxNegociosProcessadosCdTicker     ON NegociosProcessados(cdTicker);
CREATE INDEX IF NOT EXISTS idxNegociosProcessadosCdStatus     ON NegociosProcessados(cdStatus);
CREATE INDEX IF NOT EXISTS idxNegociosProcessadosIdGrupo      ON NegociosProcessados(idGrupoNegocio);

-- ===== InfoAtivos =====
CREATE TABLE IF NOT EXISTS InfoAtivos (
    cdTicker           TEXT PRIMARY KEY,
    cdInstrumento      TEXT NULL,
    cdEmissor          TEXT NULL,
    dtVencimento       TEXT NULL,
    vrDuration         REAL NULL,
    dtAtualizacaoDuration TEXT NULL,
    cdIndexador        TEXT NULL,
    cdReferencia       TEXT NULL,
    cdFonteReferencia  TEXT NULL,    -- 'Anbima' | 'MatchRef' | 'FiAnalytics'
    vrTaxaEmissao        REAL NULL,
    vrVNE                REAL NULL,
    dtInicioRentabilidade TEXT NULL,
    dtAtualizacao      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idxInfoAtivosCdIndexador ON InfoAtivos(cdIndexador);

-- ===== AnbimaIndicativos =====
CREATE TABLE IF NOT EXISTS AnbimaIndicativos (
    cdTicker        TEXT NOT NULL,
    dtReferencia    TEXT NOT NULL,
    vrTaxaAnbima    REAL NULL,
    vrSpreadAnbima  REAL NULL,
    dtCriacao       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (cdTicker, dtReferencia)
);
CREATE INDEX IF NOT EXISTS idxAnbimaDtReferencia ON AnbimaIndicativos(dtReferencia);

-- ===== MtmAnbima =====
CREATE TABLE IF NOT EXISTS MtmAnbima (
    cdTicker     TEXT NOT NULL,
    dtReferencia TEXT NOT NULL,
    vrTaxa       REAL NOT NULL,
    vrDuration   REAL NULL,
    PRIMARY KEY (cdTicker, dtReferencia)
);
CREATE INDEX IF NOT EXISTS idxMtmAnbimaDtReferencia ON MtmAnbima(dtReferencia);

-- ===== FluxoAtivos =====
CREATE TABLE IF NOT EXISTS FluxoAtivos (
    cdTicker           TEXT NOT NULL,
    dtEvento           TEXT NOT NULL,
    vrPctAmortizacao   REAL NULL,
    vrPctIncorporacao  REAL NULL,
    dtAtualizacao      TEXT NOT NULL,
    PRIMARY KEY (cdTicker, dtEvento)
);
-- Índice COBRIDOR (covering): a query do add-in
--   SELECT dtEvento, vrPctAmortizacao, vrPctIncorporacao
--   FROM FluxoAtivos WHERE cdTicker = ? ORDER BY dtEvento
-- é resolvida 100% pelo índice (index-only scan), sem tocar a tabela.
-- O índice antigo idxFluxoAtivosCdTicker(cdTicker) era redundante com a PK
-- (cdTicker, dtEvento) — removido na migração do bootstrap().
CREATE INDEX IF NOT EXISTS idxFluxoAtivosCovering
    ON FluxoAtivos(cdTicker, dtEvento, vrPctAmortizacao, vrPctIncorporacao);

-- ===== Outstanding =====
CREATE TABLE IF NOT EXISTS Outstanding (
    cdTicker      TEXT NOT NULL,
    dtOutstanding TEXT NOT NULL,
    vrOutstanding REAL NULL,
    PRIMARY KEY (cdTicker, dtOutstanding)
);
CREATE INDEX IF NOT EXISTS idxOutstandingDtOutstanding ON Outstanding(dtOutstanding);
"""


# PRAGMAs de performance aplicados em toda conexao (leitura e escrita).
# - mmap_size: I/O por memoria mapeada, evita syscalls de read() pagina a pagina.
# - cache_size negativo: KiB (-65536 = 64 MiB) de cache de paginas por conexao.
# - temp_store=MEMORY: indices/sorts temporarios em RAM.
# - synchronous=NORMAL: seguro com WAL e mais rapido nas escritas do pipeline.
# - busy_timeout: espera ao inves de falhar se o pipeline estiver escrevendo.
PRAGMAS_PERF = (
    "PRAGMA mmap_size=268435456",   # 256 MiB
    "PRAGMA cache_size=-65536",     # 64 MiB
    "PRAGMA temp_store=MEMORY",
    "PRAGMA busy_timeout=5000",
)


def ObterConexao(caminhoBanco: str | None = None) -> sqlite3.Connection:
    """
    Abre conexao SQLite com WAL, foreign_keys e PRAGMAs de performance.
    db_path padrao: cfg["paths"]["dbFile"].
    Cria o diretorio pai se nao existir.
    """
    if caminhoBanco is None:
        caminhoBanco = cfg["paths"]["dbFile"]
    os.makedirs(os.path.dirname(caminhoBanco) or ".", exist_ok=True)
    conn = sqlite3.connect(caminhoBanco)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    for p in PRAGMAS_PERF:
        conn.execute(p)
    return conn


def ObterConexaoLeitura(caminhoBanco: str | None = None) -> sqlite3.Connection:
    """
    Conexao otimizada para LEITURA pura — pensada para o add-in da calculadora.

    Diferencas vs ObterConexao:
      - abre o arquivo em modo read-only (mode=ro): nunca bloqueia o pipeline,
        nunca cria/escreve WAL, e o SO pode otimizar o acesso;
      - query_only=ON como trava de seguranca;
      - NAO roda bootstrap() (sem DDL/migracao) — assume schema ja existente.

    Lookups tipicos (~36 us/ticker para InfoAtivos + FluxoAtivos juntos):
        cur = conn.execute(
            "SELECT cdIndexador, dtVencimento, vrTaxaEmissao, vrVNE, "
            "       dtInicioRentabilidade, cdReferencia, vrDuration "
            "FROM InfoAtivos WHERE cdTicker = ?", (ticker,))
        cur = conn.execute(
            "SELECT dtEvento, vrPctAmortizacao, vrPctIncorporacao "
            "FROM FluxoAtivos WHERE cdTicker = ? ORDER BY dtEvento", (ticker,))

    IMPORTANTE p/ o add-in: reutilize UMA conexao entre chamadas (abrir/fechar
    por lookup domina o tempo). As PRAGMAs abaixo sao genericas do SQLite —
    valem em qualquer linguagem (C#/.NET, VBA/ODBC etc.), nao so Python.
    """
    if caminhoBanco is None:
        caminhoBanco = cfg["paths"]["dbFile"]
    conn = sqlite3.connect(f"file:{caminhoBanco}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    for p in PRAGMAS_PERF:
        conn.execute(p)
    return conn


def Bootstrap(conn: sqlite3.Connection) -> None:
    """Executa DDL completo. Idempotente (IF NOT EXISTS em tudo)."""
    conn.executescript(DDL)
    # Migração segura: adiciona colunas novas em tabelas existentes
    for sql in [
        "ALTER TABLE InfoAtivos ADD COLUMN dtAtualizacaoDuration TEXT NULL",
        "ALTER TABLE InfoAtivos ADD COLUMN cdFonteReferencia TEXT NULL",
        "ALTER TABLE InfoAtivos ADD COLUMN vrTaxaEmissao REAL NULL",
        "ALTER TABLE InfoAtivos ADD COLUMN vrVNE REAL NULL",
        "ALTER TABLE InfoAtivos ADD COLUMN dtInicioRentabilidade TEXT NULL",
        "ALTER TABLE InfoAtivos ADD COLUMN cdISIN              TEXT NULL",
        "ALTER TABLE InfoAtivos ADD COLUMN vrQuantidadeEmissao REAL NULL",
        "ALTER TABLE InfoAtivos ADD COLUMN dtEmissao           TEXT NULL",
    ]:
        try:
            conn.execute(sql)
        except Exception:
            pass  # coluna já existe
    # Remove indice redundante (coberto pela PK composta de FluxoAtivos).
    conn.execute("DROP INDEX IF EXISTS idxFluxoAtivosCdTicker")
    conn.commit()
    # ANALYZE: estatisticas para o planejador escolher os indices certos.
    # So roda se ainda nao houver (sqlite_stat1) — evita custo a cada conexao.
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'"
    ).fetchone() is None:
        conn.execute("ANALYZE")
        conn.commit()


def ObterBanco(caminhoBanco: str | None = None) -> sqlite3.Connection:
    """Helper principal: abre conexao e garante o schema. Scripts usam so essa."""
    conn = ObterConexao(caminhoBanco)
    Bootstrap(conn)
    return conn
