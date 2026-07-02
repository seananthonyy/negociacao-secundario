# Plano de Implementação v5 — Relatório Diário Crédito Privado

## 1. Sumário Executivo

Sistema Python local que, ao fim de cada dia útil, consolida negócios de crédito privado brasileiro (Debêntures, CRIs, CRAs) publicados pela B3, enriquece com dados estáticos do FI Analytics e taxas indicativas da Anbima, calcula taxas faltantes via cascata de calculadoras externas, computa spread over (relativo a NTN-B/DI Futuro ou absoluto), filtra negócios duplicados (corretagem/passagem de fundo aparecem como linhas repetidas no boletim), e gera dois relatórios HTML por dia (prévia D0 e definitivo D-1), **ambos agrupados por data de liquidação**.

**Por que `dtLiquidacao` é a chave do relatório:** boletas D+1 que liquidam no dia X podem ser lançadas no boletim B3 só no próprio dia X. No dia 27/05: prévia mostra trades com `dtLiquidacao = 27/05`; definitivo mostra trades com `dtLiquidacao = 26/05` (incluindo D+1 do dia 25 lançado em 26 ou 27).

**Fluxo macro:**

```
[B3 Boletim CSV] ──> TradesRaw ──> calc_taxa ──> calc_spread ──> filtrar ──> TradesProcessed ──> HTML
[FI Analytics Planilha] ──> InfoAtivos ──> (match_referencias, manual)
[Anbima Deb/CRI/CRA] ──> AnbimaIndicativos ────────────────────────────────────────> HTML
[MtmBloomberg] (externo) ──────────────────────────────────────────────────────────> HTML
```

Cada etapa é um script Python independente, idempotente, e envia email via Outlook ao terminar.

---

## 2. Estrutura de Pastas

```
./
├── scripts/
│   ├── scrape_b3_boletim.py
│   ├── scrape_fianalytics_planilha.py
│   ├── scrape_anbima_debentures.py
│   ├── scrape_anbima_cri_cra.py
│   ├── calc_taxa_negocios.py
│   ├── match_referencias.py
│   ├── calc_spread_over.py
│   ├── filtrar_trades.py
│   └── gerar_relatorio_html.py
├── lib/
│   ├── db.py
│   ├── config.py
│   ├── email_outlook.py
│   ├── logger.py
│   ├── fianalytics_api.py
│   └── b3_calc_api.py
├── data/
│   ├── trades.db
│   ├── logs/
│   └── relatorios/
│       └── YYYY-MM-DD/
│           ├── previa_HHMM.html
│           └── definitivo.html
├── templates/
│   └── relatorio.html.j2
├── vault/
├── config.toml
├── .env
├── .env.example
├── requirements.txt
└── README.md
```

Paths sempre relativos ao cwd.

---

## 3. Convenção de Nomes

- **Tabelas:** PascalCase, inglês.
- **Colunas:** camelCase com prefixo `vr` (valor numérico), `cd` (código/categoria), `dt` (data/hora), `id` (identificador).
- **Scripts:** snake_case, sem prefixo numérico.
- **Sem CHECK constraints**.

---

## 4. Schema SQLite Completo

