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
