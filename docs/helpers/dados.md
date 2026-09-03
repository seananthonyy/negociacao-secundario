# dados.py

`codigos/helpers/dados.py` — **a camada de dados**. O módulo mais importante do projeto.

---

## Overview

Substituiu o `db.py` (SQLite). O armazenamento é **Parquet**; o motor de consulta é
**DuckDB** — uma biblioteca, não um servidor.

**Por que assim:** os dados precisam viver na AWS, e o acesso disponível é bucket S3 +
Athena, sem banco SQL. Parquet é o formato que o Athena lê, e o DuckDB roda SQL sobre esses
mesmos arquivos aqui na máquina.

**O SQL antigo continua valendo sem edição.** As views do DuckDB se chamam como as tabelas
do SQLite (`NegociosBrutos`, PascalCase) mesmo que a pasta seja `negocios_brutos`. Medido
na consulta mais pesada do relatório — 5.149 linhas, mesmo resultado ao centavo e 11×
mais rápido.

Onde os arquivos moram é **uma linha** do config (`[dados] raiz`): uma pasta local ou um
`s3://bucket/prefixo`. O código é o mesmo nos dois.

---

## API pública

### Esquema e metadados

| símbolo | o que é |
|---|---|
| `ESQUEMA: dict[str, pa.Schema]` | **A fonte única da verdade.** O que a DDL era |
| `PARTICAO: dict[str, str \| None]` | Coluna de partição; `None` = tabela de estado |
| `CHAVE: dict[str, tuple]` | Quem identifica uma linha — usada pelo `Upsert` |
| `TABELAS` | Tupla com os nomes |
| `NaoCancelado(alias="")` | Predicado SQL de "negócio que vale". Casa por **prefixo** |
| `NomePasta(tabela)` | `NegociosBrutos` → `negocios_brutos` |

### Leitura

| função | devolve |
|---|---|
| `Consultar(sql, params)` | DataFrame. `?` posicional, ou dict com `$nome` |
| `Escalar(sql, params)` | O primeiro valor da primeira linha, ou `None` |
| `Linhas(sql, params)` | `list[dict]` — o que `fetchall()` com `sqlite3.Row` dava |
| `Linha(sql, params)` | Um dict, ou `None` |
| `Tuplas(sql, params)` | `list[tuple]` |
| `Ler(tabela, data=None)` | A tabela (ou uma partição) com **todas** as colunas do esquema |
| `Datas(tabela)` | As partições que a tabela já tem |
| `TemDados(tabela)` | Se existe algum arquivo |

### Escrita

| função | efeito |
|---|---|
| `Mesclar(tabela, df, politica, padrao, data)` | **Coluna a coluna.** O `ON CONFLICT DO UPDATE` |
| `Upsert(tabela, df, data)` | Substitui a **linha inteira** |
| `GravarDia(tabela, df, data)` | Reescreve a partição de um dia |
| `GravarTudo(tabela, df)` | Reescreve a tabela inteira |
| `Apagar(tabela, coluna, valores)` | `DELETE ... WHERE col IN (...)` |
| `Reconformar(tabela, log)` | Reescreve tudo no esquema atual — evolução de schema |
| `Conformar(tabela, df)` | DataFrame → Table no esquema declarado |

**Políticas do `Mesclar`:**

| política | efeito |
|---|---|
| `SOBRESCREVER` | o novo vence sempre, inclusive NULL |
| `PREFERIR_NOVO` | o novo vence, mas NULL não apaga |
| `PREFERIR_ATUAL` | só preenche buraco — o `COALESCE` invertido |

### Fluxo de caixa e invalidação

| função | efeito |
|---|---|
| `LerFluxoAtivos(cdTicker)` | A agenda do ticker |
| `SincronizarFluxoAtivos(cdTicker, linhas)` | Escreve **só se mudou**; devolve se mudou |
| `SincronizarFluxos(porTicker)` | Versão em lote; devolve os tickers que mudaram |
| `SubstituirFluxo(porTicker)` | Troca a agenda inteira — o `DELETE`+`INSERT` da B3 |
| `ZerarValidacao(df, mascara)` | Zera `stFluxoValidado` nas linhas da máscara |
| `InvalidarSeFluxoMudou(antes, depois)` | O trigger, em código |
| `DescartarPuPar(tickers)` | Apaga o histórico de PU par dos ativos |
| `SemNaN(valor)` | Qualquer sabor de nulo do pandas → `None` |

