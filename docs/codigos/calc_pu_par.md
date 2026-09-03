# calc_pu_par.py

**Passo 16** do pipeline.

---

## Overview

Grava o **PU par** de cada ativo que negociou, por data, na tabela `PuPar`. PU par é o PU
que o papel valeria precificado na **própria taxa de emissão**.

É o denominador do `%par` do negócio:

```
%par = vrPU / vrPuPar × 100
```

100 é no par, acima é ágio, abaixo é deságio. É como a mesa lê "caro ou barato" sem
depender de spread nem de match de curva — e, num papel que saiu do cadastro das
calculadoras, é o **único número que sobra** (taxa e spread somem junto com o cadastro).

**Este script não calcula o `%par`.** Ele grava só o `vrPuPar`; o `%par` sai na leitura do
relatório.

---

## Regras de negócio

### A chave inclui a DATA

PU par não é propriedade do ativo, é do par (ativo, data): ele **acreta todo dia útil**.
Guardar um valor por ativo e reusá-lo por alguns dias injeta erro **sistemático**, sempre
para cima — puPar velho é menor, então o `%par` lê alto.

Medido em 249 ativos validados, 7 dias úteis:

| indexador | n | mediana | p90 | máx |
|---|---|---|---|---|
| CDI+ | 109 | 0,411% | 0,462% | 4,73% |
| %CDI | 18 | 0,372% | 0,387% | 0,405% |
| PREFIXADO | 7 | 0,364% | 0,382% | 0,382% |
| IPCA | 115 | 0,250% | 0,292% | 0,382% |
| **TODOS** | **249** | **0,368%** | 0,437% | 4,73% |

Dói mais onde o número é mais útil: CDI+ e %CDI vivem entre 99 e 101, então a faixa
inteira do sinal são ~2 pontos e 0,4 come **20%** dela.

E há o **degrau**, pior que a deriva: evento de fluxo dentro da janela. Nos mesmos 7 dias,
**128 dos 2.583 ativos negociados (5%)** tiveram evento — um deles amortizou **100%**.

Indexar por data ainda sai **mais barato**: são ~50 mil pares distintos na base inteira, e
cada um se calcula **uma vez na vida**.

### Cascata

| # | fonte | `cdFontePuPar` | como |
|---|---|---|---|
| 1 | calculadora local | `Calc` | `CalcularPu(ativo, data, vrTaxaEmissao)`. Só `stFluxoValidado = 1` |
| 2 | B3 | `B3` | `CalcularPuGov(ticker, data, vrTaxaEmissao)` |
| 3 | FI Analytics | `FI` | `ChamarCompleto(...)`, campo `m2m` |
| 4 | — | — | não grava linha |

**O degrau 2 é o que faz o script valer a pena.** O ativo que o gate rebaixou é justamente
aquele em que a **B3 respondeu** e a nossa calc não reproduziu — então o puPar da B3 está
disponível e é autoritativo. Medido no backfill: num pregão a calc resolveu 847 ativos e a
**B3 resgatou mais 451**.

### Idempotência

Par `(cdTicker, dtReferencia)` que já existe **não é recalculado nem consultado em API** —
o gate é um `LEFT JOIN` na própria fila. O valor de um par não muda nunca: é o PU de um
papel numa data passada. **Reprocessar um pregão custa zero chamada.**

Quem invalida é `dados.DescartarPuPar`, chamado de dentro do `Mesclar`/`Sincronizar*`
quando o cadastro ou o fluxo muda de verdade. Aí o histórico **inteiro** daquele ticker
sai — inclusive as datas passadas, porque a agenda velha valia para elas também.

### Papel que sai do cadastro (distress)

Quando o emissor se aproxima do default, as calculadoras **removem o papel**. O mercado
passa a usar o último puPar conhecido como referência e negocia em *cents on the dollar*.

Este script não faz nada de especial: simplesmente **para de conseguir gravar linha nova**.
Quem resolve é a leitura, que pega o último puPar com `dtReferencia <= dtLiquidacao` e
exibe a **idade**. Não existe status "congelado" gravado — a idade *é* o status, e a
tabela fica só com valor real.

---

## CLI

```powershell
python codigos\scripts\calc_pu_par\calc_pu_par.py --date 2026-09-02
python codigos\scripts\calc_pu_par\calc_pu_par.py --tudo
```

| argumento | efeito |
|---|---|
| `--date` | Uma `dtLiquidacao` |
| `--start` / `--end` | Intervalo |
| `--tudo` | Todas as datas que a base tem |
| `--sem-api` | Só a calc local. Mede a cobertura própria sem gastar chamada |
| `--limite N` | Teto de ativos por data — fatia um backfill grande |

---

## Interação com a base

**Lê:** `NegociosProcessados` (a fila: quem negociou e ainda não tem puPar), `PuPar` (o
gate de idempotência), `InfoAtivos` (taxa de emissão, que é o argumento das APIs).

**Grava:** `PuPar`, por `Mesclar` com `SOBRESCREVER` — e não `GravarDia`, porque a
partição pode já ter linhas de uma rodada anterior que precisam ficar intactas.

---

## Detalhes técnicos

**Só a fase de API é paralelizada** (10 workers). A calc local é CPU-bound e o GIL não a
deixa escalar — mesma lição do `calc_taxa_negocios`, onde quem faz o serviço é o cache.

Backfill medido na base inteira: **45.714 de 50.512 pares (90,5%) em 23 min** — 41.002
pela calc, 3.428 pela B3, 1.284 pela FI.

**Banda de sanidade:** `20%` a `200%`, e **nada é filtrado por causa dela** — a contagem só
vai para o resumo. Uma primeira calibração com 50–150 marcava 8 negócios, e os 8 eram
Braskem, CSN e Light a ~48% do par: isso é *distress* de crédito, não erro de cadastro.
Alarme que dispara no caso legítimo mais interessante do dia ensina a ignorar o alarme.
O que a banda tem de pegar é erro de **unidade** (VNE em 1 no lugar de 1.000), que erra por
10× ou 100×.

---

## Armadilhas

**O selo de referência congelada tem DUAS leituras, e a diferença está na contagem.**
Poucos papéis com datas de congelamento **distintas** entre si é o caso real — saíram do
cadastro um a um. **Muitos** papéis com a **mesma** data não são defaults: é este script
que não rodou para o dia. Foi exatamente o que apareceu ao testar o relatório no meio do
backfill: 960 de 1.207 tickers marcados, todos apontando para o mesmo pregão.

**`stFluxoValidado != 1` não é erro.** Esses ativos caem no degrau 2 ou 3. Sem API
(`--sem-api`) eles simplesmente ficam sem puPar.

**Curva DI ausente derruba só papel CDI.** A calc levanta `ValueError` quando falta a
curva da data — o script conta como falha do ativo, cai para a API, e loga uma vez por
ticker para não virar ruído.

**PU par ≤ 0 é cadastro impossível, não erro da calc** (VNE zerado, fluxo que amortiza
100% antes da data). Dividir por ele daria infinito ou sinal trocado, então vira `None`.

**A coluna `% PU Par` da planilha da FI não serve de fonte.** Ela mede `preço indicativo
da FI ÷ puPar na data do download`; o nosso é `PU do negócio ÷ puPar na liquidação daquele
negócio`. Numerador e denominador diferentes.
