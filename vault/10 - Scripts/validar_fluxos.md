# validar_fluxos.py

> Decide **quais ativos a calculadora pode precificar** (`InfoAtivos.stFluxoValidado`). Passo **12** do pipeline. Migrado do projeto da calc em 12/07/2026; **reescrito em 13/07** quando a B3 virou fonte primária.

## O trabalho se parte em dois

**1. VALIDAÇÃO — só o fluxo de origem Anbima** (`cdFonteCadastro = 'AnbimaData'`).

O fluxo da B3 **já nasce validado** (`scrape_b3_bond_details` marca): ela *é* a fonte, não há contra o que conferir. Sobra validar o que veio da Anbima, e só a FI pode fazer isso.

Confere: `maturityDate`, `issueRate`, a **cauda futura** da agenda e o **SALDO**.

**Régua:** divergente **ou não-confirmável** ⇒ **não valida** (rigor > cobertura).

**Cobertura é magra:** a FI só tem **146** dos ~1.670 ativos que a B3 não cobre. B3 e FI cobrem quase o **mesmo universo** — onde uma falha, a outra falha junto. Não são fontes independentes de verdade. Por isso este ramo atende pouca coisa.

**FI é proibida** para ativo com incorporação: ela **omite** esses eventos, então nunca poderia confirmá-los. Fica não-validável — o que **não** é divergência.

**2. TRIPWIRE — o saldo devedor, em TODO ativo já validado**, de qualquer origem.

## Por que o tripwire existe

*"Fluxo da B3 = válido"* é uma **tautologia**: confere-se a B3 contra ela mesma. E fonte **apodrece** — o **EMIV11** foi aditado em fev/2026 (seis meses de carência de amortização), a agenda velha continuou de pé, o saldo ficou 25% errado e a taxa dava **−19,6%**.

O `ConferirSaldo` compara o **VNA que a nossa calc produz** com o **`adjustedFaceValue` da FI**. É o **único teste que enxerga o PASSADO** da agenda: a FI só devolve eventos **futuros**, então a comparação evento a evento cobre só a cauda — e a cauda bate perfeitamente num papel cuja agenda passada mudou. O saldo devedor não: ele é função de **toda** a agenda. Um número só, e já vem na resposta — **custo zero**.

**Cobertura do tripwire:** a FI cobre 1.957 dos 2.355 ativos de fonte B3 — **99,0% do volume negociado**.

**Divergiu ⇒ DESVALIDA.**

## Contrato das colunas

| coluna | quem escreve |
|---|---|
| `stTemFluxo` | o INGESTOR (scrapers). Este script **só lê**. |
| `stFluxoValidado` | aqui, ou o `scrape_b3_bond_details`. Zerado pelo ingestor quando o fluxo muda **de verdade**, e por este script quando o saldo diverge. |
| `dtValidacaoFluxo` | ISO da validação OK |
| `cdFonteValidacaoFluxo` | `'B3'` \| `'FiAnalytics'` |
| `dtUltimaTentativa` | ISO de **toda** tentativa — alimenta o throttle |

## Fila (idempotente, com throttle de 10 dias)

- **validação:** `stTemFluxo = 1 AND stFluxoValidado <> 1 AND cdFonteCadastro <> 'B3'` + throttle
- **tripwire:** `stFluxoValidado = 1` + throttle

A invalidação do ingestor **zera** `dtUltimaTentativa`, então ativo cujo fluxo mudou volta pro topo da fila na hora.

## O que saiu na reescrita

O ramo `ValidarPelaB3` (e as funções `ReferenciaB3`, `CarenciaCemNoInicio`, `MapearIndexador`) foi **removido**: a cascata B3 → FI acabou.

## Saída

`data/divergencias_fluxo.csv` — ticker, fonte, campo, situação, nosso valor, valor da fonte.

## CLI

```bash
python scripts/validar_fluxos.py                    # a fila (rotina)
python scripts/validar_fluxos.py --limite 50        # smoke
python scripts/validar_fluxos.py --tickers EMIV11   # ignora o throttle
python scripts/validar_fluxos.py --sem-tripwire     # só validação
```

## Estado em 13/07/2026

**3.027 validados** de 4.840 ativos (2.967 de fonte B3).
