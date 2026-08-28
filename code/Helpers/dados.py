"""
dados.py
========
Camada de dados do projeto. Substitui o db.py: o armazenamento e PARQUET, e o
motor de consulta e o DuckDB.

Por que assim
-------------
Os dados precisam viver na AWS (S3), e o acesso disponivel e bucket + Athena --
nao ha banco SQL. Parquet e o formato que o Athena le, e o DuckDB e uma
BIBLIOTECA (nao um servidor) que roda SQL sobre esses mesmos arquivos, aqui na
maquina. Resultado: o mesmo texto de SQL que rodava no SQLite continua rodando,
sem edicao -- medido na consulta mais pesada do relatorio, 5.149 linhas, mesmo
resultado ao centavo e 11x mais rapido.

Onde os arquivos moram e uma linha do config ([dados] raiz): uma pasta local ou
um s3://bucket/prefixo. O codigo e o mesmo nos dois.

Layout
------
    <raiz>/negocios_brutos/dtNegocio=2026-07-28/dados.parquet
    <raiz>/info_ativos/dados.parquet

Nome de pasta em snake_case (convencao do Athena) e particao no estilo Hive
(coluna=valor), que e o que o Athena entende. Os nomes de TABELA vistos pelo SQL
continuam em PascalCase -- as views do DuckDB fazem a ponte.

Duas naturezas de tabela
------------------------
- SERIE (particionada por data): so cresce, e sempre se reescreve o dia inteiro.
  Reprocessar um dia e trocar uma particao, nao dar UPDATE em linha.
- ESTADO (arquivo unico): o retrato corrente do cadastro. Reescreve inteiro a
  cada rodada -- InfoAtivos e FluxoAtivos somam 0,55 MB, entao isso e instantaneo
  e dispensa qualquer mecanismo de UPDATE em disco.

Sem idTrade
-----------
O idTrade era INTEGER PRIMARY KEY AUTOINCREMENT: existia porque o SQLite o dava
de graca. Fora dele ninguem gera esse numero. A chave passa a ser a natural, que
a propria B3 manda: cdIdentificadorNegocio (unico nas 680.651 linhas da base).
"""

from __future__ import annotations

import re
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from config import cfg

# ---------------------------------------------------------------------------
# Esquema — fonte unica da verdade (o que a DDL era no db.py)
# ---------------------------------------------------------------------------
# Tipos EXPLICITOS, nunca inferidos. Este e o gotcha nº 1 do Parquet: o SQLite e
# de tipagem dinamica, o Parquet e tipado. Uma coluna que venha toda NULL num dia
# faz o pyarrow inferir o tipo `null`, e a leitura do conjunto quebra ao juntar
# essa particao com as outras. Declarar resolve de uma vez.
#
# Datas ficam como STRING no formato ISO (YYYY-MM-DD), igual ao que o SQLite
# guardava: e o que os scripts, o Athena e o DuckDB comparam sem conversao.

TEXTO, REAL, INTEIRO = pa.string(), pa.float64(), pa.int64()

