# Fonte: FI Analytics (Planilha)

> Ver também: [[../04 - Banco de Dados]] | [[../06 - Calculadoras/FI Analytics API]] | [[../08 - Match de Referencia]]

Script responsável: `code/scripts/scrape_fianalytics_planilha.py`

---

## O que é

O FI Analytics é uma plataforma de dados de renda fixa brasileira. Além da API de calculadoras (ver [[../06 - Calculadoras/FI Analytics API]]), a plataforma disponibiliza uma planilha consolidada com informações estáticas de todos os ativos de crédito privado: indexador, duration, vencimento, emissor, etc. Essa planilha é a fonte principal para popular a tabela `InfoAtivos`.

---

## URLs

| Finalidade | URL |
|---|---|
| Login (signin) | `https://fi-analytics.com.br/signin` |
| (o login cai na) lista de debêntures | `https://fi-analytics.com.br/analytics-hub/debentures/list` |

> ⚠️ **Layout novo (jul/2026):** NÃO há mais URL de download `?type=deb`/`cri_cra`. O download é por **botão "Exportar"** na própria lista, e o formato virou **CSV** (não xlsx). Ver [[../10 - Scripts/scrape_fianalytics_planilha]].

Credenciais lidas dos segredos `fianalyticsUser` / `fianalyticsPass` (via `[env]`). Ver [[../99 - Credenciais e Links]].

---

## Fluxo Playwright (layout novo)

1. Navega até `signin`, preenche email/senha (`input[name="email"]`/`[name="password"]`) e clica **"Entrar"**.
2. O login cai na **lista de debêntures**.
3. Clica no botão **"Exportar"** (`get_by_role("button", name="Exportar")`) → baixa o CSV de debêntures.
4. Para CRI/CRA: clica no item de menu **"Lista"** (há dois; o de CRI/CRA é o **último/`.last`**, ~640; o 1º é debêntures ~1.5k) → clica **"Exportar"** de novo.
5. Parseia os CSVs e faz UPSERT em `InfoAtivos`.

**Seletores por texto/role, nunca por classe CSS** (o Tailwind com hash muda a cada build e quebrou antes). O script sai com **exit 1 se nenhuma planilha gravar tickers** (mata a falha silenciosa). `--headless` (padrão) / `--no-headless` para debug.

---

## O que é carregado em `InfoAtivos`

O CSV (UTF-8 com BOM, separador `;`, decimal vírgula) traz uma linha por ativo. Colunas mapeadas:

| Campo no CSV | Coluna em `InfoAtivos` | Observação |
|---|---|---|
| `Ticker` | `cdTicker` (PK) | |
| — | `cdInstrumento` | Inferido: planilha `deb` → `'DEB'`; `cri_cra` → ticker começa com `CRA` → `'CRA'`, senão `'CRI'` |
| `Emissor` | `cdEmissor` | (era `issuer` no xlsx antigo) |
| `Vencimento` | `dtVencimento` | |
| `Duration` | `vrDuration` | Já em anos (não dividir por 252) |
| `Indexador` | `cdIndexador` | Normalizado para `CDI+` / `%CDI` / `IPCA` / `PREFIXADO` |
| `Taxa Emissão (%)` | `vrTaxaEmissao` | Taxa de emissão em % a.a. |

Parser: `AnalisarCsv`; floats via `AnalisarFloat` (tolera decimal-vírgula BR). Campos **não mapeados**: `Preço`, `% PU Par`, `Taxa FIA (%)`, `Prêmio de Risco (%)`, DAP+, etc.

---

## Comportamento do UPSERT

O UPSERT usa `COALESCE` nas colunas compartilhadas — valores existentes não são apagados se o novo for NULL:

```sql
vrDuration    = COALESCE(excluded.vrDuration,    vrDuration),
cdIndexador   = COALESCE(excluded.cdIndexador,   cdIndexador),
cdReferencia         = COALESCE(excluded.cdReferencia,         cdReferencia),
vrTaxaEmissao = COALESCE(excluded.vrTaxaEmissao, vrTaxaEmissao),
```

`cdReferencia` é preenchido pelos scrapers Anbima ou pelo `match_referencias.py` — o FI Analytics nunca tem esse dado, então passa `NULL` e o COALESCE preserva o que já existe.

`cdFonteReferencia` é setado como `'FiAnalytics'` apenas se `cdReferencia` não for NULL (atualmente nunca ocorre, pois o FI Analytics não fornece referência). Reservado para uso futuro.

---

## CLI

```powershell
# Padrão (headless)
python scripts/scrape_fianalytics_planilha.py

# Com browser visível (debug)
python scripts/scrape_fianalytics_planilha.py --no-headless
```

Não há flags de data — a planilha sempre traz o estado atual de todos os ativos.

---

## Email ao terminar

Envia email via Outlook ao concluir (sucesso ou erro). Ver `code/lib/email_outlook.py`.
