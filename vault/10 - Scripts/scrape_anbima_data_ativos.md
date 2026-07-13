# Script: scrape_anbima_data_ativos.py

> Ver também: [[../04 - Banco de Dados]] | [[../05 - Fontes/Anbima]] | [[scrape_anbima_debentures]] | [[scrape_anbima_cri_cra]]

**Arquivo:** `code/scripts/scrape_anbima_data_ativos.py`

**Status:** implementado e validado (21/06/2026). Modo incremental reescrito em 29/06/2026 (união trades+Anbima + completude real + skip-list manual).

> ⚠️ **Virou FALLBACK em 13/07/2026.** A **B3** (`scrape_b3_bond_details`, passo 2) é agora a fonte **primária** do cadastro e do fluxo. Este script roda **depois** dela (passo 6) e só preenche o que ela **não cobriu** — na prática, `cdISIN` e `vrQuantidadeEmissao` (que a B3 não traz) e o fluxo dos ~1.842 ativos fora da cobertura da B3. Ver [[../15 - Cadastro dos Ativos]].
>
> **Duas guardas foram postas para ele não estragar cadastro de fonte B3:**
> 1. O `UpsertInfoAtivos` usa `COALESCE` em tudo e grava `cdFonteCadastro = 'AnbimaData'` **só quando a coluna está NULL** — num ativo B3 ela fica.
> 2. O `lib.db.SincronizarFluxoAtivos` **se recusa a escrever** no fluxo de um ativo `cdFonteCadastro = 'B3'`. A guarda mora na lib, não aqui, porque `FluxoAtivos` tem vários writers.
>
> **Por quê:** `vrVNE` + `dtInicioRentabilidade` + `FluxoAtivos` são um **pacote indivisível**. A B3 pré-capitaliza a carência dentro do VNE; a Anbima traz o VNE cru mais a incorporação como evento. Reescrever só o fluxo por cima deixaria o VNE capitalizado órfão, e a carência passaria a contar **duas vezes** — sem erro, sem exceção, só um PU errado.

---

## O que faz

