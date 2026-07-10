# Mapa de Renomeação PT-BR

> Artefato de revisão para a padronização PT-BR (item de backlog). **Nada é executado antes de aprovação.** Decisões fechadas: DB renomeado por completo (usuário ajusta o add-in externo depois); jargões de mercado em EN mantidos (`duration`, `spread`, `status`, `broker`, siglas).

## Decisões já tomadas
- ✅ Renomear o DB inteiro; entregar lista de colunas trocadas para o usuário atualizar o add-in.
- ✅ Manter jargões EN: `duration`, `spread` (e `spreadOver`), `status`, `broker`, e siglas (`PU`, `VNE`, `ISIN`, `Mtm`).
- ✅ `vrRate → vrTaxa` é tradução normal (não é jargão).

---

## 1. Tabelas

| Atual | Proposto | Nota |
|---|---|---|
| `TradesRaw` | **`NegociosBrutos`** | |
| `TradesProcessed` | **`NegociosProcessados`** | |
| `InfoAtivos` | manter | já PT |
| `AnbimaIndicativos` | manter | já PT |
| `MtmBloomberg` | manter | Mtm = jargão (marcação a mercado); Bloomberg nome próprio |
| `MtmAnbima` | manter | idem |
| `FluxoAtivos` | manter | já PT |
| `Outstanding` | manter ⚠️ | jargão de mercado — ou `Estoque`? (decisão pendente) |

## 2. Colunas (só as com nome EN; as já-PT não mudam)

**Transversais (várias tabelas):**
| Atual | Proposto |
|---|---|
| `dtCreatedAt` | **`dtCriacao`** |
| `dtUpdatedAt` | **`dtAtualizacao`** |
| `dtProcessedAt` | **`dtProcessamento`** |

**`NegociosBrutos` (ex-TradesRaw):**
| Atual | Proposto | Nota |
|---|---|---|
| `cdIsin` | **`cdISIN`** | padroniza com InfoAtivos (sigla em caixa alta) |

**`NegociosProcessados` (ex-TradesProcessed):**
| Atual | Proposto | Nota |
|---|---|---|
| `cdTaxaSource` | **`cdFonteTaxa`** | |
| `idTradeGroup` | **`idGrupoNegocio`** | |
| `vrSpreadOver` | manter | jargão |
| `vrSpreadOverAnbima` | manter | jargão |
| `cdStatus` | manter | jargão |
| `vrDuration` | manter | jargão |

**`InfoAtivos`:**
| Atual | Proposto | Nota |
|---|---|---|
| `cdRef` | **`cdReferencia`** | |
| `cdRefSource` | **`cdFonteReferencia`** | |
| `vrEmissionRate` | **`vrTaxaEmissao`** | |
| `dtStartProfitability` | **`dtInicioRentabilidade`** | |
| `dtUpsertDuration` | **`dtAtualizacaoDuration`** | Upsert→Atualizacao; Duration mantido |
| `vrVNE` / `cdISIN` / `vrQuantidadeEmissao` / `dtEmissao` / `dtVencimento` / `cdIndexador` | manter | siglas/PT |

**`MtmBloomberg` / `MtmAnbima`:**
| Atual | Proposto |
|---|---|
| `vrRate` | **`vrTaxa`** |
| `dtMtmBloomberg` / `vrDuration` | manter |

**`FluxoAtivos`:**
| Atual | Proposto |
|---|---|
| `dtEvent` | **`dtEvento`** |
| `vrPctAmortization` | **`vrPctAmortizacao`** |
| `vrPctIncorporation` | **`vrPctIncorporacao`** |

**`Outstanding`:** `dtOutstanding` / `vrOutstanding` → manter (se a tabela ficar `Outstanding`).

## 3. Índices
Renomear junto com as tabelas/colunas (ex.: `idxTradesRawDtNegocio → idxNegociosBrutosDtNegocio`). Mecânico, segue o map acima.

## 4. Código Python
- **Variáveis/funções que espelham colunas** seguem o map (ex.: var `dtCreatedAt` → `dtCriacao`).
- **Padrão de código (parte "deixar no nosso padrão"):** `scrape_b3_boletim.py` tem funções em snake_case (`parse_args`, `main_async`, `_parse_csv`, `_upsert_rows`) — escrito antes da convenção. Converter para PascalCase (era o TODO da F12).
- **Jargão camelCase já aceito** pela convenção do CLAUDE.md (`anbimaRows`, `totalAnbima`) — mantido como está, salvo decisão contrária.

