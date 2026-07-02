# Filtro de Qualidade de Trades

> Ver também: [[00 - Inicio]] | [[04 - Banco de Dados]] | [[01 - O Que Faz]]

Script responsável: `code/scripts/filtrar_trades.py`

---

## Por que existe

O boletim B3 registra múltiplos tipos de operações que inflam ou distorcem a leitura de mercado:

- **Passagem de fundo** — dois fundos negociam o mesmo ativo entre si (transferência interna); geram dois registros espelhados com PU praticamente idêntico.
- **Corretor** — o broker registra dois lados da intermediação: um comprador e um vendedor com taxa quase idêntica mas volumes distintos. A perna maior é o registro interno do broker.
- **Pessoa física (PF)** — operação estruturada onde o spread cobrado da PF distorce a taxa de mercado. A PF negocia sempre numa taxa pior que o mercado (mais alta se vendendo, mais baixa se comprando).

O filtro classifica esses casos para que o relatório exiba apenas os trades que refletem o preço real de mercado.

---

## Quatro status possíveis

| `cdStatus` | Critério | Significado |
|---|---|---|
| `VALIDO` | — | Trade de mercado legítimo — conta individualmente no relatório |
| `FUNDO` | Filtro 1 (PU) | Passagem de fundo — ambas as pontas marcadas |
| `BROKER` | Filtro 2 (taxa) | Operação de corretagem — **todos** os trades do grupo marcados; exibidos no relatório de forma agregada por `idGrupoNegocio` |
| `PF` | Filtro 3 (taxa, referência) | Operação de pessoa física — a perna com taxa mais distante do mercado |

**Como BROKER aparece no relatório:** trades com `cdStatus = 'BROKER'` são agrupados por `idGrupoNegocio`. Cada grupo gera uma linha agregada com:
- Taxa = `(MAX(vrTaxaCalculada) + MIN(vrTaxaCalculada)) / 2` — mid entre as pontas do corretor
- Volume = `SUM(vrVolume) / 2` — uma ponta apenas (evita dupla contagem; correto pois qtds das duas pontas são iguais)
- Spread = fórmula exponencial aplicada à taxa_media acima vs `MtmAnbima[cdReferencia][dtNegocio]` — calculado no gerador do relatório em Python

Pode haver múltiplos grupos BROKER (`idGrupoNegocio` distintos) para o mesmo ticker no mesmo dia.

Trades `FUNDO` e `PF` não aparecem no relatório. Permanecem em `NegociosProcessados` para auditoria.

---

## Filtro 1 — Passagem de Fundo

Aplicado em dois sub-critérios sequenciais dentro do mesmo union-find transitivo:

### (a) Negócios exatamente iguais — adicionado 11/06/2026

**Critério:** 2 ou mais negócios com exatamente o mesmo `(cdTicker, vrQuantidade, vrVolume)` na mesma `dtLiquidacao` — todos recebem `FUNDO`, independente de qualquer threshold.

Este pre-pass captura repasses internos onde o PU é literalmente idêntico (mesmo valor financeiro, mesma quantidade). O critério de threshold abaixo não chega a ser avaliado para esses grupos.

### (b) Threshold de PU

**Critério:** par com mesma `(cdTicker, dtLiquidacao, vrQuantidade)` onde:

```
|PU_i - PU_j| <= fundoMaxReaisPorMilhao * PU_medio / 1_000_000
```

Equivale a: diferença financeira total <= R$ `fundoMaxReaisPorMilhao` por R$1MM de notional.

**Resultado (ambos os sub-critérios):** todos os trades do componente union-find recebem `cdStatus = 'FUNDO'` e o mesmo `idGrupoNegocio`.

**Observações:**
- Union-find é transitivo: se A-B é FUNDO e B-C é FUNDO, todos os três viram FUNDO.
- Trades sem `vrTaxaCalculada` participam normalmente — PU = `vrVolume / vrQuantidade` sempre existe.
- Threshold configurável: `config.toml filtro.fundoMaxReaisPorMilhao` (padrão: 100.0 R$/MM).

**Decisão (11/06/2026):** o critério de exatos foi adicionado como pre-pass (a) antes do threshold (b) em `_AplicarFiltroFundo`. Motivação: casos onde fundos repassam posições a PU exatamente igual não eram capturados quando a diferença de PU era literalmente zero mas o threshold era avaliado de forma condicional. A nova regra garante a captura sem depender do parâmetro `fundoMaxReaisPorMilhao`.

---

## Filtro 2 — Corretor

Dois sub-critérios (a implementar em sequência):

### (a) Par com mesma quantidade

**Critério:** entre os não-FUNDO com taxa não-NULL, par com mesma `(cdTicker, dtLiquidacao, vrQuantidade)` e diferença de taxa <= tolCorretor:

| Indexador | Threshold | Configurável em |
|---|---|---|
| Não-%CDI | `corretorMaxBps / 100` p.p. | `config.toml filtro.corretorMaxBps` (padrão: 2.5 bps) |
| `%CDI` | `corretorMaxPctCdi` em unidades %CDI | `config.toml filtro.corretorMaxPctCdi` (padrão: 0.25) |

**Resultado:** todos os trades do componente → `BROKER` (incluindo o que antes era `VALIDO`).

### (b) Bloco + splits — **pendente de implementação**