ESQUEMA: dict[str, pa.Schema] = {
    "NegociosBrutos": pa.schema([
        ("cdIdentificadorNegocio", TEXTO),   # chave natural, vinda da B3
        ("cdInstrumento", TEXTO), ("cdEmissor", TEXTO), ("cdTicker", TEXTO),
        ("vrQuantidade", INTEIRO), ("vrPU", REAL), ("vrVolume", REAL),
        ("vrTaxaNegocio", REAL),
        ("dtHorarioNegocio", TEXTO), ("dtNegocio", TEXTO), ("cdISIN", TEXTO),
        ("dtLiquidacao", TEXTO), ("cdSituacao", TEXTO),
        ("dtCriacao", TEXTO), ("dtAtualizacao", TEXTO),
    ]),
    "NegociosProcessados": pa.schema([
        ("cdIdentificadorNegocio", TEXTO),
        ("cdTicker", TEXTO), ("cdEmissor", TEXTO),
        ("dtNegocio", TEXTO), ("dtLiquidacao", TEXTO),
        ("vrQuantidade", INTEIRO), ("vrPU", REAL), ("vrVolume", REAL),
        ("vrTaxaCalculada", REAL), ("cdFonteTaxa", TEXTO),
        ("vrDuration", REAL), ("vrSpreadOver", REAL),
        ("idGrupoNegocio", TEXTO), ("cdStatus", TEXTO), ("dtProcessamento", TEXTO),
    ]),
    "InfoAtivos": pa.schema([
        ("cdTicker", TEXTO), ("cdInstrumento", TEXTO), ("cdEmissor", TEXTO),
        ("cdISIN", TEXTO), ("vrQuantidadeEmissao", REAL), ("dtEmissao", TEXTO),
        ("dtVencimento", TEXTO),
        ("vrDuration", REAL), ("dtAtualizacaoDuration", TEXTO),
        ("cdIndexador", TEXTO), ("cdTipoAmortizacao", TEXTO),
        ("cdReferencia", TEXTO), ("cdFonteReferencia", TEXTO),
        ("dtAtualizacaoReferencia", TEXTO),
        ("vrTaxaEmissao", REAL), ("vrVNE", REAL), ("dtInicioRentabilidade", TEXTO),
        ("vrAniversario", INTEIRO), ("cdFonteCadastro", TEXTO),
        ("dtAtualizacao", TEXTO),
        ("stTemFluxo", INTEIRO), ("stFluxoValidado", INTEIRO),
        ("dtValidacaoFluxo", TEXTO), ("cdFonteValidacaoFluxo", TEXTO),
    ]),
    "FluxoAtivos": pa.schema([
        ("cdTicker", TEXTO), ("dtEvento", TEXTO),
        ("vrPctAmortizacao", REAL), ("vrPctIncorporacao", REAL),
        ("dtAtualizacao", TEXTO),
    ]),
    "AnbimaIndicativos": pa.schema([
        ("cdTicker", TEXTO), ("dtReferencia", TEXTO),
        ("vrTaxaAnbima", REAL), ("vrSpreadAnbima", REAL), ("dtCriacao", TEXTO),
    ]),
    "MtmAnbima": pa.schema([
        ("cdTicker", TEXTO), ("dtReferencia", TEXTO),
        ("vrTaxa", REAL), ("vrDuration", REAL),
    ]),
    "Outstanding": pa.schema([
        ("cdTicker", TEXTO), ("dtOutstanding", TEXTO), ("vrOutstanding", REAL),
    ]),
}

# Tabela -> coluna de particao. None = arquivo unico (tabela de ESTADO).
# So as duas grandes sao particionadas: 20 mil linhas/dia ja e um arquivo
# pequeno, e particionar tabela miuda so multiplica arquivo a toa (o que deixa
# tanto o Athena quanto o DuckDB mais LENTOS, nao mais rapidos).
PARTICAO: dict[str, str | None] = {
    "NegociosBrutos": "dtNegocio",
    "NegociosProcessados": "dtLiquidacao",
    "AnbimaIndicativos": "dtReferencia",
    "MtmAnbima": None,
    "InfoAtivos": None,
    "FluxoAtivos": None,
    "Outstanding": None,
}

TABELAS = tuple(ESQUEMA)

# Chave logica de cada tabela — quem identifica uma linha. Usada pelo Upsert.
CHAVE: dict[str, tuple[str, ...]] = {
    "NegociosBrutos": ("cdIdentificadorNegocio",),
    "NegociosProcessados": ("cdIdentificadorNegocio",),
    "InfoAtivos": ("cdTicker",),
    "FluxoAtivos": ("cdTicker", "dtEvento"),
    "AnbimaIndicativos": ("cdTicker", "dtReferencia"),
    "MtmAnbima": ("cdTicker", "dtReferencia"),
    "Outstanding": ("cdTicker", "dtOutstanding"),
}

COMPRESSAO = "zstd"


