# Credenciais e Links

> Ver também: [[00 - Inicio]] | [[02 - Como Rodar]] | [[13 - Migracao Banco]]

**Nunca coloque valores reais de credenciais nesta nota** (nem em qualquer arquivo versionado — o repo é público). Ver [[13 - Migracao Banco]] §3 para o modelo completo.

## Como os segredos são resolvidos (`get_secret` + `[env]`)

Segredos **não** ficam no código. `lib.config.get_secret("chaveLogica")` resolve pelo bloco `[env]` do `config.toml`, que mapeia cada chave a uma **lista de NOMES** de variáveis de ambiente candidatas (só nomes — seguro no repo público). Usa o primeiro candidato preenchido.

| Chave lógica (`get_secret`) | Candidatos (`[env]`) | Onde usar |
| --- | --- | --- |
| `b3CalcToken` | `token_calc_B3`, `B3_CALC_TOKEN` | `lib/b3_calc_api.py` (POST /login) |
| `fianalyticsApiKey` | `token_fianalytics`, `FIANALYTICS_API_KEY` | `lib/fianalytics_api.py`, `scrape_anbima_ntnb.py` (header `x-api-key`) |
| `fianalyticsUser` | `user_fianalytics`, `FIANALYTICS_USER` | `scrape_fianalytics_planilha.py`, `lib/fianalytics_api.py` |
| `fianalyticsPass` | `password_fianalytics`, `FIANALYTICS_PASS` | `scrape_fianalytics_planilha.py` (Playwright) |
| `httpProxy` / `httpsProxy` | `proxy_http`/`proxy_https` (+ padrões) | copiados p/ `HTTP_PROXY`/`HTTPS_PROXY` no load da config |

- **PC pessoal:** os nomes canônicos ficam no `code/.env` (template `code/.env.example`). Nenhuma variável de ambiente precisa ser setada.
- **Banco:** os nomes idiossincráticos (`token_calc_B3` etc.) são variáveis de ambiente da conta. Sem `.env`.

## Emails (`get_email_list` + `destinatarios.py`)

Destinatários **não** ficam no código. `lib.config.get_email_list("destinatarios"|"outlook")` tenta `code/destinatarios.py` (não versionado) → variável de ambiente (`EMAIL_DESTINATARIOS`/`OUTLOOK_TO`) → `[]`. Template versionado: `code/destinatarios.example.py`.

| Lista | Uso |
| --- | --- |
| `EMAIL_DESTINATARIOS` | rascunho do relatório do dia (`gerar_relatorio_credito.py --email-dia`) |
| `OUTLOOK_TO` | emails `[OK]/[ERROR]` de cada script (`lib/email_outlook.py`) |

**Trava pré-publicação:** rodar `python scripts/check_no_secrets.py` antes de subir qualquer coisa ao GitHub.

---

## URLs do projeto

### Fontes de dados

| Fonte | URL |
|---|---|
| B3 Boletim Diário | `https://www.b3.com.br/pt_br/market-data-e-indices/servicos-de-dados/market-data/consultas/boletim-diario/boletim-diario-do-mercado/` |
| FI Analytics login | `https://fi-analytics.com.br/signin` |
| FI Analytics planilha | `https://fi-analytics.com.br/analytics-hub/hub?type=deb` |
| Anbima debêntures | `https://www.anbima.com.br/pt_br/informar/taxas-de-debentures.htm` |
| Anbima CRI/CRA | `https://www.anbima.com.br/pt_br/informar/precos-e-indices/precos/taxas-de-cri-e-cra/taxas-de-cri-e-cra.htm` |

### APIs de calculadoras

| API | Base URL | Autenticação |
|---|---|---|
| FI Analytics | `https://endpoint.fi-analytics.com.br` | Header `X-Api-Key` |
| B3 Calculator | `https://api.calculadorarendafixa.com.br` | Bearer token (via POST /login) |

#### FI Analytics — endpoints

| Endpoint | Método | Finalidade |
|---|---|---|
| `/deb/debenturecalculator` | POST | Calcular taxa de debêntures |
| `/cr/cricracalculator` | POST | Calcular taxa de CRI e CRA |
| `/getuserbonds` | GET | Listar bonds disponíveis (fallback bondbuilder) |
| `/bondbuildercalculator` | POST | Calcular taxa via bondbuilder (fallback) |

#### B3 Calculator — endpoints

| Endpoint | Método | Finalidade |
|---|---|---|
| `/login` | POST | Obter Bearer token |
| `/calcYield/{cdTicker}/{dtNegocio}/{vrPU}` | GET | Calcular taxa (yield) |

---

## Notas relevantes por assunto

- Como as credenciais são usadas no código: [[06 - Calculadoras/FI Analytics API]] e [[06 - Calculadoras/B3 Calculator API]]
- Como as fontes scraped usam autenticação: [[05 - Fontes/FI Analytics]]
- Como configurar o `.env` antes de rodar: [[02 - Como Rodar]]
