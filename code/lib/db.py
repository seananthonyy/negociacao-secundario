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
    -- Convencao de amortizacao ('saldo_original' | 'saldo_restante'), vinda do
    -- cadastro (B3/Anbima). NULL -> a calc cai na heuristica de inferencia (soma
    -- dos %amort ~ 100%), que e AMBIGUA. Ver ResolverTipoAmort na calculadora_rf.
    cdTipoAmortizacao  TEXT NULL,
    cdReferencia       TEXT NULL,
    cdFonteReferencia  TEXT NULL,    -- 'Anbima' | 'MatchRef' | 'FiAnalytics'
    vrTaxaEmissao        REAL NULL,
    vrVNE                REAL NULL,
    dtInicioRentabilidade TEXT NULL,
    -- Dia do mes em que o ativo aniversaria (o NIk/NIk-1 do IPCA vira). SO IPCA:
    -- nos demais indexadores o VNA nao sofre correcao e a calc ignora o parametro.
    -- Dia 15 e convencao de NTN-B; a debenture aniversaria no dia das SUAS datas de
    -- pagamento. Errar isso faz a calc IGNORAR os eventos do fluxo, em silencio
    -- (SSRU11, aniversario 28: PU 15.403 contra 9.738 da B3).
    vrAniversario      INTEGER NULL,
    -- Procedencia do cadastro. vrVNE + dtInicioRentabilidade + FluxoAtivos formam um
    -- PACOTE INDIVISIVEL: a B3 pre-capitaliza a carencia dentro do VNE e nao emite
    -- evento de incorporacao; a Anbima traz o VNE cru e a incorporacao como evento.
    -- Misturar as duas fontes conta a capitalizacao DUAS VEZES, sem erro nenhum.
    cdFonteCadastro    TEXT NULL,       -- 'B3' | 'AnbimaData'
    dtAtualizacao      TEXT NOT NULL,
    -- Validacao do fluxo (contrato com a calculadora de renda fixa).
    -- O INGESTOR (este projeto) so escreve stTemFluxo e ZERA as demais;
    -- quem VALIDA (marca 1 / grava data / fonte) e o validar_fluxos.py.
    stTemFluxo            INTEGER NOT NULL DEFAULT 0,  -- 1 tem linha em FluxoAtivos
    stFluxoValidado       INTEGER NOT NULL DEFAULT 0,  -- 1 fluxo conferido contra a fonte
    dtValidacaoFluxo      TEXT NULL,                   -- ISO da validacao OK
    cdFonteValidacaoFluxo TEXT NULL,                   -- 'B3' | 'FiAnalytics' | 'Manual'
    dtUltimaTentativa     TEXT NULL                    -- ISO da ultima tentativa (validou ou nao)
);
-- idxInfoAtivosStFluxoValidado e criado no Bootstrap(), DEPOIS dos ALTER TABLE:
-- numa base que ja existe, a coluna so aparece na migracao.
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


# Colunas de InfoAtivos que DEFINEM o fluxo de caixa do ativo: se qualquer uma
# delas mudar de valor, a validacao do fluxo (stFluxoValidado) deixa de valer.
# dtVencimento NAO entra: e deduzido do proprio fluxo (ultimo evento).
COLS_INVALIDAM_FLUXO = (
    "dtInicioRentabilidade",
    "vrTaxaEmissao",
    "cdIndexador",
    "vrVNE",
    # A convencao de amortizacao muda a base da amortizacao (logo o VNA/PU), entao
    # muda o ConferirSaldo da validacao — invalida.
    "cdTipoAmortizacao",
    # Nao define o fluxo, mas define como a calc o LE: com o aniversario errado, os
    # eventos nao caem no aniversario e sao ignorados. Muda o VNA, logo muda o
    # ConferirSaldo — que faz parte da validacao. Entao invalida.
    "vrAniversario",
)

SQL_ZERA_VALIDACAO = """
    stFluxoValidado       = 0,
    dtValidacaoFluxo      = NULL,
    cdFonteValidacaoFluxo = NULL,
    dtUltimaTentativa     = NULL
"""