```sql
-- ===== TradesRaw =====
-- Nota: a coluna "Origem negócio" do boletim B3 vem sempre como
-- "Pré-registro - Voice" e não traz info útil — não é coletada.
CREATE TABLE IF NOT EXISTS TradesRaw (
    idTrade                 INTEGER PRIMARY KEY AUTOINCREMENT,
    cdIdentificadorNegocio  TEXT    NOT NULL UNIQUE,
    cdInstrumento           TEXT    NOT NULL,
    cdEmissor               TEXT    NOT NULL,
    cdTicker                TEXT    NOT NULL,
    vrQuantidade            INTEGER NOT NULL,
    vrPU                    REAL    NOT NULL,
    vrVolume                REAL    NOT NULL,
    vrTaxaNegocio           REAL    NULL,
    dtHorarioNegocio        TEXT    NOT NULL,
    dtNegocio               TEXT    NOT NULL,
    cdIsin                  TEXT    NULL,
    dtLiquidacao            TEXT    NOT NULL,
    cdSituacao              TEXT    NOT NULL,
    dtCreatedAt             TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    dtUpdatedAt             TEXT    NULL
);
CREATE INDEX IF NOT EXISTS idxTradesRawDtNegocio    ON TradesRaw(dtNegocio);
CREATE INDEX IF NOT EXISTS idxTradesRawDtLiquidacao ON TradesRaw(dtLiquidacao);
CREATE INDEX IF NOT EXISTS idxTradesRawCdTicker     ON TradesRaw(cdTicker);

-- ===== TradesProcessed =====
CREATE TABLE IF NOT EXISTS TradesProcessed (
    idTrade           INTEGER PRIMARY KEY,
    cdTicker          TEXT    NOT NULL,
    cdEmissor         TEXT    NOT NULL,
    dtNegocio         TEXT    NOT NULL,
    dtLiquidacao      TEXT    NOT NULL,
    vrQuantidade      INTEGER NOT NULL,
    vrPU              REAL    NOT NULL,
    vrVolume          REAL    NOT NULL,
    vrTaxaCalculada   REAL    NULL,
    cdTaxaSource      TEXT    NULL,      -- 'FiAnalytics' | 'B3' | NULL
    vrDuration        REAL    NULL,
    vrSpreadOver      REAL    NULL,      -- em bps
    idTradeGroup      TEXT    NULL,      -- UUID do subgrupo de duplicidade
    cdStatus          TEXT    NOT NULL,  -- 'PRIMARY' | 'DUPLICATE'
    dtProcessedAt     TEXT    NOT NULL,
    FOREIGN KEY (idTrade) REFERENCES TradesRaw(idTrade)
);
CREATE INDEX IF NOT EXISTS idxTradesProcDtLiquidacao ON TradesProcessed(dtLiquidacao);
CREATE INDEX IF NOT EXISTS idxTradesProcDtNegocio    ON TradesProcessed(dtNegocio);
CREATE INDEX IF NOT EXISTS idxTradesProcCdTicker     ON TradesProcessed(cdTicker);
CREATE INDEX IF NOT EXISTS idxTradesProcCdStatus     ON TradesProcessed(cdStatus);
CREATE INDEX IF NOT EXISTS idxTradesProcIdGroup      ON TradesProcessed(idTradeGroup);

-- ===== InfoAtivos =====
CREATE TABLE IF NOT EXISTS InfoAtivos (
    cdTicker      TEXT PRIMARY KEY,
    cdInstrumento TEXT NULL,
    cdEmissor     TEXT NULL,
    dtVencimento  TEXT NULL,
    vrDuration    REAL NULL,
    cdIndexador   TEXT NULL,
    cdRef         TEXT NULL,         -- preenchido por match_referencias.py
    dtUpdatedAt   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idxInfoAtivosCdIndexador ON InfoAtivos(cdIndexador);

-- ===== AnbimaIndicativos =====
CREATE TABLE IF NOT EXISTS AnbimaIndicativos (
    cdTicker        TEXT NOT NULL,
    dtReferencia    TEXT NOT NULL,
    vrTaxaAnbima    REAL NULL,
    vrSpreadAnbima  REAL NULL,           -- em bps
    dtCreatedAt     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (cdTicker, dtReferencia)
);
CREATE INDEX IF NOT EXISTS idxAnbimaDtReferencia ON AnbimaIndicativos(dtReferencia);

-- ===== MtmBloomberg =====  (read-only, populada externamente)
-- Tickers vêm como "NTN-B 32" ou "DI1F31"
CREATE TABLE IF NOT EXISTS MtmBloomberg (
    cdTicker        TEXT NOT NULL,
    dtMtmBloomberg  TEXT NOT NULL,
    vrRate          REAL NOT NULL,
    vrDuration      REAL NOT NULL,
    PRIMARY KEY (cdTicker, dtMtmBloomberg)
);
CREATE INDEX IF NOT EXISTS idxMtmDtMtmBloomberg ON MtmBloomberg(dtMtmBloomberg);
```

### Notas
- Datas: TEXT em ISO-8601 (`YYYY-MM-DD`); horário: `HH:MM:SS` (horário de Brasília).
- Convenções em `cdTaxaSource` e `cdStatus` são informais (sem CHECK).
- `lib/db.py` faz auto-bootstrap do DDL.

