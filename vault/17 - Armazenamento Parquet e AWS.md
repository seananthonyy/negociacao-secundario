# 17 — Armazenamento Parquet e AWS

> **Estado: conversão COMPLETA.** A camada de dados existe, e os 21 scripts, os três
> notebooks e o `tests_fase1.py` já falam Parquet. O `db.py` foi aposentado. Passa no teste
> de aceitação (34 pregões, 2.567 ativos). Falta só o acesso à AWS, que depende de
> terceiros — ver §"Pendências do lado da AWS".
> Branch: `refactor/split-bases`. Última sessão: 31/08/2026.

---

## Por que mudou

Decisão do usuário (28/08/2026): **os dados precisam viver na AWS**, sem alternativa. O
acesso disponível é **bucket S3 + Athena** — não há banco SQL, e não haverá.

Isso elimina o SQLite como destino final. Parquet é o formato que o Athena lê, então
Parquet passa a ser **a** base — não uma cópia de exportação.

O código continua rodando **local**, no PC do banco. Só o armazenamento vai para a nuvem.
Isso mantém de pé o que não roda em Linux/AWS: Outlook (`pywin32`), Playwright com browser
e, principalmente, o **Bloomberg** (`xbbg`), que exige terminal logado.

---

## A descoberta que definiu o desenho

O medo era ter de reescrever todo o SQL do projeto em pandas — o relatório tem consultas
com CTE, window function e três joins.

**Não precisa.** O **DuckDB** roda SQL sobre Parquet e é uma *biblioteca*
(`pip install duckdb`), não um servidor — então não esbarra na restrição de "não pode usar
base SQL". Medido na consulta mais pesada do relatório (`SQL_BUSCAR_VALIDO`), com o **mesmo
texto**, sem editar uma vírgula:

| | linhas | volume | tempo |
|---|---|---|---|
| SQLite | 5.149 | R$ 837,52 MM | 1.412 ms |
| DuckDB sobre Parquet | 5.149 | R$ 837,52 MM | **129 ms** |

Idêntico ao centavo, 11× mais rápido. As **views** do DuckDB se chamam como as tabelas do
SQLite (`NegociosBrutos`, PascalCase), então o SQL dos scripts não sabe que o storage mudou.

---

## Como está montado

`Helpers/dados.py` substitui o `Helpers/db.py`.

### Onde os arquivos moram

Uma linha do `config.toml`:

```toml
[dados]
raiz = "files/Parquet"          # hoje: pasta local
# raiz = "s3://bucket/negociacao"   # quando o acesso for liberado
```

**O código é o mesmo nos dois.** `dados.py` detecta o `s3://` e usa `awswrangler` para
escrever e a extensão `httpfs` do DuckDB para ler; as credenciais vêm das variáveis de
ambiente da sessão (nunca do config).

### Layout

```
<raiz>/negocios_brutos/dtNegocio=2026-07-28/dados.parquet
<raiz>/info_ativos/dados.parquet
```

Pasta em `snake_case` e partição no estilo **Hive** (`coluna=valor`) — é o que o Athena
entende. O nome de tabela visto pelo SQL continua PascalCase; a view faz a ponte.

### Duas naturezas de tabela

| natureza | tabelas | como se escreve |
|---|---|---|
| **SÉRIE** (particionada por data) | `NegociosBrutos` (`dtNegocio`), `NegociosProcessados` (`dtLiquidacao`), `AnbimaIndicativos` (`dtReferencia`) | reescreve **o dia inteiro**. Reprocessar uma data é trocar a partição, nunca dar UPDATE em linha |
| **ESTADO** (arquivo único) | `InfoAtivos`, `FluxoAtivos`, `MtmAnbima`, `Outstanding` | reescreve o arquivo **inteiro** |

Reescrever tudo funciona porque as tabelas de estado são minúsculas: `InfoAtivos` 0,20 MB
e `FluxoAtivos` 0,35 MB. **Foi essa medição que tornou o Parquet puro viável** — a objeção
inicial (54 pontos de escrita mutável no código) caiu quando ficou claro que "reescrever a
tabela inteira" custa milissegundos.

