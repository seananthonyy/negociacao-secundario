# cadastro_b3.py

`codigos/helpers/cadastro_b3.py` — **a leitura do `getBondDetails` da B3**.

---

## Overview

Traduz a resposta do `getBondDetails` para as colunas de `InfoAtivos` e `FluxoAtivos`.

**Existe como helper porque tem dois donos:** o `scrape_b3_bond_details` (que popula o
cadastro) e o `validar_calc_b3` (que refresca o cadastro de quem falhou, antes de
rebaixar). Duas cópias divergiriam, e o que elas interpretam é a fonte primária do
cadastro — o lugar mais caro do projeto para ter duas verdades.

---

## API pública

| símbolo | o que é |
|---|---|
| `CAMPOS_ESCALARES` | As colunas que a B3 preenche: emissor, vencimento, emissão, taxa, indexador, instrumento |
| `MAPA_INDEXADOR` | Vocabulário da B3 → o nosso (`CDI+`, `%CDI`, `IPCA`, `PREFIXADO`) |
| `MapearIndexador(method)` | Aplica o mapa; `None` quando não reconhece |
| `FluxoDaB3(det)` | A agenda: `[(dtEvento, vrPctAmortizacao, vrPctIncorporacao)]` |
| `PrepararAtivo(cdTicker, det, agora, antes)` | `(escalares, pacote, fluxo, acao)` — **sem gravar** |
| `GravarLote(escalares, pacotes, fluxos)` | Grava tudo de uma vez |
| `POLITICA_ESCALARES` | `PREFERIR_ATUAL` — só preenche buraco |
| `POLITICA_PACOTE` | `SOBRESCREVER` — o pacote é atômico |

`PrepararAtivo` **não escreve**: devolve o que gravar. É o que permite acumular milhares
de ativos em memória e gravar num `GravarLote` só — `InfoAtivos` e `FluxoAtivos` são
arquivos únicos, e gravar por ativo reescreveria o arquivo mil vezes.

---

## Invariantes

**1. O PACOTE é indivisível.** `vrVNE` + `dtInicioRentabilidade` + `FluxoAtivos` são
escritos juntos ou não são escritos. A B3 pré-capitaliza a carência dentro do VNE e não
emite evento de incorporação; a Anbima traz o VNE cru e a incorporação como evento.
Misturar conta a capitalização **duas vezes**. `cdFonteCadastro` registra de quem é.

**2. Escalar é `PREFERIR_ATUAL`, pacote é `SOBRESCREVER`.** Duas políticas opostas na
mesma tabela, e por isso duas mesclagens: os escalares só preenchem o que está vazio; o
pacote substitui.

**3. Este módulo NÃO valida.** "Fluxo veio da B3" não é o mesmo que "a calc precifica este
ativo certo". Quem marca `stFluxoValidado = 1` é só o `validar_calc_b3`, e só depois de a
nossa calc reproduzir um oráculo.

---

## Quem consome

`scrape_b3_bond_details` (popula o cadastro) e `validar_calc_b3` (refresca antes de
rebaixar).

---

## Armadilhas

**Data de cupom (`J`) tem de entrar como amortização ZERO.** A calc ancora o juro de cada
período no evento anterior; sem as datas de cupom ela acha que o papel acumula juros por
anos sem pagar. **Guardar só os eventos `A` derruba a aderência do modelo B3 de 92% para
25%.**

**O `yield` do evento `J` só é incorporação no estilo `IPCA-I`.** Nos demais é a própria
taxa do cupom, e lê-la como incorporação transforma todo IPCA simples em falso-divergente.

**A amortização vem como % do principal ORIGINAL**, não do saldo — a B3 manda
`saldo_original`. Interpretar como % do saldo corrente erra todo papel com mais de uma
amortização.

**Escrever o pacote zera a validação.** É de propósito (o `Mesclar` cuida), mas significa
que um refresh de cadastro joga o ativo de volta na fila do gate. Não confundir com falha.
