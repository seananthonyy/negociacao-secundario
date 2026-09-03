# fianalytics_api.py

`codigos/helpers/fianalytics_api.py` — **cliente da calculadora da FI Analytics**.

> Documento de referência de uso frequente. Endpoint por endpoint, o que mandar e o que
> volta.

---

## Overview

`https://endpoint.fi-analytics.com.br` — a calculadora da FI Analytics. No projeto ela é o
**segundo oráculo**: entra quando a B3 não cobre o papel, tanto na cascata de taxa quanto
no gate de validação.

Diferente da B3, ela responde **nos dois sentidos** com endpoints distintos do mesmo
recurso: manda-se `pu` e volta taxa, ou manda-se `rate` e volta PU.

---

## Autenticação

Chave de API em `fianalyticsApiKey` (ver `credenciais.md`), enviada no header por
`ObterHeaders()`. **Não há token de sessão nem expiração** — é mais simples que a B3.

Cliente `httpx.Client` único com keep-alive; lê `HTTP_PROXY`/`HTTPS_PROXY` do ambiente
(`trust_env`).

---

## Endpoints

Dois caminhos, escolhidos pelo tipo do papel:

| path | para |
|---|---|
| `/deb/debenturecalculator` | debêntures |
| `/cr/cricracalculator` | CRI e CRA |
| `/bb/bondbuildercalculator` | papel cadastrado à mão pelo usuário (*bondbuilder*) |
| `/bb/bondbuildercalculator/getuserbonds` | lista os papéis do bondbuilder |

### Modo `pu` → taxa

```python
taxa = ChamarPrimaria(cdTicker, cdInstrumento, "2026-07-28", 1043.72)
```

```json
POST /deb/debenturecalculator
{"ticker": "ABCD11", "date": "2026-07-28", "pu": 1043.72}
```

Resposta: campo **`m2mRate`**, em % a.a.

`CalcularTaxa(...)` é o wrapper completo: tenta o endpoint do instrumento, depois o outro,
depois o *bondbuilder* como último recurso.

### Modo `rate` → PU

```python
resp = ChamarCompleto(cdTicker, "2026-07-28", 6.75)
pu = resp["m2m"]
```

```json
POST /deb/debenturecalculator
{"ticker": "ABCD11", "date": "2026-07-28", "rate": 6.75}
```

Resposta **crua e completa**, com os campos que importam:

| campo | o que é |
|---|---|
| `m2m` | **o PU** na taxa informada |
| `m2mRate` | a taxa |
| `cashFlowEvents` | a agenda de eventos |
| `issueRate` | taxa de emissão |
| `maturityDate` | vencimento |
| `adjustedFaceValue` | VNA |
| `accruedInterest` | juros acruados |

**É este modo que dá o PU par:** passar a própria taxa de emissão do ativo. Foi ao
construir o `calc_pu_par` que se descobriu que a FI faz isso — antes ela só era usada no
sentido `pu → taxa`, o que obrigava o gate a ter dois caminhos de avaliação.

### Duration

```python
dur = ObterDuration(cdTicker, "2026-07-28", 6.75)
```

Usa o modo `rate` e extrai a duration da resposta.

---

## Invariantes

**1. Erro é `None`, nunca exceção.** Mesma regra da B3: "não respondeu" é diferente de
"respondeu e divergiu".

**2. `TaxaValida` filtra resposta lixo.** `m2mRate` só vale se for numérico, não-nulo,
positivo e diferente de zero — a API devolve zero em alguns papéis que não cobre.

**3. A cascata de endpoints é interna.** Quem chama `CalcularTaxa` não escolhe path; o
módulo tenta o do instrumento, o outro, e o bondbuilder.

---

## Quem consome

`calc_taxa_negocios` (degrau 2 da cascata) · `validar_calc_b3` (segundo oráculo) ·
`calc_pu_par` (degrau 3) · `scrape_anbima_ntnb` (duration dos vértices de NTN-B).

---

## Armadilhas

**A cobertura é magra fora de debênture.** Medido: 146 papéis de ~1.670 que a B3 não
cobre. Não conte com a FI como rede confiável — ela resgata alguns casos, não a cauda.

**`m2mRate` zero é resposta inválida, não taxa zero.** Sem o filtro do `TaxaValida`, um
papel não coberto entraria na base com taxa 0 e detonaria a média do indexador.

**A planilha de características é outra coisa.** O `scrape_fianalytics_planilha` baixa um
CSV pelo portal, com Playwright e login — não usa esta API. São duas integrações
diferentes com o mesmo fornecedor.

**A coluna `% PU Par` da planilha NÃO é o nosso `%par`.** Ela é `preço indicativo da FI ÷
PU par na data do download`; o nosso é `PU do negócio ÷ PU par na liquidação daquele
negócio`. Numerador e denominador diferentes — por isso ela é ignorada no parse, de
propósito.

**A FI e a B3 concordam entre si e discordam de nós, quando discordam.** É o sinal de que
o erro é nosso. Foi assim que o problema do desconto foi identificado.
