# 16 — Confiança nos Ativos Validados (WIP — noite 14→15/07/2026)

> **Objetivo do usuário:** que `stFluxoValidado = 1` signifique **"a calc reproduz a B3"** — em PU (no par e fora) e taxa (round-trip), em **várias datas** — para amanhã rodar a calc nos trades de verdade no relatório. Mais importante que corrigir cada erro é **entender por que acontece e consertar o VALIDADOR** para podermos confiar no dado.

> Este documento é atualizado **conforme o trabalho avança** (sobrevive ao limite de tokens). Log cronológico no fim.

## Diagnóstico do validador atual (`validar_fluxos.py`)

O modelo de confiança de hoje **não garante** que a calc precifica certo:

1. **Fluxo de origem B3 "nasce validado"** (`scrape_b3_bond_details` marca `stFluxoValidado=1`). Nunca se confere se a calc reproduz o PU/taxa da B3.
2. O único teste independente é o **`ConferirSaldo`** (tripwire): compara o **VNA** da calc com o `adjustedFaceValue` da **FI**, tolerância 0,05%. Problemas:
   - Só confere o **VNA** (saldo devedor), não o **PU** cheio nem a **taxa**. Um ativo com VNA certo e desconto/cupom errado passa.
   - Só cobre os ativos que a **FI** tem (minoria). Quem a FI não cobre → `ConferirSaldo` devolve `None` (não-confirmável) e **continua validado** (a tripwire só desvalida em divergência, não em não-confirmável).
3. Resultado medido (gate `conferir_pu`, 14/07): **68 de 3.047 validados erram o PU >1e-3** contra a B3 — ou seja, hoje "validado" inclui ativos que a calc erra.

**Conclusão:** o teste de confiança certo é **reproduzir a B3** (que é a fonte primária do cadastro). Se a calc bate a `calcPU`/`calcYield` da B3, usar a calc = usar a B3 = confiável. Se não bate, o ativo **não deve** estar validado (cai na cascata de API).

## Plano

- [x] Entender o validador atual
- [ ] Harness multi-data: para os validados, calc × B3 em PU (par + ±100bps) e taxa (round-trip), em várias datas. Quantificar a confiança real.
- [ ] Entender os modos de falha (cadastro divergente da B3 × metodologia da calc)
- [ ] Reescrever o validador: validado ⇔ calc reproduz a B3 (refresh do cadastro pela B3 + gate de PU/taxa). Quem falha, desvalida.
- [ ] Rodar sobre a base, produzir conjunto validado confiável
- [ ] Testar PU **e** taxa multi-data numa amostra grande — garantir confiança
- [ ] Ligar a calc no relatório para os validados; verificar que roda
- [ ] Documentar

## Como a calc entra no relatório (`calc_taxa_negocios.py`)

Cascata por trade: **(1) taxa direta do boletim** → **(2) calc local (só `stFluxoValidado=1`)** → (3) FI Analytics → (4) B3. A calc é o **degrau 2**: só roda quando o boletim **não** trouxe taxa, e **apenas em ativo validado** (`ativosValidados` = `WHERE stFluxoValidado=1`). Hoje está desligada (`[calc].usarCalcTaxa=false`).

**Por isso o plano fecha:** se `stFluxoValidado=1` significar "confiável", ligar a calc é **seguro por construção** — ela só precifica o que confiamos; ativo não confiável cai na cascata de API.

**Relevância (base atual, liq ≤ 10/07):**
- 65,6% dos trades são **direta** (boletim) — a calc não toca.
- 34,4% precisam de cálculo; desses, **112.294 trades / R$ 10,4 bi são de ativo validado** → é o que a calc passaria a precificar **local** em vez de FI/B3.

## Critério de confiança (calibrado com dados — harness multi-data, amostra 320)

| indexador | PU máx (par+fora) | taxa round-trip p95 | máx |
|---|---|---|---|
| CDI+ | 7,9e-5 | 0,5 bps | 0,8 |
| PREFIXADO | 2,1e-7 | 0,0 | 0,01 |
| %CDI | 8,2e-5 | 3,7 bps | 4,3 |
| IPCA | **1,8e-2** | 9,0 bps | **92** |

