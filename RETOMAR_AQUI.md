# Retomar aqui

> Reescrito em **02/09/2026, madrugada**. Branch: **`refactor/split-bases`**, HEAD
> `f986e10`, **árvore SUJA (nada commitado)**, **não mergeado**.
>
> ⚠️ **Comece por `git diff --stat`.** Há duas sessões de trabalho não commitado.
> Arquivos novos: `Helpers/datas.py`, `codigos/calc_pu_par/`. Tabela nova na base:
> **`PuPar`**. Removidos: `codigos/calc_pct_par/` e `codigos/migrar_split_bases/`.
> **Nada foi commitado de propósito** — revise antes.
>
> Teste de aceitação **re-conferido no fim**: 34 pregões, 2.568 ativos,
> **R$ 52.315,17 MM**. Idêntico. `conferir_dados` e `tests_fase1` passam.

Cole isto numa sessão nova:

> **"Leia `RETOMAR_AQUI.md`, `vault/17 - Armazenamento Parquet e AWS.md` e
> `vault/98 - Backlog.md`. Depois me diga o que você faria primeiro."**

---

## 1. Onde o projeto está

**A migração de SQLite para Parquet + DuckDB está COMPLETA e testada.** Os 21 scripts, os
três notebooks e o `tests_fase1.py` falam Parquet. O `Helpers/db.py` e o
`codigos/migrar_split_bases/` foram removidos.

Nada mais fala SQLite **exceto** `files/Database/ipca.db` e `di.db` — esses são **contrato
com a calculadora** e ficam como estão. `ObterBancoIpca()` / `ObterBancoDi()` em
`Helpers/calc.py` são legítimos; não "converta" isso.

**Estado do relatório geral, hoje:**

```
34 pregões | 2.568 ativos | R$ 52.315,17 MM   (2026-06-09 → 2026-07-28)
```

Esse é o número corrente — **não** o R$ 53.265,83 MM que o vault citava como baseline. Ele
mudou três vezes, por motivo conhecido e verificado (§4).

**Como reproduzir em ~1 min:**

```bash
cd code/codigos/gerar_relatorio_credito
NEGSEC_SEM_EMAIL=1 python gerar_relatorio_credito.py
```

**Como conferir a camada de dados (30 asserts, não toca a base real):**

```bash
python code/codigos/conferir_dados/conferir_dados.py
```

---

## 2. O que você precisa saber antes de escrever qualquer código

### `Mesclar()` é o `ON CONFLICT DO UPDATE`; `Upsert()` NÃO é

`Upsert()` troca a **linha inteira**. `InfoAtivos` tem **cinco escritores parciais**
(b3_bond_details, anbima_data, fianalytics_planilha, anbima_debentures, anbima_cri_cra) e
cada um só conhece um pedaço das colunas. Usar `Upsert` num deles **apaga em silêncio** o
que os outros escreveram.

Use `Mesclar(tabela, df, politica={...})`, com política por coluna:

| política | o SQL que traduz |
|---|---|
| `SOBRESCREVER` | `coluna = excluded.coluna` |
| `PREFERIR_NOVO` | `coluna = COALESCE(excluded.coluna, coluna)` |
| `PREFERIR_ATUAL` | `coluna = COALESCE(coluna, excluded.coluna)` |

Cada script já tem um mapa `POLITICA_*` no topo, com o SQL que ele substitui. **Leia o do
script antes de mexer** — a política difere entre scripts de propósito (ver nota 17).

### Grave em LOTE, nunca por ativo

`InfoAtivos` e `FluxoAtivos` são **arquivos únicos**. Gravar dentro de um laço reescreve a
tabela inteira a cada volta, **e** invalida o índice do `CarregarAtivo`. O padrão é:
acumula em memória → grava uma vez no fim. A única exceção documentada é
`RefrescarCadastroB3` (no `validar_calc_b3`), porque o resultado da gravação é reavaliado na
hora.

### O trigger virou código

