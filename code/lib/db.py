import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from lib.config import cfg


# Versao do schema gravada em SchemaVersao a cada bootstrap. Bump SEMPRE que a DDL
# ganhar coluna/tabela nova, para o banco de producao saber em que versao roda.
#   1 = schema PT-BR base   2 = validacao de fluxo + cadastro B3
#   3 = cdTipoAmortizacao + motor unificado (FASE 3)
#   4 = dtAtualizacaoReferencia (timer da referencia)
#   5 = split em dois arquivos (ativos.db + trades.db)
SCHEMA_VERSION = 5


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
    -- Cadastro vindo da Anbima Data (F17, 20/06/2026). Estavam FORA desta DDL ate
    -- 26/08: entraram por um ALTER fixo que sumiu quando o reconciliador generico
    -- substituiu a lista (23/07). Resultado: base nova nascia sem elas e o
    -- scrape_anbima_data_ativos quebrava no upsert. vrQuantidadeEmissao e o PROXY
    -- de outstanding que pesa a aba Visao Anbima enquanto a tabela Outstanding
    -- estiver vazia (ver gerar_relatorio_credito).
    cdISIN              TEXT NULL,
    vrQuantidadeEmissao REAL NULL,
    dtEmissao           TEXT NULL,
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
    -- Quando a cdReferencia foi (re)afirmada por alguma fonte. Renovada TODA vez que
    -- uma fonte grava uma referencia, mesmo repetindo o mesmo valor -- e um "sinal de
    -- vida", nao um registro de mudanca. Enquanto a Anbima publicar o papel, ela renova
    -- diariamente; quando ela para de cobrir, a data congela e o match_referencias
    -- assume depois de DIAS_REVALIDAR_REFERENCIA. Sem isto, ref da Anbima que deixou de
    -- ser publicada fica presa para sempre (foi o caso dos papeis na NTN-B 26 vencida).
    dtAtualizacaoReferencia TEXT NULL,
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
    -- quem VALIDA (marca 1 / grava data / fonte) e o validar_calc_b3.py.
    stTemFluxo            INTEGER NOT NULL DEFAULT 0,  -- 1 tem linha em FluxoAtivos
    stFluxoValidado       INTEGER NOT NULL DEFAULT 0,  -- 1 fluxo conferido contra a fonte
    dtValidacaoFluxo      TEXT NULL,                   -- ISO da validacao OK
    cdFonteValidacaoFluxo TEXT NULL                    -- 'B3' | 'FiAnalytics'
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

-- ===== SchemaVersao =====
-- Metadados: uma linha por versao de schema ja aplicada (trilha, nao so a atual).
-- A versao corrente e MAX(vrVersao). Ver SCHEMA_VERSION e Bootstrap.
CREATE TABLE IF NOT EXISTS SchemaVersao (
    vrVersao      INTEGER NOT NULL,
    dtAtualizacao TEXT    NOT NULL,
    cdNota        TEXT    NULL,
    PRIMARY KEY (vrVersao)
);
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
    cdFonteValidacaoFluxo = NULL
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


# A DDL mistura CREATE TABLE e CREATE INDEX. Num banco de versao anterior, um indice
# pode referenciar coluna que so o ALTER vai adicionar (ex.: idGrupoNegocio), entao a
# ordem importa: criar tabelas -> ALTER add colunas -> criar indices. Separamos os dois
# a partir da MESMA DDL (fonte unica), sem manter duas strings a mao.
#
# Antes de separar por ';', tiramos os comentarios de linha: alguns contem ';' no texto
# ('convencao de NTN-B; a debenture...') e cortariam um CREATE TABLE no meio.
DDL_SEM_COMENTARIOS = "\n".join(
    (linha[:linha.index("--")] if "--" in linha else linha) for linha in DDL.splitlines())


# ---------------------------------------------------------------------------
# Dois arquivos de banco (schema 5)
# ---------------------------------------------------------------------------
# O cadastro/mercado dos ativos e os negocios passam a viver em arquivos separados:
#   ativos.db  cadastro, agenda de fluxo, indicativas Anbima, MtM, outstanding
#   trades.db  os negocios crus e processados
# Sao dominios com ciclo de vida diferente: o ativos.db e pequeno, e o que a
# calculadora e o add-in leem, e nao precisa arrastar centenas de MB de negocio junto.
# A DDL continua UMA so (fonte unica da verdade); o que muda e o filtro por tabela.
#
# SchemaVersao vai nos DOIS: cada arquivo carrega a propria trilha de migracao.
TABELAS_POR_DOMINIO: dict[str, tuple[str, ...]] = {
    "ativos": ("InfoAtivos", "FluxoAtivos", "AnbimaIndicativos", "MtmAnbima",
               "Outstanding", "SchemaVersao"),
    "trades": ("NegociosBrutos", "NegociosProcessados", "SchemaVersao"),
}