---

## 5. Contrato de Cada Script

Padrão comum:
- CLI via `argparse`.
- `try/except/finally` em `main()`; no `finally` chama `send_completion_email(...)`.
- Datas: `--date YYYY-MM-DD` ou `--start YYYY-MM-DD --end YYYY-MM-DD`.

**Semântica das datas:**
- `scrape_b3_boletim`, `scrape_anbima_*` — `--date` = data da publicação na fonte
- `calc_taxa_negocios`, `calc_spread_over`, `filtrar_trades`, `gerar_relatorio_html` — `--date` = **`dtLiquidacao`**

### 5.1 `scrape_b3_boletim.py`

| Item | Valor |
|---|---|
| Propósito | Baixa CSV do Boletim Diário B3 e popula `TradesRaw` filtrando `cdInstrumento IN ('DEB','CRI','CRA')`. Ignora a coluna "Origem negócio". |
| CLI | `--date YYYY-MM-DD` \| `--start YYYY-MM-DD --end YYYY-MM-DD` |
| Tecnologia | Playwright (página exige seleção de data antes de baixar) |
| Escreve | `TradesRaw` via UPSERT por `cdIdentificadorNegocio`; soft-cancela ausentes; limpa `TradesProcessed` |
| Idempotência | Sim |
| Exemplo | `python scripts/scrape_b3_boletim.py --start 2026-05-25 --end 2026-05-27` |

**SQL UPSERT pattern:**
```sql
INSERT INTO TradesRaw (...) VALUES (...)
ON CONFLICT(cdIdentificadorNegocio) DO UPDATE SET
    vrTaxaNegocio = excluded.vrTaxaNegocio,
    cdSituacao    = excluded.cdSituacao,
    dtUpdatedAt   = CURRENT_TIMESTAMP;
```

**Soft-cancel de trades ausentes (por data processada):**

Após o UPSERT, qualquer trade que estava em `TradesRaw` para aquela `dtNegocio` mas **não apareceu** no boletim baixado é marcado como cancelado (soft delete):

```sql
UPDATE TradesRaw
SET    cdSituacao = 'Cancelado',
       dtUpdatedAt = CURRENT_TIMESTAMP
WHERE  dtNegocio = :data
  AND  cdIdentificadorNegocio NOT IN :ids_baixados;
```

Em seguida, os registros correspondentes em `TradesProcessed` são **deletados manualmente** (hard delete):

```sql
DELETE FROM TradesProcessed
WHERE idTrade IN (
    SELECT idTrade FROM TradesRaw
    WHERE  dtNegocio = :data
      AND  cdSituacao = 'Cancelado'
);
```

O script loga um `WARNING` listando as datas onde houve cancelamentos, pois essas datas precisam ser reprocessadas (`calc_taxa_negocios → calc_spread_over → filtrar_trades`).

**Email do script** reporta sempre três contadores (mesmo que zero): `inseridos`, `atualizados`, `cancelados`.

### 5.2 `scrape_fianalytics_planilha.py`

| Item | Valor |
|---|---|
| Propósito | Login Playwright, baixa planilha única, UPSERT em `InfoAtivos`. |
| CLI | (sem flags de data) — opcional `--headless/--no-headless` |
| Escreve | `InfoAtivos` (preserva `cdRef`; tickers ausentes permanecem) |

### 5.3 `scrape_anbima_debentures.py` / 5.4 `scrape_anbima_cri_cra.py`

| Item | Valor |
|---|---|
| Propósito | Baixa taxas indicativas Anbima, popula `AnbimaIndicativos`. |
| CLI | `--date` \| `--start --end` |
| Escreve | `AnbimaIndicativos` (INSERT OR REPLACE) |

### 5.5 `calc_taxa_negocios.py`

| Item | Valor |
|---|---|
| Propósito | Para cada trade em `TradesRaw` com `dtLiquidacao` na janela: se `vrTaxaNegocio NOT NULL`, copia direto para `vrTaxaCalculada` (`cdTaxaSource = NULL`); senão roda cascata FI Analytics → B3. |
| CLI | `--date` (= `dtLiquidacao`) \| `--start --end` |
| Escreve | `TradesProcessed` via UPSERT, com `cdStatus = 'PRIMARY'` provisório |