`trgInfoAtivosInvalidaFluxo` mora dentro de `dados.Mesclar()` — e mora lá pelo mesmo motivo
que morava no banco: é o único caminho de escrita coluna a coluna de `InfoAtivos`. Se você
criar outro caminho de escrita, **a invalidação para de acontecer, em silêncio**, e a calc
passa a precificar com fluxo velho.

### Três armadilhas do Parquet que já morderam

1. Coluna inteira com NULL vai por `"Int64"` (o inteiro nulável do pandas). `int64` puro
   estoura no pyarrow.
2. `NULL` volta como `NaN`, e `NaN != NaN`. Use `dados.SemNaN()` antes de comparar.
3. `groupby.nth()` deixou de agregar no pandas 2. Use `agg(lambda s: s.iloc[-1])`.

---

## 3. Duas decisões do usuário, já implementadas (não reabra sem perguntar)

1. **Negócio que a B3 manda sem identificador (`-`) é DESCARTADO.** Eram 25 no pregão de
   28/07. O campo é a chave da base; guardá-los colapsava negócios e fazia o `JOIN` do
   relatório contar em dobro. O `scrape_b3_boletim` descarta no parse e avisa quantos.
2. **`Cancelado Parcial B3` conta como cancelado integral.** O predicado
   `dados.NaoCancelado()` usa prefixo `'Cancelado%'`.

---

## 4. Por que o número do relatório mudou (e por que está certo)

Se alguém comparar com o vault antigo e estranhar:

| passo | volume | ativos |
|---|---|---|
| baseline citado no vault (pré-sessão) | R$ 53.265,83 MM | 2.567 |
| re-rodar calc_taxa/filtrar em 28/07 | R$ 53.265,78 MM | 2.567 |
| corrigir o filtro de cancelado | R$ 52.318,42 MM | 2.568 |
| descartar os negócios sem identificador | **R$ 52.315,17 MM** | **2.568** |

- **−R$ 0,05 MM:** 50 negócios que estavam sem taxa ganharam uma — a FI/B3 responderam agora
  para operações que não responderam quando a base foi montada. Negócio sem taxa fica fora
  dos filtros BROKER/PF, então 4 saíram de VALIDO. **Dado novo, não conversão.**
- **−R$ 947,36 MM:** o filtro procurava `'Cancelado'` exato, mas a B3 escreve
  `'Cancelado B3'`. Eram **2.402 negócios cancelados** entrando como VALIDO/BROKER. O ativo
  **a mais** não é contradição: negócio cancelado também entrava no pareamento de
  duplicados, e tirá-lo reclassifica.
- **−R$ 3,25 MM:** era o negócio `EEELA1` (09/06) sendo contado duas vezes por causa do `-`.

---

## 5. O que fazer a seguir

### O que a sessão da madrugada de 01/09 entregou

| item do backlog | estado |
|---|---|
| **#2 — dia de hoje como PRÉVIA** | ✅ **feito.** `CalcularDtPrevia()`; selo no banner, no seletor do Boletim e no email; `--sem-previa` volta ao corte em D-1 |
| **#1 — %par dos negócios** | ✅ **feito** (v2, 02/09). Tabela `PuPar` por (ativo, data), cascata calc → B3 → FI, `%par` calculado na leitura, referência congelada por idade. Backfill: **45.714 de 50.512 pares (90,5%)** em 23min26s — 41.002 pela calc, 3.428 pela B3, 1.284 pela FI. No relatório, **90,5% dos ticker-dias com %par** (era 84% na v1), e os valores batem **idênticos** aos da v1 nos ativos que ambas cobrem. **Falta só você apontar em que outras abas quer o número** |
| **#5 — precisão da calc** | ⚠️ **medido, não decidido.** Ver abaixo — a justificativa do %CDI caiu |
| **#6 — migração pro banco** | ✅ **conversor endurecido** + Passo 6-B no `INSTALACAO_BANCO.md` |
| **#8 — histórico de planilhas** | ✅ **levantado.** O item encolheu muito — ver abaixo |

### O desenho do PU par, em quatro linhas (leia antes de mexer)

