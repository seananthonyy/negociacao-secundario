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
        CriarView(con, tabela)
    conexaoCache = con
    return con


def CriarView(con: duckdb.DuckDBPyConnection, tabela: str) -> None:
    """A view que faz a ponte entre o nome PascalCase do SQL e a pasta snake_case."""
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


def TipoSql(tipo: pa.DataType) -> str:
    if pa.types.is_string(tipo):
        return "VARCHAR"
    if pa.types.is_floating(tipo):
        return "DOUBLE"
    return "BIGINT"


def Consultar(sql: str, params: tuple | list | dict | None = None):
    """Roda SQL sobre os parquets e devolve DataFrame.

    Aceita `?` posicional, como o sqlite3. Parametro NOMEADO tambem, por dict — mas a
    sintaxe no SQL e `$nome`, e nao o `:nome` do sqlite3, que o DuckDB nao entende."""
    con = Conectar()
    if params is None:
        cur = con.execute(sql)
    elif isinstance(params, dict):
        cur = con.execute(sql, params)
    else:
        cur = con.execute(sql, list(params))
    return cur.df()


def Escalar(sql: str, params: tuple | list | None = None):
    """Primeiro valor da primeira linha, ou None."""
    df = Consultar(sql, params)
    return None if df.empty else df.iloc[0, 0]


def Linhas(sql: str, params: tuple | list | None = None) -> list[dict]:
    """Resultado como lista de dicts — o que `fetchall()` devolvia com sqlite3.Row.

    Serve a quem consome linha a linha (o relatorio) em vez de em bloco: o acesso
    por nome, `r["cdTicker"]`, continua identico. O que NAO sobrevive e o acesso
    POSICIONAL (`r[0]`), que no sqlite3.Row tambem funcionava — as poucas
    ocorrencias viraram acesso por nome.

    Um NULL volta como None, e nao como o NaN do pandas: o codigo que le estas
    linhas testa `is None` e faz aritmetica com o valor, e NaN passaria calado
    pelos dois."""
    df = Consultar(sql, params)
    return [{k: SemNaN(v) for k, v in linha.items()}
            for linha in df.to_dict(orient="records")]


def Linha(sql: str, params: tuple | list | None = None) -> dict | None:
    """A primeira linha como dict, ou None — o `fetchone()`."""
    linhas = Linhas(sql, params)
    return linhas[0] if linhas else None


def Tuplas(sql: str, params: tuple | list | None = None) -> list[tuple]:
    """Resultado como lista de tuplas — o acesso POSICIONAL do sqlite3.Row (`r[0]`).

    O sqlite3.Row atendia por nome e por posicao; um dict so atende por nome. Onde o
    consumidor le por posicao (as consultas do relatorio, cujas colunas sao expressoes
    com alias e nao colunas de tabela), esta e a leitura fiel. Mesma normalizacao de
    nulo do Linhas."""
    df = Consultar(sql, params)
    return [tuple(SemNaN(v) for v in linha) for linha in df.itertuples(index=False)]


def Invalidar(tabela: str | None = None) -> None:
    """Recria as views. Chamar depois de gravar, para que a proxima consulta enxergue
    o arquivo novo (a view guarda a lista de arquivos de quando foi criada)."""
    if tabela is None or conexaoCache is None:
        Conectar(recriar=True)
    else:
        CriarView(conexaoCache, tabela)


def Ler(tabela: str, data: str | None = None):
    """A tabela (ou uma particao dela) como DataFrame, sempre com TODAS as colunas
    do esquema, na ordem do esquema — inclusive quando ainda nao ha arquivo nenhum.

    Usado por quem vai reescrever: mesclar exige ter o retrato atual em maos."""
    coluna = PARTICAO[tabela]
    colunas = ", ".join(f'"{c.name}"' for c in ESQUEMA[tabela])
    if coluna and data is not None:
        return Consultar(f'SELECT {colunas} FROM "{tabela}" WHERE "{coluna}" = ?', (data,))
    return Consultar(f'SELECT {colunas} FROM "{tabela}"')


