# Script: scrape_anbima_debentures.py

> Ver também: [[../04 - Banco de Dados]] | [[../05 - Fontes/Anbima]]

**Arquivo:** `code/scripts/scrape_anbima_debentures.py`

---

## O que faz

Baixa o XLS de taxas indicativas de debêntures publicado pela Anbima e popula duas tabelas:

- `AnbimaIndicativos` — **taxa indicativa** por ticker/data de referência (coluna "Taxa Indicativa", índice 6 — ver aviso abaixo)
- `InfoAtivos` — campos estáticos do ativo: emissor, vencimento, duration, indexador, referência (`cdReferencia`)

Download feito diretamente via httpx — sem Playwright. A URL do arquivo XLS segue padrão previsível, basta montar com a data desejada.

> **Linha gravada mesmo sem taxa:** quando a Anbima publica o ativo sem taxa indicativa (célula `--`/`N/D`/vazia → `_ParseFloat` retorna `None`), a linha **ainda é inserida** em `AnbimaIndicativos` com `vrTaxaAnbima = NULL` (a lista `infoRows` é separada, então `InfoAtivos` recebe as características de qualquer jeito). Isso é proposital. Downstream esses NULLs são ignorados (curvas filtram `WHERE vrSpreadAnbima IS NOT NULL`, e spread é sempre NULL quando a taxa é NULL). São ~90/dia, quase todos DEB CDI+/%CDI ilíquidos.

---

## URL de download

```
https://www.anbima.com.br/informacoes/merc-sec-debentures/arqs/d{AA}{mmm_pt}{DD}.xls
```

Onde `mmm_pt` é a abreviação do mês em português minúsculo (`jan`, `fev`, `mar`, ...). O valor de `debXlsBaseUrl` vem de `config.toml` em `[scrape.anbima]`.

Exemplo para 29/05/2026: `d26mai29.xls` — mas a URL já é montada por `_BuildUrl(d)`.

**HTTP 404 = dia sem publicação** (final de semana, feriado). Não é tratado como erro — o script loga `INFO` e segue para a próxima data.

**Janela de arquivamento (verificado 25/06/2026): ~4 meses / ~84 pregões, móvel.** Os XLS ficam no ar bem além de "alguns dias" — em 25/06/2026 o mais antigo disponível era **23/02/2026** (nada antes; ~1 ano atrás = 404). É rolante: anda pra frente a cada dia, então histórico mais antigo some — baixar antes de expirar. Difere do CRI/CRA, que só tem **~5 pregões** (limitação do portal — ver [[scrape_anbima_cri_cra]]).

---

## Abas do XLS processadas

| Aba | Indexador coberto |
|---|---|
| `DI_PERCENTUAL` | `%CDI` |
| `DI_SPREAD` | `CDI+` |
| `IPCA_SPREAD` | `IPCA` |
| `PREFIXADO` | `PREFIXADO` |

Abas **ignoradas**: `IGP-M` e `VENCIDOS_ANTECIPADAMENTE`.

---

## Layout do XLS

- **Linha 7**: header principal
- **Linha 8**: subheader (ignorado)
- **Linhas 9+**: dados
- **Rodapé**: linhas cujo ticker começa com `"Obs"`, contém espaço, ou é `"1.0"` são puladas via `continue` (não interrompem o loop)

---

## Colunas lidas (por índice)

| Índice | Campo Anbima | Coluna destino |
|---|---|---|
| 0 | Código | `cdTicker` |
| 1 | Nome Emissor | `cdEmissor` |
| 2 | Repac./Venc. | `dtVencimento` |
| 3 | Índice/Correção | `cdIndexador` (normalizado) |
| 5 | Taxa de Venda | ⚠ **NÃO usar** (era o bug) |
| 6 | **Taxa Indicativa** | `vrTaxaAnbima` (`_COL_TAXA_INDICATIVA = 6`) |
| 12 | Duration | `vrDuration` (÷ 252) |
| 14 | Referência NTN-B | usado para derivar `cdReferencia` |

> ⚠ **Coluna correta = índice 6 (Taxa Indicativa), NÃO índice 5 (Taxa de Venda).** Ver a decisão de 25/06/2026 no fim desta nota — havia um bug em que datas antigas foram gravadas com a col 5.

---

## Normalização de `cdIndexador`

