# Retomar aqui

> Escrito em **01/09/2026**, ao fim da sessão que terminou a migração para Parquet.
> Branch: **`refactor/split-bases`**, HEAD `43dd552`, árvore limpa, **não mergeado**.

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

### Pendência que sobrou desta sessão (a mais concreta)

**Re-rodar `filtrar_trades` na base toda.** Só rodei para 28/07. Os outros 33 pregões ainda
têm o `cdStatus` **gravado** com os negócios cancelados dentro do pareamento de duplicados.
O relatório já sai certo (ele filtra na leitura), mas a classificação gravada não. Muda
número histórico — o usuário ainda não decidiu.

```bash
cd code/codigos/filtrar_trades
NEGSEC_SEM_EMAIL=1 python filtrar_trades.py --start 2026-06-09 --end 2026-07-28
```

### Backlog — 14 itens abertos, em `vault/98 - Backlog.md`

Os dois pedidos mais recentes do usuário, e os mais diretos de atacar:

- **Mostrar o %par dos negócios.** A coluna `% PU Par` já vem na planilha da FI Analytics e
  é ignorada no parse; a calc local também produz o PU par.
- **Incluir o dia de hoje no relatório, marcado como PRÉVIA.** Atenção: o corte em D-1
  existe porque o livro de hoje é a perna D+1 do pregão anterior. O aviso resolve o engano,
  mas o dia incompleto não deveria entrar nas médias como pregão normal.

O item **🔴 prioridade** continua sendo *a calc não reproduz a taxa fora do par (2 a 14
bps)* — é ele que trava a troca da precificação pela calc local.

E um achado desta sessão que virou item: o campo **`note`** do `getBondDetails` traz
`"O ativo considera apenas a variação positiva do IPCA"` — **piso de deflação**, que a nossa
calc não modela. Um ativo com essa nota **já está validado** (`22D1289011`): a calc bateu o
PU porque nenhuma das 3 datas do gate cruzou deflação. Divergência dormindo.

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