DOMINIOS = tuple(TABELAS_POR_DOMINIO)

# Tabela -> dominio (inverso do mapa acima). SchemaVersao fica de fora: e das duas.
DOMINIO_DA_TABELA = {tab: dom for dom, tabs in TABELAS_POR_DOMINIO.items()
                     for tab in tabs if tab != "SchemaVersao"}


def TabelaDoComando(cmd: str) -> str | None:
    """Nome da tabela que um CREATE TABLE / CREATE INDEX toca (None se nao der)."""
    m = re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", cmd, re.I)
    if m:
        return m.group(1)
    # O covering index quebra a linha entre o nome e o ON -- precisa de re.S.
    m = re.search(r"\bON\s+(\w+)\s*\(", cmd, re.I | re.S)
    return m.group(1) if m else None


def DdlDominio(dominio: str, tipo: str) -> str:
    """Os comandos `CREATE {tipo}` da DDL que pertencem ao dominio, num script so."""
    tabelas = TABELAS_POR_DOMINIO[dominio]
    cmds = [s.strip() for s in DDL_SEM_COMENTARIOS.split(";")
            if f"CREATE {tipo}" in s.upper()]
    do = [c for c in cmds if TabelaDoComando(c) in tabelas]
    return (";\n".join(do) + ";") if do else ""


DDL_TABELAS = {d: DdlDominio(d, "TABLE") for d in DOMINIOS}
DDL_INDICES = {d: DdlDominio(d, "INDEX") for d in DOMINIOS}


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


def CaminhoBanco(dominio: str) -> str:
    """Caminho do arquivo de um dominio, lido do config ([paths] dbAtivos/dbTrades)."""
    chave = {"ativos": "dbAtivos", "trades": "dbTrades"}[dominio]
    return cfg["paths"][chave]


