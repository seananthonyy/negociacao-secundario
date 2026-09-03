# calc_spread_anbima.py

**Passo 17** do pipeline.

---

## Overview

Calcula o `vrSpreadAnbima` das **taxas indicativas** — o spread do preço de referência que
a Anbima publica, contra a curva. É o análogo do `calc_spread_over`, mas sobre a
indicativa em vez do negócio.

Serve à aba **Visão Anbima**, que mostra onde o mercado está marcado, independentemente do
que negociou.

---

## Regras de negócio

Mesma fórmula do spread do negócio:

```
((1 + vrTaxaAnbima/100) / (1 + taxaRef/100) − 1) × 100
```

A referência vem de `InfoAtivos.cdReferencia` e a taxa dela de `MtmAnbima`, na **mesma
`dtReferencia`** da indicativa — aqui não há a assimetria negócio/liquidação do
`calc_spread_over`.

Sem referência, ou sem MtM na data, o spread fica NULL.

---

## CLI

```powershell
python codigos\scripts\calc_spread_anbima\calc_spread_anbima.py --date 2026-09-01
```

| argumento | efeito |
|---|---|
| `--date` | Uma `dtReferencia` |
| `--start` / `--end` | Intervalo |

**Roda em `X-1u`**, não em `X`: é a data das indicativas que o relatório exibe.

---

## Interação com a base

**Lê:** `AnbimaIndicativos` da data, `InfoAtivos` (`cdReferencia`), `MtmAnbima`.

**Grava:** `AnbimaIndicativos` — só `vrSpreadAnbima`, com `SOBRESCREVER`.

---

## Detalhes técnicos

Local, sem rede. A `MtmAnbima` é lida inteira para um dict, pelo mesmo motivo do
`calc_spread_over`.

---

## Armadilhas

**A data é `X-1u`, e é fácil errar.** Rodar em `X` calcula o spread de indicativas que o
relatório daquele dia não mostra.

**Indicativa sem ativo em `InfoAtivos` fica sem spread.** Acontece com papel que a Anbima
publica e que nunca negociou — o cadastro é puxado por demanda.