- **CDI+ (79/80), PREFIXADO (79/80) e %CDI (PU perfeito) são sólidos multi-data.** O único "miss" de CDI+/PREF é B3 vazio.
- **IPCA tem uma cauda genuinamente quebrada** (6/80 no sample) — os resíduos de pró-rata; alguns erram 1-2% no PU e 30-92 bps na taxa.
- O ruído de taxa do %CDI (2-4 bps) é **abaixo da precisão do relatório** (spread em bps inteiros).

**Critério adotado (revisto 19/07): `PU (par+fora) ≤ 1e-5`, testado em 3 datas.** Ver a seção "Desenho final (19/07)" abaixo — o critério original (`1e-3` + taxa round-trip, 5 datas) foi apertado para **1e-5 (R$0,01 num PU de R$1.000)** e virou **PU-only** (a taxa round-trip é redundante no B3 — 2 pontos de PU já validam o desconto). É esse o gate que o validador novo aplica: quem passa é confiável; quem falha ou não é confirmável, desvalida (cai na cascata).

## Desenho final do gate (19/07)

Depois de rever o `validar_calc_b3` com o usuário, o gate ficou assim (é a especificação vigente — o que estiver acima e divergir é histórico):

**1. Três datas: `[8 pregões atrás, mais recente com curva ("hoje"), D+1]`.**
- O **D+1 é data futura, sem curva própria**. É precificado por **carry-forward**: a curva mais recente projeta o D+1 — exatamente o que a B3/FI fazem. `SemearCurvaCarryForward` semeia `C._ACCPROJ_CACHE[D+1]` com a curva de "hoje", **em memória, sem escrever no `di.db`, sem tocar na `calculadora_rf.py`**. Válido porque D+1 é o DU seguinte ao dado mais recente (a série realizada de DI já cobre até "hoje"; só o trecho ≥ D+1 é projetado).
- Provado empiricamente: **a calc no D+1 bate o `calcPU` da B3 a 1e-8** (AALM12, ACHE14, ADPA11). E a B3 forward-precifica qualquer data futura (testado).

**2. Régua = erro relativo de PU `≤ 1e-5`.** = 0,001% = **R$0,01 num PU de R$1.000**. É relativo (`|puNosso/puB3−1|`), então independe da escala do PU (papel de emissão 1, 1.000 ou 10.000 usa a mesma régua). **Decisão do usuário**, ciente de que isso **reprova ~160 CDI+** cujo erro fora-do-par é ruído de interpolação da curva DI esparsa (não erro da calc) — esses caem na cascata FI/B3.
- **Como bate PU (não taxa), o %CDI usa a mesma métrica.** A conversão de bps (`DiffTaxaEmBps`) sobra só para o round-trip da FI (2º oráculo).
- **PU-only:** o `--com-taxa` (round-trip Newton no B3) fica como auditoria opcional, fora do caminho padrão.

**3. Não-confirmável → INVÁLIDO.** Se nem a B3 nem a FI respondem, não dá para dar fé → `stFluxoValidado=0`, cai na cascata de API. (Antes "não mexia".) O CSV ainda distingue `REPROVADO` (oráculo respondeu e divergiu) de `nao_confirmavel` (mudo) para auditoria.

**4. Revalidação por `dtValidacaoFluxo` (`--revalidar-dias`, default 15).** Ativo validado há menos de 15 dias é **pulado** (confia); ≥15 dias ou nulo, revalida e atualiza a data. Fluxo que muda de verdade zera `stFluxoValidado` pelo trigger → cai em não-validado e é re-testado na hora, sem esperar. `--tickers` e `--revalidar-dias 0` ignoram a janela (re-testam tudo — é o que se usa após mudar a régua).

## Log cronológico