# ---------------------------------------------------------------------------
# Gravacao
# ---------------------------------------------------------------------------

def Conformar(tabela: str, df) -> pa.Table:
    """DataFrame -> Table no esquema declarado: coluna que falta nasce NULL, coluna
    a mais e descartada, e a ordem/tipo passa a ser sempre a mesma.

    Coluna inteira vai por "Int64" (o inteiro NULAVEL do pandas), nao por int64. O
    pandas representa NULL num int64 promovendo a coluna a float e usando NaN, e o
    pyarrow se recusa a converter NaN de volta para inteiro ("Cannot convert
    non-finite value"). Como stTemFluxo/stFluxoValidado/vrAniversario/vrQuantidade
    ficam NULL em linha recem-criada, sem isto toda gravacao com linha nova
    quebraria."""
    import pandas as pd
    esquema = ESQUEMA[tabela]
    saida = pd.DataFrame(index=df.index)
    for campo in esquema:
        if campo.name not in df.columns:
            saida[campo.name] = None
            continue
        coluna = df[campo.name]
        if pa.types.is_integer(campo.type):
            coluna = pd.to_numeric(coluna, errors="coerce").astype("Int64")
        elif pa.types.is_floating(campo.type):
            coluna = pd.to_numeric(coluna, errors="coerce").astype("float64")
        saida[campo.name] = coluna
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


# Contador de escrita por tabela. Quem mantem um indice em memoria (o CarregarAtivo do
# calc.py le InfoAtivos e FluxoAtivos milhares de vezes por rodada) compara a geracao
# que guardou com a atual e so reconstroi quando alguem gravou de fato. E o unico jeito
# de um cache aqui ser seguro: no SQLite cada leitura enxergava a escrita anterior de
# graca, e um cache mudo devolveria dado velho depois de qualquer gravacao.
GERACAO: dict[str, int] = {t: 0 for t in TABELAS}


def Geracao(tabela: str) -> int:
    return GERACAO[tabela]


def GravarTudo(tabela: str, df) -> int:
    """Reescreve a tabela INTEIRA. Para as tabelas de ESTADO (InfoAtivos, FluxoAtivos):
    elas sao o retrato corrente do cadastro e cabem num arquivo so."""
    dados = Conformar(tabela, df)
    GravarArquivo(dados, f"{CaminhoTabela(tabela)}/dados.parquet")
    GERACAO[tabela] += 1
    Invalidar(tabela)
    return dados.num_rows


def GravarDia(tabela: str, df, data: str) -> int:
    """Reescreve a particao de UM dia. Substitui o dia inteiro -- reprocessar uma data
    e trocar a particao, nunca dar UPDATE em linha."""
    coluna = PARTICAO[tabela]
    if not coluna:
        raise ValueError(f"{tabela} nao e particionada — use GravarTudo")
    dados = Conformar(tabela, df)
    GravarArquivo(dados, f"{CaminhoTabela(tabela)}/{coluna}={data}/dados.parquet")
    GERACAO[tabela] += 1
    Invalidar(tabela)
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