### 5.6 `match_referencias.py`

| Item | Valor |
|---|---|
| Propósito | Para cada ticker sem `cdRef`, decide a referência via duration-match. |
| CLI | **`--force` obrigatório** |
| Regra | `IPCA` → NTN-B ; `PREFIXADO` → DI Futuro ; outros → pula |

### 5.7 `calc_spread_over.py`

| Item | Valor |
|---|---|
| Propósito | Calcula `vrSpreadOver` conforme indexador (§9). |
| CLI | `--date` (= `dtLiquidacao`) \| `--start --end` |

### 5.8 `filtrar_trades.py`

| Item | Valor |
|---|---|
| Propósito | Aplica filtro de duplicados, atribui `cdStatus` e `idTradeGroup`. |
| CLI | `--date` (= `dtLiquidacao`) \| `--start --end`; opcionais `--janela-min N` `--tol-bps N` |
| Janela | Agrupa **dia a dia** (cada `dtLiquidacao` processada isoladamente) |
| Filtro de entrada | **Ignora trades com `cdSituacao = 'Cancelado'`** — esses nunca entram no processamento de duplicados nem em `TradesProcessed`. |
| Detalhes | §6 |

### 5.9 `gerar_relatorio_html.py`

| Item | Valor |
|---|---|
| Propósito | Renderiza HTML agregado por ticker, filtrando por `dtLiquidacao` e `cdStatus = 'PRIMARY'`. |
| CLI | `--date YYYY-MM-DD --mode previa\|definitivo` |
| Agregação | Média ponderada por `vrVolume`, apenas trades PRIMARY |

---

## 6. Lógica do Filtro de Duplicados

### 6.1 Resumo

O filtro classifica trades dentro de `TradesProcessed` em `PRIMARY` (conta no volume agregado) ou `DUPLICATE` (espelho do mesmo trade real — corretagem casada ou passagem entre fundos). Tudo fica na mesma tabela. `idTradeGroup` (UUID) liga os negócios irmãos para auditoria.

### 6.2 Como comparamos

Para cada par `(trade_i, trade_j)` dentro do mesmo `(cdTicker, dtLiquidacao)`:

| Critério | Default | Configurável |
|---|---|---|
| `|dtHorarioNegocio_i - dtHorarioNegocio_j|` | ≤ 5 min | sim (`janelaMinutos`) |
| `|vrTaxaCalculada_i - vrTaxaCalculada_j|` | ≤ 1 bps | sim (`tolBps`) |
| `vrQuantidade_i == vrQuantidade_j` | exato | sim (`quantidadeExata`) |

Se as 3 condições batem, os dois trades são **compatíveis**.

### 6.3 Particionamento (union-find)

1. Construímos grafo: trades = vértices, arestas = pares compatíveis.
2. Rodamos union-find — todos os trades transitivamente conectados viram um **subgrupo**.
3. **Cada trade pertence a exatamente um subgrupo.** Não existe ambiguidade.

Exemplo de transitividade: A compatível com B, B compatível com C, A NÃO compatível diretamente com C → mas A, B, C estão todos no mesmo subgrupo.

### 6.4 Classificação dentro do subgrupo

```python
idGroup = uuid4()
for t in subgrupo:
    t.idTradeGroup = idGroup

if len(subgrupo) == 1:
    subgrupo[0].cdStatus = 'PRIMARY'
else:
    # menor dtHorarioNegocio vence
    # tiebreaks: maior vrVolume → menor cdIdentificadorNegocio
    primary = min(subgrupo, key=lambda t: (
        t.dtHorarioNegocio, -t.vrVolume, t.cdIdentificadorNegocio
    ))
    primary.cdStatus = 'PRIMARY'
    for outro in subgrupo:
        if outro is not primary:
            outro.cdStatus = 'DUPLICATE'
```

### 6.5 Exemplo passo-a-passo

`cdTicker = DEBA11`, `dtLiquidacao = 2026-05-29`:

| # | horário  | qtd | PU      | taxa  | volume    |
|---|----------|-----|---------|-------|-----------|
| 1 | 10:00:00 | 100 | 1052.34 | 6.50  | 105.234   |
| 2 | 10:02:30 | 100 | 1052.34 | 6.50  | 105.234   |
| 3 | 11:00:00 | 50  | 1054.50 | 6.55  | 52.725    |
| 4 | 11:01:00 | 50  | 1054.50 | 6.55  | 52.725    |
| 5 | 14:00:00 | 200 | 1063.00 | 7.00  | 212.600   |

Resultado:
- **Sub A** = {1, 2}: pairwise compatível (Δhorário 2:30 ≤ 5min, Δtaxa 0 ≤ 1bps, qtd igual). 1 (menor horário) → PRIMARY, 2 → DUPLICATE.
- **Sub B** = {3, 4}: pairwise compatível. 3 → PRIMARY, 4 → DUPLICATE.
- **Sub C** = {5}: sozinho → PRIMARY.

Volume agregado do ticker conta apenas trades 1, 3 e 5.

---

## 7. Cascata de Calculadoras (apenas Deb/CRI/CRA)

```
trade com vrTaxaNegocio NULL
        │
        ▼
   FI Analytics
   ┌── cdInstrumento == 'DEB'        ──> POST /deb/debenturecalculator
   │   cdInstrumento in ('CRI','CRA')──> POST /cr/cricracalculator
   │   retornou m2mRate? ──> sucesso (cdTaxaSource = 'FiAnalytics')
   │
   │   se falhou ──> bondbuilder (fallback interno à FI Analytics)
   │       getuserbonds + bondbuildercalculator
   │       retornou m2mRate? ──> sucesso (cdTaxaSource = 'FiAnalytics')
   │
   ▼ se tudo FI Analytics falhou
   B3 Calculator
   POST https://api.calculadorarendafixa.com.br/login → Bearer
   GET  https://api.calculadorarendafixa.com.br/calcYield/{cdTicker}/{dtNegocio}/{vrPU}
   retornou yield? ──> sucesso (cdTaxaSource = 'B3')
   │
   ▼ se também falhou
   vrTaxaCalculada = NULL, cdTaxaSource = NULL
```

**Definição de falha:**
- HTTP != 2xx após 2 retries com backoff
- JSON sem campo esperado (`m2mRate` / `yield`) ou com valor null/0/negativo
- Timeout > 15s

**Roteamento:** `cdInstrumento` vem como `'DEB'`, `'CRI'`, `'CRA'`.

---

## 8. Match de Referência

```python
if not args.force:
    print("Use --force para executar."); exit(0)

linhas = SELECT cdTicker, cdIndexador, vrDuration FROM InfoAtivos
         WHERE cdRef IS NULL

for linha in linhas:
    if linha.vrDuration IS NULL or linha.cdIndexador IS NULL:
        continue

    if linha.cdIndexador == 'IPCA':
        prefix = 'NTN-B'
    elif linha.cdIndexador == 'PREFIXADO':
        prefix = 'DI1'
    else:
        continue

    candidatos = SELECT cdTicker, vrDuration FROM MtmBloomberg
                 WHERE cdTicker LIKE prefix || '%'
                   AND dtMtmBloomberg = (SELECT MAX(dtMtmBloomberg) FROM MtmBloomberg
                                         WHERE cdTicker LIKE prefix || '%')

    if not candidatos: continue
    melhor = min(candidatos, key=lambda c: abs(c.vrDuration - linha.vrDuration))
    UPDATE InfoAtivos SET cdRef = melhor.cdTicker WHERE cdTicker = linha.cdTicker
```

Para refazer um ticker específico: `UPDATE InfoAtivos SET cdRef = NULL WHERE cdTicker = '...'` antes do `--force`.

---

## 9. Spread Over

**Caso A — IPCA ou PREFIXADO** (com referência em MtmBloomberg):
```
spreadOver    = (1 + vrTaxaCalculada/100) / (1 + vrRate/100) - 1
vrSpreadOver  = spreadOver * 10000      # em bps
```

**Caso B — outros indexadores** (CDI+, IGPM, etc — a "taxa" já é o spread):
```
vrSpreadOver  = vrTaxaCalculada * 100    # em bps
```

**Pseudocódigo:**