def NomePasta(tabela: str) -> str:
    """PascalCase -> snake_case. `NegociosBrutos` vira `negocios_brutos`, que e a
    convencao de nome que o Athena espera."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", tabela).lower()


# ---------------------------------------------------------------------------
# Onde os arquivos moram
# ---------------------------------------------------------------------------

def Raiz() -> str:
    """Raiz do armazenamento: pasta local ou s3://bucket/prefixo."""
    return str(cfg["dados"]["raiz"]).rstrip("/")


def EhS3(caminho: str | None = None) -> bool:
    return (caminho or Raiz()).startswith("s3://")


def CaminhoTabela(tabela: str) -> str:
    return f"{Raiz()}/{NomePasta(tabela)}"


def Glob(tabela: str) -> str:
    """Padrao que casa todos os arquivos da tabela (com ou sem particao)."""
    base = CaminhoTabela(tabela)
    return f"{base}/**/*.parquet" if PARTICAO[tabela] else f"{base}/*.parquet"


def TemDados(tabela: str) -> bool:
    if EhS3():
        import awswrangler as wr
        return bool(wr.s3.list_objects(CaminhoTabela(tabela) + "/"))
    base = Path(CaminhoTabela(tabela))
    return base.is_dir() and any(base.rglob("*.parquet"))


# ---------------------------------------------------------------------------
# Consulta — DuckDB sobre os parquets
# ---------------------------------------------------------------------------

conexaoCache: duckdb.DuckDBPyConnection | None = None


def Conectar(recriar: bool = False) -> duckdb.DuckDBPyConnection:
    """Conexao DuckDB com uma VIEW por tabela, apontando para os parquets.

    E a view que faz o SQL antigo continuar valendo: ela se chama `NegociosBrutos`
    (PascalCase, como no SQLite) mesmo que a pasta seja `negocios_brutos`. Tabela
    ainda sem nenhum arquivo vira uma view VAZIA com o esquema certo -- assim uma
    consulta a ela devolve zero linha em vez de estourar "arquivo nao encontrado".
    """
    global conexaoCache
    if conexaoCache is not None and not recriar:
        return conexaoCache

    con = duckdb.connect()
    if EhS3():
        # Credenciais vem do ambiente (as mesmas variaveis que o awswrangler usa).
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute("CREATE OR REPLACE SECRET segredoS3 (TYPE s3, PROVIDER credential_chain);")

    for tabela in TABELAS:
        if TemDados(tabela):
            # hive_types: sem isto o DuckDB DEDUZ o tipo da coluna de particao pelo
            # texto da pasta -- `dtReferencia=2026-07-28` vira DATE, enquanto as
            # demais datas sao VARCHAR (ISO, como no SQLite). Ai qualquer comparacao
            # entre as duas quebra com "Cannot compare DATE and VARCHAR". Forcamos
            # VARCHAR para que particao e coluna comum sejam a mesma coisa.
            coluna = PARTICAO[tabela]
            tipos = f", hive_types={{'{coluna}': 'VARCHAR'}}" if coluna else ""
            con.execute(
                f'CREATE OR REPLACE VIEW "{tabela}" AS '
                f"SELECT * FROM read_parquet('{Glob(tabela)}', hive_partitioning=true, "
                f"union_by_name=true{tipos})")
        else:
            colunas = ", ".join(f'NULL::{TipoSql(c.type)} AS "{c.name}"'
                                for c in ESQUEMA[tabela])
            con.execute(f'CREATE OR REPLACE VIEW "{tabela}" AS '
                        f"SELECT {colunas} WHERE false")
    conexaoCache = con
    return con


def TipoSql(tipo: pa.DataType) -> str:
    if pa.types.is_string(tipo):
        return "VARCHAR"
    if pa.types.is_floating(tipo):
        return "DOUBLE"
    return "BIGINT"


def Consultar(sql: str, params: tuple | list | None = None):
    """Roda SQL sobre os parquets e devolve DataFrame. Aceita `?` como no sqlite3."""
    con = Conectar()
    cur = con.execute(sql, list(params)) if params else con.execute(sql)
    return cur.df()


def Escalar(sql: str, params: tuple | list | None = None):
    """Primeiro valor da primeira linha, ou None."""
    df = Consultar(sql, params)
    return None if df.empty else df.iloc[0, 0]