def Apagar(tabela: str, coluna: str, valores) -> int:
    """Remove as linhas cuja `coluna` esta em `valores`. Devolve quantas saiu.

    E o `DELETE ... WHERE col IN (...)`. Numa tabela particionada, so as particoes que
    de fato contem alguma dessas linhas sao reescritas — descobrir quais custa uma
    consulta e evita reescrever a serie inteira.

    Usado pelo soft-cancel do boletim: negocio que a B3 tirou do arquivo sai de
    NegociosProcessados de vez (em NegociosBrutos ele so vira 'Cancelado', para nao
    perder o rastro)."""
    import pandas as pd
    valores = list(valores)
    if not valores:
        return 0
    marcas = ",".join("?" * len(valores))
    particao = PARTICAO[tabela]
    colunas = [c.name for c in ESQUEMA[tabela]]

    if not particao:
        atual = Ler(tabela)
        fica  = atual[~atual[coluna].isin(valores)]
        n = len(atual) - len(fica)
        if n:
            GravarTudo(tabela, fica)
        return n

    dias = [r[0] for r in Consultar(
        f'SELECT DISTINCT "{particao}" FROM "{tabela}" WHERE "{coluna}" IN ({marcas})',
        valores).itertuples(index=False)]
    total = 0
    for dia in dias:
        atual = Ler(tabela, dia)
        fica  = atual[~atual[coluna].isin(valores)]
        total += len(atual) - len(fica)
        GravarDia(tabela, fica[colunas], dia)
    return total


def Datas(tabela: str) -> list[str]:
    """As datas (particoes) que a tabela ja tem."""
    coluna = PARTICAO[tabela]
    if not coluna or not TemDados(tabela):
        return []
    df = Consultar(f'SELECT DISTINCT "{coluna}" AS d FROM "{tabela}" ORDER BY 1')
    return [str(x) for x in df["d"]]


# ---------------------------------------------------------------------------
# Mesclagem coluna a coluna — o `ON CONFLICT DO UPDATE` de verdade
# ---------------------------------------------------------------------------
# O Upsert acima troca a LINHA inteira. Isso serve para quem e dono de todas as
# colunas da tabela (os negocios, o MtM), mas nao serve para InfoAtivos: ela tem
# cinco escritores (b3_bond_details, anbima_data, fianalytics_planilha,
# anbima_debentures, anbima_cri_cra) e cada um so conhece um pedaco das colunas.
# Trocar a linha inteira ali apagaria, em silencio, o que os outros escreveram.
#
# No SQLite isso era um `ON CONFLICT(cdTicker) DO UPDATE SET` com uma expressao
# por coluna. Sao apenas tres expressoes, repetidas:
#
#   coluna = excluded.coluna                   -> SOBRESCREVER
#   coluna = COALESCE(excluded.coluna, coluna) -> PREFERIR_NOVO
#   coluna = COALESCE(coluna, excluded.coluna) -> PREFERIR_ATUAL
#
# Havia uma quarta forma, condicional a OUTRA coluna:
#
#   dtAtualizacaoDuration = CASE WHEN excluded.vrDuration IS NOT NULL
#                           THEN excluded.dtAtualizacaoDuration ELSE ... END
#
# Ela nao precisa de politica propria: quem monta o DataFrame deixa a coluna
# dependente NULA quando a condicao e falsa, e PREFERIR_NOVO da exatamente o
# mesmo resultado. E o que os scripts convertidos fazem.

SOBRESCREVER   = "sobrescrever"    # o novo manda, mesmo sendo NULL
PREFERIR_NOVO  = "preferir_novo"   # o novo manda se nao for NULL
PREFERIR_ATUAL = "preferir_atual"  # so preenche buraco; o que ja existe fica


def Dobrar(df, chave: list[str], politica: dict, padrao: str):
    """Reduz linhas repetidas DENTRO do proprio df, uma so por chave.

    Precisa existir porque o executemany do SQLite aplicava as linhas UMA A UMA:
    duas linhas da mesma chave no mesmo lote se mesclavam entre si, com a mesma
    politica. Um drop_duplicates perderia isso.

    O last()/first() do groupby ignora NULL — que e precisamente a semantica do
    COALESCE. O SOBRESCREVER precisa do contrario, o ultimo valor INCLUSIVE nulo, e para
    isso vai por `agg(iloc[-1])`: o `nth(-1)`, que seria o natural, deixou de agregar no
    pandas 2 e passou a devolver as linhas originais com o indice original — o que faz o
    reset_index() abaixo nao reconstruir as colunas da chave.
    """
    import pandas as pd
    if not df.duplicated(subset=chave).any():
        return df.reset_index(drop=True)

    ordem  = list(df.columns)
    grupos = df.groupby(chave, sort=False, dropna=False)
    partes = {}
    for col in ordem:
        if col in chave:
            continue
        modo = politica.get(col, padrao)
        if modo == PREFERIR_ATUAL:
            partes[col] = grupos[col].first()   # primeiro nao-nulo
        elif modo == SOBRESCREVER:
            partes[col] = grupos[col].agg(lambda s: s.iloc[-1])  # ultimo, nulo inclusive
        else:
            partes[col] = grupos[col].last()    # ultimo nao-nulo
    return pd.DataFrame(partes).reset_index()[ordem]