1. **`PuPar` é indexada por (ativo, DATA)**, não por ativo. O PU par acreta todo dia útil:
   reusar um valor por alguns dias dá erro sistemático de **0,368% na mediana em 7 dias
   úteis** (medido em 249 ativos), e ignora evento de fluxo na janela — **5% dos ativos
   negociados** tiveram evento em 7 dias, e o MATD23 amortizou 100%. Indexar por data ainda
   é mais **barato**: cada par se calcula uma vez na vida.
2. **O `%par` não é gravado.** Sai na leitura, `vrPU / vrPuPar × 100`. Se o cadastro de um
   ativo for corrigido, o `DescartarPuPar` apaga o puPar dele e o %par some junto — em vez
   de sobrar um número velho sobre um denominador descartado.
3. **Papel que sai do cadastro** (emissor perto do default): as calculadoras removem o
   título, o script para de conseguir gravar, e a **leitura** pega o último puPar com
   `dtReferencia <= dtLiquidacao`, exibindo a **idade**. Não existe status "congelado"
   gravado — a idade é o status (`PREGOES_CONGELADO = 5`).
4. **Idempotente:** reprocessar um pregão custa **zero** chamada de API.

⚠️ **Furo conhecido:** o gate roda com `--revalidar-dias 15`, então podemos levar até 15
pregões para perceber que a B3 largou um papel — e nesse meio-tempo a nossa calc segue
acretando um par contratual para um emissor que parou de pagar. Decisão consciente: fechar
isso exigiria sondar a B3 todo pregão para todo ativo validado.

### Decisão pendente #1 — o %CDI volta para a calc local? (a mais valiosa)

O `validar_calc_b3` calculava o `piorTaxa` e **jogava fora** — por isso a re-medição que o
backlog pedia desde julho nunca acontecia. Agora ele sai no CSV e no email. Rodado em 40
ativos %CDI de maior volume (R$ 7,16 bi), 3 datas:

| | mediana | p90 | pior |
|---|---|---|---|
| **taxa vs B3 (bps de yield)** | **0,054** | 0,219 | **0,458** |
| PU (erro relativo) | 2,5e-05 | 5,7e-05 | 8,0e-05 |

**40/40 dentro de 1 bp na taxa.** O %CDI está fora da calc local por causa de uma medição
inflada ~10× que ninguém refez. O que o reprova hoje é a régua de **PU** (`TOL_PU = 1e-5`),
que em termos econômicos é **10× mais apertada** que a `TOL_TAXA_BPS = 5,0` do mesmo gate.

**A pergunta para o usuário:** no %CDI, o gate julga por PU ou por taxa? Não mexi na régua —
mudar isso muda quais ativos precificam local × por API, e é decisão dele. Detalhe e as três
opções em `vault/98 - Backlog.md`, "Frente 1".

### Decisão pendente #2 — o `MtmAnbima` tem 3,5 meses de graça, e eles EXPIRAM

Bissectei a fonte da Anbima: os `.xls` diários existem **desde 2026-02-21** (20/02 dá 404).
O `AnbimaIndicativos` já está colado nessa borda, mas o **`MtmAnbima` só tem 36 dias**
(08/06 → 28/07). São ~3,5 meses de curva NTN-B disponíveis agora e que somem conforme a
janela desliza. Não precisa de código novo:

```bash
cd code
NEGSEC_SEM_EMAIL=1 python codigos/scrape_anbima_ntnb/scrape_anbima_ntnb.py     --start 2026-02-23 --end 2026-06-05
```

Não rodei porque escreve na base e não muda nenhum número atual (a base de negócios começa
em 09/06). Mas é valor que evapora.

### Pendência que sobrou da sessão anterior (continua de pé)

**Re-rodar `filtrar_trades` na base toda.** Só rodou para 28/07. Os outros 33 pregões ainda
têm o `cdStatus` **gravado** com os negócios cancelados dentro do pareamento de duplicados.
O relatório já sai certo (filtra na leitura), mas a classificação gravada não. Muda número
histórico — o usuário ainda não decidiu.

