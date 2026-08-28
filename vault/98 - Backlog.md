# Backlog — Itens para Decisão Futura

---

> Itens que identificamos durante o desenvolvimento mas que não têm prioridade imediata ou precisam de mais contexto antes de implementar. Revisar periodicamente.

---

## Exibir as NTN-B divulgadas pela Anbima no relatorio

**Origem:** 28/08/2026, pedido do usuario.

**O que e:** dar um lugar no relatorio para as informacoes de NTN-B que a Anbima divulga
e que ja estao na base -- hoje elas entram so como *insumo* (a referencia contra a qual o
spread do papel IPCA e medido) e nunca aparecem como dado proprio.

**O que ja temos:** `MtmAnbima` guarda `cdTicker` (`NTN-B 35`, `NTN-B 37`, ...),
`dtReferencia`, `vrTaxa` e `vrDuration`, alimentada diariamente pelo `scrape_anbima_ntnb`.
A aba **Visao Anbima** ja desenha a *curva* de spread por vertice de NTN-B, mas nao mostra
a taxa nominal nem a duration de cada vertice de forma direta.

**O que precisa ser decidido:**
- Onde: aba propria ("Curva NTN-B"), um bloco dentro da Visao Anbima, ou uma tabela no topo
  do Boletim (as poucas linhas do dia).
- O que exibir: so a taxa indicativa do dia, ou a serie historica com grafico; incluir
  duration; incluir variacao vs. D-1.
- Se entra o vertice inteiro que a Anbima publica ou so os que tem papel casado por
  `match_referencias`.

**Conversa com:** [[10 - Scripts/scrape_anbima_ntnb]], [[08 - Match de Referencia]] e a aba
Visao Anbima em [[10 - Scripts/gerar_relatorio_credito]].

---

## Rotas em lote da B3 (`calcPUCSV` / `calcYieldCSV`) — investigar

**Origem:** 25/08/2026, lendo a documentação oficial do Web Service da CALC (`Documentacao API.pdf`).

**O que é:** a API da B3 tem versões **CSV/lote** dos dois métodos de cálculo que hoje chamamos
um-por-um: `calcPUCSV` (§13 da doc) e `calcYieldCSV` (§15). Hoje o `calc_taxa_negocios` faz **uma
requisição HTTP por trade** e o `validar_calc_b3` faz 2 a 4 por ativo — num pregão cheio isso é
dezenas de milhares de chamadas, e cada uma paga handshake (pior atrás do proxy do banco).

**Por que importa:**
- **Velocidade.** A cascata de API é o passo mais lento do pipeline (~25 min/dia no banco, medido
  em 13/07). Trocar N requisições por um punhado de POSTs de CSV muda a ordem de grandeza.
- **Consumo.** As chamadas de cálculo são **contadas** pela B3 — existe a rota
  `GET /consumo/pacotes/{dataInicial}/{dataFinal}` (§18), que devolve `quantidadeCalculos` por
  tipo de ativo (DI / Debenture / Titulo Publico). Falta descobrir se o lote conta como **1** ou
  como **N** cálculos. Se contar como 1, o ganho é duplo.

**O que precisa ser decidido/feito:**
- Ler §13 e §15 da doc: formato exato do CSV de entrada, limite de linhas por chamada, e o que
  volta quando **uma** linha do lote falha (erro parcial vs. lote inteiro rejeitado).
- Medir o consumo com `/consumo/pacotes` **antes e depois** de um lote de teste — é a única forma
  de saber como a B3 contabiliza.
- Onde entra primeiro: o `validar_calc_b3` é o candidato natural (roda a base inteira, PU em 3
  datas + D+1, e é tolerante a latência). O `calc_taxa_negocios` vem depois — lá o cache por
  `(ticker, dtLiquidacao, PU)` já corta 93% do trabalho, então o ganho marginal é menor.
- Conferir se o lote respeita o mesmo contrato de erro que a `lib/b3_calc_api` já trata (token
  expirado em 3h, 500 transitório, ativo sem cadastro).

**Conversa com:** [[06 - Calculadoras/B3 Calculator API]], [[10 - Scripts/validar_calc_b3]] e
[[10 - Scripts/calc_taxa_negocios]]. Doc oficial: `Documentacao API.pdf` (24 páginas, índice na p. 2).