## 5. Templates (Jinja2 / JS)
`relatorio.html.j2`, `relatorio_secundario.html`, `relatorio_credito.html` referenciam algumas chaves de payload/colunas — ajustar as que casam com nomes trocados. Baixo volume (3-4 ocorrências cada).

## 6. Vault
25 notas mencionam os nomes antigos — atualizar via documenter após o código estar verde.

---

## ✅ Decisões finais (fechadas 29/06/2026)
1. **`Outstanding`** — MANTIDO como jargão (tabela e colunas `dtOutstanding`/`vrOutstanding`).
2. **VALORES de dados** — NÃO traduzidos. `cdStatus`='BROKER', `cdFonteReferencia`='MatchRef'/'FiAnalytics', `cdSituacao`='Cancelado' ficam como tokens internos. Só nomes de tabelas/colunas/identificadores mudaram.
3. **Variáveis Python** — jargão camelCase EN sancionado pelo CLAUDE.md (`anbimaRows`, `totalAnbima`) MANTIDO; só variáveis que espelham colunas seguem o map.

## Status de execução (29/06/2026)
- ✅ Backup do `trades.db` → `data/trades_backup_pre_ptbr_20260629.db`.
- ✅ Migração do schema executada e validada (contagens == baseline; `foreign_key_check` limpo; índices recriados; `sqlite_stat1` dropado p/ re-ANALYZE).
- ✅ Refatoração do código: 19 arquivos (lib/db.py + 15 scripts + 3 templates), 356 substituições. F12 feito (15 funções de `scrape_b3_boletim.py` → PascalCase). Grep de sobras vazio; `py_compile` ok; smoke do relatório geral limpo (13 pregões, 2.129 ativos, R$ 11.983,30 MM).
- ⏳ Pendente: atualização do restante do vault (documenter) + ajuste do add-in pelo usuário.

## 🔌 Lista para o add-in da calculadora (colunas que o add-in lê e MUDARAM)

O add-in usa `get_readonly_connection()` e lê `InfoAtivos` + `FluxoAtivos`. Trocas que afetam as queries do add-in:

**InfoAtivos:**
| Antiga | Nova |
|---|---|
| `cdRef` | `cdReferencia` |
| `vrEmissionRate` | `vrTaxaEmissao` |
| `dtStartProfitability` | `dtInicioRentabilidade` |
| `dtUpsertDuration` | `dtAtualizacaoDuration` |
| `dtUpdatedAt` | `dtAtualizacao` |

(Inalteradas e ainda válidas: `cdTicker`, `cdIndexador`, `dtVencimento`, `vrDuration`, `vrVNE`, `cdISIN`, `vrQuantidadeEmissao`, `dtEmissao`, `cdInstrumento`, `cdEmissor`.)

**FluxoAtivos:**
| Antiga | Nova |
|---|---|
| `dtEvent` | `dtEvento` |
| `vrPctAmortization` | `vrPctAmortizacao` |
| `vrPctIncorporation` | `vrPctIncorporacao` |
| `dtUpdatedAt` | `dtAtualizacao` |

**Queries de exemplo já atualizadas** (ver docstring de `get_readonly_connection` em `lib/db.py`):
```sql
SELECT cdIndexador, dtVencimento, vrTaxaEmissao, vrVNE,
       dtInicioRentabilidade, cdReferencia, vrDuration
FROM InfoAtivos WHERE cdTicker = ?;

SELECT dtEvento, vrPctAmortizacao, vrPctIncorporacao
FROM FluxoAtivos WHERE cdTicker = ? ORDER BY dtEvento;
```

---

## Plano de execução (após aprovação do map)
1. **Backup do `trades.db`** (cópia datada).
2. Migração do schema numa cópia: `ALTER TABLE ... RENAME TO` / `RENAME COLUMN` (SQLite 3.25+), recriar índices.
3. Atualizar `lib/db.py` (DDL + migrações + queries de exemplo do add-in).
4. Atualizar scripts um a um (lib → scripts → templates), validando import/sintaxe a cada um.
5. Rodar o **pipeline completo** numa data de teste + regerar o relatório geral → conferência visual.
6. Entregar a **lista de colunas trocadas** para o add-in.
7. Documenter atualiza o vault.
8. Backup antigo guardado até validação final.

---

# Fase 2 — Identificadores Python (08/07/2026, EXECUTADA)

> A padronização de 29/06 renomeou o **banco**. Esta renomeou o **código Python**: 1.937 tokens em 24 arquivos + 3 notebooks. Notas antigas do vault ainda citam os nomes velhos — use a tabela abaixo para traduzir.