```python
trades = SELECT tp.idTrade, tp.cdTicker, tp.dtNegocio, tp.vrTaxaCalculada,
                ia.cdIndexador, ia.cdRef
         FROM TradesProcessed tp
         LEFT JOIN InfoAtivos ia ON ia.cdTicker = tp.cdTicker
         WHERE tp.dtLiquidacao IN janela AND tp.vrTaxaCalculada IS NOT NULL

for t in trades:
    if t.cdIndexador in ('IPCA', 'PREFIXADO'):
        if t.cdRef IS NULL: continue
        refTaxa = SELECT vrRate FROM MtmBloomberg
                  WHERE cdTicker = t.cdRef AND dtMtmBloomberg <= t.dtNegocio
                  ORDER BY dtMtmBloomberg DESC LIMIT 1
        if refTaxa IS NULL: continue
        if (t.dtNegocio - refTaxa.dtMtmBloomberg) > maxDiasFallbackCurva: continue
        spread = ((1 + t.vrTaxaCalculada/100) / (1 + refTaxa/100) - 1) * 10000
    else:
        spread = t.vrTaxaCalculada * 100

    UPDATE TradesProcessed SET vrSpreadOver = spread WHERE idTrade = t.idTrade
```

---

## 10. Email via Outlook

```python
# lib/email_outlook.py
import win32com.client
import os

def send_completion_email(script_name, success, summary_text, error_traceback=None):
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        mail = outlook.CreateItem(0)
        mail.To = os.getenv("OUTLOOK_TO", "")
        status = "OK" if success else "ERROR"
        mail.Subject = f"[credito-privado] {status} · {script_name}"
        body = f"Script: {script_name}\nStatus: {status}\n\nResumo:\n{summary_text}"
        if error_traceback:
            body += f"\n\nTraceback:\n{error_traceback}"
        mail.Body = body
        mail.Send()
    except Exception as e:
        import logging
        logging.error(f"Falha ao enviar email via Outlook: {e}")
```

**Padrão de chamada:**

```python
if __name__ == "__main__":
    import traceback
    summary, ok, tb = "", True, None
    try:
        summary = main()
    except Exception:
        ok = False
        tb = traceback.format_exc()
        raise
    finally:
        send_completion_email("scrape_b3_boletim", ok, summary, tb)
```

`OUTLOOK_TO` aceita lista separada por `;`.

---

## 11. `config.toml`

```toml
[paths]
dbFile         = "data/trades.db"
relatoriosDir  = "data/relatorios"
logsDir        = "data/logs"
templatesDir   = "templates"

[filtro]
janelaMinutos   = 5
tolBps          = 1
quantidadeExata = true

[calc]
timeoutSeconds  = 15
retries         = 2

[spread]
maxDiasFallbackCurva = 5

[alerta]
volumeMinSemTaxa = 5_000_000

[email]
ativo          = true
assuntoPrefixo = "[credito-privado]"

[scrape.b3]
baseUrl              = "https://www.b3.com.br/pt_br/market-data-e-indices/servicos-de-dados/market-data/consultas/boletim-diario/boletim-diario-do-mercado/"
instrumentosAceitos  = ["DEB", "CRI", "CRA"]

[scrape.fianalytics]
signinUrl   = "https://fi-analytics.com.br/signin"
downloadUrl = "https://fi-analytics.com.br/analytics-hub/hub?type=deb"
headless    = true

[scrape.anbima]
debUrl    = "https://www.anbima.com.br/pt_br/informar/taxas-de-debentures.htm"
cricraUrl = "https://www.anbima.com.br/pt_br/informar/precos-e-indices/precos/taxas-de-cri-e-cra/taxas-de-cri-e-cra.htm"

[api.fianalytics]
baseUrl    = "https://endpoint.fi-analytics.com.br"
debPath    = "/deb/debenturecalculator"
cricraPath = "/cr/cricracalculator"

[api.b3]
baseUrl = "https://api.calculadorarendafixa.com.br"
```

---

## 12. Variáveis de Ambiente (`.env`)

Valores reais NUNCA neste doc (repo público). Ver `code/.env.example` (PC pessoal)
e o bloco `[env]` do `config.toml` (mapeamento nome→variável, para o banco).

```
FIANALYTICS_USER=<preencher>
FIANALYTICS_PASS=<preencher>
FIANALYTICS_API_KEY=<preencher>
B3_CALC_TOKEN=<preencher>
OUTLOOK_TO=<preencher>
```