# Trigger de invalidacao. Fica no BANCO (nao nos scrapers) porque as colunas de
# COLS_INVALIDAM_FLUXO tem 4 writers (anbima_data, fianalytics_planilha,
# anbima_debentures, anbima_cri_cra) e todos usam ON CONFLICT DO UPDATE, que
# dispara AFTER UPDATE. Um lugar so, e cobre qualquer script futuro.
#
# `IS NOT` e comparacao null-safe: so dispara em mudanca REAL de valor
# (reescrever o mesmo valor, ou COALESCE que preserva o existente, nao invalida;
# preencher um NULL invalida, porque isso muda o calculo).
#
# O UPDATE de dentro do trigger nao se re-dispara: recursive_triggers e OFF por
# padrao no SQLite e, mesmo ligado, ele nao toca nenhuma coluna do WHEN.
DDL_TRIGGERS = f"""
CREATE TRIGGER IF NOT EXISTS trgInfoAtivosInvalidaFluxo
AFTER UPDATE OF {", ".join(COLS_INVALIDAM_FLUXO)} ON InfoAtivos
FOR EACH ROW
WHEN {" OR ".join(f"old.{c} IS NOT new.{c}" for c in COLS_INVALIDAM_FLUXO)}
BEGIN
    UPDATE InfoAtivos
       SET {SQL_ZERA_VALIDACAO}
     WHERE cdTicker = new.cdTicker;
END;
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
    novas = set()
    for coluna, sql in [
        ("dtAtualizacaoDuration", "ALTER TABLE InfoAtivos ADD COLUMN dtAtualizacaoDuration TEXT NULL"),
        ("cdFonteReferencia",     "ALTER TABLE InfoAtivos ADD COLUMN cdFonteReferencia TEXT NULL"),
        ("vrTaxaEmissao",         "ALTER TABLE InfoAtivos ADD COLUMN vrTaxaEmissao REAL NULL"),
        ("vrVNE",                 "ALTER TABLE InfoAtivos ADD COLUMN vrVNE REAL NULL"),
        ("dtInicioRentabilidade", "ALTER TABLE InfoAtivos ADD COLUMN dtInicioRentabilidade TEXT NULL"),
        ("cdISIN",                "ALTER TABLE InfoAtivos ADD COLUMN cdISIN              TEXT NULL"),
        ("vrQuantidadeEmissao",   "ALTER TABLE InfoAtivos ADD COLUMN vrQuantidadeEmissao REAL NULL"),
        ("dtEmissao",             "ALTER TABLE InfoAtivos ADD COLUMN dtEmissao           TEXT NULL"),
        # Validacao de fluxo. Os dois INTEGER tem DEFAULT NOT NULL, entao o
        # ALTER ja preenche as linhas existentes com 0 (= nada validado ainda).
        ("stTemFluxo",            "ALTER TABLE InfoAtivos ADD COLUMN stTemFluxo            INTEGER NOT NULL DEFAULT 0"),
        ("stFluxoValidado",       "ALTER TABLE InfoAtivos ADD COLUMN stFluxoValidado       INTEGER NOT NULL DEFAULT 0"),
        ("dtValidacaoFluxo",      "ALTER TABLE InfoAtivos ADD COLUMN dtValidacaoFluxo      TEXT NULL"),
        ("cdFonteValidacaoFluxo", "ALTER TABLE InfoAtivos ADD COLUMN cdFonteValidacaoFluxo TEXT NULL"),
        ("dtUltimaTentativa",     "ALTER TABLE InfoAtivos ADD COLUMN dtUltimaTentativa     TEXT NULL"),
        ("vrAniversario",         "ALTER TABLE InfoAtivos ADD COLUMN vrAniversario   INTEGER NULL"),
        ("cdFonteCadastro",       "ALTER TABLE InfoAtivos ADD COLUMN cdFonteCadastro TEXT NULL"),
        ("cdTipoAmortizacao",     "ALTER TABLE InfoAtivos ADD COLUMN cdTipoAmortizacao TEXT NULL"),
    ]:
        try:
            conn.execute(sql)
            novas.add(coluna)
        except Exception:
            pass  # coluna já existe

    # Backfill do stTemFluxo: so na migracao (uma passada). Dai em diante quem
    # mantem e o MarcarTemFluxo(), chamado por quem escreve em FluxoAtivos.
    if "stTemFluxo" in novas:
        conn.execute("""
            UPDATE InfoAtivos SET stTemFluxo =
                CASE WHEN EXISTS (SELECT 1 FROM FluxoAtivos f
                                  WHERE f.cdTicker = InfoAtivos.cdTicker)
                     THEN 1 ELSE 0 END
        """)

    # Trigger de invalidacao: DEPOIS dos ALTERs (o corpo referencia as colunas novas).
    # DROP antes do CREATE para que COLS_INVALIDAM_FLUXO seja sempre autoritativa
    # mesmo em base ja existente (CREATE IF NOT EXISTS sozinho nao atualizaria o
    # trigger ao adicionar uma coluna nova, ex.: cdTipoAmortizacao).
    conn.execute("DROP TRIGGER IF EXISTS trgInfoAtivosInvalidaFluxo")
    conn.executescript(DDL_TRIGGERS)
    conn.execute("CREATE INDEX IF NOT EXISTS idxInfoAtivosStFluxoValidado "
                 "ON InfoAtivos(stFluxoValidado)")
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


# ---------------------------------------------------------------------------
# Contrato de validacao de fluxo (ver vault "98 - Backlog")
#
# Este projeto (ingestor) NUNCA valida nada: so mantem stTemFluxo e ZERA a
# validacao quando o fluxo do ativo muda. Quem marca stFluxoValidado = 1 e
# grava dtValidacaoFluxo/cdFonteValidacaoFluxo/dtUltimaTentativa e o
# validar_fluxos.py (hoje no projeto da calculadora, lendo o mesmo trades.db).
#
# As 4 colunas de InfoAtivos que invalidam (COLS_INVALIDAM_FLUXO) sao cobertas
# pelo trigger trgInfoAtivosInvalidaFluxo. O FluxoAtivos NAO da pra cobrir por
# trigger: o INSERT OR REPLACE e DELETE+INSERT, entao dispararia mesmo
# reescrevendo dado identico (o caso dos tickers re-enfileirados todo dia).
# Por isso a escrita do fluxo passa por SincronizarFluxoAtivos(), que compara
# antes de escrever.
# ---------------------------------------------------------------------------

def InvalidarValidacaoFluxo(conn: sqlite3.Connection, cdTicker: str) -> None:
    """Marca o fluxo do ativo como nao-validado e limpa o resultado anterior.

    dtUltimaTentativa tambem vai a NULL: o resultado da ultima tentativa virou
    lixo no instante em que o fluxo mudou, entao o ativo volta pro topo da fila
    do validador em vez de esperar a janela de throttle."""
    conn.execute(f"UPDATE InfoAtivos SET {SQL_ZERA_VALIDACAO} WHERE cdTicker = ?",
                 (cdTicker,))


def MarcarTemFluxo(conn: sqlite3.Connection, cdTicker: str, temFluxo: bool = True) -> None:
    """Mantem InfoAtivos.stTemFluxo em dia. Chamado por quem escreve FluxoAtivos."""
    conn.execute("UPDATE InfoAtivos SET stTemFluxo = ? WHERE cdTicker = ?",
                 (1 if temFluxo else 0, cdTicker))


def LerFluxoAtivos(conn: sqlite3.Connection, cdTicker: str) -> dict:
    """Agenda atual do ticker: {dtEvento: (vrPctAmortizacao, vrPctIncorporacao)}."""
    rows = conn.execute(
        "SELECT dtEvento, vrPctAmortizacao, vrPctIncorporacao "
        "FROM FluxoAtivos WHERE cdTicker = ?", (cdTicker,)).fetchall()
    return {r["dtEvento"]: (r["vrPctAmortizacao"], r["vrPctIncorporacao"]) for r in rows}


def SincronizarFluxoAtivos(conn: sqlite3.Connection, cdTicker: str, linhas: list) -> bool:
    """Escreve a agenda do ticker em FluxoAtivos SO se ela mudou. Retorna se mudou.

    `linhas`: dicts com cdTicker, dtEvento, vrPctAmortizacao, vrPctIncorporacao,
    dtAtualizacao (o formato que ProcessarAgenda produz).

    Mudou = evento novo, ou evento existente com %amortizacao/%incorporacao
    diferente. Reescrever a agenda identica (re-scrape do dia a dia) nao conta
    como mudanca: nao escreve, nao invalida, nao mexe no dtAtualizacao.

    Quando muda: grava, invalida a validacao do fluxo e atualiza stTemFluxo.

    NAO escreve em ativo de cdFonteCadastro = 'B3'. O fluxo da B3 vem casado com o VNE
    ja capitalizado e o inicio de rentabilidade DELA (vrVNE + dtInicioRentabilidade +
    FluxoAtivos sao um pacote indivisivel — ver scrape_b3_bond_details). Se a Anbima
    reescrevesse a agenda por cima, o VNE capitalizado ficaria orfao e a carencia
    passaria a contar DUAS vezes, sem erro nenhum, so um PU errado. A guarda mora aqui,
    e nao no scraper, porque FluxoAtivos tem varios writers e vai ter mais."""
    if not linhas:
        return False

    fonte = conn.execute(
        "SELECT cdFonteCadastro FROM InfoAtivos WHERE cdTicker = ?", (cdTicker,)).fetchone()
    if fonte and fonte[0] == "B3":
        return False

    atual = LerFluxoAtivos(conn, cdTicker)
    mudou = any(
        atual.get(l["dtEvento"], object())
        != (l["vrPctAmortizacao"], l["vrPctIncorporacao"])
        for l in linhas
    )
    if not mudou:
        return False

    conn.executemany("""
        INSERT OR REPLACE INTO FluxoAtivos
            (cdTicker, dtEvento, vrPctAmortizacao, vrPctIncorporacao, dtAtualizacao)
        VALUES (:cdTicker, :dtEvento, :vrPctAmortizacao, :vrPctIncorporacao, :dtAtualizacao)
    """, linhas)
    InvalidarValidacaoFluxo(conn, cdTicker)
    MarcarTemFluxo(conn, cdTicker, True)
    return True
