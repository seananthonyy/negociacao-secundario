# Script: scrape_anbima_cri_cra.py

> Ver também: [[../04 - Banco de Dados]] | [[../05 - Fontes/Anbima]]

**Arquivo:** `code/scripts/scrape_anbima_cri_cra.py`

---

## O que faz

Baixa o CSV de taxas indicativas de CRIs e CRAs publicado pela Anbima via Playwright e popula duas tabelas:

- `AnbimaIndicativos` — taxa indicativa de venda por ticker/data de referência
- `InfoAtivos` — campos estáticos do ativo: emissor, vencimento, duration, indexador, referência (`cdReferencia`)

O download **requer Playwright** porque o link de download na página da Anbima é um blob URL gerado dinamicamente — não é possível construir uma URL direta previsível como nas debêntures. Ver [[scrape_anbima_debentures]] para a versão sem Playwright.

> **⚠ Janela de só ~5 pregões (verificado 25/06/2026).** O portal entrega **um único CSV que contém apenas os últimos ~5 dias úteis** (ex.: em 25/06 o CSV tinha 18, 19, 22, 23, 24/06). O date-picker NÃO busca histórico: setar uma data mais antiga devolve o mesmo CSV recente, e `_ParseCsv` filtra pela `Data Referência` → **0 linhas** para datas fora desses 5. **Consequência:** CRI/CRA antigo só existe se foi capturado ao vivo — **não dá pra backfillar** além de ~5 pregões. Difere das debêntures, que ficam arquivadas ~4 meses ([[scrape_anbima_debentures]]).

---

## Por que Playwright (e não httpx)

A página da Anbima para CRI/CRA gera o arquivo via JavaScript no momento do clique. O link é do tipo `blob:https://...`, que só existe dentro do contexto do browser. O script usa `page.expect_download()` para interceptar o arquivo conforme o Playwright dispara o download.

---

## Fluxo de execução

1. Abre o Chromium via `async_playwright`
2. Navega até `cricraUrl` (valor de `config.toml` em `[scrape.anbima]`)
3. Para cada data:
   - Preenche o date picker `input.anbima-ui-input__input` com `triple_click` + `fill` + `Enter`
   - Aguarda 2,5 segundos para a página atualizar
   - Clica no link CSV (`ul.anbima-ui-toolbar__menu-files a` com texto "CSV")
   - Intercepta o download com `expect_download(timeout=20_000)`
   - Lê o conteúdo em memória (`dl.read()`)
4. Para `--start/--end`: o browser permanece aberto entre datas — apenas a data é trocada. Evita overhead de abrir e fechar o browser para cada data.
5. Fecha o browser ao final

---

## Determinação de `cdInstrumento`

O tipo (CRI ou CRA) é inferido diretamente do ticker:

```python
cdInstrumento = 'CRA' if cdTicker.upper().startswith('CRA') else 'CRI'
```

---

## Colunas do CSV

O CSV é lido via `csv.DictReader` (delimitador detectado automaticamente, `;` ou `,`). Campos lidos por nome de coluna:

| Campo CSV | Coluna destino |
|---|---|
| `Código` / `Codigo` | `cdTicker` |
| `Risco de Crédito` / `Risco de Credito` | `cdEmissor` (empresa originadora, não a securitizadora) |
| `Vencimento` | `dtVencimento` |
| `Índice / Correção` / `Indice / Correcao` | `cdIndexador` (normalizado) |
| `Taxa Indicativa` | `vrTaxaAnbima` |
| `Duration` | `vrDuration` (÷ 252) |
| `Referência NTNB` / `Referencia NTNB` | usado para derivar `cdReferencia` |

O CSV pode vir em encodings diferentes. O parser tenta `utf-8-sig`, `utf-8`, `latin-1` e `cp1252` nessa ordem.

---

## Normalização de `cdIndexador` e `cdReferencia`

Lógica similar ao [[scrape_anbima_debentures]], com uma diferença: quando o indexador não é reconhecido, o fallback é `'PREFIXADO'` (não `NULL` como nas debêntures):

```python
cdIndexador = _NormalizeIndexador(rawIndexador) or 'PREFIXADO'
```

| `cdIndexador` | `cdReferencia` |
|---|---|
| `CDI+` ou `%CDI` | `FUNDING` |
| `IPCA` | `NTN-B {YY}` (do campo Referência NTNB, ex: `"15/05/2035"` → `"NTN-B 35"`) |
| `PREFIXADO` | `NULL` |

Duration vem em dias corridos no CSV. Divide por 252 para converter em anos.

---

## UPSERT em `InfoAtivos`

Mesma lógica do scraper de debêntures: `COALESCE` em `vrDuration`, `cdIndexador` e `cdReferencia` para não apagar dados de outra fonte. `dtAtualizacaoDuration` só é atualizada quando `vrDuration` não é NULL. Ver [[../04 - Banco de Dados]] para o schema completo.

---

## CLI

```powershell
# Data única (com janela do browser visível para debug)
python scripts/scrape_anbima_cri_cra.py --date 2026-05-29

# Janela historica, headless
python scripts/scrape_anbima_cri_cra.py --start 2026-05-01 --end 2026-05-29 --headless
```

| Flag | Padrao | Descricao |
|---|---|---|
| `--date` | — | Data unica de publicacao |
| `--start` / `--end` | — | Intervalo de datas |
| `--headless` | `False` | Roda Playwright sem janela visivel |

`--headless` é `False` por padrão para facilitar debug visual em caso de erro de UI.

---

## Email ao terminar

Envia email via Outlook com assunto `[OK] scrape_anbima_cri_cra` ou `[ERROR] scrape_anbima_cri_cra`. O corpo lista linhas inseridas por data.

---

## Convenção de codigo

Segue a convencao Python do projeto: variaveis `camelCase` (`dtStr`, `anbimaRows`, `vrDuration`), funcoes `PascalCase` (`_ParseCsv`, `_DownloadCsv`, `_SetDate`, `_SaveToDb`, `_ParseArgs`, `Main`). O entrypoint assincrono (`_MainAsync`) tambem e PascalCase.

**Decisao (01/06/2026):** browser permanece aberto entre datas no modo `--start/--end`. Alternativa de abrir/fechar por data foi descartada por ser mais lenta e aumentar risco de rate limiting.

**Decisao (06/06/2026) — coluna `Taxa Indicativa`, nao `Taxa Venda`:** o CSV da Anbima para CRI/CRA usa o nome de coluna `Taxa Indicativa` (com espaço inicial no arquivo — removido pelo `.strip()` aplicado nos fieldnames na leitura). O nome `Taxa Venda` era incorreto e causava `vrTaxaAnbima = NULL` para todos os tickers. Linha 181 do script corrigida para `row.get('Taxa Indicativa')`. Atenção: caso a Anbima mude o nome da coluna no futuro, o script retornará NULL silenciosamente — verificar periodicamente.

**Verificação (25/06/2026) — dados são date-specific, sem mislabeling:** apesar de o CSV vir com bytes idênticos entre execuções de datas diferentes, ele contém os últimos ~5 pregões e o filtro por `Data Referência` separa corretamente. Confirmado: taxas de CRI/CRA diferem entre 23 e 24/06 (310 diferentes vs 4 iguais), proporção igual à das debêntures — ou seja, cada data tem seu valor, não é o mesmo snapshot rotulado com datas diferentes.