**Critério:** 1 trade bloco com `qty = Q` + 2+ trades splits onde `sum(qty_splits) = Q` (exato) e `|taxa_split - taxa_bloco| <= tolCorretor` para cada split.

**Algoritmo:** DFS com backtracking sobre candidatos com `qty < Q`, ordenados desc. Blocos tentados do maior para o menor. Mínimo de 2 splits (senão é o caso (a) acima).

**Resultado:** todos — bloco e splits — recebem `BROKER` com o mesmo `idGrupoNegocio`.

**Observações gerais:**
- Trades com `vrTaxaCalculada = NULL` são excluídos deste filtro.
- Union-find/DFS permite grupos com N trades.
- Múltiplos grupos BROKER por ticker/dia são normais e esperados.

---

## Filtro 3 — Pessoa Física

**Critério:** entre os trades ainda não-classificados (não-FUNDO, não-BROKER, não-VALIDO-de-par) com taxa não-NULL, par com mesma `(cdTicker, dtLiquidacao, vrQuantidade)` e diferença de taxa > tolPF:

| Indexador | Threshold | Configurável em |
|---|---|---|
| Não-%CDI | `pfMinBps / 100` p.p. | `config.toml filtro.pfMinBps` (padrão: 20.0 bps) |
| `%CDI` | `pfMinPctCdi` em unidades %CDI | `config.toml filtro.pfMinPctCdi` (padrão: 2.0) |

**Eleição do VALIDO (mais próximo da taxa de referência):**

A PF pode estar comprando OU vendendo, gerando taxa abaixo ou acima do mercado. Por isso não podemos simplesmente escolher "o de menor taxa". Usamos uma taxa de referência externa:

1. `vrTaxaAnbima` de `AnbimaIndicativos` para a data (melhor opção)
2. Mediana ponderada por volume dos trades não-FUNDO/não-BROKER com taxa (fallback)

O trade mais próximo da referência recebe `VALIDO`; os mais que `tolPF` afastados do eleito recebem `PF`.

Tiebreak de equidistância: menor `vrVolume` vira `VALIDO` (consistente com critério CORRETOR).

**Observações:**
- Se não houver referência disponível para o ticker, o grupo não é classificado (todos ficam `VALIDO`).
- Para grupos com 3+ trades, o winner (mais próximo da ref) fica VALIDO; qualquer outro mais que `tolPF` afastado do winner vira PF.
- Trades com `vrTaxaCalculada = NULL` são excluídos deste filtro.

---

## Detecção de %CDI

Usa `cdIndexador` de `InfoAtivos` quando disponível.
Fallback: `vrTaxaCalculada > 70` → assume %CDI (CDI+ e IPCA+ ficam abaixo de 20; %CDI fica na faixa 80–120).

---

## Trades com taxa NULL

- **Filtro FUNDO:** participam normalmente (PU existe via volume/quantidade).
- **Filtros CORRETOR e PF:** excluídos (sem taxa para comparar).
- Resultado: ficam sempre com `cdStatus = 'VALIDO'` isolados. Contabilizados em `nullTaxa` no email.

---

## Regra: trades Cancelados são ignorados

O script nunca processa trades com `cdSituacao = 'Cancelado'` em `NegociosBrutos`.

---

## Idempotência

Cada rodada recalcula do zero para a `dtLiquidacao` — sobrescreve `cdStatus` e `idGrupoNegocio` de todos os trades daquele dia. Necessário para cobrir o caso de trades cancelados entre rodadas.

---

## CLI

```powershell
# Data única
python scripts/filtrar_trades.py --date 2026-05-27

# Janela histórica
python scripts/filtrar_trades.py --start 2026-05-01 --end 2026-05-27

# Parâmetros customizados
python scripts/filtrar_trades.py --date 2026-05-27 --fundo-max 80 --corretor-bps 1.0
python scripts/filtrar_trades.py --date 2026-05-27 --pf-min-bps 15 --pf-min-pctcdi 1.5
```

| Flag | Padrão | Descrição |
|---|---|---|
| `--date` | — | Data única (`dtLiquidacao`) |
| `--start` / `--end` | — | Intervalo de datas |
| `--fundo-max N` | 100.0 | Threshold FUNDO em R$/MM de notional |
| `--corretor-bps N` | 1.5 | Tolerância CORRETOR em bps (não-%CDI) |
| `--corretor-pctcdi N` | 0.15 | Tolerância CORRETOR em unidades %CDI |
| `--pf-min-bps N` | 20.0 | Threshold mínimo PF em bps (não-%CDI) |
| `--pf-min-pctcdi N` | 2.0 | Threshold mínimo PF em unidades %CDI |

---

## Parâmetros em `config.toml`

```toml
[filtro]
fundoMaxReaisPorMilhao  = 100.0   # R$/MM de notional para passagem de fundo
corretorMaxBps          = 1.5     # bps máx para par corretor (não-%CDI)
corretorMaxPctCdi       = 0.15    # unidades %CDI máx para par corretor
pfMinBps                = 20.0    # bps mín para par PF (não-%CDI)
pfMinPctCdi             = 2.0     # unidades %CDI mín para par PF
```

---

## Resumo do email

```
Data          Total   VALIDO  FUNDO   BROKER   PF  NullTaxa
----------    -----   ------  -----   ------   --  --------
2026-06-03    10200     9939    142       87   29         3
----------    -----   ------  -----   ------   --  --------
TOTAL         10200     9939    142       87   29         3
```