Scrapa o site [data.anbima.com.br](https://data.anbima.com.br) para extrair informações detalhadas por ativo (aba "Características") e o fluxo completo de pagamentos (aba "Agenda"). Grava os dados diretamente em `InfoAtivos` (UPSERT via COALESCE) e na tabela nova `FluxoAtivos`.

Cobre debêntures, CRIs e CRAs. Filtro de indexador válido: `DI+`, `IPCA`, `DI%`, `Pré-Fixado` — ativos com outros indexadores têm o JSON salvo mas **nada é inserido no banco**.

---

## Por que não usar o bulk Anbima (scrape_anbima_debentures / scrape_anbima_cri_cra)?

Os arquivos XLS/CSV do bulk Anbima trazem apenas taxas indicativas diárias e campos estáticos básicos. A Anbima Data tem dados muito mais ricos: ISIN, quantidade emitida, taxa de emissão, data de início de rentabilidade, e o fluxo completo de eventos futuros (amortizações, incorporações, pagamentos de juros).

---

## Autenticação — reCAPTCHA session-bound

A Anbima Data é um app Angular que por baixo chama:

```
https://data-api.prd.anbima.com.br/web-bff/v1/
```

Todas as requisições carregam o header `g-google-authorization: <JWT>` gerado por reCAPTCHA anônimo. Esse JWT não funciona fora da sessão Playwright e retorna HTTP 418 se tentado via httpx puro.

**Estratégia adotada:** o Playwright abre a página do ativo e intercepta as respostas da API via `page.on('response', ...)`. Para paginação da listagem, usa `page.route()` para sobrescrever o parâmetro `size=500` antes da requisição sair.

---

## Mapeamento de tipos/URLs

```python
TIPO_API = {
    'DEB': 'debentures',
    'CRI': 'certificado-recebiveis',
    'CRA': 'certificado-recebiveis',
}
TIPO_URL_SITE = {
    'DEB': 'debentures',
    'CRI': 'certificado-de-recebiveis',
    'CRA': 'certificado-de-recebiveis',
}
```

CRI e CRA compartilham o mesmo endpoint de API e de listagem (`certificado-de-recebiveis`). O campo `tipo_titulo` na resposta da listagem distingue os dois. Para tickers sem essa info, `_InferTipo` usa o prefixo do código B3 (`CRA...` → CRA, `CRI...` → CRI, demais → DEB).

---

## Endpoints da API

| Tipo | Características | Agenda |
|---|---|---|
| DEB | `GET /debentures/{ticker}` | `GET /debentures/{ticker}/agenda?page=0&size=500` |
| CRI/CRA | `GET /certificado-recebiveis/{ticker}` | `GET /certificado-recebiveis/{ticker}/agenda?page=0&size=500` |

Listagem:
- DEB: `data.anbima.com.br/busca/debentures`
- CRI+CRA: `data.anbima.com.br/busca/certificado-de-recebiveis`

Paginação da listagem interceptada via `page.route()` com `size=5000`. Com `size=500`, a paginação capturava apenas ~3000 de 4178 debêntures porque a navegação `?page=N` no Angular não mapeia linearmente para a página N da API — alguns tickers ficavam de fora. Com `size=5000` o servidor retorna todos os tickers de um tipo em uma única requisição, eliminando a necessidade de paginação.

**Problema de paginacao DEB (3000/4178) — RESOLVIDO em 21/06/2026:**

Mesmo com `SIZE_LISTING=5000`, o servidor entregava no máximo ~3000 DEBs por sessão de browser (Angular não faz novo request à API ao navegar entre páginas `?page=N`). Duas abordagens descartadas durante a investigação:
- `page.evaluate(fetch())` — retornou 0 itens (falha silenciosa de auth: sem o JWT reCAPTCHA da sessão atual)
- `page.request.get()` — retornou HTTP 418 (servidor detectou chamada direta fora do contexto do browser)

**Causa real:** o Angular SPA calcula `total_pages` com base no `size=500` original e ignora o `size=5000` nas páginas subsequentes. A partir da 6ª página, `?page=6` não retorna itens novos.

**Solução implementada — dois passes com ordens opostas:**

`_GetListingTickersWithFilter` foi refatorada em `_GetListingPass(browser, api_key, ui_base, order, log)` + um loop que chama `order='asc'` e depois `order='desc'`, em contextos de browser separados, e une os resultados:

- Passe 1 (`order=asc`): primeiros 3000 DEBs alfabeticamente
- Passe 2 (`order=desc`): últimos 3000 DEBs (cobre os 1178 restantes do final da lista)
- União deduplica por ticker → 4178/4178

CR continua com passe único (`order=asc`) porque 484 < cap real do servidor.

Log esperado em `--mode full`:
```
Listagem DEB (asc): 3000/4178 — acumulado 3000/4178
Listagem DEB (desc): 3100/4178 — acumulado 4178/4178
```

---

## Arquitetura de duas fases em `--mode full`

`--mode full` sempre executa **duas fases em sequencia**, independente de `--limit`:

**Fase 1 — Listagem (~3.5 min):** navega o site para coletar todos os tickers válidos. Com a solução de dois passes, a listagem DEB faz 2 navegações (`order=asc` + `order=desc`) + 1 para CR. Cada navegação demora ~40s (com `WAIT_MS=3000`). O resultado é uma lista de tickers que será passada para a Fase 2.

**Fase 2 — Scraping (N × ~30s):** para cada ticker da lista, 2 navegacoes: aba "Caracteristicas" (~8s de espera) + aba "Agenda" (~8s de espera). Com 4 workers, o throughput e ~4 tickers/min.

Para testes, usar `--ticker TICKER` diretamente: pula a Fase 1 completamente e so executa a Fase 2 para o ticker especificado. Muito mais rapido — cerca de 30s para um ticker individual.

```powershell
# Teste rapido de um ticker especifico (pula listagem)
python scripts/scrape_anbima_data_ativos.py --ticker FGEN13 --force
```

**Comportamento validado com `--ticker FGEN13 --force` (21/06/2026):**
- InfoAtivos: `cdEmissor`, `cdIndexador` (IPCA), `cdISIN`, `vrQuantidadeEmissao`, `dtEmissao` preenchidos
- Campos pre-existentes (`vrDuration`, `cdReferencia`, `cdFonteReferencia`) nao sobrescritos (COALESCE funciona)
- FluxoAtivos: 27 linhas com incorporacoes parciais (45%) e amortizacoes progressivas
- Duracao total: ~30s

---

## Notas de implementacao — bugs corrigidos

### Bugs do teste inicial (20/06/2026)

Tres problemas identificados e corrigidos no primeiro teste de execucao:

**1. `sys.path.insert` faltando**

O script nao tinha o ajuste de path necessario para importar modulos de `lib/`. Sem ele:

```
ModuleNotFoundError: No module named 'lib.config'
```

Correcao adicionada no topo do arquivo, antes de qualquer import de `lib/`:

```python
sys.path.insert(0, str(Path(__file__).parent.parent))
```

Todos os outros scripts do projeto ja fazem isso; foi omissao no momento da escrita inicial.

**2. `setup_logger` → `get_logger`**

A funcao em `lib/logger.py` se chama `get_logger`, nao `setup_logger`. O script estava chamando o nome errado e lancava `AttributeError` na inicializacao. Corrigido.

**3. `SIZE_LISTING = 500 → 5000`**

Com `size=500` na interceptacao da listagem, o script capturava aproximadamente 3000 de 4178 debentures. O motivo: a navegacao `?page=N` no Angular nao mapeia linearmente para a pagina N da API — parte dos tickers ficava fora do escopo das paginas retornadas. Aumentar para `size=5000` faz o servidor retornar todos os tickers de um tipo em uma unica requisicao, eliminando a necessidade de paginacao e garantindo cobertura total.

**headless=True confirmado**

Este script usa `headless=True` por design — diferente de outros scripts do projeto que usam `headless=False`. Justificativa: roda com multiplos workers em paralelo; abrir N janelas de browser simultaneamente nao faz sentido operacional.

### Bugs corrigidos em sessao posterior (21/06/2026)

**4. Double `queue.task_done()` → `ValueError` + `TargetClosedError`**

O worker chamava `queue.task_done()` explicitamente dentro do bloco `try` em cada `continue` antecipado (JSON nao encontrado, caracteristicas nao capturadas, agenda nao capturada, indexador invalido) — E o bloco `finally` tambem chamava `queue.task_done()`. Resultado: o primeiro worker a terminar lancava `ValueError: task_done() called too many times`, que propagava via `asyncio.gather()` e fechava o browser/playwright antes dos outros workers terminarem. Os workers restantes recebiam `TargetClosedError` em todas as chamadas subsequentes ao browser.

Fix: remover todos os `queue.task_done()` explícitos de dentro do bloco `try`. O bloco `finally` é o unico responsavel por chamar `task_done()`.

```python
# ERRADO (causava double call)
try:
    if not caract:
        queue.task_done()   # <- problema
        continue
    ...
finally:
    queue.task_done()       # <- chamado de qualquer forma

# CORRETO
try:
    if not caract:
        continue            # finally ainda executa
    ...
finally:
    queue.task_done()
```

**5. Check de indexador invalido muito agressivo (`not idx`)**

A condicao `if not idx or idx.upper() in INDEXADORES_INVALIDOS_TICKER` descartava tickers cujo campo `indexador.nome` viesse vazio ou `None` da API de caracteristicas. Campo vazio nao e motivo de descarte — o ativo pode ser valido e o campo simplesmente nao estar preenchido na fonte.

Fix: `if idx and idx.upper() in INDEXADORES_INVALIDOS_TICKER` — so descarta se o indexador for explicitamente um dos invalidos (IGPM, TR, INPC, DÓLAR, USD). Campo vazio passa sem descarte.

---

## Primeira carga completa (`--mode full`) — 21/06/2026

Executada em 21/06/2026, 14:43–17:08 (~2h25min). Log em `data/logs/scrape_anbima_data_ativos/2026-06-21_144355.log`.

Resultados finais (linha "Concluído" do log):

| Indicador | Valor |
|---|---|
| Tickers na listagem | 4593 (4109 DEBs válidos + 484 CRs) |
| Scrappados com sucesso | 4501 |
| Inseridos no DB | 4503 |
| Erros | 87 (~1,9%) |
| FluxoAtivos OK | 4173 |
| FluxoAtivos skip (evento_desconhecido) | 330 |
| Descartados por indexador | 3 |

> Os inseridos (4503) podem ser ligeiramente maiores que os scrappados (4501) porque `--skip-scrape` re-insere tickers de JSONs já existentes de runs anteriores.

### O que conta como "erro" no stats

Os 87 erros **não geram linha `ERROR` no log** — geram `WARNING`. O campo `stats['erros']` é incrementado em 4 situações distintas:

1. `--skip-scrape` ativado e o JSON do ticker não existe em cache (sem dados para inserir)
2. `_ScrapeCaract` retornou `None` (timeout ou falha de carregamento da aba Características)
3. `_ScrapeAgenda` retornou `None` (timeout ou falha de carregamento da aba Agenda)
4. Exception inesperada no worker (qualquer exceção não tratada dentro do bloco `try`)

Em todos os casos, o JSON não é gravado e o ticker entra como `stats['erros'] += 1`. Para re-tentar apenas os que falharam, basta reexecutar `--mode full` — os 4501 com JSON existente são pulados automaticamente, e apenas os 87 sem JSON são tentados novamente.

---

## Como testar

Teste rapido com 5 tickers:

```powershell
python scripts/scrape_anbima_data_ativos.py --mode full --limit 5
```

Validacao no banco apos execucao:

```python
import sqlite3
conn = sqlite3.connect('data/trades.db')
conn.row_factory = sqlite3.Row

# Ver os ultimos registros inseridos em InfoAtivos
for r in conn.execute(
    'SELECT cdTicker, cdInstrumento, cdEmissor, dtVencimento, cdIndexador, cdISIN, vrQuantidadeEmissao '
    'FROM InfoAtivos ORDER BY dtAtualizacao DESC LIMIT 10'
):
    print(dict(r))

# Ver fluxo de pagamentos
for r in conn.execute(
    'SELECT cdTicker, COUNT(*) n, MIN(dtEvento) primeiro, MAX(dtEvento) ultimo '
    'FROM FluxoAtivos GROUP BY cdTicker'
):
    print(dict(r))
```

---

## Flags CLI

```powershell
# Carga inicial — lista todos os tickers do site Anbima Data
python scripts/scrape_anbima_data_ativos.py --mode full

# Incremental: tickers que tiveram trade OU taxa Anbima na data e estão
# incompletos no banco
python scripts/scrape_anbima_data_ativos.py --date 2026-06-20

# Range de datas
python scripts/scrape_anbima_data_ativos.py --start 2026-06-09 --end 2026-06-20

# Debug de um ticker específico
python scripts/scrape_anbima_data_ativos.py --ticker XYZABC11

# Forçar re-scrape (ignora completude e JSON em cache, deleta e re-baixa)
python scripts/scrape_anbima_data_ativos.py --date 2026-06-20 --force

# Pular scraping; re-inserir no DB a partir dos JSONs já existentes
python scripts/scrape_anbima_data_ativos.py --skip-scrape

# Limitar a N tickers (para testes rápidos)
python scripts/scrape_anbima_data_ativos.py --mode full --limit 10

# Controle de workers paralelos (default 4)
python scripts/scrape_anbima_data_ativos.py --mode full --workers 6
```

`--mode full` e `--date`/`--start`/`--ticker` são mutuamente exclusivos. `--end` só faz sentido com `--start`.

---

## Modo incremental — como a fila de tickers é montada (reescrito 29/06/2026)

Antes, o incremental pegava tickers só de `NegociosBrutos` na data e decidia re-scrape pela mera **existência do JSON de checkpoint**. Os dois critérios foram trocados.

### 1. Fonte de tickers — UNIÃO de duas origens

Para a(s) data(s) do range, junta e deduplica por ticker:

- **Quem teve trade** — `_GetTickersFromTradesRaw`: `NegociosBrutos.dtNegocio` na(s) data(s), excluindo `cdSituacao = 'Cancelado'`. Traz `cdInstrumento` cru (pode ser NULL).
- **Quem teve taxa Anbima divulgada** — `_GetTickersFromAnbima`: `AnbimaIndicativos.dtReferencia` na(s) data(s). Como `AnbimaIndicativos` **não tem coluna de instrumento**, faz `LEFT JOIN InfoAtivos` para obter `cdInstrumento`; se ainda faltar, cai no `_InferTipo` (prefixo do ticker) na hora da dedup.

A dedup prefere o `cdInstrumento` não-nulo entre as duas fontes.

Motivo da inclusão da Anbima: um ativo pode ter taxa indicativa divulgada (logo, aparece no relatório) sem ter sido negociado naquele dia — ainda assim queremos suas características/fluxo no banco.

### 2. Critério de re-scrape — completude REAL no banco, não "JSON existe?"

`_GetTickersCompletos` decide quem já está pronto. Um ticker é **completo** (e portanto pulado, sem `--force`) somente se TODAS estas condições valerem:

- existe em `InfoAtivos`;
- as **10 colunas Anbima** estão todas não-nulas: `cdInstrumento, cdEmissor, dtVencimento, cdIndexador, vrTaxaEmissao, vrVNE, dtInicioRentabilidade, cdISIN, vrQuantidadeEmissao, dtEmissao` (constante `INFO_REQUIRED_COLS`);
- tem **pelo menos uma linha** em `FluxoAtivos`.

Se qualquer uma falhar, o ticker entra na fila. A query roda em **chunks de 500** tickers (`SQL_VAR_CHUNK`) para não estourar o teto de ~999 variáveis do SQLite. `--force` ignora a completude (manda todos os candidatos para a fila).

### 3. Guarda contra re-scrape infinito (`indexador_invalido`)

Ativos com indexador inválido **nunca** são persistidos no banco — logo, sempre apareceriam como "incompletos" e seriam raspados de novo a cada rodada. `_GetInvalidIndexadorTickers` lê os JSONs de checkpoint e exclui da fila, **incondicionalmente**, qualquer ticker cujo `skip_reasons` contenha `'indexador_invalido'`.

---

## Skip-list manual de tickers (29/06/2026)

Arquivo opcional `code/data/anbima_skip_tickers.csv` para excluir tickers de TODOS os modos:

- Um ticker por linha; `#` no início = comentário; linha em branco ignorada.
- Vírgula separa um motivo opcional — só a **1ª coluna** (o ticker) é lida; `.upper()` aplicado.
- Lido por `_LoadSkipTickers()` de `dbFile.parent / 'anbima_skip_tickers.csv'` (mesmo precedente de `_LoadFeriados`). Se o arquivo não existir, retorna set vazio.

A exclusão é **incondicional** — vale em `--mode full`, `--date`/range, `--skip-scrape` e `--ticker`, e **mesmo com `--force`** (é aplicada depois da montagem da fila, em qualquer modo). Reportada no email como `Pulados (skip-list): N`.

**Workflow pretendido:** rodar → ver no email os ativos com info faltante → inspecionar na mão → adicionar ao `anbima_skip_tickers.csv` os que a Anbima genuinamente não fornece → próximas rodadas pulam (e a fila para de inflar com lixo permanente).

---

## Checkpoint — JSON por ticker

Cada ticker processado gera `data/anbima_data_raw/{ticker}.json`:

```json
{
  "ticker": "XYZABC11",
  "cdInstrumento": "DEB",
  "info": { ... },
  "agenda": [ ... ],
  "skip_reasons": []
}
```

Se o arquivo já existe e `--force` não foi passado, o ticker é pulado sem abrir o browser. Isso garante que uma interrupção no meio de uma carga de 3k ativos seja segura — basta reexecutar para continuar de onde parou.

`skip_reasons` é gravado no JSON. Runs subsequentes releem o campo sem re-scrappear — ativos com `indexador_invalido` ou `evento_desconhecido` não são re-processados a menos que `--force` seja usado.

**Comportamento tudo-ou-nada:** se o scrape falhar (características não capturadas ou agenda não capturada), o JSON **não é gravado** e o ticker retorna `stats['erros'] += 1`.

---

## Fluxo de execução por ticker (worker)

1. Verificar JSON em cache → se existe e não `--force`: ler do arquivo, pular browser
2. Se indexador inválido (`skip_reasons: ['indexador_invalido']`): salvar JSON, **não** inserir no DB, `stats['descartados'] += 1`
3. Scrape de características via `_ScrapeCaract` → intercepta `web-bff/v1/{api}/{ticker}` (sem `/agenda` na URL)
4. Se indexador válido: scrape de agenda via `_ScrapeAgenda` → intercepta `web-bff/v1/{api}/{ticker}/agenda`
5. Processar agenda com `_ProcessAgenda` → se evento desconhecido: `skip_reasons: ['evento_desconhecido']`
6. Upsert em `InfoAtivos` sempre (para indexadores válidos)
7. Upsert em `FluxoAtivos` só se `fluxo_rows is not None`
8. Salvar JSON com `skip_reasons` preenchido

---

## Constante WAIT_MS

```python
WAIT_MS = 3000  # ms de espera após navegação antes de ler interceptações
```

Reduzida de 8000ms para 3000ms em 21/06/2026. Impacto: listagem DEB+CR passou de ~5 min para ~3.5 min. O valor de 3000ms se mostrou suficiente para garantir que os responses da API Anbima Data sejam capturados pelo listener de interceptação antes do script continuar.

---

## Normalização de cdIndexador — `_NormalizeIndexador`

O campo `indexador.nome` da API retorna apenas `'DI'` para qualquer bond referenciado ao CDI — não distingue CDI+ de %CDI. Para distinguir corretamente, usa-se o campo `remuneracao` (texto descritivo completo do ativo).

Função `_NormalizeIndexador(info: dict) -> str | None` — exemplos de entrada e saída:

| `info['remuneracao']` (exemplo) | `cdIndexador` retornado |
|---|---|
| `"DI + 3.5000%"` | `'CDI+'` |
| `"DI + 0.0000%"` | `'CDI+'` |
| `"110,5500% DI"` | `'%CDI'` |
| `"IPCA + 6.4686%"` | `'IPCA'` |
| `"Pré-Fixado 13,4700%"` | `'PREFIXADO'` |
| `"PRE 12,0000%"` | `'PREFIXADO'` |
| `None` / campo ausente | `None` |

Regras de detecção (aplicadas em ordem):
1. `"DI + "` no texto → `'CDI+'`
2. `"% DI"` no texto → `'%CDI'`
3. `"IPCA"` no texto → `'IPCA'`
4. `"PRÉ"` ou `"PRE"` no texto → `'PREFIXADO'`

`_UpsertInfoAtivos` chama `_NormalizeIndexador(info)` em vez de `(info.get('indexador') or {}).get('nome')`.

**Decisão (21/06/2026):** usar `remuneracao` como fonte do `cdIndexador` porque distingue CDI+ de %CDI, ao contrário de `indexador.nome` que retorna `'DI'` para ambos.

---

## Regras de negócio

### Indexadores

Válidos: `DI+`, `IPCA`, `DI%`, `Pré-Fixado`, `Pre-Fixado` (variante sem acento aceita).

Inválidos → JSON salvo com `skip_reasons: ['indexador_invalido']`, nada inserido no banco.

### Eventos da agenda

Conhecidos: `PAGAMENTO DE JUROS`, `AMORTIZACAO`, `VENCIMENTO (RESGATE)`, `INCORPORACAO DE JUROS`.

Detecção por substring (case-insensitive) no campo `evento`. Evento desconhecido → `InfoAtivos` inserido normalmente, `FluxoAtivos` descartado, `skip_reasons: ['evento_desconhecido']`.

---

## Campos gravados em InfoAtivos

O UPSERT usa `COALESCE` — nunca sobrescreve valor existente com NULL.

| Coluna `InfoAtivos` | Campo API | Observação |
|---|---|---|
| `cdTicker` | `codigo_b3` | PK |
| `cdInstrumento` | `tipo` | `'DEB'` / `'CRI'` / `'CRA'` |
| `cdEmissor` | DEB: `emissao.emissor.nome`; CRI/CRA: `devedor` | |
| `dtVencimento` | `data_vencimento` | |
| `cdIndexador` | `indexador.nome` | |
| `vrTaxaEmissao` | `taxa_emissao` | já vinha do FI Analytics; Anbima Data confirma/complementa |
| `vrVNE` | `vne` | valor de face original (ex: 1000) |
| `dtInicioRentabilidade` | `data_inicio_rentabilidade` | |
| `cdISIN` | `isin` | novo — adicionado via migração em `bootstrap()` |
| `vrQuantidadeEmissao` | `quantidade_emitida` (campo raiz, não `emissao.quantidade_emitida`) | quantidade desta série |
| `dtEmissao` | `emissao.data_emissao` | novo — adicionado via migração |

---

## FluxoAtivos — schema e lógica

Schema (implementado em `lib/db.py`):

```sql
CREATE TABLE IF NOT EXISTS FluxoAtivos (
    cdTicker           TEXT NOT NULL,
    dtEvento            TEXT NOT NULL,   -- data_liquidacao do evento
    vrPctAmortizacao  REAL NULL,
    vrPctIncorporacao REAL NULL,
    dtAtualizacao        TEXT NOT NULL,
    PRIMARY KEY (cdTicker, dtEvento)
);
```

PK é `(cdTicker, dtEvento)` onde `dtEvento` = `data_liquidacao` (não `data_base`). Upsert via `INSERT OR REPLACE`.

Eventos são agrupados por `data_liquidacao` antes de calcular os campos:

**vrPctAmortizacao:** lê o campo `taxa` do primeiro evento de tipo `AMORTIZACAO`, `VENCIMENTO` ou `RESGATE` encontrado na data.

**vrPctIncorporacao:**
- Sem evento `INCORPORACAO`: `NULL`
- `INCORPORACAO` sem `PAGAMENTO DE JUROS` na mesma data: `100.0`
- `INCORPORACAO` + `PAGAMENTO DE JUROS` com valores numéricos: `sum(valor_incorp) / (sum(valor_incorp) + sum(valor_juros)) * 100`
- `INCORPORACAO` + `PAGAMENTO DE JUROS` com algum valor `None` ou `'-'`: `NULL` (futuro ainda não precificado)

---

## Email ao terminar

Assunto `[OK] scrape_anbima_data_ativos` ou `[ERROR] scrape_anbima_data_ativos`.

Corpo inclui contadores:
- Tickers inseridos/atualizados no DB
- Scrappados nessa run (novos JSONs)
- Descartados por indexador inválido
- **Pulados (skip-list)** — excluídos pelo `anbima_skip_tickers.csv` (29/06/2026)
- FluxoAtivos inseridos com sucesso
- FluxoAtivos descartados (evento desconhecido)
- Erros (ticker sem dados capturados)

Seções extras quando aplicável:
- **Ativos com info faltante após scrape (N tickers)** — para cada ticker **efetivamente raspado nesta run** (não os que vieram só de cache), lista quais das colunas Anbima de `InfoAtivos` ficaram NULL. Calculado por `_ComputeInfoFaltante`, que usa exatamente a mesma extração de campos do `_UpsertInfoAtivos` (e `_IsInfoNull` trata `None`, string vazia e `'-'` como nulo). Constante `INFO_FALTANTE_COLS` (9 colunas; igual a `INFO_REQUIRED_COLS` menos `cdInstrumento`). É a seção que alimenta o workflow da skip-list manual.
- **Anomalias** — tickers com `fluxo_skip` (FluxoAtivos descartado por evento desconhecido).
- **Novos tickers scrappados** nessa run.

### Fato empírico (varredura dos ~4500 checkpoints, 29/06/2026)

A Anbima genuinamente **não fornece**, para um subconjunto pequeno de ativos, apenas:
- `cdISIN` — ~85 ativos
- `vrQuantidadeEmissao` — ~37 ativos

Todos os demais campos (inclusive `vrVNE` e `isin`/`vne` para CRI/CRA) vêm preenchidos. Esses ~120 ativos são os candidatos naturais à skip-list — sem ela, ficariam para sempre "incompletos" e seriam re-raspados em toda rodada incremental.

---

## Posição no pipeline

Este script **não** entra no pipeline diário automático — roda separadamente por ser lento e os dados de fluxo mudarem pouco dia a dia. Sugestão: rodar semanalmente em `--mode full` para atualizar fluxos, ou diariamente em `--date` para cobrir tickers novos.

Quando rodar junto com o pipeline:
```
... scrape_anbima_debentures + scrape_anbima_cri_cra ...
→ scrape_anbima_data_ativos (tickers novos pegos de NegociosBrutos)
```

---

## Paralelismo

N workers Playwright assíncronos (asyncio), cada um com seu próprio `browser_context`. Em `--skip-scrape`, força 1 worker (sem risco de race no disco).

| Workers | 3k ativos (carga inicial) | 300/dia (incremental) |
|---|---|---|
| 1 | ~87 min | ~9 min |
| 4 | ~22 min | ~2 min |
| 6 | ~15 min | ~1.5 min |

*(Estimativas baseadas em 1.74s/ativo medido em testes de 12/06/2026)*

Acesso ao banco protegido por `asyncio.Lock()` — upserts sempre sequenciais, um worker de cada vez.

---

## Decisões de design (20–21/06/2026)

- **InfoAtivos em vez de InfoAtivosAnbima:** o plano original previa uma tabela separada `InfoAtivosAnbima`. Na implementação, decidiu-se escrever diretamente em `InfoAtivos` via COALESCE — mantém uma única fonte de verdade por ativo e evita joins extras nos relatórios. A tabela `InfoAtivosAnbima` não foi criada.
- **Checkpoint JSON por ticker** em vez de tabela SQLite de status: mais fácil de inspecionar individualmente e sobrevive a corrupção do banco.
- **Tudo-ou-nada por ticker:** se falhar em qualquer ponto, o JSON não é gravado — garante que o cache nunca contenha dados parciais.
- **dtEvento = data_liquidacao** (não `data_base`): relevante para cálculos de fluxo de caixa, onde o que importa é quando o dinheiro entra na conta.
- **Sem extração de token:** interceptar respostas da API via Playwright é mais robusto que tentar reutilizar o JWT reCAPTCHA fora da sessão.
- **vrPctAmortizacao via campo `taxa`:** a API retorna o percentual diretamente no campo `taxa` dos eventos de amortização — não é necessário dividir pelo VNE corrente.
- **Listagem com filtro de indexador antes do scrape:** em `--mode full`, a listagem já descarta ativos com indexador inválido sem abrir página individual por ticker. Economiza tempo significativo na carga inicial.
- **Dois passes para listagem DEB (21/06/2026):** `order=asc` + `order=desc` em contextos separados, union de resultados. Única forma encontrada de superar o cap de ~3000 itens por sessão Angular sem depender de parâmetros de filtro não documentados.
- **`_NormalizeIndexador` usa campo `remuneracao` (21/06/2026):** `indexador.nome` retorna apenas `'DI'` para CDI+ e %CDI — ambos. O campo `remuneracao` tem o texto descritivo completo e permite distinguir `"DI + X%"` (CDI+) de `"X% DI"` (%CDI). Ver seção "Normalização de cdIndexador" acima.
- **WAIT_MS reduzido para 3000ms (21/06/2026):** de 8000ms. Suficiente para capturar responses; reduz listagem de ~5 para ~3.5 min.

**Decisão (29/06/2026 — fonte do incremental = união trades+Anbima):** o incremental passou a juntar tickers de `NegociosBrutos` (negociados) **e** de `AnbimaIndicativos` (taxa divulgada) na data. Um ativo pode entrar no relatório por ter taxa indicativa sem ter sido negociado — então precisa ter características/fluxo no banco mesmo assim. Espelha a mesma lógica de união de [[scrape_outstanding_bloomberg]].

**Decisão (29/06/2026 — re-scrape por completude real, não por JSON existente):** o gatilho de re-scrape virou uma checagem real da base (`_GetTickersCompletos`): 10 colunas Anbima não-nulas em `InfoAtivos` + ≥1 linha em `FluxoAtivos`. Antes bastava o JSON de checkpoint existir, o que mascarava ativos persistidos pela metade. `--force` continua ignorando a checagem.

**Decisão (29/06/2026 — skip-list manual incondicional):** `anbima_skip_tickers.csv` (opcional) exclui tickers em todos os modos, mesmo com `--force`. Resolve o re-scrape infinito dos ~120 ativos que a Anbima não fornece `cdISIN`/`vrQuantidadeEmissao`. Curadoria manual, alimentada pela seção "info faltante" do email.