---

## Ponto cego aberto em 24/08/2026 — ninguém mais audita a B3 contra a FI

**Origem:** remoção do `validar_fluxos` (decisão do usuário, 24/08).

**O que se perdeu:** o `ConferirSaldo` era o único teste que confrontava **B3 × FI** — duas
fontes independentes. O `validar_calc_b3` pergunta "a nossa calc reproduz a B3?", usando o
cadastro da B3: se a B3 tiver cadastro errado, a nossa calc reproduz o erro dela a 1e-5 e o
ativo **passa no gate**. Só consulta a FI quando a B3 **não** confirma.

**O caso conhecido:** FGEN13 — a B3 diz 1.280, a FI diz 508, o mercado negocia a 503.

**Por que foi removido mesmo assim:** o teste de agenda evento a evento é redundante com o PU
(no par valida fluxo+VNA, fora do par valida o desconto), e a cobertura da FI era magra (146
de ~1.670 que a B3 não cobre). O que se perdeu foi só o cruzamento independente.

**Opção se voltar a incomodar:** rodar o round-trip da FI **sempre**, não só quando a B3
falha, e reportar (sem desvalidar) o desacordo B3×FI. Custa uma chamada FI por ativo/rodada.

---

## 🔴 PRIORIDADE — a calc não reproduz a taxa fora do par (2 a 14 bps)

**Origem:** 13/07/2026, na tentativa de trocar o `calc_taxa_negocios` pela calculadora local.

**O que é:** a calc reproduz o **PU par** das fontes com precisão (**86,4%** dos 2.861 ativos validados batem a 1e-6 — medido com o gate da época), mas **não reproduz a taxa implícita num PU fora do par**.

Triangulando 4 negócios de 16/06/2026:

| ativo | calc | FI | B3 | calc − B3 | FI − B3 |
|---|---|---|---|---|---|
| TRGP13 (IPCA) | 7,3838 | 7,3620 | 7,3620 | **+2,18 bps** | 0,00 |
| CRA02300MJ7 (%CDI) | 99,0308 | 98,9105 | 98,8934 | **+13,74 bps** | +1,71 |
| 22J0346710 (%CDI) | 91,9606 | 92,0145 | 92,0015 | **−4,09 bps** | +1,30 |
| CRA025002S1 (%CDI) | 113,7916 | 113,8021 | 113,8011 | −0,95 bps | +0,10 |

**FI e B3 concordam entre si; a calc discorda das duas.** O erro é nosso.

**⚠️ Ressalva de escala (18/07):** os "bps" da tabela acima estão **inflados ~10× no %CDI**. Os valores são pontos de %CDI (multiplicador do CDI), não taxa a.a. — o `−13,74 bps` do CRA02300MJ7 é `0,1374` ponto de %CDI, que vale **~1,4-2 bps** de yield (convenção do `filtrar_trades`: 1 ponto %CDI ≈ 10 bps; conversão exata com CDI ~14,9% dá ~2 bps). Ou seja, a **divergência de %CDI existe mas é ~7-10× menor** do que parecia. O `validar_calc_b3` já foi corrigido (`DiffTaxaEmBps`); **falta re-medir o %CDI com a métrica certa e decidir se ele volta pra calc local** (hoje está fora por causa desse número inflado). O bug de desconto do IPCA/CDI+ fora do par continua real e não é afetado por essa ressalva (aqueles são taxa a.a.).

**O detalhe que aponta a causa:** o TRGP13 **bate o PU par a 1e-6** e mesmo assim erra a taxa em 2,18 bps. Se os fluxos e o VNA estão certos (e estão — o PU par fecha), a diferença só pode estar no **desconto**. Candidatos: o truncamento de 6 casas em cada VP (`Trunca(FV_i / fatorDesc, 6)`), a contagem de DU do fator de desconto, ou a convenção do %CDI (onde a taxa muda o fluxo **e** o desconto).

**Estado (19/07):** a calc foi **LIGADA** (`config.toml [calc] usarCalcTaxa = true`) para **CDI+/IPCA/PREFIXADO** — mas o **%CDI ficou de fora exatamente por este bug** (erra o desconto fora do par). Ou seja: este item passou a ser **só sobre o %CDI** (os demais indexadores a calc reproduz a B3/FI e já precificam local). Ver [[16 - Confianca nos Validados (WIP)]].