- **Início (madrugada 14→15/07):** li `validar_fluxos.py` (diagnóstico acima) e `calc_taxa_negocios.py`. Construí `harness.py` (calc × B3, PU par+fora + taxa round-trip, multi-data).
- Amostra 320 (80/indexador, 5 datas): **299 PASS / 18 FAIL / 3 B3-vazio**. Calibrei o critério de confiança (acima). CDI+/PREF/%CDI sólidos; IPCA com cauda quebrada.
- Confirmei relevância: ligar a calc move 112k trades / R$ 10,4 bi de FI/B3 para a calc local.
- **Rodando:** harness na base validada inteira (auditoria definitiva) → classificar cada validado como confiável ou não.
- Criei **`scripts/validar_calc_b3.py`** — o gate de confiança: testa calc×B3 (PU par+fora, taxa opcional) em N datas e **desvalida** quem a calc não reproduz. Critério `PU≤1e-3` (taxa round-trip é quase redundante — 2 pontos de PU já validam o desconto; `--com-taxa` liga o Newton para auditar %CDI). `DatasPadrao` puxa datas **com curva DI** de `di.db` (sem curva, todo DI levantaria exceção e reprovaria por engano).
- Harness completo (5 datas, com taxa) foi **morto** — Newton em 1.564 DI × 5 datas é pesado demais. Migrei para o gate PU-only (rápido).
- **Rodando:** `validar_calc_b3 --dry-run` na base inteira (3 datas com curva DI) → classificação real.

## Arquitetura de confiança (fluxo de validação revisado)

Ordem no pipeline: `scrape_b3_bond_details` (cadastro fresco) → `validar_fluxos` (cross-check FI) → **`validar_calc_b3` (gate de confiança, NOVO)** → … → `calc_taxa_negocios` (usa a calc só nos validados).

| peça | papel | pega |
|---|---|---|
| `scrape_b3_bond_details` | cadastro/fluxo pela B3 (primária) | dado desatualizado — **MAS só re-scrapeia info faltando; fluxo que já existe deriva** (raiz aberta) |
| `validar_fluxos` (FI) | tripwire de saldo vs FI | B3 **errada vs mercado** (FGEN13) |
| **`validar_calc_b3` (NOVO)** | calc reproduz **um oráculo** (B3 OU FI)? | **a calc não precifica certo** → rebaixa (era o buraco) |
| `calc_taxa_negocios` | calc só p/ `stFluxoValidado=1` + indexador ligado | — |

**O gate é BIDIRECIONAL e usa DOIS oráculos** (evoluiu do desenho B3-only original):
- **Promove** quem passa (não-validado → `stFluxoValidado=1`) e **rebaixa** quem falha (→ 0). Os candidatos são a base toda (ou a janela `--negociados-dias`), não só os já validados.
- **B3 é o oráculo primário** (PU par + fora, `CalcularPuGov`); **a FI é o 2º**, só quando a B3 não confirma (round-trip de taxa, `ChamarPrimaria`). Basta **um** reproduzir a calc → `cdFonteValidacaoFluxo` guarda quem (`'B3'`/`'FiAnalytics'`). Decisão do usuário (15/07): "se bater com a FI, também é válido".
- **Oráculo mudo ≠ divergência:** HTTP 500/timeout/não-cobre = não-confirma (pula aquela chamada). Distinto de "responde e diverge" (REPROVADO). **Mas** (revisão 19/07) se **nenhum** oráculo responde em nenhuma data → **não-confirmável → INVÁLIDO** (antes ficava intocado). Ver "Desenho final (19/07)".

**⚠️ O gate NÃO é read-only** (fora de `--dry-run`): grava na `InfoAtivos`. Além do `stFluxoValidado`/`dtValidacaoFluxo`/`cdFonteValidacaoFluxo` (promoção/rebaixo), o **refresh-on-fail reescreve o pacote de cadastro** — `GravarAtivo` regrava `vrVNE`, `dtInicioRentabilidade`, `cdFonteCadastro='B3'`, **substitui a `FluxoAtivos` inteira** (DELETE+INSERT) e preenche escalares NULL via COALESCE. Rodar o gate "pra ver" sem `--dry-run` muta a base.

**Raiz de deriva ainda aberta:** `scrape_b3_bond_details` nunca reatualiza um fluxo que já existe. **Defesa implementada:** o refresh-on-fail acima cura a deriva antes de rebaixar (cadastro fresco da B3 + re-teste); rebaixa só a falha genuína de metodologia.

## Classificação da base inteira (dry-run, 3 datas com curva DI, PU-only)

**2.903 confiáveis (95,1%)** · 82 reprovados · 66 não-confirmáveis (B3 vazia).

| idx | confiável | reprov | não-conf | conf% |
|---|---|---|---|---|
| CDI+ | 1346 | 7 | 45 | 96,3% |
| IPCA | 1193 | 74 | 16 | 93,0% |
| PREFIXADO | 199 | 1 | 4 | 97,5% |
| %CDI | 165 | 0 | 1 | 99,4% |

