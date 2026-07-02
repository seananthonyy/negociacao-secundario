# Calculadora: FI Analytics API

> Ver também: [[../05 - Fontes/FI Analytics]] | [[B3 Calculator API]] | [[../04 - Banco de Dados]]

Módulo responsável: `code/lib/fianalytics_api.py`
Consumido por: `code/scripts/calc_taxa_negocios.py`

---

## O que faz

Calcula a taxa de mercado (m2mRate) de um ativo de crédito privado dado o ticker, a data do negócio e o PU praticado. É o **primeiro nível** da cascata de calculadoras — tenta calcular antes de recorrer à B3. Ver [[../04 - Banco de Dados]] para a lógica de `cdFonteTaxa`.

---

## Base URL

```
https://endpoint.fi-analytics.com.br
```

---

## Autenticação

Todas as requisições incluem os headers obrigatórios:

```
Content-Type: application/json; charset=utf-8
x-api-key: <valor de FIANALYTICS_API_KEY do .env>
```

**Importante:** o servidor retorna 502 se `Content-Type` não incluir `charset=utf-8`.

---

## Endpoints principais

### Debêntures

```
POST https://endpoint.fi-analytics.com.br/deb/debenturecalculator
```

Body (JSON):
```json
{
  "ticker": "DEBA11",
  "date": "2026-05-27",
  "pu": 1052.34
}
```

Campo de resposta esperado: `m2mRate` (taxa em % a.a.)

### CRI e CRA

```
POST https://endpoint.fi-analytics.com.br/cr/cricracalculator
```

Body (JSON) — mesma estrutura:
```json
{
  "ticker": "CRIA12",
  "date": "2026-05-27",
  "pu": 1105.00
}
```

Campo de resposta esperado: `m2mRate` (taxa em % a.a.)

**Roteamento:** baseado em `cdInstrumento` da `NegociosBrutos` — `'DEB'` vai para `/deb/debenturecalculator`; `'CRI'` e `'CRA'` vão para `/cr/cricracalculator`.

---

## Cálculo inverso: PU dado taxa (busca binária)

A API FI Analytics recebe **PU** e devolve **taxa** (`m2mRate`). Não há endpoint nativo que receba taxa e devolva PU — para obter PU dado taxa é necessário **busca binária** sobre o endpoint normal.

**Como funciona:**
1. Definir um intervalo inicial de PU (ex: `[1, PU_max]`).
2. Chamar o endpoint com o PU do ponto médio; comparar o `m2mRate` retornado com a taxa alvo.
3. Reduzir o intervalo pelo sinal da diferença e repetir até convergir (diferença de taxa < tolerância, ex: 0,001% a.a.).

Típicamente converge em ~15 iterações.

**Teste realizado (21/06/2026):**

| Parâmetro | Valor |
|---|---|
| Ticker | ADAG11 (DEB, PREFIXADO, venc. 2034-12-15) |
| Data | 2026-06-19 |
| Taxa alvo | 13,47% a.a. |
| PU resultante | ≈ 450,73 |
| Iterações | ~15 |

Confirma que a API funciona corretamente para DEBs prefixados de longo prazo (8+ anos).

**Decisão (21/06/2026):** a busca binária não foi adicionada a `fianalytics_api.py` porque o uso atual (pipeline de negócios) sempre parte do PU negociado para obter a taxa. O cálculo inverso é relevante para aplicações de precificação externa que precisam de PU dado uma taxa de mercado.

---

> **Nota para versões futuras:** a API também aceita `rate` diretamente no body (em vez de `pu`), mas só em alguns endpoints e a documentação oficial não é conclusiva. O caminho confiável para PU → taxa permanece a busca binária acima.

---

## Fallback bondbuilder (interno à FI Analytics)

Se os endpoints principais falharem ou retornarem `m2mRate` inválido, o módulo tenta o fluxo bondbuilder em dois passos:

**Passo 1 — Recuperar lista de bonds do usuário:**
```
POST https://endpoint.fi-analytics.com.br/bb/bondbuildercalculator/getuserbonds
Body: {"user_email": "<FIANALYTICS_USER>", "get_company_bonds": "1"}
```
Retorna lista de objetos com `bond_name` e `_id`. O ticker é buscado no campo `bond_name` (case-insensitive).

**Passo 2 — Calcular no bondbuilder:**
```
POST https://endpoint.fi-analytics.com.br/bb/bondbuildercalculator
Body: {"doc_id": "<_id do bond>", "date": "<dtLiquidacao>", "pu": <vrPU>}
```
Campo de resposta: `m2mRate` (mesmo formato dos endpoints principais). Se o bondbuilder também falhar, passa para a [[B3 Calculator API]].

Se qualquer cálculo FI Analytics tiver sucesso: `cdFonteTaxa = 'FiAnalytics'`.

---

## Definição de falha

Uma chamada é considerada **falha** em qualquer dos seguintes casos:

- HTTP status != 2xx (tentativa única — sem retries).
- Timeout > **15 segundos**.
- JSON de resposta sem o campo `m2mRate`.
- Campo `m2mRate` com valor `null`, `0`, ou negativo.

Em caso de falha, o módulo retorna `None` imediatamente (sem tentar novamente). Se todos os endpoints FI Analytics falharem (incluindo bondbuilder), o `calc_taxa_negocios.py` aciona a [[B3 Calculator API]].

---

## Parâmetros de configuração

Definidos em `config.toml`:

```toml
[calc]
timeoutSeconds = 15
```

> `retries` foi removido — o módulo faz tentativa única em cada endpoint.

---

## Resultado no banco

Quando bem-sucedido, o `calc_taxa_negocios.py` grava em `NegociosProcessados`:

```
vrTaxaCalculada = <valor de m2mRate>
cdFonteTaxa    = 'FiAnalytics'
```

---

## Implementação (`lib/fianalytics_api.py`)

### Interface pública

```python
from lib.fianalytics_api import CalcRate

taxa = CalcRate(
    cdTicker="ISAEC2",
    cdInstrumento="DEB",      # 'DEB', 'CRI' ou 'CRA'
    dtLiquidacao="2026-05-29",
    vrPU=1000.0,
)
# retorna float em % a.a. ou None se todos os níveis falharem
```

`dtLiquidacao` é sempre a data de liquidação do negócio, nunca `dtNegocio`. Ver [[../04 - Banco de Dados]] para a distinção entre as duas datas.

### Fluxo interno

1. `CalcRate` chama `_CallPrimary` (endpoint principal por tipo de instrumento).
2. Se `_CallPrimary` retornar `None`, chama `_CallBondbuilderFallback`.
3. Se ambos retornarem `None`, `CalcRate` retorna `None` e o `calc_taxa_negocios.py` aciona a [[B3 Calculator API]].

### Funções internas

| Função | Responsabilidade |
|---|---|
| `_ParseResponse(resp, url)` | Lida com o double-encoded JSON da API (ver decisão abaixo). |
| `_GetHeaders()` | Monta `{"Content-Type": "application/json; charset=utf-8", "x-api-key": ...}`. |
| `_IsValidRate(value)` | Valida que o m2mRate é numérico, positivo e não-zero. |
| `_CallPrimary(...)` | POST no endpoint `/deb/debenturecalculator` ou `/cr/cricracalculator` conforme `cdInstrumento`. Tentativa única, sem retry. |
| `_GetUserBonds()` | POST `/bb/bondbuildercalculator/getuserbonds`. **Fetch único por processo** — resultado cacheado nas variáveis module-level `_userBondsFetched: bool` e `_userBonds: list[dict] \| None`. Chamadas subsequentes retornam o valor cacheado sem tocar na API. Não recebe `headers`/`timeout` como parâmetros externos. |
| `_FindBond(bonds, cdTicker)` | Busca o bond pelo campo `bond_name` (case-insensitive). Retorna o dict com `_id`. |
| `_CallBondbuilder(docId, ...)` | POST `/bb/bondbuildercalculator` com `{"doc_id": docId, "date": ..., "pu": ...}`. Tentativa única, sem retry. |
| `_CallBondbuilderFallback(...)` | Orquestra: `_GetUserBonds()` → `_FindBond` → extrai `_id` → `_CallBondbuilder`. Não precisa mais passar `headers`/`timeout` para `_GetUserBonds`. |

