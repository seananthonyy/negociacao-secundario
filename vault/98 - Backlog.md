# Backlog — Itens para Decisão Futura

---

> Itens que identificamos durante o desenvolvimento mas que não têm prioridade imediata ou precisam de mais contexto antes de implementar. Revisar periodicamente.

---

## Migração e instalação no ambiente do Banco (Itaú BBA)

**Origem:** 20/06/2026.

**O que é:** planejar a migração do projeto do PC pessoal para o ambiente corporativo (PC trabalho / rede do banco). Inclui: instalação de dependências (Python, Playwright, pacotes), ajuste de paths, configuração de variáveis de ambiente (.env), acesso às fontes (B3, Anbima, FI Analytics — verificar se há bloqueios de proxy/firewall), permissões de Outlook (pywin32), e possível ajuste de credenciais.

**Decisão pendente:** levantar restrições do ambiente (proxy, Python disponível, permissões de instalação, etc.) antes de planejar os passos.

---

## 🔴 BUG ABERTO — `scrape_fianalytics_planilha` quebrado (seletor do botão de download)

**Origem:** 11/07/2026, descoberto por acaso (o scraper entrou numa medição de churn de invalidação).

**Sintoma:** o script **baixa nada e grava 0 tickers**, mas sai **exit 0** com email de "concluído". O `run_step` do `pipeline_core` reporta `[OK]`. Mesmo padrão de falha silenciosa do bug do `scrape_anbima_cri_cra` (08/07).

**Causa:** o login funciona (URL confirma `analytics-hub/debentures/list`), mas o seletor do botão de download morreu:
```
Locator.wait_for: Timeout 15000ms exceeded.
waiting for locator("svg.hover\\:text-fia-500").first to be visible
```
`svg.hover:text-fia-500` é **classe Tailwind** — a mesma armadilha do portal da Anbima. O site mudou.

**Quando quebrou:** entre **08/07 e 11/07/2026**. Prova nos logs (`data/logs/scrape_fianalytics_planilha/`): run de `2026-07-08_181214` gravou **1.506 deb + 646 CRI/CRA = 2.152 tickers**; runs de 11/07 gravam 0 com 2 erros de seletor.

**Impacto:** `InfoAtivos` parou de receber da planilha FI Analytics o cadastro base, `vrDuration`, `vrTaxaEmissao` e `cdIndexador`. Downstream: ativo novo sem duration → sem `cdReferencia` no `match_referencias` → sem spread.

**Fix (mesma receita do CRI/CRA):**
1. Trocar o seletor por classe por um estável — `data-testid`, role, ou texto. **Nunca classe CSS** neste site.
2. Fazer o script **falhar (exit 1) se gravou 0 tickers em TODAS as planilhas** — hoje ele loga ERROR, retorna 0 e marca `success=True`. Data individual vazia continua sendo WARNING.

Fecha metade do item "Auditar falha silenciosa nos demais scrapers" abaixo.

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

## Usar o `getBondDetails` da B3 como fonte de cadastro (complementar à Anbima)

**Origem:** 11/07/2026, ao ler a spec `D:\ItauBBA\calculadora-renda-fixa\PLANO_VALIDACAO_FLUXOS.md`.

**✅ Atualização (12/07/2026):** o endpoint **já está ligado** — `lib/b3_calc_api.ObterDetalhesAtivo(cdTicker)` (com cache por ticker, reusando token/keepalive/retry-401). Quem o consome hoje é só o `validar_fluxos`. **O que falta é o outro uso:** virar **segunda fonte de cadastro** na cascata do `scrape_anbima_data_ativos` (Anbima → B3 → NULL), fechando o buraco de `InfoAtivos` descrito abaixo.

**O que é:** endpoint da API da B3 que já temos token (`lib/b3_calc_api.py`):

```
GET https://api.calculadorarendafixa.com.br/getBondDetails/{cdTicker}
Header: Authorization: <token de login>      # sem data, sem taxa — cadastro estático
```

