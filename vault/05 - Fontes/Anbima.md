# Fonte: Anbima (Taxas Indicativas)

> Ver também: [[../04 - Banco de Dados]] | [[../01 - O Que Faz]]

Scripts responsáveis:
- `code/scripts/scrape_anbima_debentures.py` → [[../10 - Scripts/scrape_anbima_debentures]]
- `code/scripts/scrape_anbima_cri_cra.py` → [[../10 - Scripts/scrape_anbima_cri_cra]]

---

## O que é

A Anbima publica diariamente taxas indicativas para debêntures, CRIs e CRAs. Essas taxas representam um consenso de mercado sobre os níveis de taxa para cada ativo — não são taxas de negócios executados, mas referências informativas. No relatório, elas aparecem como coluna adicional para comparação com as taxas efetivamente negociadas.

---

## URLs

| Instrumento | Fonte real usada pelo scraper |
|---|---|
| Debêntures | XLS direto: `https://www.anbima.com.br/informacoes/merc-sec-debentures/arqs/d{AA}{mmm_pt}{DD}.xls` (httpx) |
| CRI e CRA | Portal `https://data.anbima.com.br/busca/certificado-de-recebiveis?view=precos` (Playwright; CSV via blob) |
| NTN-B (curva ref.) | XLS direto: `https://www.anbima.com.br/informacoes/merc-sec/arqs/m{AA}{mmm_pt}{DD}.xls`, aba NTN-B → [[../10 - Scripts/scrape_anbima_ntnb]] |

São scripts separados pois as fontes têm estruturas diferentes.

### Janela de arquivamento (verificado 25/06/2026) — DIFERE por fonte

| Fonte | Até onde volta | Como |
|---|---|---|
| **Debêntures** | **~4 meses** (mais antigo 23/02/2026; rolante) | XLS arquivado por data — dá pra backfillar |
| **NTN-B** | provável ~4 meses (mesmo padrão de URL) | a confirmar |
| **CRI/CRA** | **só ~5 pregões** | portal entrega 1 CSV com os últimos ~5 dias úteis; **não dá pra backfillar** |

Implicação: debênture/NTN-B antigo é recuperável enquanto estiver na janela; CRI/CRA antigo só existe se foi capturado ao vivo.

---

## O que é carregado em `AnbimaIndicativos`

Cada linha representa a taxa indicativa de um ativo para uma data de referência:

| Coluna | Descrição |
|---|---|
| `cdTicker` | Código do ativo |
| `dtReferencia` | Data da publicação Anbima (`YYYY-MM-DD`) |
| `vrTaxaAnbima` | Taxa indicativa em % a.a. |
| `vrSpreadAnbima` | Spread indicativo em bps (quando disponível) |

A chave primária é `(cdTicker, dtReferencia)` — rodadas repetidas sobrescrevem via `INSERT OR REPLACE`.

---

## CLI

Ambos os scripts aceitam as mesmas flags:

```powershell
# Data única
python scripts/scrape_anbima_debentures.py --date 2026-05-27
python scripts/scrape_anbima_cri_cra.py    --date 2026-05-27

# Janela histórica
python scripts/scrape_anbima_debentures.py --start 2026-05-01 --end 2026-05-27
python scripts/scrape_anbima_cri_cra.py    --start 2026-05-01 --end 2026-05-27
```

A `--date` corresponde à data de publicação na Anbima (não a `dtLiquidacao`).

---

## Uso no relatório

As taxas Anbima não entram no cálculo de spread — apenas são exibidas no HTML como referência informativa ao lado das taxas negociadas. O usuário pode comparar a taxa do trade com o indicativo Anbima do mesmo dia para avaliar se o negócio foi feito acima ou abaixo do "mercado".

---

## Email ao terminar

Cada script envia email via Outlook ao concluir. Ver `code/lib/email_outlook.py`.

---

## Implementacao tecnica

### URLs reais usadas

Ambas as URLs vêm de `config.toml` na seção `[scrape.anbima]`:

| Chave config | Valor | Uso |
|---|---|---|
| `debXlsBaseUrl` | `https://www.anbima.com.br/informacoes/merc-sec-debentures/arqs` | Base para montar a URL do XLS de debentures por data |
| `cricraUrl` | URL da pagina de CRI/CRA | Pagina onde o Playwright navega para baixar o CSV |

Para debentures, a URL completa é `{debXlsBaseUrl}/d{AA}{mmm_pt}{DD}.xls` — montada por `_BuildUrl(d)`.

### Campos extraidos de cada arquivo

**Debentures (XLS — abas DI_PERCENTUAL, DI_SPREAD, IPCA_SPREAD, PREFIXADO):**

| Col | Campo Anbima | Destino |
|---|---|---|
| 0 | Codigo | `cdTicker` |
| 1 | Nome Emissor | `cdEmissor` |
| 2 | Repac./Venc. | `dtVencimento` |
| 3 | Indice/Correcao | `cdIndexador` |
| **6** | **Taxa Indicativa** | `vrTaxaAnbima` (col 5 = Taxa de Venda — **NÃO usar**; ver bug 25/06) |
| 12 | Duration (dias) | `vrDuration` (÷ 252) |
| 14 | Referencia NTN-B | base para `cdReferencia` |

**CRI/CRA (CSV):**

| Campo CSV | Destino |
|---|---|
| `Código` | `cdTicker` |
| `Risco de Crédito` | `cdEmissor` (originadora) |
| `Vencimento` | `dtVencimento` |
| `Índice / Correção` | `cdIndexador` |
| `Taxa Indicativa` | `vrTaxaAnbima` |
| `Duration` (dias) | `vrDuration` (÷ 252) |
| `Referência NTNB` | base para `cdReferencia` |

### Como cdReferencia e cdIndexador sao derivados

| Valor bruto | `cdIndexador` | `cdReferencia` |
|---|---|---|
| `"DI +"` / `"DI+"` | `CDI+` | `FUNDING` |
| Contem `"IPCA"` | `IPCA` | `NTN-B {YY}` (do campo Referencia NTN-B) |
| `"% DI/CDI"` | `%CDI` | `FUNDING` |
| Comeca com `"PR"` | `PREFIXADO` | `NULL` |
| Outros | `NULL` | `NULL` |

Exemplo de conversao de Referencia NTN-B: `"15/05/2035"` → `"NTN-B 35"` (ultimos dois digitos do ano).

### Mecanismo de download por instrumento

| Instrumento | Metodo | Motivo |
|---|---|---|
| Debentures | httpx direto | URL previsivel — arquivo XLS acessivel sem autenticacao ou JS |
| CRI/CRA | Playwright (Chromium) | Download e blob URL gerada dinamicamente por JS — nao e possivel URL direta |

**Decisao (01/06/2026):** `cdReferencia` populado pelos scrapers Anbima no momento do scraping, nao apenas pelo `match_referencias.py`. O match_referencias so atua em ativos sem `cdReferencia`. Isso e mais eficiente e mantém a informacao mais atualizada, pois a Anbima publica o campo "Referencia NTN-B" diretamente no arquivo.