**O gate foi consertado (13/07) e a medição mudou tudo.** Ele passou a testar em **duas** taxas: no par (valida o fluxo e o VNA) e a **100 bps do par** (valida o desconto). Rodando nos 3.023 validados:

| | ativos |
|---|---|
| batem **no par E fora dele** (≤1e-6) | **1.919 — 63,5%** |
| batem **só no par** (fluxo ok, desconto errado) | **628** |
| investigar (> 1e-3) | 135 |

**O gate antigo aprovava 86,4%. O certo aprova 63,5%** — os 628 teriam entrado com o desconto errado, e é o desconto que produz a **taxa** que vai para o relatório.

**E os 628 se separam limpo em dois fenômenos diferentes:**

- **596 erram por um fio** (1e-6 a 1e-5): **579 CDI+, 16 %CDI, 1 PREFIXADO — zero IPCA.** Em taxa dá ~0,02 bps. É ruído numérico da projeção DI, não bug. Tolerável.
- **30 erram de forma material** (> 1e-4): **28 IPCA**, 2 CDI+.

**Ou seja: o bug de desconto é do IPCA, e é uma lista de 28 papéis** (TRGP13, SABP13, PLAC23, RALM11, BARU11, MNAU18...). Todos batem o PU par com precisão absurda (5e-09 no TRGP13!) e erram ~1,3% a 100 bps do par. Fluxo e VNA perfeitos, desconto quebrado.

**Próximo passo:** pegar um desses 28 e comparar o desconto termo a termo com o `/calcPU` da B3 (que devolve o `cashFlowList` com `presentValue` de cada evento). A diferença tem que aparecer num termo específico.

**Prêmio se fechar:** medido no pregão mais cheio (16/06, 8.065 negócios validados), só há **598 pares (ticker, PU) distintos** — o cache corta 93% do trabalho, e a ~0,7s por par dá **~7 min/dia num core**, contra os ~25 min/dia da cascata de API no banco. A calc é mais rápida **e** offline.

---

## 68 ativos ainda erram o PU acima de 1e-3 (era 114) — sem mais casos catastróficos

**Origem:** 13/07/2026, atualizado 14/07. Rodar `python scripts/validar_calc_b3.py --dry-run` (o `conferir_pu` foi removido em 24/08; o gate faz a mesma medição). **Contra a régua certa** (calcYield/calcPU da B3), não a taxa gravada na base. Rodar **só em data com curva DI na base**.

**14/07 — 114 → 68 (97,8% OK)** em duas frentes (detalhe em [[14 - Rotinas da Calculadora]]):
1. **Faxina de cadastro** (só dado nosso): cupom errado 20 ativos (FI sobre B3; RED711 250 vs 2,5), indexador 4 (CRA02300MJ8 %CDI→CDI+ errava 565%), fluxo defasado/espúrio 19. Raízes corrigidas no código (scrapers viraram fill-only).
2. **Snap no `CalcularVna`** (autorizado, mexeu na `calculadora_rf.py`): amortização passou a **encostar no aniversário mais próximo** em vez de exigir data exata → matou os erros catastróficos (TPER11 70%→0,3%, 22D1226341 30%→0). Gabaritos seguem 11 OK/1 FAIL, zero regressão.

**Restam 68, todos pequenos (pior 3,8%):**