def Mesclar(tabela: str, df, politica: dict | None = None,
            padrao: str = PREFERIR_NOVO, data: str | None = None) -> int:
    """Mescla `df` no que ja existe, COLUNA A COLUNA, pela CHAVE da tabela.

    Coluna que nao vier em `df` fica intacta na linha que ja existe (e nasce NULL
    na linha nova). E isso que permite cinco escritores parciais na mesma tabela.

    `politica`: {coluna: SOBRESCREVER | PREFERIR_NOVO | PREFERIR_ATUAL}; o que nao
    estiver no mapa usa `padrao`.

    Em InfoAtivos dispara tambem a invalidacao da validacao de fluxo — o que era o
    trigger trgInfoAtivosInvalidaFluxo. Ver InvalidarSeFluxoMudou.
    """
    politica = politica or {}
    chave    = list(CHAVE[tabela])
    coluna   = PARTICAO[tabela]
    if coluna and data is None:
        raise ValueError(f"{tabela} e particionada por {coluna} — informe a data")

    colunas = [c.name for c in ESQUEMA[tabela]]
    df = df[[c for c in df.columns if c in colunas]]
    if any(c not in df.columns for c in chave):
        raise ValueError(f"{tabela}: df sem a chave {chave}")
    df = Dobrar(df, chave, politica, padrao)

    atual = Ler(tabela, data)
    if atual.empty:
        return GravarDia(tabela, df, data) if coluna else GravarTudo(tabela, df)

    # Alinha pela chave. `indicator` separa quem ja existia (mescla), quem e novo
    # (entra como veio) e quem existia mas nao veio no lote (fica intacto).
    juntos = atual.merge(df, on=chave, how="outer", suffixes=("", "Novo"),
                         indicator="origem")
    veio   = juntos["origem"] != "left_only"

    for col in df.columns:
        if col in chave:
            continue
        novo = juntos[f"{col}Novo"]
        modo = politica.get(col, padrao)
        if modo == PREFERIR_ATUAL:
            mesclado = juntos[col].combine_first(novo)
        elif modo == SOBRESCREVER:
            mesclado = novo
        else:
            mesclado = novo.combine_first(juntos[col])
        juntos[col] = mesclado.where(veio, juntos[col])

    saida = juntos[colunas]
    if tabela == "InfoAtivos":
        saida = InvalidarSeFluxoMudou(atual, saida)
    return GravarDia(tabela, saida, data) if coluna else GravarTudo(tabela, saida)


# ---------------------------------------------------------------------------
# A invalidacao de fluxo — o trigger que o Parquet nao tem
# ---------------------------------------------------------------------------
# No SQLite isto era o trgInfoAtivosInvalidaFluxo, um AFTER UPDATE no banco. Ficava
# la, e nao nos scrapers, porque as colunas abaixo tem cinco escritores e bastava
# um esquecer para a calculadora passar a precificar com fluxo velho — sem erro,
# sem log, so um PU errado. O Parquet nao tem trigger, entao a regra desce para
# ca: Mesclar e o unico caminho pelo qual InfoAtivos e escrita coluna a coluna.

