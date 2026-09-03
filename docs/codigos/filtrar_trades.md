# filtrar_trades.py

**Passo 15** do pipeline.

---

## Overview

Classifica cada negócio em `cdStatus` e agrupa os relacionados em `idGrupoNegocio`.

**Por que existe:** uma mesma operação econômica aparece **duas vezes** no boletim da B3.
Passagem de fundo e operação de corretor têm duas pernas, e somá-las infla o volume do
mercado. O filtro identifica os pares e diz qual perna conta.

---

## Regras de negócio

### Os quatro status

| status | o que é |
|---|---|
| `VALIDO` | negócio legítimo. **É o default** — quem não se enquadra nos outros fica aqui, inclusive quem está sem taxa |
| `FUNDO` | passagem de fundo — as duas pernas saem do relatório |
| `BROKER` | operação de corretagem — o grupo entra no relatório **agregado**, com volume dividido por 2 |
| `PF` | pessoa física — negócio pequeno, fora da leitura institucional |

### Como cada um é detectado

**`FUNDO`** — três sub-critérios, todos por `(cdTicker, dtLiquidacao)`:

1. dois ou mais negócios com **exatamente** a mesma quantidade e volume;
2. par com mesma quantidade e `|PU_i − PU_j| ≤ fundoMaxReaisPorMilhao × PU_médio / 1.000.000`
   — ou seja, diferença financeira de até R$ *N* por R$ 1 MM de notional;
3. **um bloco + N splits**: um negócio de quantidade *Q* e N ≥ 2 negócios somando *Q*, com
   o PU de cada split colado no do bloco pela mesma régua.

Negócio sem taxa entra também, usando `PU = vrVolume / vrQuantidade`.

**`BROKER`** — dois sub-casos, pela **taxa** em vez do PU:

1. par com mesma quantidade e diferença de taxa ≤ `corretorMaxBps`;
2. um bloco + N splits, com a mesma tolerância.

**Todos os negócios do grupo recebem `BROKER`**, e o relatório os agrega por
`idGrupoNegocio`, dividindo o volume por 2.

### A tolerância depende do indexador

Em `%CDI` a "taxa" é um **multiplicador** do CDI, não uma taxa a.a. — 1 ponto de `%CDI` ≈
10 bps de yield. Por isso o config tem pares de tolerância (`corretorMaxBps` /
`corretorMaxPctCdi`, `pfMinBps` / `pfMinPctCdi`).

### Agrupamento por union-find

Um negócio pode casar com vários. O union-find resolve os grupos transitivos e garante que
cada negócio pertence a **um** grupo, com `idGrupoNegocio` estável.

---

## CLI

```powershell
python codigos\scripts\filtrar_trades\filtrar_trades.py --date 2026-09-02
python codigos\scripts\filtrar_trades\filtrar_trades.py --start 2026-09-01 --end 2026-09-02
```

| argumento | efeito |
|---|---|
| `--date` | Uma `dtLiquidacao` |
| `--start` / `--end` | Intervalo |

---

## Interação com a base

**Lê:** `NegociosProcessados` da data, e `InfoAtivos` para o indexador (que escolhe a
tolerância).

**Grava:** `NegociosProcessados` — só `cdStatus` e `idGrupoNegocio`, por `Mesclar` com
`SOBRESCREVER` nessas duas colunas. Não toca em taxa nem em spread.

---

## Detalhes técnicos

Tudo local, sem rede. As tolerâncias vêm do bloco `[filtro]` do `config.toml`, calibradas
sobre a distribuição real.

---

## Armadilhas

**Ele só atualiza linhas que já existem.** Rodar antes do `calc_taxa_negocios` não faz
nada — e não dá erro. O sintoma é o dia inteiro ficar `VALIDO`.

**O relatório soma `VALIDO` + `BROKER`.** Excluir `BROKER` subestima o mercado em ~35% do
volume; contá-lo inteiro superestima. A regra é: agregar por grupo e dividir por 2.

**Negócio sem taxa é `VALIDO`, de propósito.** Ele não pode ser classificado como BROKER
(que depende de taxa), e sumir com ele esconderia volume real.

**Reprocessar uma data reescreve a classificação inteira.** É idempotente, mas o resultado
depende de quais negócios estão na base naquele momento — um soft-cancel posterior muda os
grupos.
