# scrape_b3_curva_di.py

> **Fase F14.** Baixa a curva pré x DI da B3 via API REST (sem Playwright) e extrai as taxas nos vértices dos contratos DI Futuro (DI1F27–DI1F34), populando `MtmAnbima`.

---

## O que faz

Para a data solicitada:

1. Consulta `Search/GetDate` para verificar se a data está disponível (~20 últimos pregões).
2. Baixa o CSV completo via `Search/GetDownloadFile` (retorno base64-encoded).
3. Decodifica o CSV (encoding latin-1, separador `;`, decimal `,`).
4. Constrói um dicionário `{du: taxa}` a partir das colunas `Dias Úteis` e `Preço/Taxa`.
5. Para cada contrato alvo (DI1F27–DI1F34):
   - Calcula o vencimento (primeiro dia útil de janeiro do ano, via `data/feriados_anbima.csv`).
   - Calcula `du` = dias úteis entre `dtRef` (exclusive) e `dtVenc` (inclusive).
   - Busca a taxa no dicionário pelo `du` exato.
   - `vrDuration = du / 252`.
6. UPSERT em `MtmAnbima`.
7. Envia email de conclusão.

---

## API

**Base:** `https://sistemaswebb3-derivativos.b3.com.br/referenceRatesProxy/`

Todos os parâmetros são passados como base64-encoded JSON no path:

```
GET Search/GetDate/{base64({"language":"pt-br","id":"PRE"})}
→ lista de datas disponíveis (~20 últimos pregões)

GET Search/GetDownloadFile/{base64({"language":"pt-br","id":"PRE","date":"YYYY-MM-DD"})}
→ CSV completo (base64-encoded no body da resposta)
```

**Produto:** `"PRE"` = "DI x pré"

**Limitação importante:** a API guarda apenas ~20 pregões de histórico. Datas mais antigas retornam erro com a lista das disponíveis. O script loga WARNING com essas datas e encerra sem gravar.

**Descoberta:** API encontrada inspecionando o bundle Angular da página B3 (`main-TTEO44ED.js`). A abordagem anterior via Playwright foi descartada porque a tabela HTML da página sempre exibe D-1 e não suporta seleção de datas históricas.

---

## Estrutura do CSV

```
Descrição da Taxa;Dias Úteis;Dias Corridos;Preço/Taxa
...
```

- Separador: `;`
- Decimal: `,` (ex: `14,3062`)
- Encoding: latin-1 (com fallback para utf-8)
- Colunas de interesse: índice 1 (`Dias Úteis`) e índice 3 (`Preço/Taxa`)

---

## Contratos alvo

DI1F27, DI1F28, DI1F29, DI1F30, DI1F31, DI1F32, DI1F33, DI1F34.

---

## Regra de vencimento dos contratos DI1F

Vencimento = **primeiro dia útil de janeiro** do ano do contrato.

```python
d = date(ano, 1, 1)
while d.weekday() >= 5 or d in feriados:
    d += timedelta(days=1)
```

---

## Cálculo de `du`

```python
du = sum(
    1 for d in range_dates(dtRef + timedelta(1), dtVenc)
    if d.weekday() < 5 and d not in feriados
)
```

`feriados` carregado de `data/feriados_anbima.csv` uma vez por execução.

---

## Tabela destino

```sql
MtmAnbima (cdTicker, dtReferencia, vrTaxa, vrDuration)
```

| Campo | Valor |
|---|---|
| `cdTicker` | `"DI1F27"`, ..., `"DI1F34"` |
| `dtReferencia` | `dtRef` passado via `--date` |
| `vrTaxa` | taxa % a.a. do vértice (ex: `14.3062`) |
| `vrDuration` | `du / 252` |

---

## CLI

```powershell
python scripts/scrape_b3_curva_di.py --date 2026-06-03
```

Só aceita `--date` (data única). Sem `--start/--end` — a janela histórica da API é restrita (~20 pregões).

---

## Dependências

- `httpx` (sem Playwright)
- `lib/db.py`, `lib/logger.py`, `lib/email_outlook.py`
- `data/feriados_anbima.csv`

---

## Resultado do teste (03/06/2026)

8/8 contratos salvos. Taxas:

| Contrato | Taxa |
|---|---|
| DI1F27 | ~14,30% |
| DI1F28–DI1F34 | 14,30%–14,45% |

---

**Decisão (06/06/2026):** Playwright descartado. A tabela HTML da página pública B3 exibe sempre o pregão mais recente sem opção de seleção histórica. A API REST do iframe `sistemaswebb3-derivativos.b3.com.br` resolve o problema: aceita `date` como parâmetro e retorna CSV com a curva completa do dia. Script usa apenas `httpx` — mais leve e sem dependência de browser.

Ver também: [[04 - Banco de Dados]], [[10 - Scripts/scrape_anbima_ntnb]], [[10 - Scripts/calc_spread_anbima]]