```bash
cd code/codigos/filtrar_trades
NEGSEC_SEM_EMAIL=1 python filtrar_trades.py --start 2026-06-09 --end 2026-07-28
```

### Uma limpeza que só você pode fazer (precisa de commit)

`code/tmp_parquet/` tem **4 parquets rastreados pelo git**, commitados por engano no
`895fd88` (o commit que criou o `conferir_dados`). Eram saída de teste. Desde 02/09 o
script usa a pasta temporária do sistema e não suja mais a árvore, e o `code/.gitignore`
já cobre o caminho — mas o que já está rastreado continua rastreado:

```bash
git rm -r --cached code/tmp_parquet
```

Não fiz porque mexe no índice do git, e commit é decisão sua.

### Achados de dado que valem investigação

- **`RBRAJ7` tem cadastro corrompido**: `vrVNE = 1,00` e `vrTaxaEmissao = 1,00`, fonte B3.
  O %par dele dá 0,0. Foi o cálculo do %par que o expôs.
- **Outros 4 ativos** com %par fora de 20–200% em 645 mil negócios: `RIOS21` (VNE 29,68),
  `BRKMA8`, `CRA0210059T`, `22E1338403`. Todos `cdFonteCadastro = 'B3'`.
- **825 ativos** ficam sem %par porque a calc falha — a maioria por **curva DI ausente no
  `di.db`** nas datas antigas (limitação desta máquina, não do código). No banco, com o
  `di.db` cheio, a cobertura sobe.

### Backlog — 9 itens abertos, em `vault/98 - Backlog.md`

Reorganizado a pedido do usuário: itens de AWS, campo `note`, ponto cego B3×FI e
`getBondDetails` como 2ª fonte foram removidos; "taxa fora do par" e "68 ativos" viraram um
item só (**Melhorar a precisão da calc local**).

O **campo `note` do `getBondDetails`** saiu do backlog por decisão do usuário, mas o risco
que ele descrevia continua real: `"O ativo considera apenas a variação positiva do IPCA"` é
**piso de deflação**, que a calc não modela, e o `22D1289011` está validado porque nenhuma
das 3 datas do gate cruzou deflação. Divergência dormindo.

### Bloqueado por terceiros

**AWS.** Nada foi testado contra a AWS de verdade — depende de Quant/TI liberarem o acesso
no PC do banco. Quando liberar, é **uma linha** do `config.toml`:

```toml
[dados]
raiz = "s3://bucket/negociacao"   # hoje: "files/Parquet"
```

---

## 6. Coisas que eu NÃO fiz de propósito

- **Não apaguei `files/Database/trades.db` e `ativos.db`** (357 MB + 30 MB). Estão fora do
  git — apagar é irreversível. Fica para o usuário, depois de conferir a base Parquet.
- **Não mergeei o branch.**
- **Não re-rodei o pipeline para datas novas.** A base continua terminando em 28/07/2026.
- **Não reescrevi as notas 03 e 04 do vault**, que descrevem o mundo SQLite. Ganharam um
  marcador no topo apontando para a 17. O *schema* que a 04 documenta continua valendo — o
  Parquet o herdou inteiro.
- **Não commitei nada** (sessão de 01/09 madrugada). A árvore está suja de propósito.
- **Não mexi na régua do gate** (`TOL_PU`) nem liguei o %CDI na calc local — é decisão do
  usuário, e muda número de produção.
- **Não rodei o backfill do `MtmAnbima`** nem o `filtrar_trades` na base toda: os dois
  escrevem na base e mudam número histórico.
- **Não regenerei o `bundle_banco.py`** (1,7 MB na raiz, gerado em 24/08). Ele está
  **defasado** — empacota código anterior ao Parquet. Regenerar com `make_bundle.py` só
  depois de commitar.
- **Não apaguei log nem relatório antigo** (335 MB em `files/logs`, 54 MB em
  `files/relatorios`). Tudo gitignored, mas é do usuário.