Devolve o **cadastro completo** do ativo: `startingdate` (início de rentabilidade — pode diferir da emissão), `issuedate`, `expiredate`, `yield` (taxa de emissão em unidade nativa), `method` (indexador: `IPCA-I`→IPCA · `DI-PERC`→%CDI · `DI-SPREAD`→CDI+ · `PRE`→PREFIXADO), `anniversaryday`, `vne`, `tipoIF`, `issuer` — **e a agenda cadastrada** (`events`: `{date, eventType, yield}`, com `A` = %amortização e `J` = %incorporação).

**Por que interessa (2 usos):**
1. **Fechar o buraco de cadastro da `InfoAtivos`.** Ativo que negocia mas não está no indicativo Anbima fica hoje com cadastro NULL permanente — e, por tabela, sem `vrDuration` → sem `cdReferencia` → sem spread. O `getBondDetails` cobre quase todos os campos de `INFO_REQUIRED_COLS` (indexador, VNE, taxa de emissão, início de rentabilidade, vencimento, emissor, instrumento) **e** o fluxo. Vira uma **segunda fonte na cascata** do `scrape_anbima_data_ativos`: Anbima → B3 → NULL. Conversa direto com o item "Calcular duration de corporates" acima.
2. **É a fonte primária da validação de fluxo** que a calculadora vai exigir (ver abaixo).

**Limitações conhecidas (da spec):**
- As datas vêm no **dia-15 cru**, mesmo caindo em fim de semana/feriado → aplicar `ProximoDu` antes de comparar/gravar (nossa base às vezes já tem o DU-ajustado).
- Os eventos `A` **não incluem o principal do vencimento** (somam ~92% no FGEN13; os ~8% finais saem como evento `V` no `calcPU`).
- Não cadastra tudo — CRI/CRA tem cobertura pior (a spec usa FI Analytics como fallback nesses).

**Não fornece:** `cdISIN` e `vrQuantidadeEmissao` — justamente os 2 campos que a Anbima também não dá para ~120 ativos. Ou seja, não resolve o re-enfileiramento eterno desses; a skip-list continua sendo o remédio.

---

## ✅ RESOLVIDO (12/07/2026) — Validação de fluxo + rotinas da calculadora migradas

O `validar_fluxos.py` **veio para cá** (`code/scripts/validar_fluxos.py`, passo 11 do pipeline), junto com as 3 rotinas de dados da calculadora (`scrape_ipca_ibge`, `scrape_ipca_projetado_anbima`, `scrape_di_bcb`) e a curva DI completa arquivada pelo `scrape_b3_curva_di`. O lado do ingestor (5 colunas, trigger, `SincronizarFluxoAtivos`) já estava pronto desde 11/07. Detalhes em [[14 - Rotinas da Calculadora]].

**Ponto em aberto (herdado) — evento que some da agenda.** O `SincronizarFluxoAtivos` mantém a semântica de `INSERT OR REPLACE` sem `DELETE`: se a Anbima **remover** um evento (aditamento), a linha velha continua em `FluxoAtivos` e o fluxo gravado não muda → não invalida. É auto-consistente (a validação continua valendo para o fluxo que está na tabela), mas uma remoção de evento na fonte passa batida — o validador só pegaria como divergência ao comparar com a B3. Decidir se o ingestor deve passar a **substituir a agenda inteira** do ticker (DELETE + INSERT). Risco de mudar: um scrape parcial/com erro apagaria eventos legítimos.

**Também em aberto (do as-built do validador):** os **439 divergentes** — dominados por **agenda truncada** na nossa base (ex.: `20L0766583` tem 1 amortização, a B3 tem 3) — são correção na **origem** (lado do ingestor). E os **18 carência-100%** em `data/carencia_conferir_pu.csv`, validados sem conferir a %incorporação, precisam de conferência por PU com a calculadora.