---

## 13. Workflow do Usuário (PowerShell)

### 13.1 Rotina de fechamento (dia 27/05)

```powershell
# 1. Boletim B3 (rebaixa últimos dias úteis pra pegar D+1 stragglers)
python scripts/scrape_b3_boletim.py --start 2026-05-25 --end 2026-05-27

# 2. Infos estáticas
python scripts/scrape_fianalytics_planilha.py

# 3. Anbima
python scripts/scrape_anbima_debentures.py --date 2026-05-27
python scripts/scrape_anbima_cri_cra.py    --date 2026-05-27

# 4. Match de ref (manual)
# python scripts/match_referencias.py --force

# 5. Processa por dtLiquidacao
$datas = @("2026-05-26", "2026-05-27")
foreach ($d in $datas) {
    python scripts/calc_taxa_negocios.py --date $d
    python scripts/calc_spread_over.py   --date $d
    python scripts/filtrar_trades.py     --date $d
}

# 6. Relatórios
python scripts/gerar_relatorio_html.py --date 2026-05-27 --mode previa
python scripts/gerar_relatorio_html.py --date 2026-05-26 --mode definitivo
```

### 13.2 Reprocessar janela

```powershell
python scripts/calc_taxa_negocios.py --start 2026-05-01 --end 2026-05-27
python scripts/calc_spread_over.py   --start 2026-05-01 --end 2026-05-27
python scripts/filtrar_trades.py     --start 2026-05-01 --end 2026-05-27
```

---

## 14. Outline do Vault Obsidian

### `00 - Inicio.md`
Parágrafo curto + wikilinks pros 10 arquivos principais.

### `01 - O Que Faz.md`
Visão de negócio em ~5 parágrafos. Glossário: trade, ticker, spread over, ref, indexador, NTN-B, DI Futuro, D+0/D+1, prévia vs definitivo, `dtLiquidacao` vs `dtNegocio`.

### `02 - Como Rodar.md`
Pré-requisitos (Python 3.11+, `pip install -r requirements.txt`, `playwright install chromium`, Outlook instalado), `.env`, comandos.

### `03 - Estrutura de Pastas.md`
Árvore do projeto + 1 linha por pasta.

### `04 - Banco de Dados.md`
5 tabelas, 1 parágrafo cada. Convenção de prefixos.

### `05 - Fontes/B3 Boletim.md`
URL, 13 colunas capturadas (excluindo "Origem negócio" que é sempre "Pré-registro - Voice"), filtro DEB/CRI/CRA, fluxo Playwright.

### `05 - Fontes/FI Analytics.md`
URL signin, URL download, fluxo Playwright, preservação de `cdRef`.

### `05 - Fontes/Anbima.md`
URLs Deb e CRI/CRA, formato.

### `06 - Calculadoras/FI Analytics API.md`
Endpoints, headers, body, response.

### `06 - Calculadoras/B3 Calculator API.md`
Base URL, login Bearer, `GET /calcYield/...`.

### `07 - Filtro de Duplicados.md`
2 status (PRIMARY/DUPLICATE), parâmetros (janela, tol_bps, qty exata), heurística union-find, exemplo dos 5 trades.

### `08 - Match de Referencia.md`
Regra IPCA→NTN-B, PREFIXADO→DI. Manual com `--force`.

### `09 - Progresso.md`
Estado vivo: feito / próximo / dúvidas abertas.

### `99 - Credenciais e Links.md`
Todos os URLs + referência ao `.env`.

---

## 15. Decisões em Aberto

| Tópico | Estado | Quando decidir |
|---|---|---|
| Visual/CSS do HTML | Tabela básica Jinja2 | Após primeira versão fim-a-fim |
| Lib de tabela interativa | `<table>` simples | Quando pedir ordenação/filtro |
| Backup do `data/trades.db` | Manual | Quando histórico passar de 6 meses |
| Confirmação de nomes da `MtmBloomberg` | Pendente | Usuário valida antes do primeiro run |

---

## Próximos passos

V5 fecha o planejamento. A partir daqui:

1. Setup do vault Obsidian no caminho que você escolher.
2. Disparar Agente A (código) e Agente B (notas do vault) em paralelo.
