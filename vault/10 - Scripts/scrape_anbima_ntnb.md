# scrape_anbima_ntnb.py

> **Fase F13.** Baixa taxas indicativas de NTN-B do mercado secundário de títulos públicos da Anbima e popula `MtmAnbima`.

---

## O que faz

Para cada data solicitada:
1. Faz download direto do XLS via `httpx` (URL previsível, sem Playwright).
2. Abre a aba `NTN-B` do arquivo.
3. Lê as colunas C (`Data de Vencimento`) e F (`Tx. Indicativas`), a partir da linha 6 (headers mesclados nas linhas 4/5).
4. Normaliza o ticker: vencimento `"15/08/2032"` → `"NTN-B 32"`.
5. Faz UPSERT em `MtmAnbima`.
6. Envia email de conclusão via Outlook.

Se a URL retornar 404, o dia é tratado como não útil e o script segue para a próxima data (sem erro).

---

## Tabela destino

```sql
MtmAnbima (cdTicker, dtReferencia, vrTaxa, vrDuration)
```

- `cdTicker`: `"NTN-B 26"`, `"NTN-B 32"`, etc.
- `dtReferencia`: data passada no `--date` (data da consulta Anbima, não o vencimento)
- `vrTaxa`: `Tx. Indicativas` em % a.a. (ex: `6.7500`)
- `vrDuration`: duration em anos, obtida via **B3 Calculator API** (`/calcPU`). Ver seção abaixo.

---

## URL e padrão de data

```
https://www.anbima.com.br/informacoes/merc-sec/arqs/m{YY}{mmm_pt}{DD}.xls
```

Exemplo para 05/06/2026: `m26jun05.xls`

Mesmo dicionário de meses em português do `scrape_anbima_debentures.py`:
`{1:'jan', 2:'fev', 3:'mar', 4:'abr', 5:'mai', 6:'jun', 7:'jul', 8:'ago', 9:'set', 10:'out', 11:'nov', 12:'dez'}`

---

## Estrutura do XLS

| Linha | Conteúdo |
|---|---|
| 1–3 | Cabeçalho institucional Anbima |
| 4–5 | Headers mesclados das colunas |
| 6+ | Dados (uma NTN-B por linha) |

Colunas de interesse (índice 0-based):
- `2` → `Data de Vencimento` (texto `DD/MM/YYYY`)
- `5` → `Tx. Indicativas` (float, % a.a.)

---

## CLI

```powershell
python scripts/scrape_anbima_ntnb.py --date 2026-06-05
python scripts/scrape_anbima_ntnb.py --start 2026-06-01 --end 2026-06-05
```

---

## Uso no pipeline

Este script alimenta `MtmAnbima` com as NTN-Bs. O `calc_spread_anbima.py` (F15) consulta essa tabela para calcular `vrSpreadOverAnbima` dos trades com `cdIndexador = 'IPCA'`, usando `InfoAtivos.cdReferencia` como chave de lookup (ex: `cdReferencia = "NTN-B 32"`).

Rodar uma vez por dia útil, junto com os scrapers Anbima de debêntures/CRI/CRA.

---

## Obtenção de Duration — Cascata FI Analytics → B3 Calculator

**Paralelismo (02/07/2026):** as chamadas de duration (I/O-bound, 2 HTTP por NTN-B) rodam em `ThreadPoolExecutor` por data, com concorrência limitada por `--workers` (default **4**, conservador p/ não bloquear a API). **Skip:** tickers que já têm `vrDuration` na data não re-chamam a API (`--force` ignora o skip); o UPSERT usa `COALESCE`, então o skip passa `None` e preserva a duration existente. UPSERT sequencial após o pool. Retomada após falha é rápida.

Para cada NTN-B lida do XLS, o script busca a duration em cascata:

### Primário: FI Analytics (`/gov/govbondcalculator`)

Dois passos, usando a infra já existente em `lib/fianalytics_api.py`:

**Passo 1 — obter ISIN:**
```
POST /financialutil/gov/getgovbondisin
{"instrument_type": "NTN-B", "maturity_date": "15/08/2032"}
→ {"isin": "BRNTN..."}
```

**Passo 2 — calcular:**
```
POST /gov/govbondcalculator
{"isin": "BRNTN...", "date": "YYYY-MM-DD", "rate": <vrTaxa da Anbima>}
→ {"maculayDuration": 4.32, ...}
```

Retorna `maculayDuration` (em anos).

### Fallback: B3 Calculator (`/calcPU`)

Usado se FI Analytics falhar. Derivação do código CETIP a partir do vencimento:
```
CETIP = "760199" + YYYY + MM + DD
NTN-B 32 (15/08/2032) → "76019920320815"
NTN-B 27 (15/05/2027) → "76019920270515"
```

```
GET /calcPU/{CETIP}/{dtReferencia}/{vrTaxa}
→ {"duration": 4.31, ...}
```

Implementado como nova função `calc_pu_gov(cetip, dtRef, taxa)` em `lib/b3_calc_api.py`.

**NTN-Bs suportadas pela B3 Calc:**
B24, B26, B27, B28, B29, B30, B31, B32, B33, B35, B37, B40, B45, B50, B55, B60

### Se ambos falharem

`vrDuration = NULL`. Script loga WARNING mas não interrompe o processamento das demais NTN-Bs.

---

## Dependências

- `xlrd` (leitura de `.xls` — já está em `requirements.txt` via `scrape_anbima_debentures.py`)
- `httpx`
- `lib/db.py`, `lib/config.py`, `lib/logger.py`, `lib/email_outlook.py`

---

Ver também: [[04 - Banco de Dados]], [[05 - Fontes/Anbima]]