Só as duas maiores são particionadas de propósito: 20 mil linhas/dia já é arquivo pequeno,
e particionar tabela miúda multiplica arquivos à toa — o que deixa Athena **e** DuckDB mais
lentos, não mais rápidos.

### A API

```python
import dados as D

D.Consultar(sql, params)      # DataFrame; aceita `?` como o sqlite3
D.Escalar(sql, params)        # primeiro valor
D.GravarDia(tabela, df, data) # reescreve a partição de um dia
D.GravarTudo(tabela, df)      # reescreve a tabela inteira
D.Upsert(tabela, df, data)    # o ON CONFLICT do SQLite, em memória
D.Invalidar()                 # recria as views após gravar
```

`Upsert` casa pela **CHAVE lógica** declarada por tabela, concatena com o que já existe e
faz `drop_duplicates(keep="last")`. Só é possível porque a unidade de reescrita (uma
partição, ou uma tabela de estado) cabe na RAM.

---

## Fim do `idTrade`

Era `INTEGER PRIMARY KEY AUTOINCREMENT`: existia **porque o SQLite o dava de graça**. Fora
dele, ninguém gera esse número.

A chave passou a ser a natural que a própria B3 manda: **`cdIdentificadorNegocio`**
(`"#10019847"`), conferida única nas 680.651 linhas da base. `idGrupoNegocio` é UUID e não
dependia do `idTrade`, então não foi afetado.

Renomeado nos 6 scripts que o usavam. O soft-cancel do `scrape_b3_boletim` carregava as
duas chaves lado a lado e ficou com uma só.

---

## Dois gotchas que já morderam

1. **Tipo explícito, nunca inferido.** O SQLite é de tipagem dinâmica, o Parquet é tipado.
   Uma coluna que venha **toda NULL** num dia faz o pyarrow inferir o tipo `null`, e a
   leitura quebra ao juntar essa partição com as outras. Por isso o `ESQUEMA` em
   `dados.py` declara o tipo de cada coluna — ele é o que a DDL era no `db.py`.

2. **`hive_types` forçado a VARCHAR.** Sem isso o DuckDB **deduz** o tipo da coluna de
   partição pelo nome da pasta: `dtReferencia=2026-07-28` vira `DATE`, enquanto as demais
   datas são texto ISO. Qualquer comparação entre as duas estoura com
   `Cannot compare DATE and VARCHAR`. Vale lembrar disso ao registrar a tabela no Athena:
   **a coluna de partição tem de ser declarada `string`**.

---

## Migração executada

`codigos/migrar_para_parquet/migrar_para_parquet.py` — dry-run por padrão, `--executar`
para valer. Traduz o vínculo do `idTrade`, confere a contagem tabela a tabela e **relê tudo
pelo DuckDB** antes de dar por feito.

Rodado sobre a base real em 28/08:

| | |
|---|---|
| linhas migradas | **1.582.200** em 7 tabelas |
| tempo | 23 s |
| tamanho | **388 MB (SQLite) → 41,6 MB (Parquet zstd)** |

---

## Os scripts, convertidos (31/08/2026)

**Os 21 scripts falam Parquet.** O `Helpers/db.py` e o `codigos/migrar_split_bases/` foram
removidos; nada mais importa o SQLite fora do `ipca.db`/`di.db`, que são contrato com a
calculadora e ficam. Os três notebooks e o `tests_fase1.py` foram junto.

Teste de aceitação em cada etapa: **34 pregões, 2.567 ativos**. O volume terminou em
**R$ 53.265,78 MM** contra os R$ 53.265,83 MM do baseline — a diferença é dado novo, não
conversão: ver "A deriva de R$ 0,05 MM" abaixo.

### O que a camada de dados ganhou para isso caber

O `Upsert` original troca a **linha inteira**. Isso serve a quem é dono de todas as colunas
(os negócios, o MtM), mas `InfoAtivos` tem **cinco escritores parciais** — b3_bond_details,
anbima_data, fianalytics_planilha, anbima_debentures, anbima_cri_cra — e cada um só conhece
um pedaço. Converter um deles para `Upsert` apagaria, em silêncio, o que os outros
escreveram.