# Colunas que DEFINEM o fluxo de caixa: se qualquer uma mudar de valor, a
# validacao (stFluxoValidado) deixa de valer. dtVencimento NAO entra — e deduzido
# do proprio fluxo (ultimo evento).
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


def ZerarValidacao(df, mascara=None):
    """`df` com a validacao de fluxo zerada nas linhas de `mascara`.

    dtValidacaoFluxo vai a NULL junto: o resultado da ultima validacao virou lixo
    no instante em que o fluxo mudou. Como e essa data que o validar_calc_b3 usa na
    janela de revalidacao, zera-la devolve o ativo ao topo da fila em vez de deixa-lo
    esperando o prazo vencer."""
    import pandas as pd
    if mascara is None:
        mascara = pd.Series(True, index=df.index)
    if not mascara.any():
        return df
    df = df.copy()
    df.loc[mascara, "stFluxoValidado"]       = 0
    df.loc[mascara, "dtValidacaoFluxo"]      = None
    df.loc[mascara, "cdFonteValidacaoFluxo"] = None
    return df


def InvalidarSeFluxoMudou(antes, depois):
    """Zera a validacao das linhas em que uma coluna de COLS_INVALIDAM_FLUXO mudou.

    A comparacao e null-safe, como o `IS NOT` do trigger: reescrever o mesmo valor
    nao invalida; preencher um NULL invalida, porque isso muda o calculo. Linha nova
    nao entra — o trigger era AFTER UPDATE, e linha nova ja nasce nao-validada."""
    chave = list(CHAVE["InfoAtivos"])
    cols  = [c for c in COLS_INVALIDAM_FLUXO
             if c in depois.columns and c in antes.columns]
    if not cols or antes.empty:
        return depois

    velho = antes.set_index(chave)[cols]
    novo  = depois.set_index(chave)[cols]
    comum = novo.index.intersection(velho.index)
    if comum.empty:
        return depois

    v, n = velho.loc[comum], novo.loc[comum]
    # O ne() sozinho diria que NULL != NULL; o & ~(ambos nulos) devolve o IS NOT.
    difere  = (v.ne(n) & ~(v.isna() & n.isna())).any(axis=1)
    tickers = set(comum[difere.values])
    if not tickers:
        return depois
    return ZerarValidacao(depois, depois[chave[0]].isin(tickers))


# ---------------------------------------------------------------------------
# Fluxo de caixa — a agenda do ativo
# ---------------------------------------------------------------------------

def SemNaN(valor):
    """Qualquer sabor de nulo do pandas (NaN, NaT, pd.NA) -> None.

    O SQLite devolvia None numa coluna NULL; o Parquet devolve NaN, e NaN != NaN.
    Sem normalizar, comparar dois fluxos identicos dava "mudou" toda vez — e cada
    "mudou" invalida a validacao e joga o ativo de volta na fila do validador."""
    import pandas as pd
    if valor is None:
        return None
    try:
        return None if pd.isna(valor) else valor
    except (TypeError, ValueError):
        return valor   # array/lista: pd.isna devolve vetor, nao escalar


def LerFluxoAtivos(cdTicker: str) -> dict:
    """Agenda atual do ticker: {dtEvento: (vrPctAmortizacao, vrPctIncorporacao)}."""
    df = Consultar('SELECT dtEvento, vrPctAmortizacao, vrPctIncorporacao '
                   'FROM "FluxoAtivos" WHERE cdTicker = ?', (cdTicker,))
    return {r.dtEvento: (SemNaN(r.vrPctAmortizacao), SemNaN(r.vrPctIncorporacao))
            for r in df.itertuples()}


