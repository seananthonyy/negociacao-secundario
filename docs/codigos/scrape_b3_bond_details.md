# scrape_b3_bond_details.py

**Passo 2** do pipeline. **A fonte primária do cadastro.**

---

## Overview

Busca cadastro e fluxo de caixa dos ativos que **negociaram**, pelo `getBondDetails` da
B3, e grava em `InfoAtivos` + `FluxoAtivos`.

Roda logo depois do boletim: o cadastro é puxado **por demanda**, então sem os negócios
não há de quem buscar.

**Por que a B3 é primária** (medido em 12/07/2026, 802 ativos IPCA validados):

| modelo | ativos que batem o PU |
|---|---|
| Anbima (VNE cru + evento de incorporação) | 719 — 89,7% |
| **B3 (VNE já capitalizado)** | **741 — 92,4%** |

E 35 papéis só batem pelo modelo B3, incluindo os erros graves (um errava 50,8%, outro
58%). Só 12 batem apenas pelo modelo Anbima, e neles o dado da B3 é lixo.

---

## Regras de negócio

### O gate: só bate na B3 quem precisa

Sem ele seriam ~2.900 chamadas por rodada. Entra na fila quem:

- nunca passou pela B3 (`cdFonteCadastro` NULL);
- tem algum campo escalar NULL;
- é `IPCA` sem `vrAniversario` (a calc precisa);
- está sem fluxo;
- **ou tem o fluxo da B3 velho** — ver abaixo.

### O refresco por idade

O gate acima só pergunta "falta alguma coisa?". Fluxo que **já existe** nunca era
re-raspado, então só envelhecia — e a fonte também apodrece: um papel foi aditado em
fev/26 e a agenda velha continuou de pé. Foi essa deriva que produziu 19 fluxos defasados
numa faxina.

**A âncora é `FluxoAtivos.dtAtualizacao`, não `InfoAtivos.dtAtualizacao`** — o segundo é
escrito por qualquer fonte que toque o ativo (a FI escreve a cada rodada), então usá-lo
faria um toque da FI zerar o relógio sem que a B3 tivesse sido consultada.

**Com teto, e do mais velho para o mais novo.** Medido: sem teto, o corte de 30 dias leva
a fila de 1.917 para 5.009 ativos de uma vez — um pico de ~3.000 requisições numa API que
**conta** chamada. Com teto, o refresco vira uma esteira: um pedaço por dia, sempre o mais
atrasado, e a população converge em poucas rodadas.

### O pacote indivisível

`vrVNE` + `dtInicioRentabilidade` + `FluxoAtivos` são escritos juntos ou não são escritos.
Ver `helpers/cadastro_b3.md`.

### Este script NÃO valida

"Fluxo veio da B3" não é o mesmo que "a calc precifica este ativo certo". Marcar validado
aqui liberava para a calc local ativo que nunca passou pelo gate, com erro de PU de até
70%. Quem marca `stFluxoValidado = 1` é só o `validar_calc_b3`.

---

## CLI

```powershell
python codigos\scripts\scrape_b3_bond_details\scrape_b3_bond_details.py --start 2026-09-01 --end 2026-09-02
python codigos\scripts\scrape_b3_bond_details\scrape_b3_bond_details.py --tickers ABCD11,EFGH22
```

| argumento | efeito |
|---|---|
| `--date` / `--start` / `--end` | Quem negociou nessas datas |
| `--todos` | Varre todo o `InfoAtivos` (backfill) |
| `--tickers A,B` | Esses, **ignorando o gate** |
| `--forcar` | Ignora o gate de informação faltante |
| `--refrescar-dias N` | Também re-raspa fluxo com mais de N dias. Default **30**; `0` desliga |
| `--refrescar-max N` | Teto de refrescados por rodada, do mais velho. Default **150**; `0` sem teto |
| `--limite N` | Corta a fila |

**A fila de faltantes vem antes da de refresco.** Quando há `--limite`, quem não tem
cadastro nenhum bloqueia a calc hoje, enquanto quem só envelheceu ainda está precificando.

---

## Interação com a base

**Lê:** `NegociosBrutos` (quem negociou), `InfoAtivos` + `FluxoAtivos` (o gate e a idade).

**Grava:** `InfoAtivos` em duas mesclagens com políticas opostas — escalares
(`PREFERIR_ATUAL`, só preenchem buraco) e pacote (`SOBRESCREVER`) — e `FluxoAtivos` por
substituição de agenda. Tudo em **lote**.

---

## Detalhes técnicos

`GET https://api.calculadorarendafixa.com.br/getBondDetails/{cdTicker}`, 8 workers. Cache
por ticker dentro da rodada.

---

## Armadilhas

**Data de cupom (`J`) tem de virar amortização ZERO.** Guardar só os `A` derruba a
aderência do modelo B3 de 92% para **25%**.

**O `yield` do `J` só é incorporação no estilo `IPCA-I`.** Nos demais é a taxa do cupom, e
lê-la como incorporação transforma todo IPCA simples em falso-divergente.

**A amortização é % do principal ORIGINAL**, não do saldo.

**Escrever o pacote zera a validação.** É de propósito — o ativo volta para a fila do gate.

**`getBondDetails` vazio não é erro.** Há papéis que a B3 não cobre; o fallback é a Anbima.

**O campo `note` é descartado.** Ele avisa onde o dado da B3 está incompleto, e uma das
frases é `"O ativo considera apenas a variação positiva do IPCA"` — **piso de deflação**,
que a nossa calc não modela. Risco dormindo até o primeiro IPCA negativo.
