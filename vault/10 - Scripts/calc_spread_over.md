# calc_spread_over.py

> Ver também: [[04 - Banco de Dados]] | [[10 - Scripts/match_referencias]] | [[08 - Match de Referencia]]

Script: `code/scripts/calc_spread_over.py`

---

## O que faz

Calcula `vrSpreadOver` em `NegociosProcessados` para cada trade com `vrTaxaCalculada NOT NULL`, usando a taxa de referência em `MtmAnbima` na `dtLiquidacao` do trade.

Roda após [[10 - Scripts/match_referencias|match_referencias.py]] (que garante que `InfoAtivos.cdReferencia` está preenchido) e antes de `gerar_relatorio_html.py`.

---

## CLI

```powershell
# Data única (dtLiquidacao)
python scripts/calc_spread_over.py --date 2026-06-03

# Intervalo
python scripts/calc_spread_over.py --start 2026-06-01 --end 2026-06-05
```

`--date` e `--start/--end` referem-se a `dtLiquidacao` (não `dtNegocio`).

---

## Lógica por trade

| Condição | `vrSpreadOver` |
|---|---|
| `vrTaxaCalculada IS NULL` | NULL (trade sem taxa, pula) |
| `cdReferencia IS NULL` | NULL (ativo sem referência) |
| `cdReferencia = 'FUNDING'` | `= vrTaxaCalculada` (taxa já é o spread; indexadores CDI+/%CDI) |
| NTN-B ou DI1 sem curva na data | NULL (nunca usa 0 como substituto) |
| NTN-B ou DI1 com curva | fórmula exponencial (ver abaixo) |

### Fórmula para NTN-B / DI1

```python
vrSpread = ((1 + vrTaxaCalculada / 100) / (1 + vrTaxa / 100) - 1) * 100
```

`vrTaxa` vem de `MtmAnbima WHERE cdTicker = cdReferencia AND dtReferencia = dtLiquidacao`. Match exato de data — sem fallback para D-1 ou data mais próxima.

O resultado fica em **%** (não em bps). A conversão para bps é feita no template HTML na hora de exibir.

---

## Comportamento quando não há curva

Se `MtmAnbima` não tem a taxa de referência para a `dtLiquidacao` do trade, `vrSpreadOver` fica NULL. O script reporta esses casos no email de conclusão como `nullSemMtm`. Isso pode acontecer em feriados ou fins de semana quando os scripts de curva não foram rodados.

---

## Saída / email

```
Resultado por dtLiquidacao (NegociosProcessados):

Data          Total  Calculado  Funding  SemRef  SemMtm
------------------------------------------------------------
2026-06-03    20632      14643     2632    5989       0
------------------------------------------------------------
TOTAL         20632      14643     2632    5989       0

ATENCAO: 5989 trade(s) com vrSpreadOver = NULL:
  - 5989 sem cdReferencia em InfoAtivos
```

Campos:
- **Calculado** = total com `vrSpreadOver` preenchido (inclui FUNDING)
- **Funding** = subset dos calculados onde `cdReferencia = 'FUNDING'`
- **SemRef** = trades cujo ativo não tem `cdReferencia` em `InfoAtivos`
- **SemMtm** = trades com ref mas sem taxa da ref na data

---

## Validação em 03/06/2026

- 20.632 trades com `vrTaxaCalculada NOT NULL`
- 14.643 com spread calculado: 2.632 FUNDING + 12.011 pela fórmula
- 5.989 NULL por `cdReferencia IS NULL`
- 0 NULL por curva ausente

---

**Decisao (07/06/2026):** para `cdReferencia = 'FUNDING'`, `vrSpreadOver = vrTaxaCalculada` sem multiplicar por 100 (a taxa já é o spread em %; a exibição em bps é responsabilidade do template HTML). Isso difere do plano original (§9 PLANEJAMENTO_v5) que indicava `vrTaxaCalculada * 100`. A distinção foi necessária para manter consistência com `calc_spread_anbima.py`, que segue a mesma convenção.
