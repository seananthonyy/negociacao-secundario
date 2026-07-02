# Banco de Dados

> Ver também: [[00 - Inicio]] | [[03 - Estrutura de Pastas]] | [[07 - Filtro de Duplicados]]

Um único arquivo SQLite em `code/data/trades.db`. O módulo `lib/db.py` faz o bootstrap automático do DDL na primeira execução de qualquer script — não é preciso criar o banco manualmente.

---

## Convenção de prefixos de colunas

| Prefixo | Significado | Exemplos |
|---|---|---|
| `vr` | Valor numérico | `vrPU`, `vrTaxaCalculada`, `vrSpreadOver` |
| `cd` | Código ou categoria (texto) | `cdTicker`, `cdInstrumento`, `cdStatus` |
| `dt` | Data ou datetime (texto ISO-8601) | `dtNegocio`, `dtLiquidacao`, `dtCriacao` |
| `id` | Identificador | `idTrade`, `idGrupoNegocio` |

Datas são armazenadas como `TEXT` em formato `YYYY-MM-DD`; horários como `HH:MM:SS` (horário de Brasília). Sem CHECK constraints nas colunas.

---

## Tabelas

### NegociosBrutos

Armazena os trades exatamente como vieram do Boletim Diário B3, filtrados para `cdInstrumento IN ('DEB', 'CRI', 'CRA')`. A coluna "Origem negócio" do boletim (sempre "Pré-registro - Voice") é descartada. O UPSERT usa `cdIdentificadorNegocio` como chave — rodadas repetidas do scraper apenas atualizam campos que possam ter mudado (taxa, situação).

```sql
CREATE TABLE IF NOT EXISTS NegociosBrutos (
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
    cdISIN                  TEXT    NULL,
    dtLiquidacao            TEXT    NOT NULL,
    cdSituacao              TEXT    NOT NULL,
    dtCriacao             TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    dtAtualizacao             TEXT    NULL
);
CREATE INDEX IF NOT EXISTS idxNegociosBrutosDtNegocio    ON NegociosBrutos(dtNegocio);
CREATE INDEX IF NOT EXISTS idxNegociosBrutosDtLiquidacao ON NegociosBrutos(dtLiquidacao);
CREATE INDEX IF NOT EXISTS idxNegociosBrutosCdTicker     ON NegociosBrutos(cdTicker);
```

### NegociosProcessados

Versão enriquecida dos trades: taxa calculada (via cascata de calculadoras), duration, spread over, e resultado do filtro de qualidade. É a tabela que alimenta os relatórios HTML. A FK `idTrade` aponta para `NegociosBrutos`. Ver [[07 - Filtro de Duplicados]] para a lógica de `cdStatus` e `idGrupoNegocio`.

```sql
CREATE TABLE IF NOT EXISTS NegociosProcessados (
    idTrade           INTEGER PRIMARY KEY,
    cdTicker          TEXT    NOT NULL,
    cdEmissor         TEXT    NOT NULL,
    dtNegocio         TEXT    NOT NULL,
    dtLiquidacao      TEXT    NOT NULL,
    vrQuantidade      INTEGER NOT NULL,
    vrPU              REAL    NOT NULL,
    vrVolume          REAL    NOT NULL,
    vrTaxaCalculada   REAL    NULL,
    cdFonteTaxa      TEXT    NULL,      -- 'FiAnalytics' | 'B3' | NULL
    vrDuration        REAL    NULL,
    vrSpreadOver      REAL    NULL,      -- em bps
    idGrupoNegocio      TEXT    NULL,      -- UUID do grupo de filtro (FUNDO/CORRETOR/PF)
    cdStatus          TEXT    NOT NULL,  -- 'VALIDO' | 'FUNDO' | 'BROKER' | 'PF'
    dtProcessamento     TEXT    NOT NULL,
    FOREIGN KEY (idTrade) REFERENCES NegociosBrutos(idTrade)
);
CREATE INDEX IF NOT EXISTS idxNegociosProcessadosDtLiquidacao ON NegociosProcessados(dtLiquidacao);
CREATE INDEX IF NOT EXISTS idxNegociosProcessadosDtNegocio    ON NegociosProcessados(dtNegocio);
CREATE INDEX IF NOT EXISTS idxNegociosProcessadosCdTicker     ON NegociosProcessados(cdTicker);
CREATE INDEX IF NOT EXISTS idxNegociosProcessadosCdStatus     ON NegociosProcessados(cdStatus);
CREATE INDEX IF NOT EXISTS idxNegociosProcessadosIdGrupo ON NegociosProcessados(idGrupoNegocio);
```

