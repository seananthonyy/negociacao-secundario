# Relatório final — FASE 3: unificação da máquina de fluxo de caixa

> Encerramento da FASE 3 / Tarefa 2 (`GerarFluxosFuturos`, item #9). Executado em
> 22–23/07/2026. Motor: repo `calculadora-renda-fixa`; orquestração: `negociacao-secundario`.
> Branch `refactor/gerador-fluxos-unificado` nos dois repos.

---

## 1. O problema

Havia **7 implementações** do mesmo walk de fluxo (VNA → incorporação → amortização →
desconto): `CalcularVna`, `_CaminharVnaDi`, `CalcularPuOperacao`, `_FluxosDescontaveis`,
`CalcularDuration`, `_EventosOperacaoDi`, `_FluxosDescontaveisDi`.

Isso não era dívida estética — **custou dois bugs do mesmo tipo**:

1. **Incorporação ignorada no PU** (corrigida na FASE 1): errava 1,3% fora do par e era
   invisível no par, então um gate que só testasse o par aprovava o ativo com taxa errada.
2. **Incorporação ignorada na DURATION** (encontrada e corrigida agora): a função
   desempacotava `pctIncorp` no laço e nunca o usava. Errava até **1,53 ano**.

O segundo bug é a prova do argumento: a correção da FASE 1 entrou num caminho e faltou
nos outros. Enquanto houvesse N cópias, a N-ésima continuaria errada em silêncio.

---

## 2. A nova arquitetura

**Um gerador, várias estratégias.**

```
GerarFluxosFuturos(dataCalc, dataInicioRent, taxaEmissao, fluxo, vne,
                   estrategia, tipoAmort, ctx)  ->  yield (dataEv, du, valorFuturo)
```

É o **único** ponto com a máquina de estado. Carrega `vna` (saldo devedor), `face` (face
original atualizada) e `ancora` (data do último evento), e por evento futuro aplica
incorporação → amortização → truncamento, entregando o valor de face **sem desconto**.
Quem desconta é o consumidor.

O que varia por indexador vive na Strategy:

| método | IPCA / PREFIXADO | %CDI / CDI+ |
|---|---|---|
| `VnaNaData` | `CalcularVna` (indexado ou nominal) | `_CaminharVnaDi` |
| `FatorPeriodo` | `round((1+j)^(du/252), 9)` | `_FatorPeriodoDi` |
| `FatorDescontoBase` | `1.0` | Fator DI puro (`CDI+`) / `None` (`%CDI`) |
| `AplicarTaxaDesconto` | `round((1+j)^(du/252), 9)` | `base × (1+spread)^(du/252)` ou `FatorDi` |

`FatorDescontoBase` / `AplicarTaxaDesconto` partem o desconto na fatia que **não** depende
da taxa negociada e na que depende — é o que permite ao solver PU→taxa caminhar o fluxo
uma vez só. Em `%CDI` a base é `None` porque o percentual entra *dentro* do fator diário
e não há o que separar.

**Todos os consumidores passaram a ser map/reduce sobre o mesmo gerador:**

| consumidor | redução |
|---|---|
| `CalcularPuOperacao` | `Σ Trunca(FV / FatorDesconto, 6)` |
| `CalcularDuration` | `[Σ(du·VP) / Σ VP] / 252` |
| `CalcularDv01` | materializa o fluxo 1×, desconta 2× (taxa e taxa+1bp) |
| `CalcularTaxaNegociacao` | walk 1×, Newton só redesconta |

**Removidas** (7 cópias → 1): `_FluxosDescontaveis`, `_DescontarPu`,
`_FluxosDescontaveisDi`, `_DescontarPuDi`, `_EventosOperacaoDi`, `_PuOperacaoDi`,
`_DurationDi`. Sobraram só os helpers de DI que a Strategy consome (`_CaminharVnaDi`,
`_FatorPeriodoDi`, `FatorDi`, `_PuParDi`).

---

## 3. Acurácia contra a B3

### 3.1 PU de operação — 6 tickers de controle (2026-07-17, par e +100 bps)

| ticker | PU par (B3) | rel. par | PU +100bps (B3) | rel. +100bps |
|--------|------------:|---------:|----------------:|-------------:|
| TRGP13 | 1061,9752 | 1,04e-08 | 987,4162 | 1,01e-08 |
| SABP13 | 1257,4243 | 8,51e-08 | 1140,4463 | 8,77e-08 |
| PLAC23 | 1301,5045 | 9,30e-08 | 1213,1039 | 9,07e-08 |
| RALM11 | 1239,2728 | 6,54e-08 | 1145,8456 | 6,55e-08 |
| BARU11 | 1272,1743 | 9,28e-08 | 1187,6653 | 9,35e-08 |
| MNAU18 | 1081,0643 | 1,94e-08 | 1000,3674 | 2,30e-08 |

Patamar de **1e-8 mantido**, idêntico ao medido antes da refatoração.

### 3.2 PU — base inteira, bit-a-bit contra o legado

4.889 ativos × (par, +100 bps) = **9.778 PUs**, `dataCalc` 2026-07-17:

| indexador | n | bit-a-bit idêntico | pior erro absoluto | pior erro relativo |
|---|---:|---:|---:|---:|
| %CDI | 570 | **570 (100%)** | 0 | 0 |
| CDI+ | 5264 | **5264 (100%)** | 0 | 0 |
| IPCA | 3260 | 1948 (59,8%) | 1,16e-10 | 9,32e-16 |
| PREFIXADO | 684 | 473 (69,2%) | 9,09e-13 | 4,54e-16 |

A diferença em IPCA/PREFIXADO é **exclusivamente ordem de somatório**: o caminho DI já
usava `sum()` (que no CPython ≥ 3.12 compensa por Neumaier) e o IPCA usava `+=` ingênuo.
Unificou-se em `sum()`, que é numericamente superior. O desvio resultante — **9,3e-16
relativo, épsilon de máquina** — está 7 ordens abaixo da concordância de 1e-8 com a B3 e
10 abaixo da régua de 1e-5 do gate.

### 3.3 Duration — a correção de bug, validada por fonte externa

Duration calculada a partir do **`cashFlowList` da própria B3**
(`D = [Σ(du_i · PV_i) / Σ PV_i] / 252`), contra a calc nova e a antiga:

| ticker | D (B3) | D nova | D antiga | nova − B3 | antiga − B3 |
|--------|-------:|-------:|---------:|----------:|------------:|
| PLAC23 | 7,9918 | 7,9918 | 6,5260 | **0,0000** | −1,4658 |
| BARU11 | 7,5303 | 7,5303 | 6,2190 | **−0,0000** | −1,3113 |
| PTCE11 | 5,6767 | 5,6767 | 4,7872 | **−0,0000** | −0,8895 |
| RALM11 | 8,6300 | 8,6300 | 7,2789 | **−0,0000** | −1,3512 |
| SABP13 | 10,7547 | 10,7547 | 9,2222 | **−0,0000** | −1,5325 |
| MNAU18 | 8,4363 | 8,4363 | 7,2932 | **−0,0000** | −1,1431 |

A duration nova reproduz a B3 **a 4 casas em 6/6**; a antiga errava de 0,89 a 1,53 ano.

Alcance na base: dos 4.889 ativos, mudaram **35** — 33 IPCA + 2 PREFIXADO — que são
**todos e só** os que têm evento de incorporação futuro. Sempre para cima (capitalizar
cupom empurra caixa para a frente), de +0,4% a +33,5% (SRQE21: 12,39 → 16,53 anos).
Nenhum papel DI mudou. Os demais ficaram em ≤ 1e-15 relativo.

#### Impacto a jusante — verificado após o merge (23/07/2026)

Uma versão anterior deste relatório advertia que os 35 ativos poderiam trocar de
referência no `match_referencias.py`. **Isso estava errado**, e a execução do script
depois do merge provou: **1 único ativo mudou de referência na base inteira** — `ENERB8`
(`None` → `NTN-B 35`), que não é um dos 35 e apenas ganhou uma atribuição que não tinha.

O mecanismo real é outro. O `match_referencias.py` **não chama a calc ao vivo**: ele lê
`InfoAtivos.vrDuration` já persistida, e o pré-passo que a calcula roda apenas
`WHERE ia.vrDuration IS NULL`. Ou seja, quem já tem duration gravada **não é
recalculado**. Dos 35 afetados:

- **16 estão com `vrDuration` NULL** → recebem a duration nova e correta assim que
  negociarem e o pré-passo rodar. A correção propaga sozinha nesses.
- **19 já têm `vrDuration` gravada** → mantêm o valor antigo indefinidamente; não há
  hoje caminho para a duration corrigida alcançá-los.

O risco prático é baixo, porque os valores gravados vieram de fonte externa (FI / B3 /
Anbima) e **já estavam mais próximos da calc nova do que da antiga**:

| ticker | gravada | calc nova | calc antiga |
|---|---:|---:|---:|
| MNAU18 | 8,0400 | 8,4363 | 7,2932 |
| RALM11 | 8,3000 | 8,6300 | 7,2789 |
| SABP13 | 9,3400 | 10,7547 | 9,2222 |
| CCPV11 | 3,3800 | 3,3808 | 3,0078 |

Os deltas contra o valor gravado (mediana ~0,2 ano, máximo 1,42 em SABP13) são pequenos
demais para trocar o vértice da NTN-B — daí o match não ter mexido em ninguém. A lacuna
de atualização em lote está registrada no backlog (flag `--forcar-duration`).

### 3.4 DV01 e taxa

| métrica | resultado |
|---|---|
| DV01 | **bit-a-bit idêntico** em 4.889/4.889 ativos, nos 4 indexadores |
| taxa (round-trip PU→taxa) | exato nos 4 indexadores; 599/600 da amostra idênticos (1 caso a 1e-4, o arredondamento do `round(taxa, 4)`) |

O solver ganhou de brinde a correção de uma inconsistência latente: o par de helpers do
DI somava com `+=` enquanto o `_PuOperacaoDi` usava `sum()` — o PU do solver e o PU
público podiam divergir no último bit. Agora é o mesmo gerador e o mesmo somatório.

---

## 4. Performance

160 ativos (40 por indexador), `dataCalc` 2026-07-17, média de 2 rodadas:

| operação | antes | depois | ganho |
|---|---:|---:|---:|
| PU de operação | 29,6 ms/ativo | 27,3 ms/ativo | **−8%** |
| taxa (PU→taxa) | 89,6 ms/ativo | 72,0 ms/ativo | **−20%** |
| DV01 | 58,4 ms/ativo | 35,8 ms/ativo | **−39%** |

O DV01 quase caiu pela metade porque passou a caminhar o fluxo uma vez em vez de duas
(o caro é o `ContarDu`, O(dias) por evento). O ganho na taxa vem do `%CDI`, que antes
refazia o walk inteiro a cada iteração do Newton e agora também reusa o fluxo.

---

## 5. Gate de validação

`validar_calc_b3.py --dry-run` sobre 80 ativos já validados (20 por indexador), 3 datas:

| execução | confiáveis | reprovados | acurácia PU ≤ 1e-5 |
|---|---:|---:|---:|
| calc **nova** | 36 (B3) | 43 | 45,6% (36/79) |
| calc **antiga** | 36 (B3) | 43 | 45,6% (36/79) |

**Resultado idêntico ao da calc antiga**, quebra por indexador inclusive. O gate não é
afetado pela refatoração — que é exatamente o que se queria demonstrar.

Com a cascata da FI ligada (execução normal, sem `--sem-fi`): **77/80 confiáveis**, 41
resgatados pela FI, só 3 rebaixados. Os 44 rebaixamentos que aparecem com `--sem-fi` são
artefato de desligar o resgate, não deterioração da base.

> **Nota de método:** rodar o gate na base inteira significaria milhares de chamadas ao
> `/calcPU` da B3 de madrugada. Usou-se amostra estratificada em `--dry-run` (sem
> gravar). A checagem de não-regressão que cobre a base inteira é a da seção 3.2.

---

## 6. Pendência aberta

`docs/BACKLOG_INCORP.md` — inconsistência **latente** entre a semântica de incorporação
dos eventos futuros (agora correta, no gerador) e a dos eventos passados
(`_CaminharVnaDi` e `CalcularVna`, que usam `elif` e descartam a amortização do evento).

Hoje é inerte: os 23 eventos CDI+ de incorporação parcial e os 39 com
incorporação+amortização na mesma data **já são todos passados**, e nenhum papel DI tem
evento divergente no futuro. Quebra por calendário, não por código. Tratar antes que um
desses eventos vire futuro.

---

## 7. Commits

**`calculadora-renda-fixa`** (branch `refactor/gerador-fluxos-unificado`):

- `fd75b7e` — `refactor(calc): unify cash flow walk into GerarFluxosFuturos strategy engine`
- `1a18411` — `refactor(calc): port duration, dv01 and yield solver to unified generator`
- `53be2f5` — `refactor(calc): remove dead flow-walk duplicates + regenerate bundle`

**`negociacao-secundario`** (mesmo branch):

- `3732389` — `comparar_calcpu_b3` consome o gerador + `docs/BACKLOG_INCORP.md`
- *(commit do aniversário na duration)* — `fix(calc): passa o aniversario na duration`
- *(este)* — relatório final

Nenhum push em `master`/`main`: tudo em branch, para revisão antes do merge.
