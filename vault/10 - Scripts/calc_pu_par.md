# calc_pu_par.py

> Ver também: [[17 - Armazenamento Parquet e AWS]] | [[10 - Scripts/validar_calc_b3]] | [[16 - Confianca nos Validados (WIP)]] | [[11 - Pipeline de Execucao]]

Script: `code/codigos/calc_pu_par/calc_pu_par.py` — **passo 15** do pipeline.

Criado em **02/09/2026**, substituindo o `calc_pct_par` (v1 de 01/09, removida).

---

## O que faz

Grava o **PU par** de cada ativo que negociou, por data, na tabela `PuPar`.

PU par é o PU que o papel valeria precificado na **própria taxa de emissão**. É o
denominador do `%par` do negócio:

```
%par = vrPU / vrPuPar × 100
```

100 é no par, acima é ágio, abaixo é deságio. É como a mesa lê "caro ou barato" sem
depender de spread nem de match de curva — e, num papel que saiu do cadastro das
calculadoras, é o **único número que sobra** (taxa e spread somem junto com o cadastro).

> **Este script não calcula o `%par`.** Ele grava só o `vrPuPar`; o `%par` sai na
> **leitura**, no `gerar_relatorio_credito`. Ver *Por que o %par não é gravado*.

---

## CLI

```powershell
python codigos\calc_pu_par\calc_pu_par.py --date 2026-07-28
python codigos\calc_pu_par\calc_pu_par.py --start 2026-06-09 --end 2026-07-28
python codigos\calc_pu_par\calc_pu_par.py --tudo             # a base inteira
python codigos\calc_pu_par\calc_pu_par.py --tudo --sem-api   # só a calc local
python codigos\calc_pu_par\calc_pu_par.py --tudo --limite 500
```

`--date` / `--start` / `--end` referem-se a `dtLiquidacao`.

---

## A chave é `(cdTicker, dtReferencia)` — e isso foi a decisão central

PU par **não é propriedade do ativo, é do par (ativo, data)**: ele acreta todo dia útil
na taxa de emissão. Guardar um valor por ativo e reusá-lo por alguns dias injeta erro
**sistemático**, sempre para cima (puPar velho é menor, então o `%par` lê alto).

Medido em 249 ativos validados, 7 dias úteis (17/07 → 28/07 de 2026):

| indexador | n | mediana | p90 | máx |
|---|---|---|---|---|
| CDI+ | 109 | 0,411% | 0,462% | 4,73% |
| %CDI | 18 | 0,372% | 0,387% | 0,405% |
| PREFIXADO | 7 | 0,364% | 0,382% | 0,382% |
| IPCA | 115 | 0,250% | 0,292% | 0,382% |
| **TODOS** | **249** | **0,368%** | 0,437% | 4,73% |

Isso entra direto no `%par`, e dói mais onde o número é mais útil: CDI+ e %CDI vivem
entre 99 e 101, então a faixa inteira do sinal são ~2 pontos e 0,4 come **20%** dela.

E há o **degrau**, pior que a deriva: evento de fluxo dentro da janela. Nos mesmos 7
dias, **128 dos 2.583 ativos negociados (5%)** tiveram evento — o `MATD23` amortizou
**100%**, o `MOVI34` 33%, o `JHSFA1` 11%. Um puPar de três dias antes daria um `%par`
sem sentido nenhum.

Indexar por data ainda sai **mais barato**: são ~50 mil pares distintos na base inteira,
e cada um se calcula **uma vez na vida**. Com janela de N dias, a mesma chamada de API é
re-paga para sempre — e devolve um número errado.

---

## Cascata

| # | fonte | `cdFontePuPar` | como |
|---|---|---|---|
| 1 | calculadora local | `Calc` | `calc.CalcularPu(ativo, data, vrTaxaEmissao)`. Só `stFluxoValidado = 1` |
| 2 | B3 | `B3` | `b3_calc_api.CalcularPuGov(ticker, data, vrTaxaEmissao)` |
| 3 | FI Analytics | `FI` | `fianalytics_api.ChamarCompleto(ticker, data, vrTaxaEmissao)`, campo `m2m` |
| 4 | — | — | não grava linha |

**A B3 vem antes da FI** porque é a fonte primária do cadastro e a régua do próprio gate
[[10 - Scripts/validar_calc_b3|validar_calc_b3]]: usar a FI primeiro daria um `%par` de
uma fonte com validação de outra.

**O degrau 2 é o que faz o script valer a pena.** O ativo que o gate rebaixou é
justamente aquele em que a **B3 respondeu** e a nossa calc não reproduziu — então o puPar
da B3 está disponível e é autoritativo. Medido no backfill: em 11/06/2026 a calc resolveu
847 ativos e a **B3 resgatou mais 451**.

**Paralelismo:** só a fase de API roda em `ThreadPoolExecutor` (10 workers). A calc local
é CPU-bound e o GIL não a deixa escalar — mesma lição do
[[10 - Scripts/calc_taxa_negocios|calc_taxa_negocios]], onde quem faz o serviço é o cache.

---

## Idempotência