| Valor bruto | `cdIndexador` salvo |
|---|---|
| `"DI +"` / `"DI+"` | `CDI+` |
| `"IPCA"` | `IPCA` |
| `"% DI/CDI"` | `%CDI` |
| Começa com `"PR"` | `PREFIXADO` |
| Outros | `NULL` |

---

## Lógica de `cdReferencia`

| `cdIndexador` | `cdReferencia` |
|---|---|
| `CDI+` ou `%CDI` | `FUNDING` |
| `IPCA` | `NTN-B {YY}` derivado do campo "Referência NTN-B" (ex: `"15/05/2035"` → `"NTN-B 35"`) |
| `PREFIXADO` | `NULL` |

---

## Duration: dias → anos

Os arquivos XLS da Anbima trazem duration em **dias corridos**. O script divide por 252 para converter para anos antes de gravar em `vrDuration`.

```python
vrDuration = round(rawDuration / 252, 6) if rawDuration is not None else None
```

---

## UPSERT em `InfoAtivos`

O UPSERT preserva valores existentes quando o valor novo seria NULL:

```python
vrDuration   = COALESCE(excluded.vrDuration,  vrDuration),
cdIndexador  = COALESCE(excluded.cdIndexador, cdIndexador),
cdReferencia        = COALESCE(excluded.cdReferencia,       cdReferencia),
```

Isso garante que dados preenchidos por outra fonte (ex: FI Analytics) não sejam apagados.

A coluna `dtAtualizacaoDuration` é atualizada **apenas quando `vrDuration` não é NULL**:

```python
dtAtualizacaoDuration = CASE WHEN excluded.vrDuration IS NOT NULL
                        THEN excluded.dtAtualizacaoDuration
                        ELSE dtAtualizacaoDuration END
```

---

## CLI

```powershell
# Data única
python scripts/scrape_anbima_debentures.py --date 2026-05-29

# Janela histórica
python scripts/scrape_anbima_debentures.py --start 2026-05-01 --end 2026-05-29
```

`--date` e `--start/--end` são mutuamente exclusivos. `--end` é obrigatório quando `--start` é usado.

---

## Email ao terminar

Envia email via Outlook com assunto `[OK] scrape_anbima_debentures` ou `[ERROR] scrape_anbima_debentures`. O corpo lista o número de linhas inseridas/atualizadas em `AnbimaIndicativos` e `InfoAtivos` por data.

---

## Convenção de código

Segue a convenção Python do projeto: variáveis `camelCase` (`dtRef`, `anbimaRows`, `vrDuration`), funções `PascalCase` (`_BuildUrl`, `_ParseSheet`, `_ProcessDate`, `_ParseArgs`, `Main`).

**Decisao (01/06/2026):** download direto sem Playwright para debêntures, pois a Anbima disponibiliza o XLS via URL previsível. CRI/CRA exige Playwright porque o link de download é um blob URL gerado dinamicamente. Ver [[scrape_anbima_cri_cra]].

**Bug + fix (25/06/2026) — coluna errada (Taxa de Venda) nas datas antigas:** descobriu-se que `AnbimaIndicativos` para os pregões de **08–19/06/2026** tinha sido gravado com a **col 5 (Taxa de Venda)** em vez da **col 6 (Taxa Indicativa)**. Confirmado comparando o banco com o XLS de 09/06 (batia 100% com a col 5). As datas 22–24/06, raspadas com o código já corrigido (`_COL_TAXA_INDICATIVA = 6`), estavam certas. **Fix:** re-raspei 08–19/06 (o UPSERT sobrescreve) + `calc_spread_anbima --force 08-19` (recalcula `vrSpreadAnbima` a partir da taxa correta) + regerei o relatório. **Não apaguei a tabela** (apagaria CRI/CRA antigos, que já usam a coluna certa e o portal não devolve datas antigas). Backup da tabela salvo antes, por garantia.

**Backfill (25/06/2026):** com a janela de ~4 meses confirmada, raspei debêntures de **23/02→24/06/2026** (84 pregões, ~107k linhas) pra capturar o máximo de histórico de taxa indicativa enquanto está no ar. As datas novas (pré-08/06) ficam com `vrSpreadAnbima = NULL` até puxarmos as curvas MtM (NTN-B/DI) correspondentes. Ver [[../09 - Progresso]].
