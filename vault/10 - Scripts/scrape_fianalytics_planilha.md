# Script: scrape_fianalytics_planilha.py

> Ver também: [[../04 - Banco de Dados]] | [[../05 - Fontes/FI Analytics]] | [[../06 - Calculadoras/FI Analytics API]]

**Arquivo:** `code/scripts/scrape_fianalytics_planilha.py`

---

## O que faz

Faz login no FI Analytics via Playwright, baixa as planilhas Excel de debêntures e de CRI/CRA e faz UPSERT em `InfoAtivos`. É a fonte principal para `vrTaxaEmissao`, `cdEmissor`, `dtVencimento`, `vrDuration` e `cdIndexador`.

Não tem `--date` — a planilha sempre reflete o estado atual de todos os ativos cadastrados na plataforma.

---

## Planilhas baixadas

| Tipo | URL | `cdInstrumento` padrão |
|---|---|---|
| Debêntures | `https://fi-analytics.com.br/analytics-hub/hub?type=deb` | `'DEB'` |
| CRI/CRA | `https://fi-analytics.com.br/analytics-hub/hub?type=cri_cra` | inferido do ticker |

Para CRI/CRA: ticker começa com `'CRA'` → `'CRA'`; senão → `'CRI'`.

---

## Colunas mapeadas

| Campo na planilha | Variável Python | Coluna em `InfoAtivos` |
|---|---|---|
| `Ticker` | `cdTicker` | `cdTicker` (PK) |
| `issuer` | `cdEmissor` | `cdEmissor` |
| `Vencimento` | `dtVencimento` | `dtVencimento` |
| `Duration` | `vrDuration` | `vrDuration` (já em anos) |
| `Indexador` | `cdIndexador` | `cdIndexador` (normalizado) |
| `Taxa Emissão (%)` | `vrTaxaEmissao` | `vrTaxaEmissao` |

Campos disponíveis mas **não mapeados**: `Preço`, `% Pu Par`, `Taxa FIA (%)`, `Prêmio de Risco (%)`.

---

## Fluxo Playwright

1. Navegar até `https://fi-analytics.com.br/signin`
2. Preencher credenciais (`FIANALYTICS_USER`, `FIANALYTICS_PASS` do `.env`)
3. Aguardar login (redirecionamento)
4. Para cada planilha: navegar até URL → acionar download → aguardar arquivo
5. Processar XLSX com `openpyxl` (salvo em temp file pois `BytesIO` é instável)
6. UPSERT em `InfoAtivos`

---

## Localização do cabeçalho no XLSX

O XLSX pode ter linhas de intro antes do header real. O parser procura a primeira linha que contenha a string `'Ticker'` e usa essa como cabeçalho. Colunas são acessadas por nome (via `_colIdx(name)`), não por índice — tolerante a colunas extras ou reordenação.

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