---

## Trocar a precificação (`calc_taxa_negocios`) pela calculadora local

**Origem:** 12/07/2026 — é o **item 5** do `MIGRACAO.md` da calculadora, deixado fora da migração das rotinas por decisão do usuário.

**O que é:** hoje o `calc_taxa_negocios` obtém a taxa de cada trade **batendo em API** (cascata FI Analytics → B3 → NULL). Com a calc importada (`lib/calc.py` já resolve o import), isso vira **cálculo em memória**.

**Por que importa:** no banco o passo leva **~25 min/dia** com 24 workers — só ~20% dos trades batem em API, mas cada chamada paga handshake pelo proxy. Localmente seria quase instantâneo, e sem depender de terceiros.

**O que precisa ser decidido/feito:**
- A calc **só precifica fluxo confirmado** (`stFluxoValidado = 1`) — hoje **2.040 de 4.197** ativos com fluxo. Então a cascata vira **calc local → FI → B3 → NULL**, não uma substituição pura.
- **Revalidar contra a base atual:** comparar a taxa que a calc devolve com a `vrTaxaCalculada` já gravada (que veio das APIs) num conjunto grande de trades, e entender toda divergência antes de trocar.
- Decidir o que fazer com o `cdFonteTaxa` (ganha um valor novo, ex.: `'CalcLocal'`).

**Sondagem feita em 12/07/2026 (dados reais da base, ativos com `stFluxoValidado = 1`):**

- **IPCA/PREFIXADO: a calc reproduz a base exatamente** — 6 negócios, diferença de **0,0 bps** contra a taxa das APIs (inclusive um que tinha vindo da FI Analytics).
- **BUG DE PERFORMANCE NA CALC — CORRIGIDO (com autorização do usuário).** O `FatorDi` chamava `ContarDu(dataCalc, d)` para **cada** dia útil projetado, e o `ContarDu` varre dia a dia desde `dataCalc` → custo **quadrático** no prazo restante. Um CDI+ longo levava **19s por PU**, e o `CalcularTaxaNegociacao` faz até 100 PUs (50 iterações de Newton × 2) → **~30 min por negócio**. Fix: contador de DU **incremental** (`z += 1`) em vez de recontar. Mesmo número, mesmo resultado. Depois: **8 negócios DI em 18,7s**. Os 11 gabaritos da calc que passavam continuam passando (o 1 FAIL é anterior e não toca `FatorDi` — instrumentado: 0 chamadas naquele caso).
- **Performance ainda é o risco da troca.** Mesmo corrigido, a média ficou em **~2,3s por negócio DI** (com outliers de 6–11s: o Newton re-caminha toda a série de DI a cada iteração). O `calc_taxa` processa ~4k trades/dia que precisam de cálculo → ~2,5h num core. E, ao virar **CPU-bound**, o `ThreadPoolExecutor` atual **deixa de ajudar** (GIL) — precisaria de multiprocessing ou de memoizar a caminhada do VNA por `(ticker, data)`, já que só o fator de desconto muda entre as iterações. **Sem isso, a troca não é ganho garantido** frente aos ~25 min de hoje.
- **Divergência a investigar (exemplo: EMIV11).** Ativo `stFluxoValidado = 1` (validado pela **FI Analytics**, não pela B3): negociou a **PU 706,71** mas a calc dá **PU 564,75** na taxa de emissão → o Newton foge para **−19,63%** contra os 4,50% da API. A agenda tem 48 eventos em convenção de **saldo restante** com percentuais minúsculos no início (0,05%). É problema de dado/convenção de amortização, não do cálculo. **Peneirar esses casos é parte do trabalho desta fase** — e sugere que "validado pela FI" é uma garantia mais fraca que "validado pela B3".

Conversa direto com o item "Calcular duration de corporates" (a calc também expõe `CalcularDuration`).

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