def Invalidar() -> None:
    """Recria as views. Chamar depois de gravar, para que a proxima consulta enxergue
    o arquivo novo (a view guarda a lista de arquivos de quando foi criada)."""
    Conectar(recriar=True)


# ---------------------------------------------------------------------------
# Gravacao
# ---------------------------------------------------------------------------

def Conformar(tabela: str, df) -> pa.Table:
    """DataFrame -> Table no esquema declarado: coluna que falta nasce NULL, coluna
    a mais e descartada, e a ordem/tipo passa a ser sempre a mesma."""
    import pandas as pd
    esquema = ESQUEMA[tabela]
    saida = pd.DataFrame(index=df.index)
    for campo in esquema:
        if campo.name in df.columns:
            saida[campo.name] = df[campo.name]
        else:
            saida[campo.name] = None
    return pa.Table.from_pandas(saida, schema=esquema, preserve_index=False)


def GravarArquivo(tabela: pa.Table, destino: str) -> None:
    """Escreve UM arquivo parquet, local ou no S3."""
    if EhS3(destino):
        import awswrangler as wr
        wr.s3.to_parquet(df=tabela.to_pandas(), path=destino, compression=COMPRESSAO,
                         index=False, dataset=False)
        return
    caminho = Path(destino)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tabela, caminho, compression=COMPRESSAO)


def GravarTudo(tabela: str, df) -> int:
    """Reescreve a tabela INTEIRA. Para as tabelas de ESTADO (InfoAtivos, FluxoAtivos):
    elas sao o retrato corrente do cadastro e cabem num arquivo so."""
    dados = Conformar(tabela, df)
    GravarArquivo(dados, f"{CaminhoTabela(tabela)}/dados.parquet")
    Invalidar()
    return dados.num_rows


def GravarDia(tabela: str, df, data: str) -> int:
    """Reescreve a particao de UM dia. Substitui o dia inteiro -- reprocessar uma data
    e trocar a particao, nunca dar UPDATE em linha."""
    coluna = PARTICAO[tabela]
    if not coluna:
        raise ValueError(f"{tabela} nao e particionada — use GravarTudo")
    dados = Conformar(tabela, df)
    GravarArquivo(dados, f"{CaminhoTabela(tabela)}/{coluna}={data}/dados.parquet")
    Invalidar()
    return dados.num_rows


def Upsert(tabela: str, df, data: str | None = None) -> int:
    """Mescla `df` no que ja existe, casando pela CHAVE da tabela: linha nova entra,
    linha existente e substituida, e o que nao veio em `df` fica intacto.

    E o `ON CONFLICT DO UPDATE` do SQLite, feito em memoria -- possivel porque a
    unidade de reescrita (uma particao de um dia, ou uma tabela de estado inteira)
    e pequena o bastante para caber na RAM.
    """
    import pandas as pd
    coluna = PARTICAO[tabela]
    if coluna and data is None:
        raise ValueError(f"{tabela} e particionada por {coluna} — informe a data")

    if coluna:
        atual = Consultar(f'SELECT * FROM "{tabela}" WHERE "{coluna}" = ?', (data,))
    else:
        atual = Consultar(f'SELECT * FROM "{tabela}"')

    chave = list(CHAVE[tabela])
    if atual.empty:
        junto = df
    else:
        atual = atual[[c.name for c in ESQUEMA[tabela]]]
        junto = pd.concat([atual, df[[c for c in df.columns if c in atual.columns]]],
                          ignore_index=True)
        junto = junto.drop_duplicates(subset=chave, keep="last")

    return GravarDia(tabela, junto, data) if coluna else GravarTudo(tabela, junto)


def Datas(tabela: str) -> list[str]:
    """As datas (particoes) que a tabela ja tem."""
    coluna = PARTICAO[tabela]
    if not coluna or not TemDados(tabela):
        return []
    df = Consultar(f'SELECT DISTINCT "{coluna}" AS d FROM "{tabela}" ORDER BY 1')
    return [str(x) for x in df["d"]]