### InfoAtivos

Informações estáticas dos ativos, consolidadas de três fontes: planilha FI Analytics, arquivos Anbima (debêntures e CRI/CRA) e `match_referencias.py`. Nenhuma fonte sobrescreve o que outra já preencheu — todos os UPSERTs usam `COALESCE` nas colunas compartilhadas. Ver [[05 - Fontes/FI Analytics]], [[05 - Fontes/Anbima]] e [[08 - Match de Referencia]].

#### Colunas e fontes

| Coluna | Tipo | Fonte(s) | Descrição |
|---|---|---|---|
| `cdTicker` | TEXT PK | todas | Código do ativo |
| `cdInstrumento` | TEXT | Anbima, FI Analytics | `'DEB'` / `'CRI'` / `'CRA'` |
| `cdEmissor` | TEXT | Anbima, FI Analytics | Nome do emissor (CRI/CRA: empresa originadora = "Risco de Crédito") |
| `dtVencimento` | TEXT | Anbima, FI Analytics | Data de vencimento (YYYY-MM-DD) |
| `vrDuration` | REAL | Anbima (÷252), FI Analytics (já em anos) | Duration em anos |
| `dtAtualizacaoDuration` | TEXT | automático | Data em que `vrDuration` foi inserida/atualizada pela última vez |
| `cdIndexador` | TEXT | Anbima, FI Analytics | `CDI+` / `%CDI` / `IPCA` / `PREFIXADO` |
| `cdReferencia` | TEXT | Anbima, `match_referencias.py` | Benchmark para spread: `FUNDING`, `NTN-B {YY}`, `DI1F{YY}` |
| `cdFonteReferencia` | TEXT | automático | Quem populou `cdReferencia` — ver tabela abaixo |
| `vrTaxaEmissao` | REAL | FI Analytics, Anbima Data | Taxa de emissão (% a.a.) |
| `vrVNE` | REAL | Anbima Data | Valor nominal na emissão / saldo devedor inicial |
| `dtInicioRentabilidade` | TEXT | Anbima Data | Data de início do rendimento |
| `cdISIN` | TEXT | Anbima Data | Código ISIN do ativo |
| `vrQuantidadeEmissao` | REAL | Anbima Data | Quantidade emitida desta série |
| `dtEmissao` | TEXT | Anbima Data | Data de emissão da série |
| `dtAtualizacao` | TEXT | automático | Timestamp do último UPSERT |

#### `cdFonteReferencia`

| Valor | Quem seta | Quando |
|---|---|---|
| `'Anbima'` | `scrape_anbima_debentures.py` / `scrape_anbima_cri_cra.py` | Ativo encontrado nos arquivos Anbima com campo de referência preenchido |
| `'FiAnalytics'` | `scrape_fianalytics_planilha.py` | Ativo com `cdReferencia` vindo da planilha FI Analytics (atualmente sempre NULL — reservado para uso futuro) |
| `'MatchRef'` | `match_referencias.py` | Match automático por duration contra `MtmAnbima` |
| `NULL` | — | Sem ref ainda atribuída |

`match_referencias.py` **nunca** sobrescreve `cdFonteReferencia = 'Anbima'` — refs da Anbima são consideradas definitivas. Ver [[10 - Scripts/match_referencias]].

#### `dtAtualizacaoDuration`

