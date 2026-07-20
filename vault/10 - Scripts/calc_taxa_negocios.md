# calc_taxa_negocios.py

Arquivo: `code/scripts/calc_taxa_negocios.py`
Dependências de lib: [[10 - Scripts/libs#lib/db.py|db]], [[10 - Scripts/libs#lib/config.py|config]], [[10 - Scripts/libs#lib/logger.py|logger]], [[10 - Scripts/libs#lib/email_outlook.py|email_outlook]], [[10 - Scripts/libs#lib/fianalytics_api.py|fianalytics_api]], [[10 - Scripts/libs#lib/b3_calc_api.py|b3_calc_api]]

> ✅ **LIGADA em 15/07/2026.** A **calculadora local** é o degrau 2 da cascata (depois da taxa direta do boletim), **ON** (`config.toml [calc] usarCalcTaxa = true`) para **CDI+/IPCA/PREFIXADO** validados. %CDI e não-validados seguem em FI→B3. Confiança garantida pelo gate [[10 - Scripts/validar_calc_b3|validar_calc_b3]]. Ver seção "A calc local" abaixo.

## O que faz

Para cada trade ativo em `NegociosBrutos` com `dtLiquidacao` na janela informada, calcula `vrTaxaCalculada` e grava o resultado em `NegociosProcessados` via UPSERT. Trades com `cdSituacao = 'Cancelado'` são ignorados.

## A calc local (degrau 2, LIGADA)

**Cascata atual:** `vrTaxaNegocio` (direto do boletim) → **Calc local** → FI Analytics → B3 → NULL.

A calc entra em ativo com `stFluxoValidado = 1` **e** `cdIndexador` em `[calc].indexadores` (`["CDI+","IPCA","PREFIXADO"]`), **quando `[calc].usarCalcTaxa = true`** (ON desde 15/07).

**Confiança (gate `validar_calc_b3`):** `stFluxoValidado=1` só vale quando a calc **reproduz a B3 (ou FI)** em PU — o gate desvalida quem não bate (ver [[10 - Scripts/validar_calc_b3]]). Por isso ligar é seguro por construção: a calc só precifica o que confiamos.

**Por que %CDI ficou de fora:** a calc reproduz o **PU par** mas erra a **taxa fora do par** no %CDI (o desconto do multiplicador de CDI). CDI+/IPCA/PREFIXADO reproduzem a B3 quase perfeitamente (medido em trades reais); só o %CDI tem cauda (~2 bps em yield, mas o número medido em pontos de %CDI enganava). Ver [[../98 - Backlog]] e [[../14 - Rotinas da Calculadora]].

**Performance:** a calc é **CPU-bound** (GIL → `ThreadPoolExecutor` não paraleliza), mas foi **otimizada ~100× em 19/07** (memoização do Newton em `calculadora_rf.py`: IPCA 3,4s→34ms, CDI+ 2,8s→28ms, bit-idêntico). Com o cache por `(cdTicker, dtLiquidacao, vrPU)`, o passo fecha em poucos minutos. **Impacto medido:** ~60% dos trades não-diretos vêm da calc local (vs API) → ~70% menos chamada de API no `calc_taxa`.

## A cascata original (API)

O cálculo segue uma **cascata**:

```
1. vrTaxaNegocio do boletim B3 não nulo → copia direto (cdFonteTaxa = NULL, coluna "Direta" no email)
2. FI Analytics (CalcRate) → cdFonteTaxa = 'FiAnalytics'
3. B3 Calculator (CalcYield) → cdFonteTaxa = 'B3'
4. Falhou tudo → vrTaxaCalculada = NULL, cdFonteTaxa = NULL
```

Só chega nos níveis 2 e 3 quando o boletim B3 não trouxe `vrTaxaNegocio` preenchido para aquele trade. Detalhes de cada calculadora: [[06 - Calculadoras/FI Analytics API]] e [[06 - Calculadoras/B3 Calculator API]].

O que significa **"Direta"** no resumo do email: o trade já veio com `vrTaxaNegocio` preenchido no boletim B3. A taxa foi copiada diretamente para `vrTaxaCalculada` sem nenhuma chamada de API. `cdFonteTaxa = NULL` para esses trades.

## Como rodar

```powershell
# A partir de code/ como diretório de trabalho
python scripts/calc_taxa_negocios.py --date 2026-05-29
python scripts/calc_taxa_negocios.py --start 2026-05-01 --end 2026-05-29
python scripts/calc_taxa_negocios.py --date 2026-05-29 --workers 16
python scripts/calc_taxa_negocios.py --date 2026-05-29 --force
```

## Parâmetros CLI

| Flag | Tipo | Padrão | Descrição |
|---|---|---|---|
| `--date` | `YYYY-MM-DD` | — | Data única a processar. Mutuamente exclusivo com `--start`. |
| `--start` | `YYYY-MM-DD` | — | Início do intervalo de `dtLiquidacao`. Requer `--end`. |
| `--end` | `YYYY-MM-DD` | — | Fim do intervalo (inclusive). Requer `--start`. |
| `--workers` | inteiro | `8` | Número de threads paralelas para chamadas de API. |
| `--force` | flag | `False` | Recalcula taxa mesmo para trades que já têm `vrTaxaCalculada IS NOT NULL` no banco. |

## Re-run inteligente (cache de banco)

Na ausência de `--force`, o script consulta `NegociosProcessados` antes de iniciar o pool de threads e separa os trades em dois grupos:

- **Cached:** trades com `vrTaxaCalculada IS NOT NULL` — mantidos como estão, sem chamada de API.
- **A processar:** trades com `vrTaxaCalculada IS NULL` (ou ausentes de `NegociosProcessados`) — enviados ao pool.

Isso significa que trades que já foram calculados com sucesso em rodadas anteriores nunca geram chamadas de API desnecessárias. Trades que falharam (`NULL`) numa rodada anterior são automaticamente re-tentados na próxima rodagem sem `--force`.

Com `--force`, o cache é ignorado e todos os trades são recalculados.

## Paralelismo

As chamadas de API rodam em `ThreadPoolExecutor(max_workers=workers)`. Cada trade a processar vira um `Future` que executa `_ApplyCascata(trade, log)`.

```python
with ThreadPoolExecutor(max_workers=workers) as executor:
    future_to_trade = {executor.submit(_ApplyCascata, t, log): t for t in to_process}
    for future in as_completed(future_to_trade):
        ...
```

Os UPSERTs no SQLite permanecem **sequenciais**, executados após o pool completar (fase separada). Isso evita contention de escrita no banco entre threads.

Os módulos `lib/fianalytics_api` e `lib/b3_calc_api` são thread-safe — ver [[10 - Scripts/libs#lib/fianalytics_api.py|fianalytics_api]] e [[10 - Scripts/libs#lib/b3_calc_api.py|b3_calc_api]].

## Progresso em tempo real

Cada trade que completa via `as_completed` gera uma linha de log no formato:

```
[N/M] TICKER → origem taxa% | Xs elapsed | ~Xs restante
```

O ETA é estimado pelo ritmo médio dos trades já concluídos (só exibido a partir do segundo trade). Exemplo:

```
[3/47] ISAEC2 → FiAnalytics 6.7046% | 4s elapsed | ~55s restante
[4/47] DEBA11 → direta 8.1200% | 5s elapsed | ~51s restante
[5/47] XYZC14 → sem taxa None | 5s elapsed | ~48s restante
```

## Alertas de trades sem taxa

Trades com `vrTaxaCalculada = NULL` e `vrVolume >= cfg["alerta"]["volumeMinSemTaxa"]` (atualmente R$ 5.000.000) aparecem como tabela destacada no corpo do email, ordenados por volume decrescente.

O threshold `volumeMinSemTaxa` já existia em `config.toml` na seção `[alerta]`. Trades sem taxa abaixo do threshold também são contados no resumo (coluna `SemTaxa`), mas não geram alerta no email.

Exemplo de seção de alerta no email:

```
========================================================
ALERTAS — sem taxa calculada, volume >= R$ 5.000.000:
  Data          Ticker        Emissor                 Volume (R$)
  ------------------------------------------------------------------
  2026-05-29    BPAC11        Banco BTG Pactual    25.000.000,00
  2026-05-29    VALE14        Vale S.A.             8.500.000,00
========================================================
```

## UPSERT em NegociosProcessados

```sql
INSERT INTO NegociosProcessados (
    idTrade, cdTicker, cdEmissor, dtNegocio, dtLiquidacao,
    vrQuantidade, vrPU, vrVolume, vrTaxaCalculada, cdFonteTaxa,
    vrDuration, vrSpreadOver, idGrupoNegocio, cdStatus, dtProcessamento
) VALUES (...)
ON CONFLICT(idTrade) DO UPDATE SET
    vrTaxaCalculada = excluded.vrTaxaCalculada,
    cdFonteTaxa    = excluded.cdFonteTaxa,
    dtProcessamento   = excluded.dtProcessamento
```

Campos que ainda não existem nesta fase (`vrDuration`, `vrSpreadOver`, `idGrupoNegocio`) são inseridos como `NULL`. `cdStatus` é fixado como `'VALIDO'` — o filtro de qualidade ([[07 - Filtro de Duplicados]]) atualiza esse campo depois para `FUNDO`, `BROKER` ou `PF` conforme o caso.

Trades **cached** (não precisaram de API call) não geram UPSERT — já estão corretos no banco.

## Logs e email

- Logs em: `data/logs/calc_taxa_negocios/{YYYY-MM-DD_HHMMSS}.log` — um arquivo por rodagem.
- Nível `INFO` no console (inclui linha de progresso por trade), `DEBUG` no arquivo (inclui cada chamada de API tentada).
- Email ao final via [[10 - Scripts/libs#lib/email_outlook.py|email_outlook]] — assunto `[OK] calc_taxa_negocios` ou `[ERROR] calc_taxa_negocios`.
- O corpo do email inclui tabela de resumo por `dtLiquidacao` + seção de alertas (se houver).

Exemplo de resumo no email:

```
Resultado por dtLiquidacao:

Data          Total   Direta  FIAnaly     B3  SemTaxa
--------------------------------------------------------
2026-05-29       47       12       28      5        2
--------------------------------------------------------
TOTAL            47       12       28      5        2

ATENCAO: 2 trade(s) sem taxa calculada (vrTaxaCalculada = NULL).
```

## Dependências

| Módulo | Uso |
|---|---|
| `lib/db.py` | `get_db()` — abre banco e garante schema |
| `lib/config.py` | `cfg["alerta"]["volumeMinSemTaxa"]` — threshold de alerta |
| `lib/logger.py` | `get_logger("calc_taxa_negocios")` — log em arquivo + console |
| `lib/email_outlook.py` | `send_completion_email()` — notificação ao fim |
| `lib/fianalytics_api.py` | `CalcRate()` — primeiro nível da cascata |
| `lib/b3_calc_api.py` | `CalcYield()` — segundo nível da cascata |

Para detalhes de banco: [[04 - Banco de Dados]].

## Decisões de implementação

**Decisão (31/05/2026) — Thread safety em libs de API:** `b3_calc_api` e `fianalytics_api` usam `threading.Lock()` com double-check locking nos pontos de estado global (`_bearerToken` e `_userBondsFetched`). Necessário para uso correto com `ThreadPoolExecutor` — garante que apenas uma thread faz login ou fetch de bonds mesmo com várias disparando simultaneamente.

**Decisão (31/05/2026) — Paralelismo com UPSERTs sequenciais:** chamadas de API são paralelizadas via `ThreadPoolExecutor`; UPSERTs permanecem sequenciais após o pool. SQLite não suporta escritas concorrentes sem WAL, e mesmo com WAL o overhead de lock/unlock por trade seria maior que o ganho. O gargalo de tempo está nas chamadas de API, não nos UPSERTs.

**Decisão (31/05/2026) — Re-run inteligente via cache de banco:** trades com `vrTaxaCalculada IS NOT NULL` em `NegociosProcessados` não geram chamada de API em re-runs. Isso permite re-executar o script para adicionar trades novos de uma data sem refazer chamadas custosas para os que já foram calculados. `--force` existe para o caso de querer forçar recalcular tudo (ex: mudança de parâmetros das APIs).

**Decisão (31/05/2026) — Alertas por volume, não por quantidade:** o threshold de alerta é em volume financeiro (`vrVolume >= volumeMinSemTaxa`), não em contagem de trades. Um trade de R$ 50M sem taxa é mais crítico do que dez trades de R$ 10k cada.

**Decisão (31/05/2026) — cdFonteTaxa = NULL para taxa direta:** trades que chegam com `vrTaxaNegocio` preenchido têm `cdFonteTaxa = NULL` em `NegociosProcessados` (não `'Direto'` ou similar). O campo `NULL` significa "sem chamada de API — veio direto do boletim". Essa distinção é documentada no label "Direta" do resumo do email.