`Mesclar()` traz as três expressões que o `ON CONFLICT DO UPDATE` usava, como **política por
coluna**:

| política | o SQL que ela traduz |
|---|---|
| `SOBRESCREVER` | `coluna = excluded.coluna` |
| `PREFERIR_NOVO` | `coluna = COALESCE(excluded.coluna, coluna)` |
| `PREFERIR_ATUAL` | `coluna = COALESCE(coluna, excluded.coluna)` |

Havia uma quarta forma, condicional a **outra** coluna (`CASE WHEN excluded.vrDuration IS
NOT NULL THEN excluded.dtAtualizacaoDuration ELSE ...`). Ela não precisa de política: quem
monta o DataFrame deixa a coluna dependente **nula** quando a condição é falsa, e
`PREFERIR_NOVO` dá o mesmo resultado.

Cada script convertido carrega um mapa `POLITICA_*` ao lado, com a expressão SQL que ele
substitui. **Três diferenças entre scripts que o SQL escondia** ficaram explícitas:

- `cdIndexador` é `PREFERIR_ATUAL` no cri_cra e no fianalytics, e `PREFERIR_NOVO` no
  debentures. Não é descuido: o cri_cra **chuta** `PREFIXADO` quando o CSV não traz índice,
  e a FI Analytics só preenche buraco — nenhum dos dois pode passar por cima do que a
  Anbima ou a B3 apuraram.
- `vrDuration` sobrescreve no curva_di e preserva no ntnb: o DI1 tira a duration do próprio
  vértice e ela sempre vem; a da NTN-B depende da cascata FI→B3, que pode devolver `None`.
- `vrSpreadAnbima` **sai** do DataFrame de `AnbimaIndicativos`. Quem o calcula é o
  `calc_spread_anbima`, e como `Mesclar` só toca coluna presente, ele sobrevive.

Outras peças novas: `Ler`, `Linhas`/`Tuplas`/`Linha` (o `fetchall`/`fetchone` do
`sqlite3.Row`, por nome e por posição), `Apagar` (o `DELETE ... WHERE col IN`),
`SubstituirFluxo` e `SincronizarFluxos` (versão em lote), e `Geracao` (contador de escrita
por tabela).

### O trigger, resolvido

`trgInfoAtivosInvalidaFluxo` desceu para `Mesclar()`, e mora lá **pelo mesmo motivo que
morava no banco**: é o único caminho de escrita coluna a coluna de `InfoAtivos`, então
nenhum dos cinco escritores pode esquecer. A comparação é null-safe, como o `IS NOT` do
trigger — reescrever o mesmo valor não invalida; preencher um `NULL` invalida.

### Gravar em LOTE, não por ativo

`InfoAtivos` e `FluxoAtivos` são **arquivos únicos**. Gravar ativo a ativo os reescreveria
inteiros uma vez por ativo. Todo script que passa por milhares deles acumula em memória e
grava uma vez — que é o que o `commit()` único do SQLite já era na prática:

- `scrape_b3_bond_details`: `GravarAtivo` virou `PrepararAtivo()` puro + `GravarLote()`.
- `scrape_anbima_data_ativos`: os workers acumulam num lote compartilhado, descarregado a
  cada 200 ativos. Perder um lote num crash não custa scraping — o checkpoint JSON já foi
  gravado.
- `match_referencias` e `validar_calc_b3`: os `UPDATE` por ativo viraram um `Mesclar` no fim.

**A exceção documentada** é o `RefrescarCadastroB3` do `validar_calc_b3`: ele grava um ativo
por vez de propósito, porque o resultado da gravação é reavaliado na hora e não há lote a
formar. Só os reprovados chegam lá, e `--sem-refresh` desliga o passo.

### `CarregarAtivo` ganhou um índice

No SQLite custava dois acertos de índice e ninguém contava. No DuckDB **cada consulta reabre
os parquets**, e o `match_referencias` e o `validar_calc_b3` chamam isso uma vez por ativo,
milhares por rodada. `Helpers/calc.py` passa a manter `InfoAtivos` + `FluxoAtivos` (0,55 MB)
em memória.

