# calc_taxa_negocios.py

**Passo 14** do pipeline.

---

## Overview

Resolve a **taxa de cada negócio** e cria as linhas em `NegociosProcessados`. É o passo que
transforma preço em taxa — o que o boletim da B3 não faz para a maioria das operações.

---

## Regras de negócio

### A cascata

| # | fonte | `cdFonteTaxa` | quando |
|---|---|---|---|
| 1 | o próprio boletim | `NULL` | `vrTaxaNegocio` não é nulo — copia direto |
| 2 | **calculadora local** | `Calc` | ativo com `stFluxoValidado = 1` e indexador em `["CDI+","IPCA","PREFIXADO"]` |
| 3 | FI Analytics | `FiAnalytics` | |
| 4 | B3 Calculator | `B3` | |
| 5 | — | `NULL` | todas falharam |

**`%CDI` fica de fora da calc local**, por decisão histórica: a medição de 13/07/2026
apontou divergência de até 13,7 bps no desconto fora do par. Essa medição estava
**inflada ~10×** (os "bps" eram pontos de `%CDI`, não taxa a.a.) e a re-medição correta
deu **0,05 bps de mediana**. A decisão de trazê-lo de volta está aberta no backlog.

### Negócio já com taxa não é recalculado

Sem `--force`, o script carrega as taxas já gravadas para aquela data e só processa o que
falta. Isso é o que torna o reprocessamento barato — e o que faz `--force` ser necessário
quando a régua muda.

**Negócio cancelado pela B3 é ignorado** (`dados.NaoCancelado()`).

---

## CLI

```powershell
python codigos\scripts\calc_taxa_negocios\calc_taxa_negocios.py --date 2026-09-02
python codigos\scripts\calc_taxa_negocios\calc_taxa_negocios.py --date 2026-09-02 --limit 40
```

| argumento | efeito |
|---|---|
| `--date` | Uma `dtLiquidacao` |
| `--start` / `--end` | Intervalo |
| `--force` | Recalcula também o que já tem taxa |
| `--limit N` | Só N negócios, **priorizando os que precisam de API** — smoke test |
| `--workers N` | Threads da fase de API |
| `--sem-calc` | Desliga o degrau 2, para comparação A/B |

---

## Interação com a base

**Lê:** `NegociosBrutos` (os negócios do dia), `NegociosProcessados` (quais já têm taxa),
`InfoAtivos` + `FluxoAtivos` (via `CarregarAtivo`, para a calc local).

**Grava:** `NegociosProcessados`, em dois lotes:
- **linha nova** entra inteira, com `cdStatus = 'VALIDO'` como default;
- **linha existente** leva só `vrTaxaCalculada`, `cdFonteTaxa` e `dtProcessamento`.

Partir em dois é obrigatório: `Mesclar` aplica a política a toda coluna presente no
DataFrame, e sobrescrever aqui apagaria o `cdStatus` do `filtrar_trades` e o
`vrSpreadOver` do `calc_spread_over`.

---

## Detalhes técnicos

**O cache é quem faz o serviço, não o pool.** Medido no pregão mais cheio: 8.065 negócios
validados colapsam em **598 pares (ticker, data, PU) distintos** — o cache corta 93% do
trabalho. A ~0,7 s por par, dá ~7 min/dia num core, contra os ~25 min/dia da cascata de API
no banco. A calc é CPU-bound, então o `ThreadPoolExecutor` não a paraleliza (GIL).

Alerta de volume: negócio sem taxa acima de `[alerta] volumeMinSemTaxa` entra no email.

---

## Armadilhas

**Rodar antes do `validar_calc_b3` usa a calc em ativo não confirmado.** O gate é o passo
13 por isso.

**Rodar depois do `filtrar_trades` não adianta.** O filtro só atualiza `cdStatus` em linhas
que já existem — é este script que as cria.

**Falha da calc local não aborta o negócio:** ele cai para FI/B3. Mas a contagem por ticker
vai para o email — falha em massa num ticker indica cadastro quebrado, não azar.

**`%CDI` e não-validados custam API.** No banco, cada chamada paga handshake pelo proxy, e
as da B3 são contadas. É o passo mais lento do pipeline.
