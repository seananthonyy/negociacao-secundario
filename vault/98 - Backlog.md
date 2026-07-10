# Backlog — Itens para Decisão Futura

---

> Itens que identificamos durante o desenvolvimento mas que não têm prioridade imediata ou precisam de mais contexto antes de implementar. Revisar periodicamente.

---

## Migração e instalação no ambiente do Banco (Itaú BBA)

**Origem:** 20/06/2026.

**O que é:** planejar a migração do projeto do PC pessoal para o ambiente corporativo (PC trabalho / rede do banco). Inclui: instalação de dependências (Python, Playwright, pacotes), ajuste de paths, configuração de variáveis de ambiente (.env), acesso às fontes (B3, Anbima, FI Analytics — verificar se há bloqueios de proxy/firewall), permissões de Outlook (pywin32), e possível ajuste de credenciais.

**Decisão pendente:** levantar restrições do ambiente (proxy, Python disponível, permissões de instalação, etc.) antes de planejar os passos.

---

## Auditar falha silenciosa nos demais scrapers (exit 0 gravando 0 linhas)

**Origem:** 08/07/2026, depois do bug do `scrape_anbima_cri_cra` (ver [[09 - Progresso]]).

**O problema (padrão, não instância):** o `scrape_anbima_cri_cra` ficou **6 pregões gravando 0 linhas com exit 0**. O padrão que permitiu isso: `except` que loga WARNING e retorna `None`/`0,0`, `Main` que nunca olha o total gravado, `success=True` incondicional. O `run_step` do `pipeline_core` só olha o exit code → reportava `[OK]`.

**O que fazer:** varrer os outros scrapers procurando o mesmo padrão e aplicar a mesma regra:
- **0 linha gravada em TODAS as datas pedidas → exit 1** (scraper quebrado).
- **0 linha numa data específica → WARNING** + a lista do que a fonte realmente tem (é "não tem dado", não "quebrou").
- Seletor Playwright: **nunca por classe CSS** em portais com CSS-modules (Anbima Data e o portal de CRI/CRA já têm classes com hash que mudam a cada build). Usar `data-testid`/`data-cy`.

**Candidatos a auditar:** `scrape_anbima_debentures` (baixa XLS por URL previsível — 404 fora da janela é legítimo, mas e se o layout mudar?), `scrape_fianalytics_planilha`, `scrape_b3_boletim` (já tem fallback em cascata; conferir se o fallback pode retornar CSV vazio "com sucesso"), `scrape_anbima_ntnb`, `scrape_b3_curva_di`.

**Reforço barato já implementado:** os 3 notebooks (`setup_teste`, `setup_inicial`, `run_secundario`) conferem no `.db`, por fluxo, se o que era pra ser gravado foi gravado — foi exatamente isso que pegou este bug.

---

## Calcular duration de corporates para fechar o gap do match_referencias

**Origem:** 06/07/2026. **Prioridade: vamos implementar.**

**O problema:** hoje a `vrDuration` de deb/CRI/CRA é **lida direto do XLS da Anbima** — nunca calculada. Ativo que negocia mas **não aparece no indicativo Anbima** fica sem duration → sem `cdReferencia` no `match_referencias` → sem spread. É um buraco permanente na `InfoAtivos`.

**A ideia:** fazer pro corporate o que já fazemos pro NTN-B (que **calcula** duration via cascata `FIA /gov/govbondcalculator → B3 CalcPuGov → NULL`). Para ativo sem ref mas com `cdIndexador` + `FluxoAtivos` + `vrTaxaEmissao` (+ `dtVencimento`, `vrVNE`), **calcular a duration** — descontando na **`vrTaxaCalculada`** que o `calc_taxa` já produziu (yield do PU negociado) — salvar em `InfoAtivos.vrDuration`, e deixar o `match_referencias` casar normalmente.

**Três caminhos (do mais barato ao mais caro):**
- **A) Piggyback no `calc_taxa`** — o `_CallBondbuilder` do FI Analytics **já é chamado** pra corporates e devolve JSON. Se esse JSON já traz `maculayDuration` (como o `govbondcalculator` traz), basta **ler o campo hoje ignorado** e fazer UPSERT em `vrDuration`. Zero chamada nova.
- **B) Cascata dedicada** — `_GetDurationCorp(...) → FIA bondbuilder → B3 → NULL`, rodada só pros ativos com `vrDuration IS NULL`. Mais código, desacoplado.
- **C) Calcular em Python do fluxo** (day-count 252) — evitar; `FluxoAtivos` pode não ter todas as datas de cupom.

**Próximo passo (decide entre A e B):** inspecionar uma **resposta real do `_CallBondbuilder`** e ver se `maculayDuration` está no payload. Se sim → caminho A.

**Ressalvas:** duration sai as-of a data do trade (`dtAtualizacaoDuration` = essa data → precisa da curva NTN-B/DI raspada nela, mesma dependência de hoje); pra `%CDI`/floaters a duration é aproximada, mas basta pro match de vértice mais próximo. Ver [[project_match_referencias]] na memória.

---

## Importar histórico de taxas (Anbima, NTN-B/DI, DI projetado) de planilhas existentes

**Origem:** 06/07/2026.

**O que é:** planejar como **importar histórico já existente em planilhas** (Excel/CSV que o usuário mantém) para dentro do `trades.db`, cobrindo período **anterior** ao que os scrapers alcançam. As fontes online guardam janela curta (deb/NTN-B ~4 meses; curva DI ~20 pregões; CRI/CRA ~5 pregões), então o histórico profundo só entra por importação manual das planilhas.

**Três conjuntos a importar:**
- **Taxas indicativas Anbima** (deb/CRI/CRA) → alvo `AnbimaIndicativos` (`cdTicker`, `dtReferencia`, `vrTaxaAnbima`, ...).
- **NTN-B / DI (MtM)** → alvo `MtmAnbima` (curvas de referência por `dtReferencia`).
- **DI projetado histórico** → definir onde grava (provavelmente `MtmAnbima` com os tickers `DI1F..`, ou tabela nova se a semântica de "projetado" divergir do MtM raspado).

**Decisões pendentes (planejar antes de codar):**
- Levantar o **layout real** de cada planilha (abas, colunas, formato de data BR, unidade da taxa — % a.a. vs decimal).
- Mapear colunas da planilha → colunas das tabelas destino; garantir **UPSERT idempotente** (não duplicar com o que os scrapers já trouxeram; scraper x planilha — quem prevalece na sobreposição?).
- Script único de importação (`importar_historico_planilhas.py`?) com `--tipo anbima|mtm|di-proj` e `--arquivo`, ou um por fonte.
- Conferir se o **DI projetado** casa com a chave/semântica de `MtmAnbima` ou precisa de coluna/tabela própria.

**Por que importa:** destrava spread histórico profundo (match de referência precisa de NTN-B/DI na data do trade) e relatórios cobrindo período longo, sem depender da janela curta das fontes online.