def SubstituirFluxo(porTicker: dict) -> int:
    """Troca a agenda INTEIRA dos tickers de `porTicker` — o `DELETE ... WHERE cdTicker`
    seguido de INSERT, que e como a B3 grava (a agenda dela vem completa, e um evento
    que sumiu tem de sumir da base tambem; um upsert deixaria o velho para tras).

    `porTicker`: {cdTicker: [linhas]}. Ticker fora do dict fica intacto.

    Recebe todos os tickers de uma vez porque FluxoAtivos e um arquivo so: fazer isto
    por ticker reescreveria a tabela inteira uma vez por ativo."""
    import pandas as pd
    if not porTicker:
        return 0
    atual = Ler("FluxoAtivos")
    resto = atual[~atual["cdTicker"].isin(porTicker)] if not atual.empty else atual
    novas = pd.DataFrame([l for linhas in porTicker.values() for l in linhas])
    junto = pd.concat([resto, novas], ignore_index=True) if not resto.empty else novas
    return GravarTudo("FluxoAtivos", junto)


def MarcarTemFluxo(cdTicker: str, temFluxo: bool = True) -> None:
    """Mantem InfoAtivos.stTemFluxo em dia. Chamado por quem escreve FluxoAtivos."""
    info = Ler("InfoAtivos")
    alvo = info["cdTicker"] == cdTicker
    if not alvo.any():
        return
    info.loc[alvo, "stTemFluxo"] = 1 if temFluxo else 0
    GravarTudo("InfoAtivos", info)


def InvalidarValidacaoFluxo(cdTicker: str) -> None:
    """Marca o fluxo do ativo como nao-validado e limpa o resultado anterior."""
    info = Ler("InfoAtivos")
    GravarTudo("InfoAtivos", ZerarValidacao(info, info["cdTicker"] == cdTicker))


def SincronizarFluxoAtivos(cdTicker: str, linhas: list) -> bool:
    """Escreve a agenda do ticker em FluxoAtivos SO se ela mudou. Retorna se mudou.

    `linhas`: dicts com cdTicker, dtEvento, vrPctAmortizacao, vrPctIncorporacao,
    dtAtualizacao (o formato que ProcessarAgenda produz).

    Mudou = evento novo, ou evento existente com %amortizacao/%incorporacao
    diferente. Reescrever a agenda identica (o re-scrape do dia a dia) nao conta como
    mudanca: nao escreve, nao invalida, nao mexe no dtAtualizacao.

    Quando muda: grava, invalida a validacao do fluxo e atualiza stTemFluxo — numa
    escrita so de InfoAtivos, em vez das tres do SQLite.

    NAO escreve em ativo de cdFonteCadastro = 'B3'. O fluxo da B3 vem casado com o VNE
    ja capitalizado e o inicio de rentabilidade DELA (vrVNE + dtInicioRentabilidade +
    FluxoAtivos sao um pacote indivisivel — ver scrape_b3_bond_details). Se a Anbima
    reescrevesse a agenda por cima, o VNE capitalizado ficaria orfao e a carencia
    passaria a contar DUAS vezes, sem erro nenhum, so um PU errado. A guarda mora aqui,
    e nao no scraper, porque FluxoAtivos tem varios writers e vai ter mais."""
    import pandas as pd
    if not linhas:
        return False

    fonte = Escalar('SELECT cdFonteCadastro FROM "InfoAtivos" WHERE cdTicker = ?',
                    (cdTicker,))
    if fonte == "B3":
        return False

    atual = LerFluxoAtivos(cdTicker)
    mudou = any(
        atual.get(l["dtEvento"], object())
        != (SemNaN(l["vrPctAmortizacao"]), SemNaN(l["vrPctIncorporacao"]))
        for l in linhas
    )
    if not mudou:
        return False

    Upsert("FluxoAtivos", pd.DataFrame(linhas))

    info = Ler("InfoAtivos")
    alvo = info["cdTicker"] == cdTicker
    if alvo.any():
        info = ZerarValidacao(info, alvo)
        info.loc[alvo, "stTemFluxo"] = 1
        GravarTudo("InfoAtivos", info)
    return True