Indica quando `vrDuration` foi inserida ou atualizada pela última vez. Permite saber se a duration de um ativo está desatualizada (Anbima publica nova duration a cada dia útil). Só é atualizada quando `vrDuration` não é NULL — via `CASE WHEN` no UPSERT.

```sql
CREATE TABLE IF NOT EXISTS InfoAtivos (
    cdTicker             TEXT PRIMARY KEY,
    cdInstrumento        TEXT NULL,
    cdEmissor            TEXT NULL,
    dtVencimento         TEXT NULL,
    vrDuration           REAL NULL,
    dtAtualizacaoDuration     TEXT NULL,          -- data em que vrDuration foi inserida/atualizada
    cdIndexador          TEXT NULL,
    cdReferencia                TEXT NULL,          -- FUNDING | NTN-B {YY} | DI1F{YY} | NULL
    cdFonteReferencia          TEXT NULL,          -- 'Anbima' | 'FiAnalytics' | 'MatchRef' | NULL
    vrTaxaEmissao       REAL NULL,          -- taxa de emissão (% a.a.) — fonte: FI Analytics / Anbima Data
    vrVNE                REAL NULL,          -- valor nominal na emissão — fonte: Anbima Data
    dtInicioRentabilidade TEXT NULL,          -- data início rendimento — fonte: Anbima Data
    -- colunas adicionadas via migração em bootstrap() (F17):
    cdISIN               TEXT NULL,          -- código ISIN — fonte: Anbima Data
    vrQuantidadeEmissao  REAL NULL,          -- quantidade emitida desta série — fonte: Anbima Data
    dtEmissao            TEXT NULL,          -- data de emissão da série — fonte: Anbima Data
    dtAtualizacao          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idxInfoAtivosCdIndexador ON InfoAtivos(cdIndexador);
```

**Decisao (01/06/2026):** `cdReferencia` e `vrDuration` populados primariamente pelos scrapers Anbima/FI Analytics. `match_referencias.py` só atua nos ativos sem `cdReferencia` preenchido. COALESCE garante que nenhum scraper sobrescreva valor existente com NULL.

**Decisao (07/06/2026):** `cdFonteReferencia` adicionada para distinguir refs Anbima das atribuídas automaticamente por `match_referencias.py`.

**Decisao (20/06/2026):** novas colunas `vrTaxaEmissao` (mapeada de "Taxa Emissão (%)" da planilha FI Analytics), `vrVNE`, `dtInicioRentabilidade` e `vrOutstanding` adicionadas via migração segura em `bootstrap()`. Bug corrigido: `scrape_fianalytics_planilha.py` agora grava `cdFonteReferencia = 'FiAnalytics'` (antes gravava incorretamente `'Anbima'`).

**Decisao (20/06/2026 — F17):** novas colunas `cdISIN`, `vrQuantidadeEmissao` e `dtEmissao` adicionadas via migração em `bootstrap()`, populadas por `scrape_anbima_data_ativos.py`. Tabela separada `InfoAtivosAnbima` descartada — dados gravados diretamente via COALESCE.

**Decisao (21/06/2026, revertida):** as colunas `vrOutstanding` e `dtUpsertOutstanding` chegaram a existir em `InfoAtivos` (reservadas, sem fonte ativa) mas foram **removidas** em 21/06/2026 via `ALTER TABLE ... DROP COLUMN`. O outstanding passou a viver em **série temporal própria** — ver a tabela `Outstanding` abaixo — porque varia por data, ao contrário das demais colunas de `InfoAtivos` (constantes por ativo).

### AnbimaIndicativos

Taxas indicativas diárias publicadas pela Anbima para debêntures, CRIs e CRAs. Cada linha é a taxa de um ticker para uma data de referência. Usada no relatório HTML como coluna informativa (não entra no cálculo de spread). Ver [[05 - Fontes/Anbima]].

