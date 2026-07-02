# Módulos lib/

Módulos compartilhados em `code/lib/`. Todos os scripts em `code/scripts/` importam daqui.

---

## lib/config.py

Proxy lazy para `config.toml` via `tomllib` nativo (Python 3.11+ stdlib) + resolução de segredos e emails. Carrega o arquivo apenas na primeira leitura de chave.

**Como usar:**
```python
from lib.config import cfg, get_secret, get_email_list, get_env

base_url = cfg["scrape"]["b3"]["baseUrl"]
apiKey   = get_secret("fianalyticsApiKey")     # resolve via [env]
emails   = get_email_list("outlook")            # destinatarios.py → env → []
```

**Funções expostas:**

| Função | O que faz |
|---|---|
| `cfg` | proxy lazy de `config.toml` (`__getitem__`/`__contains__`/`get`). |
| `get_secret(chave, default)` | **Novo (01/07).** Resolve um segredo pela lista de nomes de variáveis de ambiente em `[env].chave`; usa o 1º candidato preenchido. Ver [[99 - Credenciais e Links]] e [[13 - Migracao Banco]] §3. |
| `get_email_list(which)` | **Novo (01/07).** `which` ∈ {`destinatarios`, `outlook`}. Tenta `code/destinatarios.py` → variável de ambiente (`[env]`) → `[]`. |
| `get_env(key, default)` | leitura direta de variável de ambiente (garante `.env` carregado). Ainda usado para config não-sensível. |

**Como funciona internamente:**
- `_ensure_cfg()` carrega o TOML uma vez e, em seguida, chama `_apply_proxy_env()` — copia o proxy resolvido (`httpProxy`/`httpsProxy`) para `HTTP_PROXY`/`HTTPS_PROXY` padrão (que httpx e Playwright leem nativamente), respeitando valores já presentes.
- `get_secret` lê os candidatos de `[env]` e retorna o 1º `os.getenv` não-vazio → mesmo código roda no PC pessoal (`.env`, nomes canônicos) e no banco (variáveis da conta).
- O `.env` é lido de `code/.env` (relativo à raiz `code/`); `load_dotenv` **não** sobrescreve variáveis já no ambiente.

**Decisões:** proxy lazy (30/05) evita erro de import sem `config.toml`. O modelo `[env]`/`get_secret`/`destinatarios.py` (01/07) tira todo segredo do código para permitir repo público — ver [[13 - Migracao Banco]].

---

## lib/db.py

Abre a conexão SQLite e garante o schema do banco [[04 - Banco de Dados]].

**Como usar:**
```python
from lib.db import get_db

conn = get_db()   # abre + bootstrap
# ... usa conn ...
conn.close()
```

**Funções expostas:**

| Função | O que faz |
|---|---|
| `get_connection(db_path=None)` | Abre conexão SQLite read-write com WAL + foreign_keys + PRAGMAs de performance. Cria diretório pai se não existir. Usa `cfg["paths"]["dbFile"]` como default. |
| `get_readonly_connection(db_path=None)` | **Novo (23/06).** Abre conexão **somente leitura** (`file:...?mode=ro` + `query_only=ON`) com os PRAGMAs de performance, **sem rodar bootstrap**. Pensada para o add-in externo da calculadora. |
| `bootstrap(conn)` | Executa o DDL completo via `executescript`, dropa índice redundante e roda `ANALYZE` na primeira vez. Idempotente por `IF NOT EXISTS`. |
| `get_db(db_path=None)` | Helper principal: chama `get_connection` + `bootstrap`. É o único ponto de entrada que os scripts usam. |

**Tabelas criadas:** ver lista completa e DDL em [[04 - Banco de Dados]] (`NegociosBrutos`, `NegociosProcessados`, `InfoAtivos`, `AnbimaIndicativos`, `MtmAnbima`, `FluxoAtivos`, `Outstanding`). (`MtmBloomberg` removida em 02/07 — era schema morto.)

**Pragmas ativados:** `journal_mode=WAL` (leitura concorrente), `foreign_keys=ON` e, no `get_connection`, `synchronous=NORMAL`. Além disso, o bloco `_PRAGMAS_PERF` é aplicado em **toda** conexão (read-write e read-only).

### Performance para o add-in da calculadora (23/06/2026)

O foco foi deixar lookups por ticker em `InfoAtivos`/`FluxoAtivos` o mais rápido possível, porque um **add-in externo** (calculadora de PU da mesa) consulta o banco a cada precificação.

