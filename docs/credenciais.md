# Credenciais e segredos

> **Este documento não contém nenhum valor de segredo** — só o nome das variáveis e onde
> elas são procuradas. É o que permite o repositório ser público.

---

## 1. Como um segredo é resolvido

O `config/config.toml` tem um bloco `[env]` que mapeia cada segredo para uma **lista de
nomes de variável de ambiente**, na ordem em que devem ser tentados:

```toml
[env]
b3CalcToken        = ["token_calc_B3",        "B3_CALC_TOKEN"]
fianalyticsApiKey  = ["token_fianalytics",    "FIANALYTICS_API_KEY"]
fianalyticsUser    = ["user_fianalytics",     "FIANALYTICS_USER"]
fianalyticsPass    = ["password_fianalytics", "FIANALYTICS_PASS"]
httpProxy          = ["proxy_http",  "HTTP_PROXY",  "http_proxy"]
httpsProxy         = ["proxy_https", "HTTPS_PROXY", "https_proxy"]
outlookTo          = ["OUTLOOK_TO"]
emailDestinatarios = ["EMAIL_DESTINATARIOS"]
calculadoraDir     = ["CALCULADORA_DIR"]
```

`config.ObterSegredo("b3CalcToken")` tenta cada candidato e usa o primeiro preenchido.

**Por que uma lista, e não um nome só:** no PC pessoal os segredos vêm de um `.env` com
nomes próprios; no banco vêm das variáveis de ambiente da conta, que têm outros nomes. Os
dois convivem porque ambos estão na lista de candidatos, e **nenhum arquivo precisa ser
editado ao trocar de máquina**.

O `.env` é carregado por `load_dotenv(config/.env)` com `override=False` — variável já
definida no ambiente vence o arquivo. É o que faz o banco ignorar o `.env` mesmo que ele
exista.

---

## 2. Segredo por segredo

| segredo | para quê | PC pessoal | banco |
|---|---|---|---|
| `b3CalcToken` | API da calculadora da B3 (`/calcPU`, `/calcYield`, `/getBondDetails`) | `config/.env` | variável da conta |
| `fianalyticsApiKey` | calculadora da FI Analytics | `config/.env` | variável da conta |
| `fianalyticsUser` / `fianalyticsPass` | login no portal, para baixar a planilha | `config/.env` | variável da conta |
| `httpProxy` / `httpsProxy` | proxy corporativo | ausente | variável da conta |
| `outlookTo` | destinatário do email de conclusão de cada script | `config/.env` | variável da conta |
| `emailDestinatarios` | destinatários do rascunho do relatório do dia | `config/.env` | variável da conta |
| `calculadoraDir` | caminho absoluto da `calculadora-renda-fixa` | não usado (o config resolve pelo caminho relativo) | variável da conta |

**O token da B3 expira em ~3h.** O `b3_calc_api` renova sozinho ao receber 401 — não é
preciso intervir.

---

## 3. Emails

Dois caminhos, e os dois resolvem em runtime:

- **Email de conclusão de cada script** — vai para `outlookTo`.
- **Rascunho do relatório do dia** (`gerar_relatorio_credito --email-dia`) — vai para a
  lista de `emailDestinatarios`, resolvida por `config.ObterListaEmails("destinatarios")`.

Se `emailDestinatarios` não estiver definido, a lista cai para o módulo
`codigos/helpers/destinatarios.py` (variável `EMAIL_DESTINATARIOS`), que **não é
versionado**. O `destinatarios.example.py` mostra o formato.

**`NEGSEC_SEM_EMAIL=1`** faz qualquer script pular o Outlook e gravar o corpo em
`cache/emails/`. Cobre também o caminho de erro. Use sempre em teste e em rodada de lote —
evita o spam e o *gotcha* da instância COM órfã do Outlook (processo sem janela, que fica
pendurado e trava a próxima execução).

---

## 4. URLs

Estão em `fontes.md`, com o endpoint e o corpo de cada chamada. Não são duplicadas aqui
para não divergirem.

As poucas que vivem no `config.toml`:

```toml
[scrape.b3]        baseUrl, instrumentosAceitos
[scrape.anbima]    debXlsBaseUrl, cricraUrl
[scrape.fianalytics] signinUrl, downloadUrl
[api.b3]           baseUrl
[api.fianalytics]  baseUrl e os quatro paths
```

---

## 5. O que nunca entra no git

| arquivo | por quê |
|---|---|
| `config/.env` | todos os segredos |
| `codigos/helpers/destinatarios.py` | emails internos |
| `database/parquets/` | a base — grande, e no banco a fonte é o S3 |
| `cache/` · `relatorios/` · `backups/` | saída gerada |
| `codigos/scripts/*/logs/` | log de execução |

**Antes de qualquer `git push`:**

```powershell
python codigos\scripts\check_no_secrets\check_no_secrets.py
```

Ele varre os arquivos publicáveis atrás de padrão de segredo, caminho absoluto de máquina
e email interno. Sai diferente de zero se achar algo.

---

## Armadilhas

**Nome de variável, nunca valor.** Se um valor entrar no `config.toml`, ele vai para o
repositório público na próxima sincronização. O `[env]` existe exatamente para que o
config possa ser versionado.

**`.gitignore` ignora `logs/`, não a pasta do script.** A pasta de cada script guarda
também a skip-list e o template, que são curados à mão e **têm** de ser versionados.
Ignorar a pasta inteira faria esses arquivos sumirem do repositório sem ninguém perceber.

**O extrator do bundle protege por sufixo.** `PROTEGIDOS = ('.env', 'destinatarios.py',
'anbima_skip_tickers.csv')` — ele se recusa a sobrescrever esses ao importar uma versão
nova no banco. O `config.toml` é caso à parte: a versão nova sai ao lado como
`config.toml.novo`, para comparar e mesclar à mão.

**O caminho do `MESCLAR` no `make_bundle` é literal.** Se a estrutura de pastas mudar e
ele não acompanhar, o extrator sobrescreve o `config.toml` do banco em vez de preservá-lo.
