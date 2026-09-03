# b3_calc_api.py

`codigos/helpers/b3_calc_api.py` — **cliente da Calculadora de Renda Fixa da B3**.

> Documento de referência de uso frequente. Endpoint por endpoint, o que mandar e o que
> volta.

---

## Overview

`https://api.calculadorarendafixa.com.br` — a calculadora oficial da B3. Faz três coisas
para o projeto:

1. **Converte preço em taxa e taxa em preço** para um papel numa data.
2. **Devolve o cadastro e a agenda** de um ativo (`getBondDetails`) — é a **fonte primária
   do cadastro** do projeto.
3. Serve de **régua** para o gate: a nossa calculadora local só é considerada confiável
   num ativo se reproduzir o que esta API devolve.

> ⚠️ **As chamadas de cálculo são CONTADAS.** Existe a rota
> `GET /consumo/pacotes/{dataInicial}/{dataFinal}` para acompanhar. É por isso que o
> `calc_pu_par` é idempotente por (ativo, data) e o `scrape_b3_bond_details` tem teto de
> refresco por rodada.

---

## Autenticação

`POST /login`, com o token de conta em `b3CalcToken` (ver `credenciais.md`). Devolve um
**token de sessão que expira em ~3 horas**.

O módulo cuida sozinho: `GarantirToken()` obtém na primeira chamada, e um `401` dispara
`ResetarToken()` + nova autenticação + repetição da requisição. **Quem chama não precisa
saber que existe token.**

O cliente é um `httpx.Client` único, com keep-alive — atrás do proxy do banco cada
handshake novo é caro. Ele lê `HTTP_PROXY`/`HTTPS_PROXY` do ambiente sozinho
(`trust_env`).

---

## Endpoints

### `GET /calcPU/{cetip}/{dtRef}/{taxa}` → PU e duration

```python
pu, duration = CalcularPuGov(cdTicker, "2026-07-28", 6.75)
```

| parâmetro | formato |
|---|---|
| `cetip` | código do papel (o `cdTicker` da base) |
| `dtRef` | `AAAA-MM-DD` |
| `taxa` | taxa em % a.a., ponto decimal (`6.75`) |

Devolve `(pu, duration)` ou `(None, None)` se a B3 não responder ou não cobrir o papel.

**O PU par sai daqui:** passar a **própria taxa de emissão** do ativo devolve o PU no par.
É o que o `calc_pu_par` e o gate usam.

---

### `GET /calcYield/{cdTicker}/{dtLiquidacao}/{vrPU}` → taxa

```python
taxa = CalcularYield(cdTicker, "2026-07-28", 1043.72)
```

Devolve a taxa em % a.a., ou `None`.

**Em `%CDI` a "taxa" é um MULTIPLICADOR do CDI** (`98.5` = 98,5% do CDI), não uma taxa
a.a. Comparar diferenças de `%CDI` com diferenças de taxa exige converter — 1 ponto de
`%CDI` ≈ 10 bps de yield. Ver `DiffTaxaEmBps` no `validar_calc_b3`.

---

### `GET /getBondDetails/{cdTicker}` → cadastro e agenda

```python
det = ObterDetalhesAtivo(cdTicker)
```

Devolve um dict com o cadastro e a lista de eventos, ou `None`. Tem **cache por ticker**
dentro do processo — o mesmo papel consultado duas vezes não paga duas chamadas.

Campos que o projeto usa (a tradução está em `cadastro_b3.py`):

| campo da B3 | vira |
|---|---|
| `vne` | `vrVNE` — **já capitalizado**, ver Armadilhas |
| `startingdate` | `dtInicioRentabilidade` |
| `method` | `cdIndexador`, via `MAPA_INDEXADOR` |
| `maturitydate` | `dtVencimento` |
| `issuedate` | `dtEmissao` |
| `rate` | `vrTaxaEmissao` |
| lista de eventos | `FluxoAtivos` — `A` = amortização, `J` = cupom |
| `note` | **descartado hoje** — ver Armadilhas |

---

### `GET /consumo/pacotes/{dataInicial}/{dataFinal}` → consumo

Devolve `quantidadeCalculos` por tipo de ativo (DI / Debênture / Título Público). **Não
está implementado no módulo** — é a rota para conferir quanto foi gasto.

---

## Invariantes

**1. Erro é `None`, nunca exceção.** Todas as funções públicas devolvem `None` quando a
API não responde ou não cobre o papel. Quem chama trata "oráculo mudo" como *não sei*, e
não como *divergiu* — essa distinção é o coração do gate.

**2. Um `Client` só, com keep-alive.** Atrás do proxy do banco, abrir conexão por chamada
domina o tempo.

**3. O token é problema do módulo.** Ninguém fora dele deve chamar `Autenticar`.

---

## Quem consome

`scrape_b3_bond_details` (cadastro) · `validar_calc_b3` (a régua) · `calc_taxa_negocios`
(degrau 3 da cascata) · `calc_pu_par` (degrau 2) · `scrape_anbima_ntnb` (duration dos
vértices).

---

## Armadilhas

**As chamadas são contadas.** Um backfill descuidado consome cota de verdade. Existem
versões em **lote** dos dois métodos de cálculo (`calcPUCSV` §13 e `calcYieldCSV` §15 da
documentação oficial) que ainda não usamos — e não se sabe se o lote conta como 1 ou como
N. Está no backlog.

**O `vne` da B3 já vem capitalizado.** Ela embute a carência no valor nominal e **não**
emite evento de incorporação. A Anbima faz o oposto. Por isso `vrVNE` +
`dtInicioRentabilidade` + `FluxoAtivos` são um pacote indivisível: misturar as duas fontes
conta a capitalização duas vezes, em silêncio.

**Guardar só os eventos `A` derruba a aderência de 92% para 25%.** As datas de cupom (`J`)
precisam entrar no fluxo como amortização **zero**, senão a calc acha que o papel acumula
juros por anos sem pagar.

**O campo `note` é descartado, e ele avisa coisa importante.** A B3 usa esse texto livre
para dizer onde o dado dela está incompleto ou onde o papel foge do padrão — inclusive
`"O ativo considera apenas a variação positiva do IPCA"`, que é **piso de deflação**, algo
que a nossa calc não modela. Capturá-lo saiu do backlog por decisão do usuário, mas o risco
segue real e dorme até o primeiro IPCA negativo.

**`getBondDetails` vazio não é erro.** Há papéis que a B3 simplesmente não cobre
(`TSSS15`, `VAMOA4`, `RDORE7`, `CEPEA5` entre eles). O `None` é resposta legítima, e o
fallback é a Anbima.

**HTTP 500 transitório acontece.** O módulo repete uma vez; se persistir, devolve `None`.
Não confundir com "papel não existe".
