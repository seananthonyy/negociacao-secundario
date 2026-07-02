# scrape_b3_boletim.py

Arquivo: `code/scripts/scrape_b3_boletim.py`
Dependências de lib: [[10 - Scripts/libs#lib/db.py|db]], [[10 - Scripts/libs#lib/config.py|config]], [[10 - Scripts/libs#lib/logger.py|logger]], [[10 - Scripts/libs#lib/email_outlook.py|email_outlook]]

## O que faz

Baixa o Boletim Diário da B3 de negociações de crédito privado (Debêntures, CRIs e CRAs) usando Playwright para interagir com a página web, parseia o CSV retornado e grava os negócios na tabela `NegociosBrutos` do banco [[04 - Banco de Dados]]. Cada execução é idempotente: negócios já existentes são atualizados (UPSERT por `cdIdentificadorNegocio`). O CSV nunca é salvo em disco — é processado em memória direto para o banco.

## Como rodar

```powershell
# Roda a partir de code/ como diretório de trabalho
python scripts/scrape_b3_boletim.py --date 2026-05-29
python scripts/scrape_b3_boletim.py --start 2026-05-01 --end 2026-05-29
python scripts/scrape_b3_boletim.py --date 2026-05-27 --headless
python scripts/scrape_b3_boletim.py --date 2026-05-27 --debug-only
```

## Parâmetros CLI

| Flag | Tipo | Descrição |
|---|---|---|
| `--date` | `YYYY-MM-DD` | Data única a processar. Mutuamente exclusivo com `--start`. |
| `--start` | `YYYY-MM-DD` | Início do intervalo. Requer `--end`. |
| `--end` | `YYYY-MM-DD` | Fim do intervalo (inclusive). Requer `--start`. |
| `--headless` | flag | Roda o Playwright em modo headless. Padrão: `False` (browser visível para debug). |
| `--debug-only` | flag | Só salva HTML e PNG de debug em `data/debug/`, sem baixar CSV nem gravar no banco. |

## Como a página B3 funciona

A B3 não expõe o boletim diário como URL direta de download. A página principal (`www.b3.com.br`) carrega um **iframe** que aponta para `arquivos.b3.com.br/bdi/tabelas?lang=pt-BR`. É esse iframe — e não a página principal — que contém os controles de seleção de tabela, data e o botão de export.

O download real é feito por um POST para:
```
POST https://arquivos.b3.com.br/bdi/table/export/csv?lang=pt-BR
Content-Type: application/json

{"Name":"Trade","Date":"YYYY-MM-DD","FinalDate":"YYYY-MM-DD","ClientId":"","Filters":{}}
```

O CSV retornado tem:
- Delimitador `";"`
- Encoding `utf-8-sig` (BOM)
- Um preamble de 7 linhas descritivas antes do header real (o script detecta o header dinamicamente, buscando a primeira linha que contenha uma coluna conhecida do `COLUMN_MAP`)

## Fluxo interno

1. **Navega direto para o iframe** (`arquivos.b3.com.br/bdi/tabelas`) — mais estável que acessar pela página principal da B3.
2. **Clica na aba "Renda fixa"** — via `get_by_text`, com fallback por `wai-aria`.
3. **Seta a data no `duet-date-picker`** — via API JS `setValue()` do Web Component; se não funcionar, tenta preencher o input visível com `.type()` + Enter.
4. **Seleciona a tabela "Negócio a negócio"** — via `select#selectTabelas` com valor `Trade@true`; tenta variações se não encontrar.
5. **Clica no botão CSV** — itera 10 seletores candidatos (`button:has(span.b3__ico--csv)`, `[class*='ico--csv']`, `button:has-text('CSV')`, etc.) e usa `expect_download` do Playwright.
6. **Fallback 1 — POST direto** no endpoint `/bdi/table/export/csv` se o clique não gerar download.
7. **Fallback 2 — CSV interceptado** via listener de responses (captura qualquer response com content-type CSV/octet-stream vindo de `arquivos.b3.com.br`).
8. **Parseia o CSV** em memória e grava no banco via UPSERT.

Snapshots HTML e PNG são salvos em `data/debug/` após cada passo para facilitar diagnóstico.

## Estratégia de range de datas

Quando `--start/--end` é passado, o script **primeiro tenta um POST único** com `Date = start` e `FinalDate = end`. Se a API retornar HTML (indicando que o range não é suportado naquele momento), cai para um **loop data por data**, onde cada data abre um contexto Playwright isolado. Isso mantém estado limpo entre datas e evita contaminação de cookies/sessão.

## Fallbacks de download

```
Clique no botão CSV (10 seletores candidatos)
    ↓ falhou (nenhum seletor encontrou elemento ou download não disparou)
POST direto em /bdi/table/export/csv (3 bodies candidatos)
    ↓ falhou (API retornou HTML ou status != 200)
CSV interceptado via listener de response (último capturado)
    ↓ falhou (nenhuma response CSV capturada)
Loga erro e retorna 0 trades para a data
```

## Mapeamento de colunas

| Coluna no CSV da B3 | Coluna em NegociosBrutos |
|---|---|
| `Instrumento financeiro` | `cdInstrumento` |
| `Emissor` | `cdEmissor` |
| `Código IF` | `cdTicker` |
| `Quantidade negociada` | `vrQuantidade` |
| `Preço negócio` | `vrPU` |
| `Volume financeiro (R$)` | `vrVolume` |
| `Taxa negócio` | `vrTaxaNegocio` |
| `Horário negócio` | `dtHorarioNegocio` |
| `Data negócio` | `dtNegocio` |
| `Cód. identificador do negócio` | `cdIdentificadorNegocio` |
| `Código ISIN` | `cdISIN` |
| `Data liquidação` | `dtLiquidacao` |
| `Situação negócio` | `cdSituacao` |
| `Origem negócio` | *(ignorada — sempre "Pre-registro - Voice")* |

## Tratamento de dados

- **Números BR** (`1.234.567,89`): remove pontos, troca vírgula por ponto antes de converter para `float`. `vrQuantidade` é convertido para `int`.
- **Datas** (`DD/MM/YYYY`): convertidas para `YYYY-MM-DD` via `_NormalizeDate()`.
- **Horário** (`HH:MM`): normalizado para `HH:MM:SS` (acrescenta `:00` se necessário).
- **`vrTaxaNegocio`**: nullable — valores `""`, `"-"`, `"N/A"`, `"0"` viram `NULL` no banco.
- **`cdISIN`**: nullable — campo ausente ou vazio vira `NULL`.
- **Filtro por instrumento**: só passam linhas onde `cdInstrumento` está na lista `instrumentosAceitos` do `config.toml` (ex: `["DEB", "CRI", "CRA"]`).

## UPSERT e idempotência

O script usa `ON CONFLICT(cdIdentificadorNegocio)` para garantir idempotência:

```sql
INSERT INTO NegociosBrutos (...) VALUES (...)
ON CONFLICT(cdIdentificadorNegocio) DO UPDATE SET
    vrTaxaNegocio = excluded.vrTaxaNegocio,
    cdSituacao    = excluded.cdSituacao,
    dtAtualizacao   = CURRENT_TIMESTAMP
```

Apenas `vrTaxaNegocio` e `cdSituacao` são atualizados em conflito — campos que podem mudar após o pregão (taxa calculada posteriormente, situação cancelada/confirmada). Os demais campos permanecem como na primeira inserção.

A função interna `_UpsertRows` retorna `(inseridos, atualizados)`; a contagem de `cancelados` vem da função separada `_SoftCancelMissing` (lógica de soft-cancel descrita abaixo).

## Soft-cancel de trades desaparecidos

Quando o script é re-executado para uma data que já teve dados carregados (ex: correção de boletim retroativo), pode acontecer de um trade que existia em `NegociosBrutos` não aparecer mais no novo boletim baixado. Isso indica que o negócio foi cancelado no sistema da B3.

**Lógica implementada, por data processada:**

1. Após o UPSERT normal, o script consulta os `cdIdentificadorNegocio` que existem em `NegociosBrutos` para aquela `dtNegocio` mas que **não estavam no lote recém processado**.
2. Esses registros recebem `cdSituacao = 'Cancelado'` em `NegociosBrutos` (soft delete — o registro permanece no banco para auditoria).
3. As linhas correspondentes em `NegociosProcessados` são **deletadas fisicamente** (hard delete), pois não devem mais aparecer em nenhum relatório.
4. O script loga um `WARNING` listando as datas onde houve cancelamentos, com o total de trades afetados. Essas datas precisam ser reprocessadas pelos scripts de cálculo e filtro downstream.

```python
# Pseudocódigo da detecção de soft-cancel
ids_no_boletim = {row["cdIdentificadorNegocio"] for row in lote}
ids_no_banco   = {r[0] for r in conn.execute(
    "SELECT cdIdentificadorNegocio FROM NegociosBrutos WHERE dtNegocio = ?", (data,)
)}
cancelados_ids = ids_no_banco - ids_no_boletim

if cancelados_ids:
    conn.execute(
        "UPDATE NegociosBrutos SET cdSituacao='Cancelado' WHERE cdIdentificadorNegocio IN (...)"
    )
    conn.execute(
        "DELETE FROM NegociosProcessados WHERE cdIdentificadorNegocio IN (...)"
    )
    log.warning("Datas com cancelamentos — reprocessar: %s", data)
```

**Decisão (31/05/2026) — soft-cancel em NegociosBrutos, hard delete em NegociosProcessados:** manter o registro cancelado em `NegociosBrutos` preserva o histórico de auditoria. Já em `NegociosProcessados` o trade cancelado não tem sentido existir (foi dado como processado mas o negócio não ocorreu), então o hard delete é mais limpo e evita confusão nos relatórios.

## Logs e email

- Logs em: `data/logs/scrape_b3_boletim/{YYYY-MM-DD_HHMMSS}.log` — um arquivo por rodagem, nunca sobrescreve.
- Nível `INFO` no console, `DEBUG` no arquivo (inclui detalhes de cada seletor tentado, requests de rede capturadas, etc.).
- Snapshots de debug em: `data/debug/` — HTML + PNG por passo por data.
- Email ao final via [[10 - Scripts/libs#lib/email_outlook.py|email_outlook]] — assunto `[OK] scrape_b3_boletim` ou `[ERROR] scrape_b3_boletim`. Destinatário: `OUTLOOK_TO` no `.env`, default `antoniopx12@gmail.com`. O email reporta sempre os três contadores: `inseridos`, `atualizados`, `cancelados` (mesmo que algum seja zero).

## Dependências

| Módulo | Uso |
|---|---|
| `lib/db.py` | `get_db()` — abre banco e garante schema |
| `lib/config.py` | `cfg["scrape"]["b3"]` — URL base, instrumentos aceitos |
| `lib/logger.py` | `get_logger("scrape_b3_boletim")` — log em arquivo + console |
| `lib/email_outlook.py` | `send_completion_email()` — notificação ao fim |
| `playwright` | Automação de browser (Chromium) |

## Decisões de implementação

**Decisão (30/05/2026) — iframe direto em vez da página principal da B3:** navegar direto para `arquivos.b3.com.br/bdi/tabelas` é mais estável porque elimina dependência do carregamento do outer frame da B3, que tem mais scripts de terceiros e anti-bot.

**Decisão (30/05/2026) — POST de range antes do loop:** para economizar tempo em backfills longos, o script tenta um único POST com `Date != FinalDate`. Se a API não suportar o range, cai para iteração. Cada data no loop usa um contexto Playwright isolado para evitar state leak entre datas.

**Decisão (30/05/2026) — fallback em cascata para download:** a UI da B3 muda com frequência. A cascata (click → POST direto → intercepção de response) garante que pelo menos um método funcione mesmo que o seletor do botão mude. O body real do POST foi descoberto por inspeção de rede em 30/05/2026: `{"Name":"Trade","Date":"...","FinalDate":"...","ClientId":"","Filters":{}}`.

**Decisão (30/05/2026) — CSV processado em memória:** o conteúdo CSV nunca é escrito em disco (exceto snapshots de debug). Isso evita arquivos temporários orphaned se o processo morrer e simplifica a lógica de limpeza.

**Decisão (06/06/2026) — POST body capturado sempre reflete D-1:** durante a automação Playwright, o body do POST que é interceptado (via listener de requests) corresponde ao carregamento inicial da página — que usa a data de D-1. Quando o seletor de data é alterado via JS, a página carrega novos dados mas o body capturado não é atualizado. Fix implementado: ao reusar o `export_post_body` capturado, o script sempre sobrescreve `Date` e `FinalDate` com a data alvo correta antes de fazer o POST:

```python
corrected = {**export_post_body, "Date": date_str, "FinalDate": date_str}
```

Sem esse fix, todos os boletins de datas diferentes de D-1 retornavam os trades de D-1.

**Decisão (29/06/2026) — F12: funções convertidas para PascalCase:** este script foi escrito antes da convenção de nomes do projeto, então tinha funções em snake_case (`parse_args`, `main_async`, `_parse_csv`, `_upsert_rows`, `_normalize_date`, etc.). Na padronização PT-BR todas as 15 funções foram renomeadas para PascalCase (`_ParseArgs`, `_MainAsync`, `_ParseCsv`, `_UpsertRows`, `_NormalizeDate`, `_SoftCancelMissing`, `_ScrapeDate`, `Main`, ...), alinhando com os demais scripts. Apenas nomes — sem mudança de comportamento. Ver [[../99 - Mapa de Renomeacao PT-BR]].