def ObterConexao(caminhoBanco: str) -> sqlite3.Connection:
    """
    Abre conexao SQLite com WAL, foreign_keys e PRAGMAs de performance.
    Cria o diretorio pai se nao existir.
    """
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
        caminhoBanco = CaminhoBanco("ativos")   # InfoAtivos + FluxoAtivos moram la
    conn = sqlite3.connect(f"file:{caminhoBanco}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    for p in PRAGMAS_PERF:
        conn.execute(p)
    return conn


def ColunasEsperadas(ddl: str) -> dict[str, list[tuple[str, str]]]:
    """Le a DDL e devolve {tabela: [(coluna, definicaoParaAlter)]}. Fonte unica da
    verdade do schema — a migracao compara ISTO com o banco vivo, entao qualquer
    coluna nova na DDL e migrada sem precisar de lista paralela (que foi a origem de
    colunas esquecidas fora de InfoAtivos).

    A definicao e ajustada para ser aceita por ALTER TABLE ADD COLUMN numa tabela ja
    com linhas: DEFAULT nao-constante (CURRENT_TIMESTAMP) e removido, e NOT NULL sem
    DEFAULT vira NULL (SQLite recusa NOT NULL sem default constante em tabela populada;
    a coluna nasce NULL nas linhas antigas, o que e o unico resultado possivel)."""
    esperadas: dict[str, list[tuple[str, str]]] = {}
    for bloco in re.finditer(r'CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);', ddl, re.S):
        tabela, corpo = bloco.group(1), bloco.group(2)
        cols: list[tuple[str, str]] = []
        for bruta in corpo.split('\n'):
            linha = bruta.strip()
            if '--' in linha:
                linha = linha[:linha.index('--')]
            linha = linha.strip().rstrip(',').strip()
            if not linha:
                continue
            if linha.split()[0].upper() in ('PRIMARY', 'FOREIGN', 'UNIQUE', 'CHECK', 'CONSTRAINT'):
                continue
            partes = linha.split(None, 1)
            if len(partes) < 2:
                continue
            nome, definicao = partes
            if 'PRIMARY KEY' in definicao.upper():
                continue  # coluna de PK: existe desde a criacao, nunca se adiciona por ALTER
            if 'CURRENT_TIMESTAMP' in definicao.upper():
                definicao = re.sub(r'DEFAULT\s+CURRENT_TIMESTAMP', '', definicao, flags=re.I)
            if 'NOT NULL' in definicao.upper() and 'DEFAULT' not in definicao.upper():
                definicao = re.sub(r'NOT\s+NULL', '', definicao, flags=re.I)
            cols.append((nome, ' '.join(definicao.split())))
        esperadas[tabela] = cols
    return esperadas


COLUNAS_ESPERADAS = ColunasEsperadas(DDL)


# Colunas que a DDL NAO tem mais e que devem ser removidas do banco vivo. Contrapartida
# do reconciliador aditivo: sem isto, coluna aposentada fica para sempre na base de
# producao, sem ninguem escrevendo, confundindo quem le o schema.
#   InfoAtivos.dtUltimaTentativa -- era o throttle do validar_fluxos (removido em
#   24/08/2026); o validar_calc_b3 usa dtValidacaoFluxo.
COLUNAS_REMOVIDAS: dict[str, list[str]] = {
    "InfoAtivos": ["dtUltimaTentativa"],
}


def ColunasARemover(conn: sqlite3.Connection, dominio: str) -> dict[str, list[str]]:
    """{tabela: [coluna]} que o banco vivo ainda tem e a DDL nao preve mais.
    Restrito as tabelas do dominio: cada arquivo migra so o que e dele."""
    doDominio = set(TABELAS_POR_DOMINIO[dominio])
    existentes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    sobrando: dict[str, list[str]] = {}
    for tabela, cols in COLUNAS_REMOVIDAS.items():
        if tabela not in existentes or tabela not in doDominio:
            continue
        atuais = {r[1] for r in conn.execute(f"PRAGMA table_info({tabela})")}
        pend = [c for c in cols if c in atuais]
        if pend:
            sobrando[tabela] = pend
    return sobrando


def ColunasFaltantes(conn: sqlite3.Connection, dominio: str) -> dict[str, list[tuple[str, str]]]:
    """{tabela: [(coluna, def)]} que a DDL preve e o banco vivo NAO tem. So considera
    tabelas que JA existem — as ausentes o executescript(DDL) cria inteiras.
    Restrito as tabelas do dominio: cada arquivo migra so o que e dele."""
    doDominio = set(TABELAS_POR_DOMINIO[dominio])
    existentes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    faltando: dict[str, list[tuple[str, str]]] = {}
    for tabela, cols in COLUNAS_ESPERADAS.items():
        if tabela not in existentes or tabela not in doDominio:
            continue
        atuais = {r[1] for r in conn.execute(f"PRAGMA table_info({tabela})")}
        pend = [(n, d) for n, d in cols if n not in atuais]
        if pend:
            faltando[tabela] = pend
    return faltando


def LerVersaoSchema(conn: sqlite3.Connection) -> int:
    """Maior vrVersao gravada, ou 0 se a tabela ainda nao existe / esta vazia."""
    try:
        v = conn.execute("SELECT MAX(vrVersao) FROM SchemaVersao").fetchone()[0]
        return v or 0
    except sqlite3.OperationalError:
        return 0


def CaminhoBancoDaConexao(conn: sqlite3.Connection) -> Path | None:
    """Caminho do arquivo do banco 'main', ou None se for :memory:."""
    for _seq, nome, arquivo in conn.execute("PRAGMA database_list"):
        if nome == "main" and arquivo:
            return Path(arquivo)
    return None


def BackupPreventivo(conn: sqlite3.Connection) -> Path | None:
    """Copia o banco para data/backups/<nome>_pre_migracao_<ts>.db ANTES de migrar.
    Usa a API de backup do SQLite (segura com WAL). None se for :memory:."""
    origem = CaminhoBancoDaConexao(conn)
    if origem is None:
        return None
    destinoDir = origem.parent / "backups"
    destinoDir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    destino = destinoDir / f"{origem.stem}_pre_migracao_{ts}.db"
    bkp = sqlite3.connect(str(destino))
    try:
        conn.backup(bkp)
    finally:
        bkp.close()
    return destino


def ContarLinhas(conn: sqlite3.Connection, tabelas) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tabelas}