| # | grupo | erro | de quem é |
|---|---|---|---|
| **57** | IPCA pro-rata/índice | 0,1-2,8% (quase tudo <1%) | metodologia fina — **oscila com a data** em torno de 1e-3 (projeção/pró-rata do IPCA corrente). NÃO corrigir `vrAniversario` por minimização: é superajuste (testado). Poucos maiores = fluxo incompleto (24G1674104: 121 vs 151 eventos) |
| **7** | CDI+ | até 3,8% | batem no par, erram **fora do par** → é a [[#🔴 PRIORIDADE — a calc não reproduz a taxa fora do par (2 a 14 bps)\|taxa fora do par]], não novo |
| **4** | PREFIXADO | — | B3 devolve `getBondDetails` vazio hoje (TSSS15/VAMOA4/RDORE7/CEPEA5) — gap da B3 |

**Raiz de processo não fechada:** `scrape_b3_bond_details.py` só re-scrapeia quem tem info **faltando**; fluxo que já existe **nunca é atualizado** → deriva (foi o que causou os 19 fluxos defasados). Falta um refresh periódico do fluxo da B3.

**Item fechado em 24/08/2026:** o `conferir_pu --desvalidar` foi removido junto com o script. O `validar_calc_b3` já faz isso por padrão e **está** no pipeline — desvalida quem não reproduz a B3/FI, e esses caem na cascata de API. Não há mais o que decidir.

---

## Migração e instalação no ambiente do Banco (Itaú BBA)

**Origem:** 20/06/2026.

**O que é:** planejar a migração do projeto do PC pessoal para o ambiente corporativo (PC trabalho / rede do banco). Inclui: instalação de dependências (Python, Playwright, pacotes), ajuste de paths, configuração de variáveis de ambiente (.env), acesso às fontes (B3, Anbima, FI Analytics — verificar se há bloqueios de proxy/firewall), permissões de Outlook (pywin32), e possível ajuste de credenciais.

**Decisão pendente:** levantar restrições do ambiente (proxy, Python disponível, permissões de instalação, etc.) antes de planejar os passos.

---

## ✅ RESOLVIDO (19/07/2026) — `scrape_fianalytics_planilha` quebrado (layout novo do site)

**Consertado em 19/07** com base no tutorial do usuário (`instrucoes.txt`). O site refez o layout: (1) o download não é mais por URL `?type=deb`/`cri_cra` — agora é botão **"Exportar"** na lista (login cai na lista de debêntures); (2) CRI/CRA se acessa pelo item de menu **"Lista"** (há dois; o de CRI/CRA é o **último**, ~640; o 1º é debêntures ~1.5k); (3) o formato mudou de **xlsx → CSV** (separador `;`, decimal vírgula, UTF-8 BOM) e a coluna do emissor virou **`Emissor`** (era `issuer`). Seletores agora por **texto/role** (`get_by_role("button", name="Exportar")`, `button:has-text("Lista").last`), nunca por classe CSS. Adicionado **exit 1 se nenhuma planilha gravar tickers** (mata a falha silenciosa). **Validado ao vivo: 1.507 deb + 640 CRI/CRA = 2.147 tickers.** Texto original abaixo (contexto).

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

## ✅ RESOLVIDO em grande parte (20/07/2026) — falha silenciosa (exit 0 na falha)

**Corrigido o cerne (20/07):** **10 scripts** capturavam a exceção, logavam, mandavam email, mas **não faziam `sys.exit(1)`** → saíam exit 0 e o `pipeline_core` reportava `[OK]` falso. Adicionado `if not success: sys.exit(1)` no fim do `Principal` de: `calc_taxa_negocios`, `calc_spread_over`, `calc_spread_anbima`, `filtrar_trades`, `match_referencias`, `scrape_anbima_debentures`, `scrape_anbima_ntnb`, `gerar_relatorio_credito`, `gerar_relatorio_html`, `scrape_outstanding_bloomberg`. (Os demais já usavam `raise`/`sys.exit`: `scrape_b3_boletim`, `scrape_b3_bond_details`, `scrape_b3_curva_di`, `scrape_di_bcb`, `scrape_ipca_*`, `scrape_anbima_cri_cra`, `validar_fluxos`, `validar_calc_b3`, `conferir_pu`.) **Também:** `MontarIntervaloDatas` retornava `[args.date]` sem validar (data inválida → 0 linhas → exit 0 mudo) — agora valida com `date.fromisoformat`. E o `scrape_fianalytics_planilha` (19/07) passou a exit 1 se gravar 0 tickers. **Falta** (subitem): o "0 linha numa fonte específica → WARNING + lista do que a fonte tem" (distinguir "quebrou" de "não tem dado") ainda pode ser refinado nos scrapers Anbima. Texto original abaixo.

**Origem:** 08/07/2026, depois do bug do `scrape_anbima_cri_cra` (ver [[09 - Progresso]]).

**O problema (padrão, não instância):** o `scrape_anbima_cri_cra` ficou **6 pregões gravando 0 linhas com exit 0**. O padrão que permitiu isso: `except` que loga WARNING e retorna `None`/`0,0`, `Main` que nunca olha o total gravado, `success=True` incondicional. O `run_step` do `pipeline_core` só olha o exit code → reportava `[OK]`.

**O que fazer:** varrer os outros scrapers procurando o mesmo padrão e aplicar a mesma regra:
- **0 linha gravada em TODAS as datas pedidas → exit 1** (scraper quebrado).
- **0 linha numa data específica → WARNING** + a lista do que a fonte realmente tem (é "não tem dado", não "quebrou").
- Seletor Playwright: **nunca por classe CSS** em portais com CSS-modules (Anbima Data e o portal de CRI/CRA já têm classes com hash que mudam a cada build). Usar `data-testid`/`data-cy`.

**Achado novo (13/07):** `calc_spread_over.py --date NAO-E-DATA` sai com **exit 0 e silêncio total** — nem erro, nem aviso, nem linha no log. Data inválida deveria abortar. Vale conferir o mesmo em `calc_spread_anbima` e `calc_taxa_negocios` (compartilham o `MontarIntervaloDatas`).

**Candidatos a auditar:** `scrape_anbima_debentures` (baixa XLS por URL previsível — 404 fora da janela é legítimo, mas e se o layout mudar?), `scrape_fianalytics_planilha`, `scrape_b3_boletim` (já tem fallback em cascata; conferir se o fallback pode retornar CSV vazio "com sucesso"), `scrape_anbima_ntnb`, `scrape_b3_curva_di`.

**Reforço barato já implementado:** os 3 notebooks (`setup_teste`, `setup_inicial`, `run_secundario`) conferem no `.db`, por fluxo, se o que era pra ser gravado foi gravado — foi exatamente isso que pegou este bug.

---

## ✅ FEITO (19/07/2026) — Calcular duration de corporates para fechar o gap do match_referencias

**Implementado como pré-passo no `match_referencias.py`** (`PreencherDurationFaltante` + `CalcularDurationAtivo`): antes do match, calcula a `vrDuration` dos IPCA/PREFIXADO que negociaram mas estão sem ela, pela **mesma cascata de confiança do calc_taxa** — **validado → calc local** (`lib/calc.CalcularDuration`, DU/252 → anos); **não-validado → FI → B3** (FI: `maculayDuration` já em anos, via `lib/fianalytics_api.ObterDuration`/`ChamarCompleto` modo `rate`; B3: `CalcularPuGov` devolve `(pu, duration)`). Descontada na `vrTaxaEmissao` (percent — a FI recebe a taxa em percent no modo `rate`), as-of a data da curva de benchmark mais recente. **Resultado:** IPCA/PREF negociados sem duration **520 → 41** (389 calc + 46 B3 + 44 FI); os 41 restantes nenhuma fonte cobre (CRIs obscuros). **cdReferencia sem ref: 1147 → 679.** **Ressalva aberta:** sem `cdFonteDuration`, uma duration calculada bloqueia (COALESCE) uma futura da Anbima — raro nesses ativos fora do indicativo, mas é um refino possível. Texto original abaixo (contexto).

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

**✅ Atualização (12/07/2026):** o endpoint **já está ligado** — `lib/b3_calc_api.ObterDetalhesAtivo(cdTicker)` (com cache por ticker, reusando token/keepalive/retry-401). Quem o consome hoje é o `validar_calc_b3` e o `scrape_b3_bond_details`. **O que falta é o outro uso:** virar **segunda fonte de cadastro** na cascata do `scrape_anbima_data_ativos` (Anbima → B3 → NULL), fechando o buraco de `InfoAtivos` descrito abaixo.

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

**✅ FEITO (15-19/07/2026) para CDI+/IPCA/PREFIXADO.** A calc está **LIGADA** (`config.toml [calc] usarCalcTaxa = true`, `indexadores = ["CDI+","IPCA","PREFIXADO"]`) como degrau 2 da cascata, só em `stFluxoValidado=1`. A confiança é garantida pelo gate **`validar_calc_b3`** (ver [[16 - Confianca nos Validados (WIP)]] e [[11 - Pipeline de Execucao]] passo 13): a calc só precifica ativo cuja calc reproduz a B3/FI em PU a ≤1e-5. **%CDI segue de fora** (a calc não reproduz o desconto fora-do-par — ver item do topo). O texto abaixo é o registro da investigação que levou a isso.

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


---

## `--forcar-duration` no `match_referencias.py` (recalcular em lote a duration dos validados)

**Origem:** 23/07/2026, no fechamento da FASE 3 (unificação da máquina de fluxo).

**O que é:** uma flag que faça o pré-passo do `match_referencias.py` **recalcular** a `vrDuration` dos ativos validados, em vez de só preencher quem está `NULL`.

**Por que importa:** o pré-passo hoje roda `WHERE ia.vrDuration IS NULL` — quem já tem duration gravada nunca é recalculado. Quando a calc melhora, a melhoria **não alcança esses papéis**. Foi exatamente o que aconteceu agora: a FASE 3 corrigiu um bug na `CalcularDuration` (a incorporação de juros era ignorada — errava até 1,53 ano contra a B3), e dos 35 ativos afetados, **19 seguem com o valor antigo** porque já tinham `vrDuration` gravada. Os outros 16 (com NULL) vão pegar o valor novo naturalmente.

**Por que não é urgente:** os 19 valores congelados vieram de fonte externa (FI / B3 / Anbima), não da calc bugada, e já estavam mais perto da calc nova do que da antiga. Os deltas contra o gravado ficaram em ~0,2 ano na mediana (máx. 1,42 em SABP13) — pequenos demais para trocar o vértice da NTN-B no duration-match. Rodado o `match_referencias` depois do merge, **nenhum dos 35 mudou de referência**; só `ENERB8` ganhou uma atribuição nova, sem relação com a duration.

**O que precisa ser decidido/feito:**
- Escopo da flag: só `stFluxoValidado = 1` (onde a calc local é confiável) ou também FI/B3? A cascata de `CalcularDurationAtivo` já cobre os três — a questão é quem se deixa sobrescrever.
- Se a flag recalcula tudo ou aceita `--tickers` / um corte por `dtAtualizacaoDuration` mais velha que N dias (recálculo por envelhecimento seria mais automático que uma flag manual).
- Guardar a **fonte** da duration (hoje `CalcularDurationAtivo` devolve `fonte`, mas ela não é persistida). Sem isso não dá para saber quais linhas vieram da calc e mereceriam refresh após uma mudança do motor.
- Rodar em `--dry-run` antes, comparando gravado × recalculado, e só então decidir se vale gravar.

**Conversa com:** `docs/RELATORIO_FASE3_FINAL.md` §3.3 (medição do impacto) e `docs/BACKLOG_INCORP.md` (a outra pendência aberta pela FASE 3).

---

## Flag `--sem-email` para execuções em lote e validação

**Origem:** 23/07/2026, no fechamento da FASE 3.

**O que é:** uma flag global que suprima o envio de email no fim do script, para rodadas de teste/validação em sequência.

**Por que importa:** a convenção do projeto é que **todo script manda email no fim, sucesso ou erro** — ótimo para execução agendada, ruim para depuração. No fechamento da FASE 3 o `validar_calc_b3.py` foi rodado 4× seguidas (comparando calc nova × antiga, amostras diferentes) e o `match_referencias.py` 1×: cada uma disparou um email. O `--dry-run` protege o banco, mas não a caixa de entrada. Pior: uma das chamadas deixou uma **instância órfã de COM do Outlook** (processo sem janela, `MainWindowTitle` vazio, respondendo mas pendurado) — o gotcha de `pywin32` já conhecido no projeto. Rodar validação em lote hoje custa spam e um zumbi de processo.

**O que precisa ser decidido/feito:**
- Onde mora a flag: em cada `LerArgumentos()` ou num helper comum (o envio já é centralizado — a supressão deveria ser também, senão vira 12 implementações e volta o problema de N cópias).
- Se `--dry-run` deveria **implicar** `--sem-email` automaticamente. Provavelmente sim: quem não grava, geralmente também não quer notificar.
- Alternativa/complemento: variável de ambiente (ex.: `SEM_EMAIL=1`) para uma sessão inteira de depuração, sem repetir a flag em cada comando.
- Conferir se o caminho de erro também respeita a flag — um script que falha em modo de teste não deveria mandar email de erro.

**Conversa com:** a nota do gotcha de COM em [[Email rascunho do relatório]] e a convenção "Email no fim de cada script" no `CLAUDE.md`.