Par `(cdTicker, dtReferencia)` que já existe **não é recalculado nem consultado em API** —
o gate é um `LEFT JOIN` na própria fila. O valor de um par não muda nunca: é o PU de um
papel numa data passada. **Reprocessar um pregão inteiro custa zero chamada.**

Quem invalida é **`dados.DescartarPuPar`**, chamado de dentro do `Mesclar` /
`SincronizarFluxos` / `SincronizarFluxoAtivos` quando o cadastro ou o fluxo do ativo muda
de verdade. Aí o **histórico inteiro** daquele ticker sai — inclusive as datas passadas,
porque a agenda velha valia para elas também — e a próxima rodada refaz só o que ainda
interessa. Coberto pela seção 9 do `conferir_dados`.

---

## Papel que sai do cadastro (distress)

Quando o emissor se aproxima do default, as calculadoras **removem o papel do cadastro**.
O mercado passa a usar o **último puPar disponível** como referência e negocia em *cents
on the dollar*.

Este script não faz nada de especial: simplesmente **para de conseguir gravar linha
nova**. Quem resolve é a **leitura** — a CTE `PuParVigente` do
[[10 - Scripts/gerar_relatorio_credito|gerar_relatorio_credito]] pega o último puPar com
`dtReferencia <= dtLiquidacao` e exibe a **idade** dele.

**Não existe status "congelado" gravado.** A idade *é* o status, e a tabela fica só com
valor real. Acima de `PREGOES_CONGELADO = 5` o relatório marca como referência congelada.

> ⚠️ **Furo conhecido:** o gate roda com `--revalidar-dias 15`, então podemos levar até 15
> pregões para perceber que a B3 largou um papel — e nesse meio-tempo a **nossa** calc
> segue acretando um par contratual para um emissor que parou de pagar. Decisão
> consciente: fechar isso exigiria sondar a B3 todo pregão para todo ativo validado.

**Custo aceito:** sem linha gravada, o script re-tenta B3 e FI todo pregão para esses
papéis, e falha sempre. São ~2 chamadas por pregão por nome em default.

### Como LER o selo — duas leituras, e a diferença está na contagem

Descoberto testando o relatório no meio do backfill: com o `PuPar` populado só até 14/07,
o boletim de 28/07 marcou **960 de 1.207 tickers** como referência congelada. O mecanismo
estava certo — ele pegou o último puPar disponível e reportou a idade honestamente. O que
faltava era o texto não afirmar uma causa que o dado não prova.

| o que se vê | o que é |
|---|---|
| **poucos** papéis, com datas de congelamento **diferentes entre si** | o caso real: saíram do cadastro das calculadoras, um a um, quando cada emissor entrou em dificuldade |
| **muitos** papéis compartilhando a **mesma** data | o `calc_pu_par` não rodou para o dia. Rode-o e o selo some |

Por isso a pílula do Boletim diz *"PU par de pregão anterior"* e não *"papéis fora do
cadastro"*: o relatório reporta o **fato** (a idade), e o disclaimer ensina as duas leituras.

---

## Por que o `%par` não é gravado

Se o `%par` ficasse em `NegociosProcessados`, corrigir o fluxo de um ativo (que apaga o
puPar dele) deixaria o `%par` **velho** de pé, calculado com um denominador em que
ninguém mais acredita — e seria preciso lembrar de limpar a coluna em toda partição
afetada. É o mesmo problema que fez o trigger de invalidação virar código dentro do
`Mesclar` (ver [[17 - Armazenamento Parquet e AWS]]).

Com só o puPar gravado, a correção **se propaga sozinha**: sumiu o puPar, o relatório
mostra travessão em vez de um número errado.

---

## Banda de sanidade

`PCT_PAR_MIN = 20` / `PCT_PAR_MAX = 200`, e **nada é filtrado por causa dela** — a
contagem só vai para o resumo.

Uma primeira calibração com 50–150 marcava 8 negócios, e os 8 eram **Braskem, CSN e Light
a ~48% do par**. Isso é *distress* de crédito, não erro de cadastro: papel de emissor em
dificuldade negocia a fração do par, e é exatamente o que a mesa quer ver. Alarme que
dispara no caso legítimo mais interessante do dia ensina o usuário a ignorar o alarme.

O que a banda tem de pegar é erro de **unidade** (VNE em 1 no lugar de 1.000, indexador
trocado, fluxo vazio), que erra por 10× ou 100×, nunca por 2×. Foi assim que o `RBRAJ7`
apareceu: `vrVNE = 1,00` e `vrTaxaEmissao = 1,00`, fonte B3.

---

## Onde o número aparece

- **Boletim Diário** — coluna **% Par**, ágio em verde / deságio em vermelho, selo `Np`
  quando a referência está congelada, pílula com a contagem de congelados do dia e
  disclaimer explicando as três fontes.
- **Email do dia** (`--email-dia`) — coluna **% Par**, `*` para referência congelada.

> ⚠️ **Pendente:** o usuário ainda vai apontar em que outras abas quer o número
> (Por Ativo? Spread × Duration? Visão Mercado?).