### Conexão

`Conectar(recriar=False)` devolve a conexão DuckDB com uma view por tabela.
`Invalidar(tabela=None)` recria as views — **necessário depois de gravar**, porque a view
guarda a lista de arquivos de quando foi criada. As funções de escrita já chamam.

---

## Invariantes

**1. Tipo de coluna é declarado, nunca inferido.** É o gotcha nº 1 do Parquet: o SQLite é
de tipagem dinâmica, o Parquet é tipado. Uma coluna que venha toda NULL num dia faz o
pyarrow inferir o tipo `null`, e a leitura do conjunto quebra ao juntar essa partição com
as outras.

**2. Escrita parcial vai por `Mesclar`, nunca por `Upsert`.** O `Upsert` troca a linha
inteira e só serve a quem é dono de **todas** as colunas da tabela. Cinco scripts escrevem
em `InfoAtivos`, cada um dono de algumas colunas — usar `Upsert` ali apagaria o trabalho
dos outros quatro.

**3. Grave em LOTE, nunca por ativo.** `InfoAtivos` e `FluxoAtivos` são arquivos únicos:
gravar num laço de mil ativos reescreve o arquivo mil vezes.

**4. Evoluir o `ESQUEMA` exige `Reconformar`.** A view é `SELECT *` sobre os parquets,
então coluna nova não existe em arquivo nenhum até alguém reescrever — e todo `Ler()`
(que lista as colunas do esquema uma a uma) estoura com *column not found* nesse
meio-tempo.

**5. A coluna de partição é sempre VARCHAR.** Sem `hive_types`, o DuckDB deduz o tipo pelo
texto da pasta: `dtReferencia=2026-07-28` viraria DATE enquanto as demais datas são
VARCHAR, e qualquer comparação entre as duas quebra com *Cannot compare DATE and VARCHAR*.

**6. Datas são texto ISO**, não tipo data. É o que os scripts, o Athena e o DuckDB
comparam sem conversão.

---

## Quem consome

**Todos os 24 scripts** e os helpers `calc.py` e `cadastro_b3.py`. É o único caminho de
leitura e escrita da base — nenhum script abre arquivo Parquet diretamente.

---

## Armadilhas

**`Conformar` descarta e cria em silêncio.** Coluna que não está no esquema é descartada;
coluna do esquema que falta no DataFrame nasce NULL. Sem erro, sem log. É o comportamento
certo no uso diário (é o que permite escrita parcial) e **veneno numa migração** — por isso
o `migrar_para_parquet` compara coluna a coluna antes de escrever.

**`SUM()` de float não serve de checksum.** Reconformar `NegociosProcessados` deu diferença
de R$ 0,01 em R$ 163 bilhões. Não é perda de dado: é ordem de acumulação de ponto
flutuante, porque a ordem das linhas nos arquivos mudou. A conferência certa é diferença
simétrica linha a linha (`EXCEPT` nos dois sentidos), que deu zero.

**A view guarda a lista de arquivos.** Gravar sem `Invalidar` faz a próxima consulta ler o
estado anterior. As funções de escrita já invalidam, mas quem escrever por fora precisa
lembrar.

**Inteiro nulável vai por `Int64`, não `int64`.** O pandas representa NULL num `int64`
promovendo a coluna a float e usando NaN, e o pyarrow se recusa a converter NaN de volta
para inteiro. Como `stTemFluxo`, `stFluxoValidado`, `vrAniversario` e `vrQuantidade` ficam
NULL em linha recém-criada, sem isso toda gravação com linha nova quebraria.

**`NaoCancelado()` casa por prefixo, e tem de continuar assim.** A B3 escreve
`Cancelado B3` e `Cancelado Parcial B3`; o literal `Cancelado` é nosso. Comparação exata
deixava milhares de cancelados passarem por bons.

**A invalidação mora aqui, e não nos scrapers, de propósito.** `InfoAtivos` tem cinco
escritores; bastava um esquecer de zerar a validação para a calculadora passar a
precificar com fluxo velho — sem erro, sem log, só um PU errado.