A guarda contra dado velho é a **geração** de `dados.py`: quem grava a incrementa, e o
índice se reconstrói na leitura seguinte. Sem isso o cache devolveria, por exemplo, o
`stFluxoValidado` de antes da validação.

O mesmo padrão vale para as consultas ponto a ponto: `calc_spread_anbima` e
`calc_spread_over` passam a ler o `MtmAnbima` de uma vez, e o `match_referencias` busca
candidatos **por curva**, não por ativo.

### Três armadilhas que morderam de verdade

1. **`Conformar` passava float com NaN para coluna `int64`** e o pyarrow recusa (*"Cannot
   convert non-finite value"*). Coluna inteira vai por `"Int64"`, o inteiro nulável do
   pandas. Sem isso, **toda** gravação com linha nova quebraria.
2. **`NULL` volta como `NaN`, e `NaN != NaN`.** O `SincronizarFluxoAtivos` achava que toda
   agenda idêntica tinha mudado — reescrevia e **invalidava a validação todo dia**.
   Resolvido por `SemNaN()`, aplicado também no `Linhas`/`Tuplas` (o consumidor testa
   `is None` e faz aritmética com o valor; `NaN` passaria calado pelos dois).
3. **`groupby.nth(-1)` deixou de agregar no pandas 2.** Era como o `Dobrar()` implementava o
   `SOBRESCREVER` (o único modo que precisa do último valor *inclusive nulo*); passou a
   devolver as linhas originais com o índice original, e a gravação estourava com
   `"['cdIdentificadorNegocio'] not in index"`. Agora vai por `agg(iloc[-1])`.

### A deriva de R$ 0,05 MM

Depois de re-rodar `calc_taxa_negocios` e `filtrar_trades` sobre 28/07, o relatório passou de
R$ 53.265,83 MM para R$ 53.265,78 MM. **Não é a conversão.** Comparando partição a partição
contra o baseline: **50 negócios** que estavam com `vrTaxaCalculada` NULL ganharam taxa — a
FI Analytics e a B3 responderam agora para operações que não responderam quando a base foi
montada, que é exatamente o que o script retenta. Negócio sem taxa fica **fora** dos filtros
BROKER e PF por construção, então 4 dos 50 passaram a ser dedupáveis e saíram de VALIDO.

### O que este trabalho descobriu — e precisa de decisão

Duas coisas apareceram sozinhas e estão em [[98 - Backlog]]:

- **A B3 manda negócio sem identificador (`-`)** — 25 no pregão de 28/07. Como o campo virou
  a chave, eles colapsam. E, diferente do SQLite (onde o `UNIQUE` era global), no Parquet a
  chave só é única **dentro da partição**: com um `-` por pregão, o `JOIN` do relatório
  conta o negócio duas vezes. Medido: R$ 3,24 MM de inflação. **Essa parte é regressão do
  Parquet.**
- **`cdSituacao != 'Cancelado'` não pega o vocabulário da B3**, que escreve `Cancelado B3` e
  `Cancelado Parcial B3` — 13.854 negócios cancelados contam como bons. Pré-existente.

---

## Pendências do lado da AWS

Nada foi testado contra a AWS de verdade: o PC do banco precisa que Quant/TI configurem
acesso antes.

- [ ] Confirmar que dá para **escrever** no bucket (é o único requisito de verdade).
- [ ] Descobrir se o usuário pode **registrar tabela** no Glue/Athena. Se não puder, os
      arquivos vão para o S3 do mesmo jeito e outra equipe registra depois.
- [ ] Ao registrar: **coluna de partição como `string`**, e preferir *partition projection*
      a crawler (dispensa `MSCK REPAIR TABLE` a cada carga).

O relatório HTML vai para um **dashboard que o time de Quant já tem** e liberou espaço —
não é problema nosso. Enquanto não sobe, o `gerar_relatorio_credito` continua gerando o
HTML localmente, lendo dos parquets.

---

Ver também: [[04 - Banco de Dados]] (o schema que o Parquet herdou), [[03 - Estrutura de Pastas]], [[09 - Progresso]] e [[98 - Backlog]].