**Bug do gate encontrado e corrigido:** 7 dos 82 "reprovados" tinham `piorPU=9.90` (sentinela) por causa de **HTTP 500 transitório da B3** na chamada fora-do-par (ASSR21, RENTE3, VBBR18…) — o gate confundia "B3 não respondeu" com "calc errou". Corrigido: falha da B3 (500/timeout) agora é **não-confirma** (pula a data/chamada), só conta como falha quando a B3 responde E a nossa calc diverge.

**Os 66 não-confirmáveis:** 59 têm `cdFonteCadastro=None` (origem desconhecida, B3 **não os cobre** — 0 getBondDetails, não é transitório). **Desvalidados** (não dá para vouch; caem na cascata FI/B3 como hoje, sem regressão). Restam 7 B3-cobertos com 500 transitório (reconfirmam na próxima rodada).

## Aplicação do gate + resultado

- Gate aplicado nos 82 reprovados (com a correção do bug B3-500 + refresh-on-fail): **12 resgatados** (glitches B3), **8 recuperados por refresh de fluxo** (deriva curada), **70 desvalidados** (metodologia genuína).
- 59 de origem nula desvalidados.
- **Base validada final: 2.922** (era 3.051), toda B3-verificada.

## Verificação em TRADES REAIS (o teste que importa) — 15/07

Taxa da calc vs B3 (`calcYield`) e FI nos **PUs efetivamente negociados** (amostra 160 trades, 2 datas):

| indexador | calc−B3 mediana | máx |
|---|---|---|
| **CDI+** | **0,00 bps** | 0,36 |
| **IPCA** | **0,00 bps** | 0,01 |
| **PREFIXADO** | **0,00 bps** | 0,00 |
| %CDI | 0,51 bps | **14,68** |

**CDI+/IPCA/PREFIXADO: a calc reproduz a B3 em trade real quase perfeitamente.** O %CDI tem cauda de ~15 bps em desconto profundo (CRA02300MJ7, PU 1392) — FI e B3 concordam, a calc não. É o desconto de %CDI (problema aberto).

## Decisão: LIGAR a calc para CDI+/IPCA/PREFIXADO; %CDI na cascata

- `config.toml [calc] usarCalcTaxa = true` + `indexadores = ["CDI+","IPCA","PREFIXADO"]`.
- `calc_taxa_negocios`: só carrega esses indexadores em `ativosValidados`; %CDI e não-validados caem em FI→B3 (sem regressão — hoje FI/B3 já os acertam).
- Volume calc-elegível: CDI+/IPCA/PREF = 58% (R$ 5,4 bi) vão para a calc local; %CDI (42%, R$ 3,9 bi) segue na cascata.

## ⚠️ REABRIR: o corte do %CDI pode ter sido por métrica mal-escalada (18/07)

No %CDI a "taxa" é um **multiplicador do CDI** (98,5 = 98,5% do CDI), não taxa a.a. O erro que motivou o corte foi medido como `diff × 100` (converte **p.p. → bps**), que só vale pra taxa a.a. — no %CDI **infla ~10×**.

Exemplo do backlog: CRA02300MJ7 calc 99,0308 vs B3 98,8934 → o doc registra **"+13,74 bps"**, mas `0,1374` **pontos de %CDI** valem **~1,4 bps** pela convenção do `filtrar_trades` (1 ponto %CDI ≈ 10 bps: `corretorMaxBps 2,5 ↔ corretorMaxPctCdi 0,25`; `pfMinBps 20 ↔ pfMinPctCdi 2,0`), ou **~2 bps** pela conversão exata (Δ%CDI/100 × CDI, CDI ~14,9%). Ou seja, **~7-10× menor** que o número de corte.

**Correção aplicada (18/07):** `validar_calc_b3.py` ganhou `DiffTaxaEmBps(cdIndexador, delta)` — %CDI usa ×10, demais ×100 — aplicada nos dois round-trips de taxa (B3 `--com-taxa` e FI). **Falta re-medir o %CDI com a métrica certa** e decidir se ele volta pra calc local. **Ainda fora por ora** (não re-liguei sem re-medição).

## Integração no pipeline

