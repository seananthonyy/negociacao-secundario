# 17 — Armazenamento Parquet e AWS

> **Estado: fundação pronta, scripts ainda não migrados.** A camada de dados existe e está
> testada; os 21 scripts continuam falando SQLite. Ver §"O que falta".
> Branch: `refactor/split-bases`. Última sessão: 29/08/2026.

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

## O que falta

**Os 21 scripts ainda chamam `ObterBanco()` e falam SQLite.** A fundação está pronta e
testada, mas nada do pipeline foi convertido.

Ordem sugerida:

1. **Os fáceis** — quem só faz `INSERT`/upsert por data: os scrapers da Anbima, NTN-B,
   curva DI, boletim. Viram `D.Upsert(tabela, df, data)`.
2. **Os que dão trabalho** — `filtrar_trades` (marca `cdStatus` linha a linha) e
   `calc_taxa_negocios` (upsert com `ON CONFLICT`). Os `UPDATE` viram
   *ler a partição → alterar em pandas → regravar o dia*.
3. **`gerar_relatorio_credito`** — o SQL sobrevive; troca só a conexão. É o teste de
   aceitação: tem de sair **34 pregões, 2.567 ativos, R$ 53.265,83 MM**.
4. **Aposentar** `Helpers/db.py`, `migrar_split_bases.py` e os `.db`.

Cuidados:

- **O trigger `trgInfoAtivosInvalidaFluxo` não existe mais.** Ele zerava a validação
  quando uma coluna do fluxo mudava. Vira código Python em quem escreve `InfoAtivos`
  (ver `COLS_INVALIDAM_FLUXO` no `db.py`). **Se isso for esquecido, a calc local passa a
  precificar com fluxo desatualizado, em silêncio.**
- `SincronizarFluxoAtivos()` (só grava se mudou) precisa de equivalente.
- O **add-in do Excel** lê `InfoAtivos`/`FluxoAtivos` em SQLite. Ficou **fora de escopo por
  decisão do usuário** (28/08) — ele resolve num código à parte.

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