> **Leitura adicional (29/06/2026):** o modo incremental de [[10 - Scripts/scrape_anbima_data_ativos]] passou a ler `AnbimaIndicativos.dtReferencia` (além de `NegociosBrutos.dtNegocio`) para montar a fila de tickers — quem teve taxa Anbima divulgada na data também entra na fila, mesmo sem ter sido negociado. Como `AnbimaIndicativos` não tem coluna de instrumento, o script faz `LEFT JOIN InfoAtivos` para obter `cdInstrumento`. Sem mudança de schema — só comportamento de leitura.

```sql
CREATE TABLE IF NOT EXISTS AnbimaIndicativos (
    cdTicker        TEXT NOT NULL,
    dtReferencia    TEXT NOT NULL,
    vrTaxaAnbima    REAL NULL,
    vrSpreadAnbima  REAL NULL,           -- em bps
    dtCriacao     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (cdTicker, dtReferencia)
);
CREATE INDEX IF NOT EXISTS idxAnbimaDtReferencia ON AnbimaIndicativos(dtReferencia);
```

### MtmBloomberg (REMOVIDA — 02/07/2026)

Tabela do plano original (referências de mercado via Bloomberg). **Nunca foi usada:** a implementação (F9) passou a usar `MtmAnbima`, e a `MtmBloomberg` ficou como schema morto (0 linhas, 0 referências no código). DDL e índice removidos de `lib/db.py` e dropados do `trades.db`.

### InfoAtivosAnbima

**Esta tabela não foi criada.** Na implementação de F17, decidiu-se gravar os dados do Anbima Data diretamente em `InfoAtivos` via UPSERT com COALESCE, mantendo fonte única de verdade por ativo. Três colunas novas foram adicionadas a `InfoAtivos` via migração em `bootstrap()`: `cdISIN`, `vrQuantidadeEmissao` e `dtEmissao`.

Os dados brutos de cada scrape ficam preservados nos arquivos JSON em `data/anbima_data_raw/{ticker}.json`.

Populado por: [[10 - Scripts/scrape_anbima_data_ativos|scrape_anbima_data_ativos.py]].

---

### FluxoAtivos

Fluxo de pagamentos futuros por ativo, extraído da aba "Agenda" do Anbima Data. Uma linha por `(cdTicker, dtEvento)` — múltiplos eventos numa mesma `data_liquidacao` são agregados antes de gravar.

`dtEvento` = `data_liquidacao` do evento (quando o dinheiro é recebido, não `data_base`).

Os campos calculados pelo script antes da inserção:
- `vrPctAmortizacao`: campo `taxa` do evento AMORTIZACAO/VENCIMENTO/RESGATE (API já retorna em %)
- `vrPctIncorporacao`: `sum(valor_incorp) / (sum(valor_incorp) + sum(valor_juros)) * 100`; NULL se sem incorporação ou com valores futuros ainda não precificados

Upsert via `INSERT OR REPLACE`. Dados brutos preservados nos JSONs em `data/anbima_data_raw/`.

```sql
CREATE TABLE IF NOT EXISTS FluxoAtivos (
    cdTicker           TEXT NOT NULL,
    dtEvento            TEXT NOT NULL,   -- data_liquidacao do evento (YYYY-MM-DD)
    vrPctAmortizacao  REAL NULL,       -- % do principal amortizado nessa data
    vrPctIncorporacao REAL NULL,       -- % dos juros incorporado (vs pago); NULL se não há incorporação
    dtAtualizacao        TEXT NOT NULL,
    PRIMARY KEY (cdTicker, dtEvento)
);
-- Índice COBRIDOR (covering): todas as colunas da query de fluxo cabem no índice
CREATE INDEX IF NOT EXISTS idxFluxoAtivosCobertura
    ON FluxoAtivos(cdTicker, dtEvento, vrPctAmortizacao, vrPctIncorporacao);
```

**Decisão (20/06/2026):** schema intencional — apenas os dois campos calculados por design. Dados brutos (taxa, valor unitário, situação Previsto/Liquidado) ficam no JSON de checkpoint para consulta manual se necessário. Adicionar colunas extra quando houver caso de uso concreto.