- **Índice cobridor (covering) em `FluxoAtivos`:** `(cdTicker, dtEvento, vrPctAmortizacao, vrPctIncorporacao)`. A query de fluxo vira index-only scan (não toca a tabela). Cashflow por ticker: **538 µs → 84 µs**. O índice antigo `idxFluxoAtivosCdTicker` foi removido (redundante — prefixo da PK) via `DROP INDEX` no `bootstrap()`.
- **`_PRAGMAS_PERF`** em toda conexão: `mmap_size=256 MiB`, `cache_size=-65536` (64 MiB), `temp_store=MEMORY`, `busy_timeout=5000`. São SQLite puro — valem em C#/.NET, VBA/ODBC etc.
- **`ANALYZE`** rodado no `bootstrap()` apenas se `sqlite_stat1` não existir (popula estatísticas do planner uma única vez).
- **`get_readonly_connection()`** abre o banco em modo `ro` + `query_only=ON`, sem bootstrap. Com ela, InfoAtivos + FluxoAtivos juntos resolvem em **~26–36 µs/ticker**.

**Dica de uso do add-in:** reutilizar **uma única conexão** entre lookups — abrir/fechar a cada consulta domina o tempo total. Queries ideais e mapeamento de campos em [[04 - Banco de Dados]] (seção "Mapeamento InfoAtivos + FluxoAtivos para calculadora externa de PU").

**Decisão (23/06/2026):** otimização aplicada só no caminho de leitura — não muda o schema lógico nem os dados. O índice cobridor e os PRAGMAs são transparentes para os scripts existentes; o `get_readonly_connection` é uma porta de entrada nova e isolada para consumidores externos.

---

## lib/logger.py

Cria um logger nomeado com dois handlers: console (`INFO`) e arquivo (`DEBUG`).

**Como usar:**
```python
from lib.logger import get_logger

log = get_logger("scrape_b3_boletim")
log.info("Iniciando scrape...")
log.debug("Detalhe técnico só vai pro arquivo")
```

**Comportamento:**
- Arquivo de log em: `data/logs/{name}/{YYYY-MM-DD_HHMMSS}.log`
- O timestamp é fixado no início do processo (`_SESSION_TS`), então todas as chamadas dentro de uma mesma rodagem escrevem no mesmo arquivo.
- Cada rodagem nova cria um arquivo novo — nunca sobrescreve.
- Idempotente: se `get_logger(name)` for chamado mais de uma vez com o mesmo nome, retorna o logger já configurado sem duplicar handlers.
- Formato: `2026-05-30 14:32:10,123 INFO     scrape_b3_boletim — mensagem`

**Decisão (30/05/2026):** subpasta por script (`logs/scrape_b3_boletim/`) mantém os logs organizados conforme o número de scripts cresce. Arquivo por timestamp permite identificar cada run individualmente sem rotação manual.

---

## lib/email_outlook.py

Envia email via Outlook (COM automation) ao final de cada script.

**Como usar:**
```python
from lib.email_outlook import send_completion_email

send_completion_email(
    script_name="scrape_b3_boletim",
    success=True,
    summary_text="Total trades: 142\n...",
    error_traceback=None,   # passa traceback como string se success=False
    logger=log,             # opcional — passa o logger do script chamador
)
```

**Comportamento:**
- Verifica `cfg["email"]["ativo"]` antes de qualquer coisa. Se `false`, loga e retorna sem enviar.
- Destinatários resolvidos por `get_email_list("outlook")` (`code/destinatarios.py` → variável `OUTLOOK_TO` → `[]`). Aceita múltiplos, separados por `;` ou `,`. Sem default hardcoded — se vazio, loga aviso e não envia. Ver [[99 - Credenciais e Links]].
- Cria uma mensagem Outlook por destinatário via `win32com.client.Dispatch("Outlook.Application")`.
- Assunto: `[OK] {script_name}` ou `[ERROR] {script_name}`. Esse é o formato padrão para **todos** os scripts do projeto.
- Corpo inclui o `summary_text` e, em caso de erro, o `error_traceback` completo.
- **Logger opcional:** aceita parâmetro `logger: logging.Logger`. Se passado, usa esse logger para registrar o envio; caso contrário usa `logging.getLogger(__name__)`. Permite que os logs de email apareçam no arquivo de log do script chamador.
- **Falha silenciosa:** qualquer exceção (Outlook não aberto, pywin32 não instalado, etc.) é capturada e logada — nunca propaga para o script chamador.

