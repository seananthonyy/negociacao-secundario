# Script: scrape_fianalytics_planilha.py

> Ver também: [[../04 - Banco de Dados]] | [[../05 - Fontes/FI Analytics]] | [[../06 - Calculadoras/FI Analytics API]]

**Arquivo:** `code/scripts/scrape_fianalytics_planilha.py`

---

## O que faz

Faz login no FI Analytics via Playwright, baixa os **CSVs** de debêntures e de CRI/CRA e faz UPSERT em `InfoAtivos`. É a fonte principal para `vrTaxaEmissao`, `cdEmissor`, `dtVencimento`, `vrDuration` e `cdIndexador`.

Não tem `--date` — a planilha sempre reflete o estado atual de todos os ativos cadastrados na plataforma.

> ⚠️ **Layout novo (jul/2026) — consertado 19/07:** o site refez o front. Não há mais URL `?type=deb`. O download é por botão **"Exportar"** na lista; CRI/CRA via menu **"Lista"**; formato **xlsx→CSV**; coluna `issuer`→`Emissor`. Seletores por texto/role, exit 1 se gravar 0. Base do fix: tutorial do usuário.

---

## Como baixa (não é mais por URL)

| Planilha | Como | `cdInstrumento` padrão |
|---|---|---|
| Debêntures | login cai na lista de deb → botão **"Exportar"** | `'DEB'` |
| CRI/CRA | menu **"Lista"** (o **último** dos 2, ~640) → botão **"Exportar"** | inferido do ticker |

Para CRI/CRA: ticker começa com `'CRA'` → `'CRA'`; senão → `'CRI'`.

---

## Colunas mapeadas

| Campo na planilha | Variável Python | Coluna em `InfoAtivos` |
|---|---|---|
| `Ticker` | `cdTicker` | `cdTicker` (PK) |
| `Emissor` | `cdEmissor` | `cdEmissor` (era `issuer` no xlsx antigo) |
| `Vencimento` | `dtVencimento` | `dtVencimento` |
| `Duration` | `vrDuration` | `vrDuration` (já em anos) |
| `Indexador` | `cdIndexador` | `cdIndexador` (normalizado) |
| `Taxa Emissão (%)` | `vrTaxaEmissao` | `vrTaxaEmissao` |

Campos disponíveis mas **não mapeados**: `Preço`, `% Pu Par`, `Taxa FIA (%)`, `Prêmio de Risco (%)`.

---

## Fluxo Playwright (layout novo)

1. Navega até `signin`, preenche email/senha (`input[name="email"]`/`[name="password"]`), clica **"Entrar"** (`Autenticar`)
2. Login cai na **lista de debêntures** → clica **"Exportar"** (`BaixarViaExportar`) → CSV deb
3. Clica no menu **"Lista"** (`IrParaCriCra`, `.last`) → **"Exportar"** → CSV CRI/CRA
4. `AnalisarCsv` parseia (UTF-8 BOM, sep `;`, decimal vírgula) e faz UPSERT em `InfoAtivos`

Seletores por **texto/role** (`get_by_role("button", name="Exportar")`, `button:has-text("Lista").last`), nunca classe CSS.

---

## Parsing do CSV

CSV UTF-8 com BOM, separador `;`, decimal vírgula (`AnalisarCsv` + `csv.reader`). O parser acha a primeira linha com `'Ticker'` como cabeçalho e acessa colunas por nome (`ColIdx`) — tolerante a colunas extras/reordenação. Floats por `AnalisarFloat` (converte `1.234,56` BR → `1234.56`).

---

## Comportamento do UPSERT

Usa `COALESCE` para não apagar valores existentes quando o novo é NULL:

```sql
vrDuration     = COALESCE(excluded.vrDuration,    vrDuration),
cdIndexador    = COALESCE(excluded.cdIndexador,   cdIndexador),
cdReferencia          = COALESCE(excluded.cdReferencia,         cdReferencia),
vrTaxaEmissao = COALESCE(excluded.vrTaxaEmissao, vrTaxaEmissao),
```

`dtAtualizacaoDuration` é atualizada apenas quando `vrDuration` não é NULL.

`cdReferencia` é sempre `NULL` neste script (FI Analytics não fornece referência de benchmark). `cdFonteReferencia` é setado como `'FiAnalytics'` somente se `cdReferencia` não for NULL — na prática nunca ocorre atualmente.

---

## CLI

```powershell
# Padrão (headless)
python scripts/scrape_fianalytics_planilha.py

# Com browser visível (debug de login)
python scripts/scrape_fianalytics_planilha.py --no-headless
```

---

## Email ao terminar

Assunto `[OK] scrape_fianalytics_planilha` ou `[ERROR] scrape_fianalytics_planilha`. Corpo lista o número de registros processados por tipo de planilha.