## Regras aplicadas
- **Funções e classes:** PascalCase, em português, **sem `_` inicial**.
- **Variáveis e parâmetros:** camelCase, em português.
- **Constantes de módulo:** UPPER_SNAKE, sem `_` inicial (`_SQL_UPSERT` → `SQL_UPSERT`).
- **Ficam em inglês:** jargão de mercado (`vrSpreadOver`, `vrDuration`, `vrPU`, `cdISIN`, `Mtm*`, `Outstanding`, `Broker`, `Yield`), API de terceiros (`parse_args`, `status_code`), **nomes de arquivo** (`run_diario.py`, `pipeline_core.py`) e **chaves de contrato** (colunas do DB, `data_json` do template Jinja).
- `dest=` explícito no argparse quando a flag tem hífen (`--email-dia` → `dest="emailDia"`).

## API pública das libs

| Antes | Agora |
|---|---|
| `lib.db.get_db` | **`ObterBanco`** |
| `lib.db.get_connection` | **`ObterConexao`** |
| `lib.db.get_readonly_connection` | **`ObterConexaoLeitura`** |
| `lib.db.bootstrap` | **`Bootstrap`** |
| `lib.logger.get_logger` | **`ObterLogger`** |
| `lib.config.get_secret` | **`ObterSegredo`** |
| `lib.config.get_env` | **`ObterEnv`** |
| `lib.config.get_email_list` | **`ObterListaEmails`** |
| `lib.config.get_playwright_proxy` | **`ObterProxyPlaywright`** |
| `lib.email_outlook.send_completion_email` | **`EnviarEmailConclusao`** |
| `lib.email_outlook.send_html_email` | **`EnviarEmailHtml`** |
| `lib.b3_calc_api.CalcYield` | **`CalcularYield`** |
| `lib.b3_calc_api.CalcPuGov` | **`CalcularPuGov`** |
| `lib.fianalytics_api.CalcRate` | **`CalcularTaxa`** |

`lib.config.cfg` (o proxy lazy) **não mudou**.

## Fluxos do `pipeline_core`

`boletim`→**`Boletim`** · `anbima_deb`→**`AnbimaDeb`** · `anbima_cricra`→**`AnbimaCriCra`** · `fianalytics`→**`FiAnalytics`** · `anbima_data`→**`AnbimaData`** · `ntnb`→**`Ntnb`** · `curva_di`→**`CurvaDi`** · `outstanding`→**`Outstanding`** · `calc_taxa`→**`CalcTaxa`** · `filtrar`→**`Filtrar`** · `spread_anbima`→**`SpreadAnbima`** · `match_ref`→**`MatchRef`** · `spread_over`→**`SpreadOver`** · `relatorio`→**`Relatorio`**

Helpers: `dia_util_anterior`→**`DiaUtilAnterior`** · `ultimos_n_dias_uteis`→**`UltimosNDiasUteis`** · `dias_uteis_entre`→**`DiasUteisEntre`** · `run_step`→**`RodarPasso`** · `run_dia`→**`RodarDia`** · `run_ultimos_n`→**`RodarUltimosN`** · `run_intervalo`→**`RodarIntervalo`** · `run_setup`→**`RodarSetup`**

## Padrões comuns dos scripts CLI

`_ParseArgs`→**`LerArgumentos`** · `_ProcessDate`→**`ProcessarData`** · `_BuildDateRange`→**`MontarIntervaloDatas`** · `_BuildSummary`→**`MontarResumo`** · `Main`→**`Principal`** · `_MainAsync`→**`PrincipalAsync`** · `_DateStats`→**`EstatisticasData`** · `_SCRIPT_NAME`→**`NOME_SCRIPT`**

## Como foi feito (e por que é seguro)
Renomeação via `tokenize`, tocando **só tokens `NAME`** — strings (todo o SQL!), comentários e docstrings ficaram intactos por construção. Depois: `py_compile` nos 24 arquivos, checagem AST de nome-usado-mas-não-definido, `--help` nos 16 scripts, cadeia de cálculo real contra o `trades.db`, 3 scrapers reais (incl. Playwright), e **A/B do relatório**: HTML gerado pelo código pré-rename e pós-rename contra o mesmo banco saiu **byte a byte idêntico** (md5 `77c35a32…`).

**Dois bugs que só apareceram em runtime** (nenhum check estático pega): `args.email_dia` e `args.inicio_boletim`/`args.outstanding` deixaram de existir quando o `dest` do argparse não acompanhou o rename. Daí a regra do `dest=` explícito.
