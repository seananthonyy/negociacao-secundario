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
| Download da planilha | `https://fi-analytics.com.br/analytics-hub/hub?type=deb` |

As credenciais de acesso são lidas das variáveis de ambiente `FIANALYTICS_USER` e `FIANALYTICS_PASS`. Ver [[../99 - Credenciais e Links]].

---

## Fluxo Playwright

O download requer autenticação prévia. O fluxo é:

1. Navegar até a URL de signin.
2. Preencher email e senha com as credenciais do `.env`.
3. Aguardar o login ser concluído (redirecionamento ou elemento pós-login).
4. Navegar até a URL de download da planilha.
5. Acionar o download (botão ou link direto).
6. Aguardar o arquivo aparecer na pasta de downloads.
7. Processar a planilha e fazer UPSERT em `InfoAtivos`.

O script aceita `--headless` / `--no-headless` para controlar se o browser abre de forma visível (útil para debug de login). O padrão em `config.toml` é `headless = true`.

---

## O que é carregado em `InfoAtivos`

A planilha traz uma linha por ativo. Colunas mapeadas:

| Campo na planilha | Coluna em `InfoAtivos` | Observação |
|---|---|---|
| `Ticker` | `cdTicker` (PK) | |
| — | `cdInstrumento` | Inferido: planilha `deb` → `'DEB'`; `cri_cra` → ticker começa com `CRA` → `'CRA'`, senão `'CRI'` |
| `issuer` | `cdEmissor` | |
| `Vencimento` | `dtVencimento` | |
| `Duration` | `vrDuration` | Já em anos (não dividir por 252) |
| `Indexador` | `cdIndexador` | Normalizado para `CDI+` / `%CDI` / `IPCA` / `PREFIXADO` |
| `Taxa Emissão (%)` | `vrTaxaEmissao` | Taxa de emissão em % a.a. |

Campos da planilha **não mapeados** atualmente: `Preço`, `% Pu Par`, `Taxa FIA (%)`, `Prêmio de Risco (%)`.

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