**Dependências externas:** `pywin32` (só funciona no Windows com Outlook instalado).

**Decisão (30/05/2026):** falha silenciosa é intencional — problema de email não deve interromper o pipeline de dados.

**Decisão (31/05/2026):** formato de assunto padronizado para `[OK] script` / `[ERROR] script` em todos os scripts. Formato anterior (`[credito-privado] OK · script`) era mais verboso sem ganho prático.

**Decisão (20/06/2026):** timeout reduzido de 60s para 20s (`_EMAIL_TIMEOUT = 20`). O Outlook raramente demora mais de 20s para confirmar permissão; 60s era desnecessariamente longo.

---

## lib/fianalytics_api.py

Módulo de calculadora FI Analytics — **primeiro nível da cascata** de cálculo de taxa. Acionado por `calc_taxa_negocios.py` antes de recorrer à [[06 - Calculadoras/B3 Calculator API]].

Tenta dois níveis internos antes de desistir: (1) endpoint principal por tipo de instrumento; (2) fluxo bondbuilder (`POST /bb/bondbuildercalculator/getuserbonds` + `POST /bb/bondbuildercalculator`).

Para detalhes do contrato da API, autenticação e decisões de implementação, ver [[06 - Calculadoras/FI Analytics API]].

**Interface:**

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

**Notas de uso:**
- Sempre passar `dtLiquidacao`, nunca `dtNegocio`.
- O retorno já está em **% a.a.** — a conversão de decimal (formato nativo da API) para % é feita internamente (×100).
- Usa `logging.getLogger(__name__)` — sem handlers próprios. O script chamador configura o logging via `lib/logger.py`.
- **Cache de getUserBonds:** `POST /bb/bondbuildercalculator/getuserbonds` é chamado no máximo uma vez por processo. O resultado fica em `_userBondsFetched` / `_userBonds` (module-level). Reutilizado em todas as chamadas subsequentes ao bondbuilder.
- **Cache de resultados:** `CalcRate` cacheia cada resultado por `(cdTicker, dtLiquidacao, vrPU)` exatos em `_rateCache`. Usa sentinel `_CACHE_MISS` para distinguir "ausente do cache" de "valor `None` cacheado". Sem retries — tentativa única em cada endpoint.
- **Thread-safe:** `_bondsFetchLock = threading.Lock()` protege `_GetUserBonds()` com double-check locking — garante que o fetch da lista de bonds acontece apenas uma vez mesmo com várias threads concorrentes (ex: `ThreadPoolExecutor` em `calc_taxa_negocios.py`).

---

## lib/b3_calc_api.py

Módulo de calculadora B3 — **segundo nível da cascata** de cálculo de taxa. Acionado por `calc_taxa_negocios.py` quando a [[06 - Calculadoras/FI Analytics API]] falha para um trade.

Consulta o endpoint `GET /calcYield/{cdTicker}/{dtLiquidacao}/{vrPU}` da B3 Calculator API e retorna o yield em % a.a.

Para detalhes do contrato da API, autenticação e regras de falha, ver [[06 - Calculadoras/B3 Calculator API]].

**Interface:**

```python
from lib.b3_calc_api import CalcYield, ResetToken

yieldVal = CalcYield("DEBA11", "2026-05-27", 1052.34)
# retorna float (% a.a.) ou None se a API falhar

ResetToken()  # força renovação do token na próxima chamada
```

**Notas de uso:**
- O token é gerenciado internamente (lazy, module-level). Não é necessário fazer login explicitamente.
- `ResetToken()` pode ser chamado externamente para forçar renovação em caso de erro 401 persistente.
- Usa `logging.getLogger(__name__)` — sem handlers próprios. O script chamador configura o logging via `lib/logger.py`.
- **Cache de resultados:** `CalcYield` cacheia cada resultado por `(cdTicker, dtLiquidacao, vrPU)` exatos em `_rateCache`. Usa sentinel `_CACHE_MISS` para distinguir "ausente do cache" de "valor `None` cacheado". Evita chamadas duplicadas ao reprocessar trades.
- **Sem retries genéricos:** tentativa única. Em 401, `_DoRequest()` renova o token e repete uma segunda vez imediatamente (sem sleep) — cobre expiração de sessão mid-run sem loop de retry.
- **Thread-safe:** `_tokenLock = threading.Lock()` protege `_EnsureToken()` com double-check locking — garante que apenas uma thread faz login mesmo com várias disparando simultaneamente (ex: `ThreadPoolExecutor` em `calc_taxa_negocios.py`).