def Bootstrap(conn: sqlite3.Connection, dominio: str) -> None:
    """Garante o schema no banco vivo, preservando 100% dos dados. Idempotente.

    Autonomo: rodar qualquer script (todos chamam ObterBanco) sobre um trades.db de
    versao anterior migra o schema na hora — adiciona as colunas/tabelas que faltam
    via ALTER TABLE ADD COLUMN, NUNCA recria nem apaga tabela. Antes de qualquer ALTER,
    tira um backup preventivo; ao final, confere que nenhuma linha sumiu e grava a
    versao em SchemaVersao. Numa base ja atual, e um no-op barato (so o diff de colunas).
    """
    # 1. Estado ANTES de tocar em nada.
    faltando   = ColunasFaltantes(conn, dominio)
    sobrando   = ColunasARemover(conn, dominio)
    versaoAtual = LerVersaoSchema(conn)
    temTabelas = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] > 0
    precisaMigrar = bool(faltando) or bool(sobrando) or versaoAtual < SCHEMA_VERSION

    # 2. Backup preventivo — so quando ha migracao real sobre um banco que ja tem dados.
    #    (Banco novo/vazio nao tem o que proteger; base ja atual nao migra.)
    backup = None
    if (faltando or sobrando) and temTabelas:
        backup = BackupPreventivo(conn)

    tabelasTocadas = set(faltando) | set(sobrando)
    contagensAntes = ContarLinhas(conn, tabelasTocadas) if tabelasTocadas else {}

    # 3. Cria as TABELAS que faltam (aditivo, IF NOT EXISTS). Os indices ficam para
    #    depois dos ALTER — um indice pode citar coluna que ainda vamos adicionar.
    conn.executescript(DDL_TABELAS[dominio])

    # 4. Adiciona as colunas que faltam. Idempotente por construcao (so as ausentes).
    novas: set[str] = set()
    for tabela, cols in faltando.items():
        for coluna, definicao in cols:
            try:
                conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}")
                novas.add(f"{tabela}.{coluna}")
            except sqlite3.OperationalError:
                pass  # corrida/coluna ja presente — segue

    # 4b. Agora que todas as colunas existem, cria os indices.
    conn.executescript(DDL_INDICES[dominio])

    # Backfill do stTemFluxo: so na migracao (uma passada). Dai em diante quem
    # mantem e o MarcarTemFluxo(), chamado por quem escreve em FluxoAtivos.
    if "InfoAtivos.stTemFluxo" in novas:
        conn.execute("""
            UPDATE InfoAtivos SET stTemFluxo =
                CASE WHEN EXISTS (SELECT 1 FROM FluxoAtivos f
                                  WHERE f.cdTicker = InfoAtivos.cdTicker)
                     THEN 1 ELSE 0 END
        """)

    # Backfill do dtAtualizacaoReferencia: so na migracao. Semeia com o
    # dtAtualizacaoDuration, que e a melhor procuracao disponivel — nas duas fontes que
    # gravam referencia, ela foi escrita no MESMO upsert da duration (scrapers Anbima:
    # dtRef do XLS; match_referencias: data da curva). Sem semear, toda a base nasceria
    # "referencia vencida" e a 1a rodada tentaria recalcular ~2.200 ativos de uma vez.
    if "InfoAtivos.dtAtualizacaoReferencia" in novas:
        conn.execute("""
            UPDATE InfoAtivos SET dtAtualizacaoReferencia = dtAtualizacaoDuration
            WHERE  cdReferencia IS NOT NULL AND dtAtualizacaoDuration IS NOT NULL
        """)

    # Trigger de invalidacao: DEPOIS dos ALTERs (o corpo referencia as colunas novas).
    # DROP antes do CREATE para que COLS_INVALIDAM_FLUXO seja sempre autoritativa
    # mesmo em base ja existente (CREATE IF NOT EXISTS sozinho nao atualizaria o
    # trigger ao adicionar uma coluna nova, ex.: cdTipoAmortizacao).
    if dominio == "ativos":
        conn.execute("DROP TRIGGER IF EXISTS trgInfoAtivosInvalidaFluxo")
        conn.executescript(DDL_TRIGGERS)
        conn.execute("CREATE INDEX IF NOT EXISTS idxInfoAtivosStFluxoValidado "
                     "ON InfoAtivos(stFluxoValidado)")
        # Remove indice redundante (coberto pela PK composta de FluxoAtivos).
        conn.execute("DROP INDEX IF EXISTS idxFluxoAtivosCdTicker")

    # 4c. Remove as colunas aposentadas. ALTER TABLE DROP COLUMN existe desde o SQLite
    #     3.35 (2021); em runtime mais velho, apenas registra e segue — coluna sobrando
    #     nao quebra nada (ninguem escreve nela), enquanto abortar o bootstrap quebraria
    #     o projeto inteiro. So mexe em COLUNA: nenhuma linha e tocada.
    if sobrando and sqlite3.sqlite_version_info < (3, 35, 0):
        print(f"[db] SQLite {sqlite3.sqlite_version} nao suporta DROP COLUMN — "
              f"colunas aposentadas mantidas: {sobrando}")
    elif sobrando:
        for tabela, cols in sobrando.items():
            for coluna in cols:
                conn.execute(f"ALTER TABLE {tabela} DROP COLUMN {coluna}")
                print(f"[db] coluna aposentada removida: {tabela}.{coluna}")

    # 5. Validacao de sanidade: migracao SO mexe em COLUNA, nunca em linha.
    #    Provamos: nenhuma tabela migrada perdeu registro. Se perdeu, algo esta muito
    #    errado — aborta o commit e preserva o backup, sem gravar a versao.
    if tabelasTocadas:
        contagensDepois = ContarLinhas(conn, tabelasTocadas)
        perdas = {t: (contagensAntes[t], contagensDepois[t])
                  for t in tabelasTocadas if contagensDepois[t] < contagensAntes.get(t, 0)}
        if perdas:
            conn.rollback()
            raise RuntimeError(
                f"Migracao ABORTADA — contagem caiu apos ALTER (antes/depois): {perdas}. "
                f"Nada foi commitado; backup preventivo em {backup}.")

    # 6. Registra a versao corrente do schema (uma linha por versao; INSERT OR IGNORE
    #    para nao duplicar em bases ja na versao). So quando houve migracao real.
    if precisaMigrar:
        conn.execute(
            "INSERT OR IGNORE INTO SchemaVersao (vrVersao, dtAtualizacao, cdNota) VALUES (?,?,?)",
            (SCHEMA_VERSION, datetime.now().isoformat(timespec="seconds"),
             f"{sum(len(v) for v in faltando.values())} coluna(s) migrada(s)"
             + (f"; backup {backup.name}" if backup else "")))

    conn.commit()
    # ANALYZE: estatisticas para o planejador escolher os indices certos.
    # So roda se ainda nao houver (sqlite_stat1) — evita custo a cada conexao.
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'"
    ).fetchone() is None:
        conn.execute("ANALYZE")
        conn.commit()


