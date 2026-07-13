# Cadastro dos Ativos — B3 primária, Anbima fallback

> **Mudou em 13/07/2026.** Antes, a Anbima Data era a fonte do cadastro e do fluxo, e o `validar_fluxos` conferia contra a B3. Agora **a B3 é a fonte primária** e a Anbima é o fallback. Motivo: medição, não preferência.

## Por que a B3 virou primária

Comparando o PU calculado contra o PU da própria fonte, em 802 ativos IPCA validados:

| modelo de cadastro | batem o PU (≤ 1e-6) |
|---|---|
| **Anbima** (VNE cru + incorporação como evento) | 719 — 89,7% |
| **B3** (VNE já capitalizado) | **741 — 92,4%** |

E **35 papéis só batem pelo modelo B3** — justamente os graves: CRA020001US errava **50,8%**, CRA019001E7 **26,0%**, SSRU11 **58%**. Na direção contrária, 12 só batem pelo Anbima, e neles o dado da B3 é lixo (o CTNSA5 devolve `1e+23`).

Mas o ganho maior nem é esse. **1.085 tickers negociaram nos últimos 90 dias e não tinham fluxo nenhum na base** — 38% do volume. Destes, 701 nem existiam no `InfoAtivos`. A B3 cobre 64% deles. Puxar cadastro **por demanda** (o que negociou) em vez de varrer a Anbima leva a cobertura de fluxo validado de **58% para ~76% do volume negociado**.

## O pacote indivisível

`vrVNE` + `dtInicioRentabilidade` + `FluxoAtivos` **descrevem a mesma carência de dois jeitos incompatíveis**:

| | B3 | Anbima |
|---|---|---|
| SSRU11 VNE | **10.561,83** (já capitalizado) | **10.000** (cru) |
| SSRU11 início | 28/11/2018 (fim da carência) | 29/06/2018 |
| incorporação | nenhuma (embutida no VNE) | 1 evento, 100% em 28/11/2018 |

As duas estão certas. **Misturar conta a capitalização duas vezes** — sem erro, sem exceção, só um PU errado. Daí a coluna **`cdFonteCadastro`** (`'B3'` | `'AnbimaData'`), e a guarda em `lib.db.SincronizarFluxoAtivos`, que **se recusa a escrever** em ativo de fonte B3. A guarda mora na lib, não no scraper, porque `FluxoAtivos` tem vários writers.

## As três armadilhas do fluxo da B3

Custaram **122 ativos com erro de PU acima de 1%**. Todas silenciosas.

**1. Datas cruas.** A B3 manda a data **sem ajustar** para dia útil (o TAEE17 incorpora em **15/03/2025, um sábado**). A calc casa evento com aniversário, e o aniversário passa por `ProximoDu`. Evento **passado** que caia em fim de semana **não casa e é descartado**. Guardamos já ajustado.

**2. `IPCA-I` não emite a amortização do vencimento.** Os `'A'` somam menos de 100 (TAEE17: **96,8**) e o vencimento vem só com um `'J'` de yield 0. É fatal porque o `InferirTipoAmort` da calc decide a convenção **pela soma**: ≠ 100 vira `saldo_restante`, e o papel inteiro passa a amortizar na convenção errada. Completamos o resto no vencimento.

**3. As datas de cupom (`'J'`) precisam entrar** como eventos de amortização **zero**. A calc ancora o juros de cada período no evento anterior; sem elas ela acha que o papel acumula juros por 11 anos sem pagar. Guardar só os `'A'` derruba a aderência de **92% para 25%**.

Com as três: **12 de 12** dos papéis testados batem exato.

## O fluxo, ponta a ponta

```
boletim (NegociosBrutos)
   │
   ├─> scrape_b3_bond_details   ← FONTE PRIMÁRIA. Só os tickers que negociaram E têm
   │      (gate de info faltante)  campo faltante. Grava o pacote e marca validado.
   │
   └─> scrape_anbima_data_ativos ← só o que a B3 não cobriu. COALESCE em tudo;
          (demanda-dirigido)        não toca no fluxo de ativo B3.
                │
                └─> validar_fluxos
                       ├─ valida o fluxo da Anbima, pela FI
                       └─ TRIPWIRE de saldo em TUDO que está validado
```

## Cobertura real (medida em 12/07/2026, 4.197 ativos com fluxo)

| | ativos | % do volume 90d |
|---|---|---|
| B3 cobre → fluxo dela, validado por construção | 2.355 | **58,2%** |
| B3 não cobre, FI cobre → Anbima + validável | 146 | 1,7% |
| **nem B3 nem FI** → nunca validam | **1.526** | **2,2%** |
| (antes do fluxo novo) negociaram e não tinham fluxo | 1.085 | 38,0% |

Os órfãos parecem muitos (1.526) mas são **cauda morta**: 2,2% do volume. O buraco que importava eram os 1.085 sem fluxo, e é ele que o fluxo novo fecha.

**B3 e FI cobrem quase o mesmo universo.** Onde uma falha, a outra falha junto — elas não são fontes independentes de verdade. É por isso que validar o fluxo da Anbima pela FI rende tão pouco (146 ativos).

## O tripwire — por que "fluxo da B3 = válido" não basta

Adotar o fluxo da B3 e chamá-lo de validado é uma **tautologia**: confere-se a B3 contra ela mesma. E fonte apodrece — o **EMIV11** foi aditado em fev/2026 (seis meses de carência de amortização), a agenda velha continuou de pé, o saldo ficou 25% errado e a taxa dava **−19,6%**.

O antídoto é o **`ConferirSaldo`**: comparar o VNA que a nossa calc produz com o `adjustedFaceValue` da FI. É o **único teste que enxerga o passado** da agenda — a FI só devolve eventos futuros, então a comparação evento a evento cobre só a cauda, e a cauda bate perfeitamente num papel cuja agenda passada mudou. O saldo devedor não: ele é função de **toda** a agenda. Um número só, e já vem na resposta.

Cobertura do tripwire: a FI cobre **1.957 dos 2.355** ativos de fonte B3 — **99,0% do volume negociado**. Divergiu ⇒ **desvalida**.

Ver [[04 - Banco de Dados]] §Validação de fluxo e [[14 - Rotinas da Calculadora]].