### Cache de resultados

`CalcRate` mantém um cache de resultados module-level `_rateCache: dict[tuple, object] = {}` com chave `(cdTicker, dtLiquidacao, vrPU)` exatos.

- Resultados `float` e `None` são ambos armazenados no cache.
- Para distinguir "chave não existe no cache" de "chave existe com valor `None`", usa-se um sentinel `_CACHE_MISS` (objeto singleton definido no módulo).
- Ao receber uma chave já presente, `CalcRate` retorna o valor cacheado sem chamar nenhum endpoint.

```python
_CACHE_MISS = object()

result = _rateCache.get((cdTicker, dtLiquidacao, vrPU), _CACHE_MISS)
if result is not _CACHE_MISS:
    return result  # None ou float, sem nova chamada à API
```

### Novas entradas em `config.toml` (adicionadas na F5)

```toml
[api.fianalytics]
getUserBondsPath    = "/bb/bondbuildercalculator/getuserbonds"
bondbuilderPath     = "/bb/bondbuildercalculator"
```

As entradas `baseUrl`, `debPath` e `cricraPath` já existiam.

---

## Decisões de implementação

**Decisão (31/05/2026) — Double-encoded JSON:** a API FI Analytics retorna o body como uma string JSON dentro de outra string. `resp.json()` entrega uma `str`; `json.loads()` faz o parse real para `dict`. A função `_ParseResponse()` encapsula esse comportamento e protege com try/except caso a resposta não seja JSON válido.

```python
raw = resp.json()          # -> str  (ex: '"{\"m2mRate\": 0.067...}"')
return json.loads(raw) if isinstance(raw, str) else raw  # -> dict
```

**Decisão (31/05/2026) — `m2mRate` em decimal, não em %:** a API retorna a taxa como decimal (ex: `0.06704551`), não como percentual por ano. O módulo multiplica por 100 antes de retornar (`rate = float(rawRate) * 100`) para manter consistência com `vrTaxaNegocio` do boletim B3 e com o retorno da [[B3 Calculator API]]. Verificado em teste real: ISAEC2 / 2026-05-29 / PU 1000 → `m2mRate = 0.06704551` → `6.7046% a.a.` (FI Analytics) ≈ `6.7045% a.a.` (B3 Calculator).

**Decisão (31/05/2026) — Bondbuilder paths, body e Content-Type (verificados por teste real):** `POST /bb/bondbuildercalculator/getuserbonds` com `{"user_email": ..., "get_company_bonds": "1"}` retorna lista de `{bond_name, _id}`. O cálculo usa `POST /bb/bondbuildercalculator` com `{"doc_id": bond["_id"], "date": ..., "pu": ...}`. O servidor exige `Content-Type: application/json; charset=utf-8` (502 sem o charset). Testado: 26D06419698 / 2026-05-29 / PU 1000 → 7.0302% a.a.

**Decisão (31/05/2026) — Sem retries; cache de resultados e getUserBonds:** tentativa única em cada endpoint — falha retorna `None` imediatamente. Cache de resultados por `(cdTicker, dtLiquidacao, vrPU)` exatos (`_rateCache` + sentinel `_CACHE_MISS`) evita chamadas repetidas para o mesmo trade. `getUserBonds` é buscado uma única vez por processo (variáveis module-level `_userBondsFetched` / `_userBonds`), evitando chamadas redundantes quando múltiplos trades usam o bondbuilder.