**Decisão (23/06/2026) — índice cobridor (covering) para o add-in da calculadora:** o lookup de fluxo de caixa por ticker (`SELECT dtEvento, vrPctAmortizacao, vrPctIncorporacao FROM FluxoAtivos WHERE cdTicker=? ORDER BY dtEvento`) é a consulta crítica do add-in externo. O índice `idxFluxoAtivosCobertura` inclui **todas** as colunas que a query lê, então o SQLite resolve tudo dentro do índice (index-only scan) sem tocar na tabela. Efeito medido: **538 µs → 84 µs** por ticker. O índice antigo `idxFluxoAtivosCdTicker` foi **removido** (era redundante — `cdTicker` já é o prefixo da PK composta `(cdTicker, dtEvento)`); o `bootstrap()` faz `DROP INDEX IF EXISTS idxFluxoAtivosCdTicker` na migração. Ver [[10 - Scripts/libs]] (lib/db.py).

Populada por: [[10 - Scripts/scrape_anbima_data_ativos|scrape_anbima_data_ativos.py]].

### Mapeamento InfoAtivos + FluxoAtivos para calculadora externa de PU

Campos necessários para calcular PU de um ativo a partir de uma taxa alvo (ex: calculadora de precificação própria da mesa):

**De `InfoAtivos` (1 row por ticker):**

| Coluna | Alias no cálculo | Observação |
|---|---|---|
| `dtInicioRentabilidade` | `data_inicio` | Data de início do rendimento (ISO YYYY-MM-DD) |
| `vrVNE` | `vne_original` | Valor nominal na emissão / saldo devedor inicial |
| `vrTaxaEmissao` | `taxa_aa` | Taxa de emissão em % a.a. |
| `cdIndexador` | tipo de indexador | `CDI+` / `%CDI` / `IPCA` / `PREFIXADO` |
| `dtVencimento` | `vencimento` | Data de vencimento |

**De `FluxoAtivos` (N rows por ticker):**

| Coluna | Alias no cálculo | Observação |
|---|---|---|
| `dtEvento` | data de liquidação do evento | Quando o dinheiro é recebido |
| `vrPctAmortizacao` | `% amortizado` | NULL se não há amortização nessa data |
| `vrPctIncorporacao` | `% incorporação` | NULL para eventos futuros ainda sem precificação |

**Campos NÃO disponíveis no banco (derivar):**

- `dia_aniversario` — derivar do dia do mês de `dtInicioRentabilidade` (ex: `dtInicioRentabilidade = "2022-03-15"` → aniversário no dia 15 de cada mês)
- `tipo_amort` (gradual / bullet) — não retornado pela API Anbima Data; inferir da série de `vrPctAmortizacao` em `FluxoAtivos` (múltiplas datas com % < 100 → gradual; uma única data com 100% → bullet)

**Queries ideais do add-in (por ticker):**

```sql
-- características do ativo (1 row)
SELECT cdIndexador, dtVencimento, vrTaxaEmissao, vrVNE,
       dtInicioRentabilidade, cdReferencia, vrDuration
FROM   InfoAtivos
WHERE  cdTicker = ?;

-- fluxo de pagamentos (N rows) — resolvida via índice cobridor (index-only scan)
SELECT dtEvento, vrPctAmortizacao, vrPctIncorporacao
FROM   FluxoAtivos
WHERE  cdTicker = ?
ORDER BY dtEvento;
```

Com a conexão read-only (ver [[10 - Scripts/libs]] → `get_readonly_connection`) e os PRAGMAs de performance ativos, o conjunto InfoAtivos + FluxoAtivos resolve em **~26–36 µs/ticker**. O add-in deve **reutilizar uma única conexão** entre lookups — abrir/fechar a cada consulta domina o tempo total.

---

### MtmAnbima

Populada automaticamente por scripts locais (F13 `scrape_anbima_ntnb`, F14 `scrape_b3_curva_di`). Consolida dois tipos de referência:

- **NTN-B** (`cdTicker = "NTN-B 26"`, `"NTN-B 32"`, etc.) — taxa indicativa do mercado secundário de títulos públicos, publicada diariamente pela Anbima. Referência para bonds indexados a IPCA.
- **DI Futuro** (`cdTicker = "DI1F26"`, etc.) — taxa da curva pré × DI, publicada diariamente pela B3. Referência para bonds PREFIXADOS.

`vrDuration` é preenchida para ambos os tipos. Para NTN-B: lida do XLS da Anbima (se disponível na planilha) ou calculada analiticamente a partir do cashflow da NTN-B (cupom 6% a.a. real semestral + principal). Para DI Futuro: lida da fonte B3. Permite match por duration em análises futuras além do lookup direto por `cdReferencia`.

```sql
CREATE TABLE IF NOT EXISTS MtmAnbima (
    cdTicker     TEXT NOT NULL,   -- "NTN-B 32", "DI1F26", etc.
    dtReferencia TEXT NOT NULL,
    vrTaxa       REAL NOT NULL,   -- % a.a.
    vrDuration   REAL NULL,       -- anos; preenchido para NTN-B e DI Futuro
    PRIMARY KEY (cdTicker, dtReferencia)
);
CREATE INDEX IF NOT EXISTS idxMtmAnbimaDtReferencia ON MtmAnbima(dtReferencia);
```

Populada por: [[10 - Scripts/scrape_anbima_ntnb|scrape_anbima_ntnb.py]] (NTN-B) e `scrape_b3_curva_di.py` (DI Futuro, a implementar). Consultada por `calc_spread_anbima.py`.

---

### Outstanding

Série temporal do **saldo em circulação** (`AMT_OUTSTANDING` da Bloomberg) por ativo de crédito privado. Um valor por ticker por data — varia ao longo do tempo (amortizações, recompras), por isso é tabela própria e não coluna de `InfoAtivos`. Criada em 22/06/2026; populada a partir de 28/06/2026.

```sql
CREATE TABLE IF NOT EXISTS Outstanding (
    cdTicker      TEXT NOT NULL,
    dtOutstanding TEXT NOT NULL,   -- data de negócio e/ou divulgação Anbima (ISO)
    vrOutstanding REAL NULL,       -- AMT_OUTSTANDING na data (NULL se Bloomberg não retornar)
    PRIMARY KEY (cdTicker, dtOutstanding)
);
CREATE INDEX IF NOT EXISTS idxOutstandingDtOutstanding ON Outstanding(dtOutstanding);
```

Populada por: [[scrape_outstanding_bloomberg|scrape_outstanding_bloomberg.py]] — para cada dia, busca os tickers da união de negociados (`NegociosBrutos.dtNegocio`) + divulgados Anbima (`AnbimaIndicativos.dtReferencia`), excluindo NTN-B/DI1. Consultada pela aba **Visão Anbima** do [[gerar_relatorio_credito]] (`_PESO_CTE` → peso da média ponderada por indexador, com casa de data `dtOutstanding = dtReferencia`). Só roda no PC do banco (Bloomberg); vazia no PC pessoal.

---

## Módulo `lib/db.py`

Os scripts nunca abrem conexões SQLite diretamente — usam as três funções abaixo, todas em `code/lib/db.py`.

### `get_db(db_path=None)`

Função principal. Scripts usam apenas esta. Abre a conexão e garante que o schema já existe antes de devolver a conexão ao chamador.

```python
conn = get_db()   # path vem de cfg["paths"]["dbFile"]
```

### `get_connection(db_path=None)`

Abre a conexão bruta e configura PRAGMAs:
- `journal_mode=WAL` — evita locks em leituras simultâneas
- `foreign_keys=ON` — garante integridade referencial (NegociosProcessados → NegociosBrutos)
- `synchronous=NORMAL` — mais rápido que `FULL`, seguro com WAL
- o bloco de PRAGMAs de performance `_PRAGMAS_PERF` (abaixo)

