# Auditoria pré-migração (30/06/2026)

> Revisão geral do código antes de migrar pro ambiente do banco (Itaú BBA). Itens A–F são **decisões pendentes do usuário**. Ver [[98 - Backlog]] (item Migração).

## ✅ Já executado
- **Apagado `templates/relatorio_credito.html`** — template morto, zero referências (substituído por `relatorio_secundario.html` há tempos). **Atenção: projeto NÃO é repo git — deleção é permanente.**
- **(01/07/2026 — Fase 0 da migração, ver [[13 - Migracao Banco]] e [[09 - Progresso]]):**
  - **Item A — `dump_api_samples.py`: DELETADO.** Único arquivo com segredo hardcoded, fora do pipeline.
  - **Item B — 8 imports mortos: REMOVIDOS.** (`os` em logger; `field`+`send_completion_email` em gerar_relatorio_html; `Route` em scrape_b3_boletim; `urllib.parse` em scrape_anbima_data_ativos; `datetime` em scrape_anbima_ntnb; `statistics` em gerar_relatorio_credito.)
  - **Item C — gap de email no `gerar_relatorio_html`: RESOLVIDO** removendo o import morto (o relatório diário não é o caminho padrão; não passou a mandar `[OK]/[ERROR]`).
  - **Item E — nomes: RESOLVIDO** (`tipoDe`→`_TipoDe`, `_colIdx`→`_ColIdx`). Closures Playwright mantidas.
  - **Segredos:** movidos para resolver `get_secret` via `[env]` + `destinatarios.py`; `check_no_secrets.py` criado. Ver [[13 - Migracao Banco]] §3-§4.
- **Item D (API lib em snake_case): MANTIDO** por decisão (idiomático, usado por todos os scripts).
- **Item F (nota PENDENCIAS): pendente** de fusão na nota principal.

## ✅ Confirmações (verificado, está OK)
- **Outstanding → gráfico Visão Anbima:** fiação correta. `_PESO_CTE` em `gerar_relatorio_credito.py` puxa de `Outstanding` (date-matched `dtPeso = ai.dtReferencia`); os 3 gráficos da Visão Anbima ponderam por ele. ⚠️ Tabela `Outstanding` **vazia no PC pessoal** (sem Bloomberg) → gráfico sem dados aqui; **popular no banco** via `scrape_outstanding_bloomberg.py`. Sem fallback (por design).
- **Segredos hardcoded:** SÓ em `dump_api_samples.py` (`FIA_API_KEY`, `B3_TOKEN_RAW`). Todo o resto (libs `fianalytics_api`/`b3_calc_api`, scrapers) lê do `.env` via `get_env`. ✅
- **Sem TODO/FIXME/HACK** no código.
- **Sem funções mortas internas.** Detector AST cross-file apontou só `get_readonly_connection` (lib/db.py), que é **intencional** — API pública pro add-in externo da calculadora, não chamada pelo Python. Manter.

## 🟡 Decisões pendentes (A–F)

**A. `scripts/dump_api_samples.py`** — utilitário de debug, fora do pipeline, com `FIA_API_KEY`/`B3_TOKEN_RAW` em texto plano. Único foco de segredo. → **Recomendo deletar** (ou mover segredos pro `.env` se quiser manter pra debug). Relevante p/ migração ao banco.

**B. 9 imports não usados** (remoção trivial, zero risco):
| Arquivo | Import morto |
|---|---|
| `lib/logger.py` | `os` |
| `scripts/gerar_relatorio_credito.py` | `statistics` |
| `scripts/gerar_relatorio_html.py` | `field` (de dataclasses), `send_completion_email` |
| `scripts/scrape_anbima_data_ativos.py` | `urllib.parse` |
| `scripts/scrape_anbima_ntnb.py` | `datetime` |
| `scripts/scrape_b3_boletim.py` | `Route` (playwright) |
| `scripts/dump_api_samples.py` | `sys`, `os` |

**C. Gap de email no `gerar_relatorio_html.py`** — importa `send_completion_email` mas **nunca chama função de email**. O **relatório diário não manda email `[OK]/[ERROR]`** como todos os outros scripts. → Decisão: adicionar o email (consistência) OU só remover o import.

**D. API do `lib/` em snake_case** (`get_db`, `get_logger`, `get_connection`, `get_readonly_connection`, `bootstrap`, `send_completion_email`, `send_html_email`, `get_env`, `_CfgProxy.get`) — viola a convenção "funções PascalCase", mas é API estável usada por TODOS os 15 scripts. → **Recomendo manter** (idiomático, churn grande/arriscado pré-migração) ou padronizar com cuidado.

**E. Nomes menores:** `gerar_relatorio_html.py:366 def tipoDe(` (camelCase → deveria `_TipoDe`); closures Playwright em `scrape_anbima_data_ativos.py` (`on_resp`, `route_listing`, `route_agenda`) em snake_case (handlers locais — aceitável). → Padronizar ou aceitar.

**F. Nota temporária `10 - Scripts/scrape_anbima_data_ativos_PENDENCIAS_30-06`** — os 3 bugs já estão corrigidos. → Fundir o conteúdo na nota principal `10 - Scripts/scrape_anbima_data_ativos` e apagar a temp (o nome "PENDENCIAS" sugere coisa aberta).

## Escopo (honestidade)
**Coberto:** imports não usados (AST), funções mortas (AST cross-file), segredos hardcoded, TODOs, nomes de função, fiação do outstanding, templates usados/mortos.
**NÃO coberto (3ª passada, se quiser):** revisão linha-a-linha da lógica SQL de cada script; auditoria exaustiva de nomes de *variáveis* locais (camelCase).

## Próximo passo
Usuário decide quais de A–F executar. Nenhum item A–F foi aplicado ainda (só a deleção do template morto).
