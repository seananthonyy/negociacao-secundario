# scrape_anbima_data_ativos — Pendências e diagnóstico (30/06/2026)

> Itens abertos do primeiro teste real do modo incremental. **Ler antes de mexer no script.** Ver [[10 - Scripts/scrape_anbima_data_ativos]] e [[11 - Pipeline de Execucao]].

## Contexto
Primeira execução real do modo incremental (`--start 2026-06-25 --end 2026-06-29`). Resultado:
`scrappados 18 · inseridos 267 · erros 657 · fluxo_ok 50 · fluxo_skip 217 · descartados 6 · info_faltante [] · skip_list 0`.

## Diagnóstico: o scraper FUNCIONA
- Os **657 "características não capturadas"** são, na maioria, **CRIs de código numérico recente (2024/2025) que genuinamente NÃO estão na Anbima** (confirmado pelo usuário). Não é bug de scraping.
- Escopo: dos 746 CRI/CRA negociados p/ liq 29/06, **494 (66%) têm info completa** em InfoAtivos; **252 (34%) estão totalmente ausentes** (ex.: `25K3796700`, `24K1425359`) — esses são os que falham.
- **Prova de que o scraper funciona:** `13F0056986` (CRI da TSC Jaraguá do Sul Garden Shopping) — rodado com instrumento correto **CRI**, captura tudo: IPCA+6%, VNE 400.000, venc. 2035-12-05, início rent. 2018-10-05, devedor TSC; só `isin`/`quantidade_emitida` vêm None (genuinamente ausentes na Anbima p/ esse ativo). No banco ele já tinha 50 eventos de fluxo do full load (21/06).

## 🔴 BUG 1 — `_InferTipo` erra CRI/CRA de código numérico (A CORRIGIR)
`_InferTipo('13F0056986')` retorna **`DEB`** (código numérico não começa com CRI/CRA) → scrape vai p/ `…/debentures/…` em vez de `…/certificado-de-recebiveis/…` → "características não capturadas".
- **Afeta:** o modo `--ticker` (debug) E o **fallback de `_GetTickersFromAnbima`** (ticker que vem só da Anbima, sem `cdInstrumento` em InfoAtivos/NegociosBrutos). Um CRI numérico nessa situação é raspado como DEB e falha indevidamente.
- **Regra decidida com o usuário:** se começa com `CRA` → CRA; senão, se **começa com dígito → CRI**; senão (começa com letra) → DEB.
- **Usuário acha que comprimento pode ser mais robusto** (CRI/CRA têm tamanho padronizado; DEB ele não tem certeza). **PENDENTE:** rodar análise no banco antes de finalizar — agrupar `SELECT DISTINCT cdTicker, cdInstrumento FROM NegociosBrutos` por `cdInstrumento` e ver distribuição de `len(cdTicker)` e prefixo (CRA / dígito / letra). [A query foi montada mas não chegou a rodar — o usuário estourou o limite.]
- **Além disso:** no fluxo incremental, **preferir sempre o `cdInstrumento` autoritativo de `NegociosBrutos`** quando existir; `_InferTipo` só como último recurso. No `--ticker`, considerar um override `--instrumento`.

## 🔴 BUG 2 — `--force` destrutivo (A CORRIGIR — usuário pediu "arruma")
No worker: `if args.force and json_path.exists(): json_path.unlink()` roda **ANTES** do scrape. Se o scrape falha, o checkpoint JSON é **perdido** (foi o que apagou `13F0056986.json`). O dado no DB sobrevive (o --force não toca no banco).
- **Fix:** só sobrescrever/`unlink` o checkpoint **após** scrape bem-sucedido (escrever em arquivo novo e substituir, ou só apagar depois de capturar `info`).

## Observações / melhorias menores
- A completude "qualquer campo NULL" mantém `13F0056986` (e os ~120 com `cdISIN`/`vrQuantidadeEmissao` genuinamente ausentes) **na fila toda execução** → re-scrape recorrente. Mitigação prevista: skip-list (`anbima_skip_tickers.csv`). Quando esses são raspados com sucesso, aparecem em `info_faltante` no email → usuário adiciona ao CSV.
- **Gap:** tickers genuinamente ausentes na Anbima (os 252) **falham o scrape inteiro** → NÃO entram em `info_faltante` (só os que raspam mas têm campo NULL entram). Logo o email não os sugere p/ skip-list. Enhancement possível: reportar também os que falham scrape (ou jogá-los na skip-list automaticamente após N falhas).
- Os **217 `evento_desconhecido`** (fluxo com evento que o parser não conhece) persistem InfoAtivos mas nunca geram FluxoAtivos → re-scrape recorrente por design.

## ✅ BUG 3 — agenda (FluxoAtivos) truncada em 100 eventos (CORRIGIDO)
Descoberto ao re-raspar `13F0056986` (284 eventos): só 100 eram capturados. **Causa raiz** (diagnosticada): (a) a API de agenda **capa o `size` em 100** por página (o rewrite p/ 500 não ajudava); (b) o `route_agenda` **forçava `page=0`** em todo request → só a 1ª página vinha; (c) replay direto via `page.request.get` dá **401** (API exige JWT de sessão reCAPTCHA-bound) — só funciona via o XHR do próprio SPA.
- **Fix aplicado em `_ScrapeAgenda`:** `SIZE_AGENDA` 500→**100**; `route_agenda` deixou de forçar `page=0` (só eleva o size); novo loop navega `…/agenda?page=0,1,2…` até juntar `total_elements`, com teto `needed = ceil(total/100)+1`, `MAX_AGENDA_PAGES=50` e parada por progresso (2 páginas seguidas sem novidade). Navegar `?page=N` dispara novo XHR (confirmado).
- **Validado:** `13F0056986` agora sem aviso "incompleta"; capturou os 284 eventos → **142 linhas em FluxoAtivos** (antes 100). Tickers com ≤100 eventos seguem em 1 página (sem custo extra).

## Status (atualizado 30/06, fim da sessão)
- ✅ **BUG 1 (`_InferTipo`) CORRIGIDO.** Regra nova: `CRA`→CRA; `[0]` dígito→CRI; senão DEB. Validado: `13F0056986`/`24K1425359`/`12E0025287`→CRI, `AALM12`/`VERT18`→DEB, `CRA*`→CRA. Padrões confirmados no banco (DEB len6/letra, CRI len10/dígito, CRA len11/'CRA').
- ✅ **BUG 2 (`--force` destrutivo) CORRIGIDO.** Removido o `unlink()` pré-scrape; o `write_text()` sobrescreve só no sucesso. Checkpoint de `13F0056986` recriado.
- ✅ **BUG 3 (agenda truncada em 100 eventos) CORRIGIDO** — paginação em `_ScrapeAgenda` (size=100 + loop `?page=N`). Validado: `13F0056986` → 284 eventos → 142 linhas FluxoAtivos.
- Validação pós-fix: `--ticker 13F0056986` → scrappados 1, erros 0, fluxo_ok 1, `info_faltante=[('13F0056986','CRI',['cdISIN','vrQuantidadeEmissao'])]` (skip-list workflow OK). `py_compile` OK.
