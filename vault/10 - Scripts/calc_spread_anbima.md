# calc_spread_anbima.py

> **Fase F15.** Calcula `vrSpreadAnbima` para cada ticker/data em `AnbimaIndicativos`, usando as taxas de referência em `MtmAnbima` (NTN-B via [[scrape_anbima_ntnb]] e curva DI via [[scrape_b3_curva_di]]).

**Arquivo:** `code/scripts/calc_spread_anbima.py`

---

## Conceito

O spread Anbima é uma propriedade do **ativo na data** — não de cada trade individual. Por isso é calculado uma vez por `cdTicker/dtReferencia` e gravado em `AnbimaIndicativos.vrSpreadAnbima`.

No relatório, o join é:
```sql
NegociosProcessados JOIN AnbimaIndicativos ON cdTicker + dtLiquidacao
```

---

## Lógica de cálculo

Para cada linha em `AnbimaIndicativos` da data solicitada:

| Condição | vrSpreadAnbima |
|---|---|
| `vrTaxaAnbima IS NULL` | `NULL` (sem taxa Anbima) |
| `cdReferencia IS NULL` em `InfoAtivos` | `NULL` (ativo sem referência cadastrada) |
| `cdReferencia = 'FUNDING'` (CDI+ / %CDI) | `= vrTaxaAnbima` (spread é a própria taxa) |
| `cdReferencia` não encontrado em `MtmAnbima` na data | `NULL` (sem curva na data) |
| Caso geral | fórmula exponencial |

**Fórmula exponencial:**
```python
vrSpread = ((1 + vrTaxaAnbima/100) / (1 + vrTaxa/100) - 1) * 100
```

Onde `vrTaxa` vem de `MtmAnbima` para o `cdReferencia` do ativo na mesma data.

**Por que exponencial?** Taxas de renda fixa são compostas. A diferença aritmética `vrTaxaAnbima - vrTaxa` não captura o efeito correto de compounding — a fórmula acima dá o spread verdadeiro em % a.a.

---

## Fontes de `cdReferencia` em `InfoAtivos`

| `cdIndexador` | `cdReferencia` | Fonte da taxa de referência |
|---|---|---|
| `CDI+` ou `%CDI` | `FUNDING` | (sem lookup em MtmAnbima) |
| `IPCA` | `NTN-B YY` (ex: `NTN-B 35`) | `MtmAnbima` via [[scrape_anbima_ntnb]] |
| Pré (DI futuro) | `DI1FYY` (ex: `DI1F27`) | `MtmAnbima` via [[scrape_b3_curva_di]] |
| `PREFIXADO` | `NULL` | (sem spread) |

---

## Idempotência

- Sem `--force`: pula tickers que já têm `vrSpreadAnbima IS NOT NULL`. Retorna imediatamente para datas já calculadas.
- Com `--force`: recalcula todos os tickers da data, sobrescrevendo valores anteriores.

---

## CLI

```powershell
python scripts/calc_spread_anbima.py --date 2026-06-03
python scripts/calc_spread_anbima.py --start 2026-06-01 --end 2026-06-05
python scripts/calc_spread_anbima.py --date 2026-06-03 --force
```

| Flag | Obrigatório | Descrição |
|---|---|---|
| `--date` | sim (ou `--start`) | Data única de referência |
| `--start` / `--end` | sim (juntos) | Intervalo de datas |
| `--force` | não | Recalcula mesmo se já calculado |

---

## Tabela destino

```sql
UPDATE AnbimaIndicativos
SET vrSpreadAnbima = ?
WHERE cdTicker = ? AND dtReferencia = ?
```

Não insere linhas novas — apenas atualiza `vrSpreadAnbima` nos registros já existentes (inseridos pelos scrapers Anbima).

---

## Resumo de execução (email e log)

O script loga e envia por email uma tabela com contadores por data:

| Contador | Significado |
|---|---|
| `Calculado` | spreads gravados (inclui FUNDING) |
| `Funding` | subconjunto de Calculado com `cdReferencia = 'FUNDING'` |
| `NullSemTaxa` | sem `vrTaxaAnbima` em `AnbimaIndicativos` |
| `NullSemRef` | sem `cdReferencia` em `InfoAtivos` |
| `NullSemMtm` | `cdReferencia` não encontrado em `MtmAnbima` na data |
| `JaCalc` | já tinha spread e `--force` não foi passado |

**Resultado do teste (03/06/2026):** 1.418 spreads calculados de 1.643 tickers totais. 0 `NullSemMtm`.

---

## Dependências

- `lib/db.py`, `lib/logger.py`, `lib/email_outlook.py`
- Tabelas: `AnbimaIndicativos`, `InfoAtivos`, `MtmAnbima`
- Deve rodar **depois** de: [[scrape_anbima_ntnb]], [[scrape_b3_curva_di]] (para que `MtmAnbima` esteja populado)

---

**Decisão (06/06/2026) — spread em AnbimaIndicativos, não em NegociosProcessados:** o spread é uma propriedade do ativo na data de referência, independente de qual trade específico estamos olhando. Gravar em `AnbimaIndicativos` evita redundância (N trades com o mesmo spread para o mesmo ativo/data) e facilita reprocessamento: recalcular o spread não exige reprocessar os trades.

Ver também: [[04 - Banco de Dados]], [[08 - Match de Referencia]], [[10 - Scripts/scrape_anbima_ntnb]], [[10 - Scripts/scrape_b3_curva_di]]
