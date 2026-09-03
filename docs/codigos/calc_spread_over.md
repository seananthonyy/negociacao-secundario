# calc_spread_over.py

**Passo 19** do pipeline. **A entrega final.**

---

## Overview

Calcula o `vrSpreadOver` de cada negócio: quanto ele paga **acima da curva de
referência**. É o número que a mesa lê.

---

## Regras de negócio

Por negócio, nesta ordem:

| situação | `vrSpreadOver` |
|---|---|
| `vrTaxaCalculada` é NULL | NULL — sem taxa não há spread |
| `cdReferencia` é NULL | NULL — ativo sem referência casada |
| `cdReferencia = 'FUNDING'` | a própria `vrTaxaCalculada` |
| senão | `((1 + taxa/100) / (1 + taxaRef/100) − 1) × 100` |

**A referência é buscada por `dtNegocio`, não por `dtLiquidacao`.** Cada negócio tem a
sua, e uma mesma liquidação mistura várias — é o que torna obrigatório ter MtM das duas
pontas (`X-1u` e `X`).

**`FUNDING` é o caso do papel que não tem curva comparável:** o spread é a própria taxa.

**Ignora `cdStatus = 'BROKER'`** — o spread do grupo BROKER é calculado no relatório, a
partir da taxa média das duas pernas.

---

## CLI

```powershell
python codigos\scripts\calc_spread_over\calc_spread_over.py --date 2026-09-02
python codigos\scripts\calc_spread_over\calc_spread_over.py --start 2026-09-01 --end 2026-09-02
```

| argumento | efeito |
|---|---|
| `--date` | Uma `dtLiquidacao` |
| `--start` / `--end` | Intervalo |

---

## Interação com a base

**Lê:** `NegociosProcessados` (os negócios com taxa), `InfoAtivos` (`cdReferencia`),
`MtmAnbima` (a tabela **inteira**, de uma vez).

**Grava:** `NegociosProcessados` — só `vrSpreadOver`, com `SOBRESCREVER` (porque gravar
NULL também é decisão).

---

## Detalhes técnicos

**A `MtmAnbima` é lida de uma vez, para um dict.** Antes era uma consulta por negócio —
dezenas de milhares por data — e no DuckDB cada uma reabre os parquets. A tabela toda são
poucos milhares de linhas.

A chave do dict é `(cdTicker, dtReferencia)`, e não só o ticker, porque a busca é por
`dtNegocio`.

---

## Armadilhas

**`nullSemMtm > 0` no resumo é sintoma de MtM faltando**, não de negócio ruim. Quase
sempre é o passo 7 ou 8 que não rodou para uma das pontas.

**Rodar antes do `match_referencias` deixa tudo NULL.** O spread depende de
`cdReferencia`, que é o passo 18 que preenche.

**Spread de `%CDI` não é bps.** É multiplicador do CDI — o relatório exibe diferente, e
comparar as duas escalas na mesma média não faz sentido.
