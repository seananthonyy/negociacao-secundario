# scrape_outstanding_bloomberg.py

> Puxa o **outstanding** (saldo em circulação / `AMT_OUTSTANDING`) dos ativos da **Bloomberg** e popula a tabela `Outstanding`. Implementado em 28/06/2026.

---

## O que faz

Para cada dia do intervalo informado:

1. **Monta o conjunto de tickers a buscar** pela **união** de duas fontes:
   - **Negociados** — tickers com `dtNegocio` naquele dia em `NegociosBrutos` (exclui `cdSituacao = 'Cancelado'`).
   - **Anbima** — tickers com `dtReferencia` naquele dia em `AnbimaIndicativos` (debêntures / CRI / CRA divulgados pela Anbima).
2. Filtra `NTN-B*` e `DI1*` (esses vivem em `MtmAnbima`, não nessas tabelas — entram por segurança).
3. **Re-run inteligente:** sem `--force`, pula os pares `(ticker, data)` que já têm `vrOutstanding` não-nulo em `Outstanding`.
4. **Busca na Bloomberg** — agrupa por data e faz **1 chamada `blp.bdp` por data** (a lista de tickers daquele dia + override `AMOUNT_OUTSTANDING_AS_OF_DT`).
5. **UPSERT** em `Outstanding (cdTicker, dtOutstanding, vrOutstanding)`, com `dtOutstanding` = a data de negócio/divulgação.
6. Email de conclusão via Outlook (`[OK]`/`[ERROR]`), com summary por data (alvo / com valor / sem valor).

Um ticker tradado em 03/06 **e** divulgado pela Anbima em 04/06 gera **dois pares** → busca o outstanding em cada data.

---

## Tabela destino

```sql
Outstanding (cdTicker, dtOutstanding, vrOutstanding)
PRIMARY KEY (cdTicker, dtOutstanding)
```

- `cdTicker`: ticker do ativo (deb/CRI/CRA).
- `dtOutstanding`: data de negócio e/ou divulgação Anbima (ISO `YYYY-MM-DD`).
- `vrOutstanding`: `AMT_OUTSTANDING` na data (float, ou `NULL` se a Bloomberg não retornar).

Série temporal por ativo (um outstanding por ticker por data), seguindo o padrão de `MtmAnbima`/`AnbimaIndicativos`.

---

## CLI

```powershell
python scripts/scrape_outstanding_bloomberg.py --date 2026-06-03
python scripts/scrape_outstanding_bloomberg.py --start 2026-06-01 --end 2026-06-05
python scripts/scrape_outstanding_bloomberg.py --start 2026-06-01 --end 2026-06-05 --force
```

`--force` rebusca também os pares `(ticker, data)` já gravados.

---

## Bloomberg (`xbbg`)

Usa `xbbg.blp.bdp` com o campo `AMT_OUTSTANDING` e o override de data `AMOUNT_OUTSTANDING_AS_OF_DT` (formato `YYYYMMDD`). O ticker é enviado como `"{cdTicker} Corp"`; o retorno vem num DataFrame indexado por `"{ticker} Corp"`, coluna `amt_outstanding`. `NaN`/ticker ausente → `vrOutstanding = NULL`.

> **Só roda no PC do banco**, com terminal Bloomberg logado. O `import xbbg` é **local** (dentro do fetch), então o resto do script (montagem das listas via SQL) é inspecionável/compilável sem o pacote. **Não testável no PC pessoal.** `xbbg>=0.7` adicionado ao `requirements.txt`.

---

## Uso no pipeline

Roda como **script avulso** (igual aos demais scrapers) — ainda **não** está encaixado no pipeline diário automático. Popula `Outstanding`, que é consumida pela aba **Visão Anbima** do `gerar_relatorio_credito.py` (peso da média ponderada por indexador = outstanding real, date-matched). Ver [[gerar_relatorio_credito]].

**Ordem natural de uso no banco:** rodar para o range já carregado (ex.: `--start 2026-06-09 --end 2026-06-25`) → regenerar o relatório geral → a Visão Anbima passa a aparecer ponderada por outstanding real.

---

## Dependências

- `xbbg` (Bloomberg; só no banco)
- `lib/db.py`, `lib/logger.py`, `lib/email_outlook.py`

---

Ver também: [[04 - Banco de Dados]], [[gerar_relatorio_credito]], [[../09 - Progresso]]