Também define `row_factory = sqlite3.Row` para acesso por nome de coluna. Cria o diretório pai do arquivo se ainda não existir.

### `get_readonly_connection(db_path=None)`

**Novo (23/06/2026).** Abre uma conexão **somente leitura** pensada para o add-in externo da calculadora. Difere de `get_connection`:
- Abre o banco em modo read-only via URI (`file:...?mode=ro`) + `PRAGMA query_only=ON` — não consegue gravar, mesmo por engano.
- Aplica os mesmos PRAGMAs de performance (`_PRAGMAS_PERF`).
- **Não chama `bootstrap()`** — não cria nem migra schema; só lê o que já existe. Isso a torna rápida e sem efeitos colaterais.

Indicada para qualquer leitor externo que só consulta `InfoAtivos`/`FluxoAtivos`.

### PRAGMAs de performance (`_PRAGMAS_PERF`)

Aplicados em toda conexão (read-write e read-only):

| PRAGMA | Valor | Efeito |
|---|---|---|
| `mmap_size` | 256 MiB | lê o arquivo via memory-map, menos syscalls |
| `cache_size` | -65536 (64 MiB) | cache de páginas maior em RAM |
| `temp_store` | MEMORY | tabelas/índices temporários em RAM |
| `busy_timeout` | 5000 ms | espera lock em vez de falhar na hora |

São SQLite puro — funcionam em qualquer linguagem (C#/.NET, VBA/ODBC, etc.). O add-in pode replicar essas mesmas linhas se não usar o `lib/db.py`.

### `bootstrap(conn)`

Executa o DDL completo via `conn.executescript(DDL)`. Todas as instruções usam `IF NOT EXISTS`, então é seguro chamar mais de uma vez — idempotente por design. Além do DDL, na migração ele:
- faz `DROP INDEX IF EXISTS idxFluxoAtivosCdTicker` (índice redundante, substituído pelo cobridor `idxFluxoAtivosCobertura`);
- roda `ANALYZE` **apenas se** a tabela `sqlite_stat1` ainda não existir — popula as estatísticas do query planner uma única vez, sem custo em toda execução.

### Dependência de configuração

`get_db()` e `get_connection()` leem o path do banco a partir de `cfg["paths"]["dbFile"]`, que vem de `config.toml` via [[06 - Calculadoras/FI Analytics API|lib/config.py]]. O `cfg` é um proxy lazy: carrega o arquivo apenas na primeira leitura, usando `tomllib` nativo do Python 3.11+ (sem dependência extra).

**Decisão (30/05/2026):** usar `tomllib` nativo em vez de `toml` ou `tomli` para manter zero dependências extras no módulo de configuração. Isso impõe Python >= 3.11 como requisito mínimo do projeto.

---

## Fluxo de dados entre tabelas

```
[B3 Boletim CSV]
      |
      v
  NegociosBrutos
      |
      v (calc_taxa_negocios.py)
  NegociosProcessados  <-- InfoAtivos (cdIndexador, cdReferencia, vrDuration)
      |                      ^                  ^
      |                      |                  |
      |              FI Analytics planilha   match_referencias.py
      |                                      (cdReferencia via MtmAnbima)
      v (filtrar_trades.py)
  NegociosProcessados.cdStatus / idGrupoNegocio
      |
      v (calc_spread_anbima.py)
  AnbimaIndicativos.vrSpreadAnbima  <-- MtmAnbima (vrTaxa)
      |
      v (match_referencias.py)
  InfoAtivos.cdReferencia / cdFonteReferencia    <-- MtmAnbima (candidatos NTN-B / DI1)
      |
      v (calc_spread_over.py)
  NegociosProcessados.vrSpreadOver      <-- MtmAnbima (vrTaxa, match exato de data)
      |
      v (gerar_relatorio_html.py)
  HTML  <-- AnbimaIndicativos (taxa indicativa informativa)

[Anbima merc-sec títulos públicos] --> MtmAnbima (NTN-B)
[B3 curva pré × DI]               --> MtmAnbima (DI Futuro)
```