def ObterBancoAvulso(caminhoBanco: str, dominio: str) -> sqlite3.Connection:
    """Um arquivo so, sem anexar o outro dominio. Para teste e para ferramenta que
    mexe num banco especifico (o migrador do split, por exemplo)."""
    conn = ObterConexao(caminhoBanco)
    Bootstrap(conn, dominio)
    return conn


def ObterBanco(dominio: str = "trades") -> sqlite3.Connection:
    """Helper principal: abre conexao e garante o schema. Scripts usam so essa.

    Desde o schema 5 o banco sao DOIS arquivos (ativos.db e trades.db). Esta funcao
    abre um como `main` e ANEXA o outro, entao consulta que junta as duas metades
    (calc_taxa, filtrar_trades, calc_spread_over, relatorio) segue funcionando SEM
    qualificar nome de tabela — o SQLite resolve o nome no banco anexado.

    `dominio` diz so quem fica como `main`; os dois ficam gravaveis. Na pratica cada
    script escreve num dominio so, entao nenhuma transacao cruza os dois arquivos
    (o que, com WAL nos dois, nao teria commit atomico).
    """
    # Schema garantido nos dois arquivos: um script de ativos que anexa um trades.db
    # ainda inexistente veria um arquivo vazio e quebraria no primeiro JOIN. Numa base
    # ja atual isto e so o diff de colunas — barato.
    for dom in DOMINIOS:
        conexao = ObterConexao(CaminhoBanco(dom))
        try:
            Bootstrap(conexao, dom)
        finally:
            conexao.close()

    conn = ObterConexao(CaminhoBanco(dominio))
    outro = next(d for d in DOMINIOS if d != dominio)
    conn.execute(f"ATTACH DATABASE ? AS {outro}", (CaminhoBanco(outro),))
    conn.execute(f"PRAGMA {outro}.journal_mode=WAL")
    conn.execute(f"PRAGMA {outro}.synchronous=NORMAL")
    return conn


# ---------------------------------------------------------------------------
# Contrato de validacao de fluxo (ver vault "98 - Backlog")
#
# Este projeto (ingestor) NUNCA valida nada: so mantem stTemFluxo e ZERA a
# validacao quando o fluxo do ativo muda. Quem marca stFluxoValidado = 1 e
# grava dtValidacaoFluxo/cdFonteValidacaoFluxo e o
# validar_calc_b3.py, o unico validador.
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

    dtValidacaoFluxo vai a NULL junto: o resultado da ultima validacao virou lixo
    no instante em que o fluxo mudou. Como e essa data que o validar_calc_b3 usa
    na janela de revalidacao (--revalidar-dias), zera-la devolve o ativo ao topo
    da fila em vez de deixa-lo esperar o prazo vencer."""
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
