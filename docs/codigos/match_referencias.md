# match_referencias.py

**Passo 18** do pipeline.

---

## Overview

Preenche o `cdReferencia` de `InfoAtivos`: contra qual vértice de curva o spread de cada
papel deve ser medido.

Roda **sem argumentos**, idempotente, sobre a base inteira, a cada ciclo.

---

## Regras de negócio

### O casamento

| indexador do papel | referência |
|---|---|
| `IPCA` | vértice de **NTN-B** de duration mais próxima |
| `PREFIXADO` | contrato **DI1** de duration mais próxima |
| `CDI+` / `%CDI` | `FUNDING` — o spread é a própria taxa |

**Duration-match, com data exata.** A duration do papel é comparada com a dos vértices em
`MtmAnbima` **na mesma data de referência** — não com a média nem com o vértice mais
recente.

### Não sobrescreve a Anbima

Ativo com `cdFonteReferencia = 'Anbima'` tem a referência que a própria Anbima publicou, e
ela vence o duration-match. O script só preenche o que está vazio ou o que ele mesmo
atribuiu.

### O pré-passo de duration

Ativo sem `vrDuration` não pode ser casado. Antes do match, o script calcula a duration de
quem está com ela NULL, descontando na **taxa de emissão** e usando a curva mais recente
disponível.

**Ele só preenche NULL**, nunca recalcula. Quando a calc melhora, a melhoria não alcança
quem já tem duration gravada — é item aberto do backlog (`--forcar-duration`).

### O timer da referência

`dtAtualizacaoReferencia` faz a referência **envelhecer**: a atribuição da Anbima deixa de
ser eterna e é reavaliada após 30 dias. Há também o gatilho de referência órfã — NTN-B que
venceu deixa de ser referência válida.

---

## CLI

```powershell
python codigos\scripts\match_referencias\match_referencias.py
```

Sem argumentos, de propósito: o casamento é global e depende do estado corrente da base,
não de uma data.

| argumento | efeito |
|---|---|
| `--force` | Reavalia também quem está dentro da janela de 30 dias |

---

## Interação com a base

**Lê:** `InfoAtivos` (indexador, duration, referência atual) e `MtmAnbima` (os vértices).

**Grava:** `InfoAtivos` — `cdReferencia`, `cdFonteReferencia`, `dtAtualizacaoReferencia`
e, no pré-passo, `vrDuration` + `dtAtualizacaoDuration`. Em lote, num `Mesclar` só.

---

## Detalhes técnicos

Local, sem rede — exceto o pré-passo de duration, que pode chamar a calc.

---

## Armadilhas

**Rodar depois do `calc_spread_over` não adianta.** O spread lê o `cdReferencia` no
momento em que roda; mudá-lo depois não recalcula nada.

**Referência sem MtM na data do negócio dá spread NULL**, mesmo com o match certo. São
problemas diferentes: um é do match, outro é do passo 7/8.

**A duration gravada nunca é recalculada.** 19 papéis carregam valores de uma versão
anterior da calc. O impacto foi medido — nenhum deles mudaria de vértice — mas a dívida
existe.
