# Calculadora: B3 Calculator API

> Ver também: [[FI Analytics API]] | [[../04 - Banco de Dados]]

Módulo responsável: `code/lib/b3_calc_api.py`
Consumido por: `code/scripts/calc_taxa_negocios.py`

---

## O que faz

Calcula a taxa de mercado (yield) de um ativo de crédito privado. É o **segundo nível** da cascata de calculadoras — só é acionado quando a [[FI Analytics API]] falha para um trade específico. Se também falhar, `vrTaxaCalculada` fica `NULL`.

---

## Base URL

```
https://api.calculadorarendafixa.com.br
```

---

## Autenticação

A API exige login antes de cada sessão de cálculos:

```
POST https://api.calculadorarendafixa.com.br/login
```

Body (JSON):
```json
{
  "token": "<valor de B3_CALC_TOKEN do .env>"
}
```

A resposta retorna JSON com o campo `Authorization` contendo o token de sessão:
```json
{"login":"usuario","Authorization":"usuario<hash>","perfil":"DEPENDENTE"}
```

O token é passado diretamente no header — **sem prefixo `Bearer`**:
```
Authorization: <valor de Authorization da resposta>
```

O módulo `b3_calc_api.py` gerencia o token em memória e renova automaticamente em 401.

---

## Endpoint de cálculo

```
GET https://api.calculadorarendafixa.com.br/calcYield/{cdTicker}/{dtLiquidacao}/{vrPU}
```

Exemplo:
```
GET /calcYield/DEBA11/2026-05-27/1052.34
Authorization: <token>
```

**Importante:** usar sempre `dtLiquidacao` do trade como data, não `dtNegocio`.

Campo de resposta esperado: `yield` (taxa em % a.a.)

---

## Definição de falha

Uma chamada é considerada **falha** em qualquer dos seguintes casos:

- HTTP status != 2xx (tentativa única — sem retries genéricos).
- Timeout > **15 segundos**.
- JSON de resposta sem o campo `yield`.
- Campo `yield` com valor `null`, `0`, ou negativo.

**Exceção — HTTP 401:** o token é renovado e uma segunda tentativa é feita imediatamente (sem sleep) via `_DoRequest()`. Cobre expiração de sessão mid-run sem precisar de retry genérico.

Em caso de falha, o `calc_taxa_negocios.py` grava:

```
vrTaxaCalculada = NULL
cdFonteTaxa    = NULL
```

---

## Resultado no banco

Quando bem-sucedido, o `calc_taxa_negocios.py` grava em `NegociosProcessados`:

```
vrTaxaCalculada = <valor de yield>
cdFonteTaxa    = 'B3'
```

---

## Parâmetros de configuração

Definidos em `config.toml`:

```toml
[api.b3]
baseUrl = "https://api.calculadorarendafixa.com.br"

[calc]
timeoutSeconds = 15
```

> `retries` foi removido — o módulo faz tentativa única (mais uma segunda tentativa específica para 401).

---

## Implementação (`lib/b3_calc_api.py`)

### Interface pública

| Função | Assinatura | O que faz |
|---|---|---|
| `CalcYield` | `CalcYield(cdTicker, dtLiquidacao, vrPU) -> float \| None` | Retorna yield em % a.a. ou `None` se falhar |
| `ResetToken` | `ResetToken() -> None` | Descarta o token em memória, forçando novo login na próxima chamada |

`dtLiquidacao` deve estar no formato `YYYY-MM-DD`. `vrPU` é o PU do negócio como float.

### Gestão de token e estado module-level

O token é armazenado em `_bearer_token`, variável module-level inicializada como `None`.

- `_ensure_token()` só chama `_login()` se `_bearer_token` for `None` — reutiliza em memória nas chamadas seguintes.
- Em 401, `_DoRequest()` reseta o token e refaz a requisição uma única vez sem sleep (não é retry genérico — é refresh de autenticação).

O cache de resultados usa as variáveis module-level:
- `_rateCache: dict[tuple, object] = {}` — chave `(cdTicker, dtLiquidacao, vrPU)` exatos.
- `_CACHE_MISS` — sentinel singleton para distinguir "chave ausente" de "chave com valor `None`".

### `_DoRequest()`

Função interna responsável por executar o GET com tratamento de 401:

1. Chama `_ensure_token()` e dispara o GET.
2. Se a resposta for 401: reseta o token, chama `_ensure_token()` novamente e repete o GET uma segunda vez.
3. Qualquer outro status de erro (ou segunda falha em 401) — retorna a resposta para o chamador tratar.

Não há sleep nem loop — apenas uma segunda tentativa estritamente para o caso de token expirado.

### Extração do token no /login

A resposta de `POST /login` contém o campo `Authorization` — usado diretamente no header sem prefixo `Bearer`. Se o campo estiver ausente ou vazio, levanta `RuntimeError`.

### Cache de resultados

`CalcYield` verifica `_rateCache` antes de qualquer chamada de rede:

```python
_CACHE_MISS = object()

result = _rateCache.get((cdTicker, dtLiquidacao, vrPU), _CACHE_MISS)
if result is not _CACHE_MISS:
    return result  # None ou float, sem chamada à API
```

Resultados `float` e `None` são ambos armazenados. Evita chamadas duplicadas para o mesmo trade quando `calc_taxa_negocios.py` reprocessa.

### Logging

Usa `logging.getLogger(__name__)` sem criar handlers próprios — o script chamador é responsável pela configuração via `lib/logger.py`.

**Decisão (31/05/2026):** campo `Authorization` na resposta de `/login` é passado diretamente no header sem prefixo `Bearer` — verificado por teste real contra a API em 31/05/2026.

**Decisão (31/05/2026):** usar `dtLiquidacao` (não `dtNegocio`) como data na URL — trades podem ser negociados num dia e liquidar no seguinte; a calculadora deve receber a data de liquidação.

**Decisão (31/05/2026) — Sem retries genéricos; cache de resultados:** tentativa única em cada chamada. Em 401, token é renovado e uma segunda tentativa é feita imediatamente (sem sleep) via `_DoRequest()` para cobrir expiração de sessão mid-run. Cache de resultados por `(cdTicker, dtLiquidacao, vrPU)` exatos (`_rateCache` + sentinel `_CACHE_MISS`) evita chamadas duplicadas para o mesmo trade.

---

## Cascata completa

```
vrTaxaNegocio NOT NULL
    → copia direto para vrTaxaCalculada (cdFonteTaxa = NULL)

vrTaxaNegocio NULL
    → FI Analytics (debenturecalculator ou cricracalculator)
        → sucesso → cdFonteTaxa = 'FiAnalytics'
        → falha → bondbuilder FI Analytics
            → sucesso → cdFonteTaxa = 'FiAnalytics'
            → falha → B3 Calculator (calcYield)
                → sucesso → cdFonteTaxa = 'B3'
                → falha → vrTaxaCalculada = NULL, cdFonteTaxa = NULL
```