`ValidarCalcB3(negociados-dias=120)` entra em `RodarDia` e `RodarCadeiaDias` **depois** do `validar_fluxos`, **antes** do `calc_taxa`. Escopado aos validados que negociaram (a base toda leva ~13 min; os negociados são o que aparece no relatório). Refresh-on-fail cura a deriva; desvalida a falha genuína.

## Log (cont.)

- Classifiquei a base (2.903 confiáveis / 82 reprov / 66 não-conf), achei e corrigi o bug B3-500 do gate, apliquei (2.922 validados B3-verificados).
- Verifiquei taxa em trades reais → CDI+/IPCA/PREF perfeitos; %CDI com cauda.
- **Liguei a calc** (CDI+/IPCA/PREF) e integrei o gate no pipeline.
- **Rodando:** `calc_taxa --date 2026-07-10 --force` (calc ON) → conferir a fonte da taxa e coerência.
- Próximo: rodar o relatório, conferir, commitar.

- **18/07 — o gate virou bidirecional + 2 oráculos** (registrado retroativamente na tabela de arquitetura acima; o código já estava à frente da nota): promove/rebaixa, B3 primária + FI secundária, e **grava cadastro na `InfoAtivos`** no refresh-on-fail.
- **18/07 — corrigida a escala de bps do %CDI** (`DiffTaxaEmBps`): o corte do %CDI foi medido em `diff×100`, que infla ~10× num multiplicador de CDI. Ver seção "REABRIR" acima — **falta re-medir e decidir se o %CDI volta**.

- **19/07 — desenho final do gate fechado com o usuário (ver seção "Desenho final (19/07)"):**
  - Datas viraram `[8 pregões atrás, hoje, D+1]`; o **D+1 é precificado por carry-forward** (curva mais recente projeta a data futura). Verifiquei que a B3 forward-precifica e que a calc bate o D+1 a 1e-8.
  - Régua apertada de `1e-3` → **`1e-5` (R$0,01 num PU de R$1.000)**, PU-only, mesma métrica p/ %CDI. O usuário escolheu ciente de que reprova ~160 CDI+ (ruído da curva DI, não erro da calc).
  - **Não-confirmável → INVÁLIDO** (antes intocado).
  - **Revalidação por `dtValidacaoFluxo`**: novo `--revalidar-dias` (default 15) pula o validado recente; `0` re-testa tudo (usado após a mudança de régua).
  - Smoke `--dry-run` confirmou o comportamento (AALM12 R$0,03 e ACHE14 R$0,08 reprovados; confiáveis intactos; base não mexida).
  - **Rodada real `--revalidar-dias 0` na base inteira (4.856 precificáveis) — resultado:** base validada **2.922 → 2.806** (96 promovidos, 212 rebaixados). Por indexador validado: **IPCA 1.234, CDI+ 1.222, PREFIXADO 201, %CDI 149**. Fonte: **B3 1.705, FI 1.101**.
  - **Impacto medido no `calc_taxa_negocios` (liq 07-10, dia cheio):** dos 19.521 trades, 63,2% são taxa direta do boletim (nunca tocam API) e 36,8% vêm de API; **4.853 desses são de ativo validado + indexador da calc → migram pra calc local**. Em chamadas de API (o passo cacheia por par `ticker,PU`): **de ~3.294 chamadas/dia, ~2.318 (70,4%) somem**. Sobram ~976 (%CDI + não-validados). Deve derrubar os ~25 min/dia do banco pra poucos minutos, e offline.
  - **⚠️ Achado importante — a FI está resgatando o CDI+ que a régua de R$0,01 reprova.** Dos 1.222 CDI+ validados, só **333 passaram pela B3** (PU ≤ 1e-5); **889 foram resgatados pela FI** (round-trip de taxa ≤ 5 bps). Ou seja: o erro fora-do-par do CDI+ é ruído de ~0,25 bps de taxa (a FI confirma que a calc está certa), mas grande demais em PU pra régua de R$0,01. **A régua apertada não derrubou o CDI+ — ela empurrou ~889 papéis da confirmação-B3 pra confirmação-FI**, ao custo de muito mais chamada de API (a rodada ficou lenta). O gate efetivo do CDI+ virou os 5 bps da FI, não o R$0,01 da B3. **Decisão em aberto:** aceitar isso (a calc reproduz a FI, que concorda com a B3 → confiável) ou apertar também a FI / afrouxar a B3 pra R$0,10 (mesmos papéis, menos tráfego de API).
