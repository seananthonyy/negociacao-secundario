# Backlog — Itens para Decisão Futura

---

> Itens que identificamos durante o desenvolvimento mas que não têm prioridade imediata ou precisam de mais contexto antes de implementar. Revisar periodicamente.

---

## ✅ FEITO (02/09/2026) — %par dos negócios, sobre a tabela `PuPar`

**Origem:** pedido do usuário (31/08). Implementado em 01/09 numa v1 que o desenho de 02/09
substituiu por inteiro. **Está tudo no código; falta só o usuário apontar em que outros
lugares do relatório quer ver o número.**

**O que ficou no disco:**

| | |
|---|---|
| `Helpers/dados.py` | tabela `PuPar` no `ESQUEMA`/`PARTICAO`/`CHAVE`; `DescartarPuPar()` ligado ao `InvalidarSeFluxoMudou`, ao `SincronizarFluxos` e ao `SincronizarFluxoAtivos` |
| `codigos/calc_pu_par/` | script novo (substitui o `calc_pct_par`, removido). Cascata calc → B3 → FI, fase de API em 10 workers, idempotente por (ticker, data) |
| `gerar_relatorio_credito` | CTE `PuParVigente` nas duas consultas do boletim; `CalcularPctPar()` e `PregoesEntre()`; `%par`, idade e selo de congelada no boletim e no email |
| `relatorio_secundario.html` | coluna **% Par** com ágio/deságio, selo `Np` de referência congelada, pílula de contagem e disclaimer |
| `pipeline_core` | passo `PuPar(X)` nas três rotinas |

**O que é:** o preço do negócio como percentual do **PU par** — o PU que o papel valeria
precificado na própria taxa de emissão. `%par = vrPU / puPar × 100`. É como a mesa lê "caro
ou barato" sem depender de spread nem de match de curva. 100 é no par, acima é ágio, abaixo
é deságio.

### Por que a v1 sai

A v1 gravava `vrPctPar` em `NegociosProcessados` e só cobria `stFluxoValidado = 1` (86,3%).
Dois problemas:

1. **Cobertura.** O ativo que o gate rebaixou é justamente aquele em que a **B3 respondeu** e
   a nossa calc não reproduziu — ou seja, o puPar da B3 está disponível e é autoritativo.
   Faltava a cascata.
2. **Duas verdades.** Com o `vrPctPar` gravado, corrigir o fluxo de um ativo (que apaga o
   puPar dele) deixaria o %par velho de pé em `NegociosProcessados`, calculado com um
   denominador em que ninguém mais acredita. É o mesmo problema que fez o trigger virar
   código dentro do `Mesclar()`.

### O desenho fechado

| | |
|---|---|
| **Tabela nova** | `PuPar`: `cdTicker`, `dtReferencia`, `vrPuPar`, `cdFontePuPar`, `dtCriacao`. Série, particionada por `dtReferencia`. Só valores REAIS — nada sintético |
| **Cascata** | calc local (só validados) → **B3** (`CalcularPuGov(tk, dt, vrTaxaEmissao)`) → **FI** (`ChamarCompleto(tk, dt, vrTaxaEmissao)`, campo `m2m`) |
| **Idempotência** | par `(cdTicker, dtReferencia)` que já existe não recalcula nem chama API. O valor de um par não muda nunca |
| **Invalidação** | correção de fluxo apaga os `PuPar` daquele ticker, dentro do `Mesclar()` — junto do trigger que já mora lá |
| **`%par`** | **calculado na LEITURA**, `vrPU / vrPuPar × 100`. Não é gravado. A fórmula vive num helper só, no estilo do `dados.NaoCancelado()` |
| **Script** | `calc_pct_par` vira **`calc_pu_par`** — o nome passa a descrever o que ele grava |
| **`vrPctPar`** | **sai** do `ESQUEMA` de `NegociosProcessados`, via `Reconformar` |

### Por que puPar é indexado por DATA (e não por ativo)

Foi a decisão central. O puPar **acreta todo dia útil** na taxa de emissão, então guardar um
valor por ativo e reusá-lo por N dias injeta erro **sistemático** (sempre para cima — puPar
velho é menor, então o %par lê alto). Medido em 249 ativos validados, 17/07 → 28/07 (7 dias
úteis):

| indexador | n | mediana | p90 | máx |
|---|---|---|---|---|
| CDI+ | 109 | 0,411% | 0,462% | 4,73% |
| %CDI | 18 | 0,372% | 0,387% | 0,405% |
| PREFIXADO | 7 | 0,364% | 0,382% | 0,382% |
| IPCA | 115 | 0,250% | 0,292% | 0,382% |
| **TODOS** | **249** | **0,368%** | 0,437% | 4,73% |

Isso entra direto no %par. E dói mais onde o número é mais útil: CDI+ e %CDI vivem entre 99
e 101 — a faixa inteira do sinal são ~2 pontos, e 0,4 come 20% dela.

**Segundo modo de falha, pior:** evento de fluxo dentro da janela. Entre 17/07 e 28/07,
**128 dos 2.583 ativos negociados (5%)** tiveram evento — MATD23 amortizou **100%**, MOVI34
33%, JHSFA1 11%. Não é deriva, é degrau.

**E indexar por data é mais BARATO:** a base inteira tem **50.628 pares distintos**. Cada um
se calcula uma vez na vida. Com janela de N dias você re-paga a mesma chamada de API para
sempre — e recebe um número errado.

### Papel que sai do cadastro (distress) — a parte mais delicada

Quando o emissor se aproxima do default, **as calculadoras removem o papel do cadastro**. O
mercado passa a usar o **último puPar disponível** como referência, e negocia em *cents on
the dollar*. Para esses nomes taxa e spread somem — **o %par vira o único número que a mesa
tem**.

**A regra:** o relatório busca o último puPar com `dtReferencia <= dtLiquidacao` (padrão que
já existe no `SQL_BUSCAR_VALIDO`, com `ROW_NUMBER` sobre a Anbima) e **mostra sempre a
idade** quando ela é > 0.

**Não existe status `Congelado` gravado** — decisão do usuário, e está certa: a idade *é* o
status. A `PuPar` guarda só o que foi realmente calculado, e o `cdFontePuPar` fica só com a
procedência, sem misturar estado. A objeção original era contra referência velha
**invisível**; com a idade na tela, o leitor julga sozinho — 2 pregões é soluço de API
(deriva ~0,1%), 30 pregões é papel que saiu do cadastro.

**Limiar de exibição: 5 pregões.** Abaixo disso a idade aparece discreta; a partir daí o
ativo é marcado como **referência congelada**, com a idade em destaque, e fica **fora das
médias** — antes do congelamento o %par é ágio/deságio contra o par corrente, depois é
*cents on the dollar* contra referência fixa. São grandezas diferentes.

Descongela **sozinho** se o papel voltar às calculadoras (é o comportamento natural do
desenho), e carrega **sem prazo**, sempre com a idade visível.

**Custo aceito:** sem linha gravada para o papel congelado, o script re-tenta B3/FI todo
pregão e falha sempre. São ~2 chamadas por pregão por nome em default — punhado de ativos.

### ⚠️ Furo declarado: até 15 pregões de atraso para perceber o congelamento

O `validar_calc_b3` roda com `--revalidar-dias 15`: ativo validado há pouco não é re-testado.
Se a B3 largar o papel hoje, podemos levar até 15 pregões para descobrir — e nesse
meio-tempo a **nossa** calc continua acretando um par contratual para um emissor que parou de
pagar.

Fechar isso exigiria sondar a B3 em todo pregão para todo ativo validado. **Decisão: aceitar
o atraso.** O mecanismo já existe (o gate desvalida quem é não-confirmável), só tem latência.
Se incomodar, o caminho é reduzir o `--revalidar-dias` só para quem negociou.

### Onde exibe — ⚠️ PENDENTE

Boletim Diário e email do dia já mostram. **O usuário ainda vai apontar os outros lugares**
(Por Ativo? Spread × Duration? Visão Mercado?).

**Conversa com:** [[10 - Scripts/validar_calc_b3]], [[16 - Confianca nos Validados (WIP)]],
[[06 - Calculadoras/B3 Calculator API]] e [[17 - Armazenamento Parquet e AWS]].

---

## Relatório: incluir o dia de hoje, marcado como PRÉVIA (31/08/2026)

**Origem:** pedido do usuário.

**O que é:** hoje o relatório **corta em D-1** (`CalcularDtCorte()`), então a liquidação do
próprio dia nunca aparece. O pedido é passar a incluí-la, com um aviso de **"prévia"** na
tela.

**Por que o corte existia** (ver [[09 - Progresso]]): o pregão de hoje não fechou. Os
negócios que já estão na base para a liquidação de hoje são a **perna D+1 do pregão
anterior** — um dia pela metade. Publicá-los sem aviso mostrava volume e spread de um dia
incompleto como se fosse fechado.

**O aviso resolve a objeção** — o dado deixa de ser enganoso quando está rotulado. Mas o
corte não deve simplesmente sumir:

- O dia de hoje **não pode entrar nas médias e agregados** como um pregão normal (ele
  puxaria a média de volume para baixo e o gráfico teria um degrau no fim).
- A flag `--ate` continua valendo para quem quer a foto fechada.

**O que precisa ser decidido:**
- O aviso é um selo na aba/linha do dia, um banner no topo, ou os dois?
- O dia-prévia entra nos gráficos de série (com marcação visual) ou só no Boletim Diário?
- Ele entra no "Volume Total" do topo? (Sugiro que **não** — ou que apareça separado.)
- O email do dia (`--email-dia`) também passa a poder ser de hoje?

---

## ✅ RESOLVIDO (31/08/2026) — negócio sem identificador é descartado

A B3 manda o campo *"Cód. identificador do negócio"* como `-` em parte das operações (25 no
pregão de 28/07/2026). Esse campo é a **chave** da base desde que o `idTrade AUTOINCREMENT`
do SQLite foi aposentado.

Duas consequências, ambas silenciosas: (a) todos os `-` de um pregão colapsavam numa linha
só — o SQLite fazia o mesmo, com `UNIQUE`; (b) no Parquet a chave só é única **dentro da
partição**, então acumulava um `-` por pregão, e o `JOIN` do relatório (que não filtra data)
passava a contar o mesmo negócio várias vezes. Medido: R$ 3,24 MM em dobro.

**Decisão do usuário (31/08):** descartar. Perde-se o negócio, mas não se corrompe o total.
O `scrape_b3_boletim` agora descarta no parse e **avisa** quantos foram. A linha `-` que
estava na base (EEELA1, 09/06) foi removida de `NegociosBrutos` e `NegociosProcessados`; o
relatório caiu R$ 3,25 MM, que era exatamente a contagem dobrada.

A chave voltou a ser única de verdade: **680.650 linhas / 680.650 identificadores** em
`NegociosBrutos`, e 645.318/645.318 em `NegociosProcessados`.

> Se um dia a perda incomodar, o caminho é **sintetizar** a chave (hash de
> ticker+horário+PU+volume+quantidade) em vez de descartar.

---

## ✅ RESOLVIDO (31/08/2026) — o filtro de cancelado não pegava o vocabulário da B3

O projeto filtrava com `cdSituacao != 'Cancelado'`, comparação **exata**. A B3 escreve
`Cancelado B3` (13.828) e `Cancelado Parcial B3` (26) — o literal `'Cancelado'` (1.868) é
**nosso**, do `SoftCancelAusentes`. Resultado: **13.854 negócios cancelados pela B3
passavam por bons** em todo o pipeline.

O predicado passou a viver em **`dados.NaoCancelado()`**, com prefixo `'Cancelado%'` — um
lugar só, cobre os três e não quebra se a B3 inventar um quarto. Nove call sites.

**Impacto medido no relatório geral:** R$ 53.265,78 MM → **R$ 52.318,42 MM** (−947,36 MM,
−1,8%); ativos 2.567 → **2.568**. O ativo a mais não é contradição: negócio cancelado também
participava do pareamento de duplicados, e tirá-lo reclassifica — em 28/07 o FUNDO caiu de
11.575 para 11.534 e o VALIDO subiu de 5.145 para 5.173.

### Duas pontas soltas que sobraram

1. ~~`Cancelado Parcial B3` tratado como cancelado integral~~ — **decidido pelo usuário
   (31/08): é isso mesmo, conta como cancelado.** O prefixo `'Cancelado%'` fica.

2. **O `filtrar_trades` só foi re-rodado para 28/07.** Os outros 33 pregões ainda têm o
   `cdStatus` calculado com os cancelados dentro do pareamento. O relatório já sai certo
   (ele filtra na leitura), mas a classificação **gravada** não. Re-rodar a base inteira
   muda número histórico — decisão do usuário.

---

## ✅ RESOLVIDO (29/08/2026) — flag `--sem-email` para execucoes em lote

Ja existia e ninguem tinha registrado: **`NEGSEC_SEM_EMAIL=1`** no ambiente faz qualquer
script pular o Outlook e gravar o corpo do email em `files/emails/`. Cobre o caminho de erro
tambem. Usado o tempo todo nas rodadas de validacao desta sessao.

Fica valendo como convencao: **usar sempre em teste e em rodada de lote** — evita o spam e o
gotcha da instancia COM orfa do Outlook.

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

## Melhorar a precisão da calc local (PU e taxa fora do par)

**Origem:** 13/07/2026 (taxa fora do par) e 14/07/2026 (erro de PU). **Fundidos em
01/09/2026** — são a mesma pergunta: onde a calc local ainda não reproduz a B3/FI, e por quê.

**Onde já estamos.** A calc está **LIGADA** desde 19/07 (`config.toml [calc] usarCalcTaxa =
true`) para **CDI+, IPCA e PREFIXADO**, como degrau 2 da cascata, só em `stFluxoValidado=1`.
Quem garante a confiança é o gate `validar_calc_b3`: a calc só precifica ativo cuja calc
reproduz a B3/FI em PU a ≤1e-5, testado **no par** (valida fluxo e VNA) **e a 100 bps do
par** (valida o desconto). **97,8% dos ativos batem.** O que segue aberto são duas frentes.

### Frente 1 — %CDI fora do par  ⬅️ MEDIDO EM 01/09/2026, e o numero mudou tudo

O %CDI nao entrou na calc local porque ela nao reproduzia o desconto fora do par. **Mas a
medicao que motivou isso estava inflada ~10x**: os "bps" da tabela original eram pontos de
%CDI (multiplicador do CDI), nao taxa a.a. — o `−13,74 bps` do CRA02300MJ7 e `0,1374` ponto
de %CDI, que com CDI ~14,9% vale **~2 bps** de yield.

**A re-medicao foi feita.** O `validar_calc_b3` ja tinha o `--com-taxa` e o `DiffTaxaEmBps`,
mas o `piorTaxa` era **calculado e jogado fora**: entrava na decisao (`b3Pass` exige
`piorTaxa <= TOL_TAXA_BPS`) e nao saia em relatorio nenhum — por isso a medicao nunca
acontecia. Agora ele sai no CSV (`piorTaxaBps`) e numa secao propria do email.

Rodado em **40 ativos %CDI validados, os de maior volume negociado** (R$ 7,16 bi somados),
em 3 datas (16/07, 28/07, 29/07), `--dry-run --com-taxa --sem-fi`:

| metrica | mediana | p90 | pior |
|---|---|---|---|
| **taxa round-trip vs B3 (bps de yield)** | **0,054** | **0,219** | **0,458** |
| PU (erro relativo) | 2,5e-05 | 5,7e-05 | 8,0e-05 |

**40 de 40 dentro de 1 bp na taxa.** A divergencia de taxa do %CDI nao e 13,7 bps, nem os
~2 bps da ressalva de escala: e **meio bp no pior caso**.

**O que reprova o %CDI hoje nao e a taxa — e a regua de PU.** 25 dos 40 sao REPROVADOS, e
todos por `piorPU > TOL_PU` (1e-5 = R$ 0,01 por R$ 1.000). Os erros ficam entre 2e-05 e
8e-05, ou seja **R$ 0,02 a R$ 0,08 por R$ 1.000** — que, convertidos em taxa, sao os 0,05 a
0,46 bps da tabela. Nenhum passa de 1e-4.

**Ou seja: o gate reprova em PU um ativo cuja TAXA ele mesmo aceitaria com folga de 10x**
(`TOL_TAXA_BPS = 5,0` contra 0,46 medido). As duas reguas do mesmo gate discordam sobre o
que e material, e no %CDI e a mais apertada que decide.

**A decisao que sobrou (do usuario, nao do codigo):** para %CDI, o gate deveria julgar por
PU ou por TAXA? Tres caminhos:
1. **Gate por taxa no %CDI** — e a grandeza que vai para o relatorio; o PU e meio de prova.
2. **`TOL_PU` proprio para %CDI** (1e-4 cobriria os 40) — mais simples, mas escolhe um
   numero por conveniencia, que e como a regua errada nasce.
3. **Deixar como esta** — o %CDI segue na cascata FI→B3, custando ~25 min/dia de API por
   uma diferenca de meio bp.

**Ressalva de amostra:** os 40 sao os de MAIOR VOLUME entre os 157 %CDI validados, nao uma
amostra uniforme. E o recorte certo para decidir (sao os que movem o relatorio), mas a cauda
dos 117 restantes nao foi medida. Repetir com `--tickers` da lista inteira antes de mexer na
regua.

**O IPCA fora do par foi RESOLVIDO (21/07).** Reproduz a B3 a 1e-8 no par **e** fora dele,
provado termo a termo em 6 IPCA-I com o `comparar_calcpu_b3.py`; a causa era a incorporacao
futura, ja corrigida. Os 28 papeis IPCA que erravam ~1,3% a 100 bps do par (TRGP13, SABP13,
PLAC23, RALM11, BARU11, MNAU18...) sairam da lista.

### Frente 2 — 68 ativos ainda erram o PU acima de 1e-3 (era 114)

Medido com `validar_calc_b3 --dry-run`, contra a régua certa (`calcYield`/`calcPU` da B3),
**só em data com curva DI na base**. 14/07 levou 114 → 68 (97,8% OK) em duas frentes: faxina
de cadastro (cupom errado em 20 ativos, indexador em 4, fluxo defasado/espúrio em 19 — raízes
corrigidas, scrapers viraram fill-only) e o **snap no `CalcularVna`** (a amortização passou a
encostar no aniversário mais próximo), que matou os casos catastróficos (TPER11 70%→0,3%).

Os 68 que restam são todos pequenos (pior 3,8%):

| # | grupo | erro | de quem é |
|---|---|---|---|
| **57** | IPCA pro-rata/índice | 0,1-2,8% (quase tudo <1%) | metodologia fina — **oscila com a data** em torno de 1e-3. NÃO corrigir `vrAniversario` por minimização: é superajuste (testado). Poucos maiores = fluxo incompleto (24G1674104: 121 vs 151 eventos) |
| **7** | CDI+ | até 3,8% | batem no par, erram fora dele |
| **4** | PREFIXADO | — | B3 devolve `getBondDetails` vazio (TSSS15/VAMOA4/RDORE7/CEPEA5) — gap da B3, não nosso |

### Raiz de processo, não fechada

`scrape_b3_bond_details.py` só re-scrapeia quem tem info **faltando**; **fluxo que já existe
nunca é atualizado** → deriva silenciosa. Foi exatamente o que produziu os 19 fluxos
defasados da faxina de 14/07. Falta um refresh periódico do fluxo da B3.

### Prêmio se fechar

Medido no pregão mais cheio (16/06, 8.065 negócios validados), só há **598 pares (ticker, PU)
distintos** — o cache corta 93% do trabalho, e a ~0,7s por par dá **~7 min/dia num core**,
contra os ~25 min/dia da cascata de API no banco. A calc é mais rápida **e** offline.

**Conversa com:** [[16 - Confianca nos Validados (WIP)]], [[14 - Rotinas da Calculadora]] e
[[10 - Scripts/validar_calc_b3]].

---
## Migração e instalação no ambiente do Banco (Itaú BBA)

**Origem:** 20/06/2026. **Atualizado em 01/09/2026** com a parte de dados.

**O que é:** levar o projeto do PC pessoal para o ambiente corporativo. São **duas coisas
distintas** que sempre andaram juntas nesta nota: o **ambiente** (instalar e rodar) e os
**dados** (não recomeçar do zero).

### Parte A — ambiente

Instalação de dependências (Python, Playwright, pacotes), ajuste de paths, variáveis de
ambiente (`.env`), acesso às fontes (B3, Anbima, FI Analytics — verificar bloqueios de
proxy/firewall), permissões de Outlook (`pywin32`) e ajuste de credenciais.

O passo a passo já existe: **`INSTALACAO_BANCO.md`** na raiz. O que continua pendente é
levantar as restrições reais do ambiente (proxy, Python disponível, permissão de instalação).

### Parte B — aproveitar os dados da arquitetura antiga no Parquet

**Isto é o que falta decidir.** O PC do banco roda em produção sobre o **SQLite** (`trades.db`
e companhia) e tem histórico que a base local não tem; o branch `refactor/split-bases` trocou
o armazenamento por **Parquet + DuckDB**. Na virada, esse histórico ou é **convertido** ou é
**perdido** — e boa parte dele **não é re-scrapeável**: as fontes online guardam janela curta
(deb/NTN-B ~4 meses, curva DI ~20 pregões, CRI/CRA ~5 pregões), e as chamadas de cálculo da
B3 são **contadas**, então re-derivar taxa de pregão antigo custa consumo de verdade.

**O que precisa ser decidido/feito:**
- **Um conversor SQLite → Parquet**, uma vez só, tabela a tabela. O SQL não muda (as views do
  DuckDB têm o nome das tabelas antigas), então o trabalho é de **tipo e partição**, não de
  query: aplicar o `ESQUEMA` do `dados.py` coluna a coluna (**tipo é declarado, nunca
  inferido**) e particionar por data as tabelas de série.
- **A chave dos negócios muda.** O `idTrade` (`AUTOINCREMENT`) morreu com o SQLite; a chave
  passou a ser o `cdIdentificadorNegocio` que a B3 manda. Conferir se **todo** negócio
  histórico do banco tem esse campo preenchido — o que não tiver é descartado (é a mesma
  regra do RESOLVIDO de 31/08), e é melhor descobrir o volume disso **antes** da virada.
- **Quem prevalece na sobreposição** entre o histórico do banco e o que já existe no Parquet
  local (1.582.200 linhas). O caminho seguro é `Mesclar()` com política por coluna, não
  `Upsert()` — ninguém é dono de todas as colunas de `InfoAtivos`.
- **Ordem da virada:** converter **antes** de ligar o pipeline novo no banco, para que a
  primeira rodada em Parquet já ache o histórico no lugar e não tente re-scrapear a janela
  toda.
- Conferir se `InfoAtivos`/`FluxoAtivos` (tabelas **estado**, arquivo único) precisam de
  reconciliação com o cadastro atual da B3, ou se entram como estão e o pipeline atualiza.

**Conversa com:** [[17 - Armazenamento Parquet e AWS]], [[13 - Migracao Banco]],
`INSTALACAO_BANCO.md` e o item de importação de histórico de planilhas abaixo (é o mesmo
problema pela outra ponta: lá a fonte é Excel, aqui é o SQLite de produção).

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

## ✅ RESOLVIDO (12/07/2026) — Validação de fluxo + rotinas da calculadora migradas

O `validar_fluxos.py` **veio para cá** (`code/scripts/validar_fluxos.py`, passo 11 do pipeline), junto com as 3 rotinas de dados da calculadora (`scrape_ipca_ibge`, `scrape_ipca_projetado_anbima`, `scrape_di_bcb`) e a curva DI completa arquivada pelo `scrape_b3_curva_di`. O lado do ingestor (5 colunas, trigger, `SincronizarFluxoAtivos`) já estava pronto desde 11/07. Detalhes em [[14 - Rotinas da Calculadora]].

**Ponto em aberto (herdado) — evento que some da agenda.** O `SincronizarFluxoAtivos` mantém a semântica de `INSERT OR REPLACE` sem `DELETE`: se a Anbima **remover** um evento (aditamento), a linha velha continua em `FluxoAtivos` e o fluxo gravado não muda → não invalida. É auto-consistente (a validação continua valendo para o fluxo que está na tabela), mas uma remoção de evento na fonte passa batida — o validador só pegaria como divergência ao comparar com a B3. Decidir se o ingestor deve passar a **substituir a agenda inteira** do ticker (DELETE + INSERT). Risco de mudar: um scrape parcial/com erro apagaria eventos legítimos.

**Também em aberto (do as-built do validador):** os **439 divergentes** — dominados por **agenda truncada** na nossa base (ex.: `20L0766583` tem 1 amortização, a B3 tem 3) — são correção na **origem** (lado do ingestor). E os **18 carência-100%** em `data/carencia_conferir_pu.csv`, validados sem conferir a %incorporação, precisam de conferência por PU com a calculadora.

---

## Trocar a precificação (`calc_taxa_negocios`) pela calculadora local

**✅ FEITO (15-19/07/2026) para CDI+/IPCA/PREFIXADO.** A calc está **LIGADA** (`config.toml [calc] usarCalcTaxa = true`, `indexadores = ["CDI+","IPCA","PREFIXADO"]`) como degrau 2 da cascata, só em `stFluxoValidado=1`. A confiança é garantida pelo gate **`validar_calc_b3`** (ver [[16 - Confianca nos Validados (WIP)]] e [[11 - Pipeline de Execucao]] passo 13): a calc só precifica ativo cuja calc reproduz a B3/FI em PU a ≤1e-5. **%CDI segue de fora** (a calc não reproduz o desconto fora-do-par — ver [[#Melhorar a precisão da calc local (PU e taxa fora do par)|Melhorar a precisão da calc local]]). O texto abaixo é o registro da investigação que levou a isso.

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

## Importar histórico de taxas de planilhas — LEVANTADO em 01/09/2026, e o item encolheu

**Origem:** 06/07/2026. **O sub-item "levantar o layout real de cada planilha" foi feito.**

**O que era:** importar histórico de planilhas do usuário para cobrir período anterior ao que
os scrapers alcançam, porque "as fontes online guardam janela curta".

### O que foi medido

**1. As planilhas não são o que o item supunha.** Varrido `D:\ItauBBA\Planilhas` (8 arquivos):
`BACKUPCalculadora`, `CalcCDIPorcento`, `CalcIPCA`, `CalcPre`, `CalculadoraCRICRA`,
`CalculadoraTitulosPublicos`, `plan_vna`. **Todas são calculadoras de precificação**, não
séries históricas de taxa indicativa. As abas de dados que elas carregam são `IPCA
Histórico`, `Projeção IPCA`, `CDI Histórico` (desde 2010) e `DI Projeção` — insumos da
própria planilha, e **exatamente o que os nossos scrapers já coletam**. A aba `NTN-B` é uma
calculadora de um papel numa data, não uma curva por data.

**Não existe, nesta máquina, planilha com histórico de taxa indicativa Anbima nem de curva
NTN-B/DI por data.** Se ela existe, está no PC do banco.

**2. Metade do item já está resolvida pelos scrapers.** Cobertura real hoje:

| série | cobertura na base | fonte | veredito |
|---|---|---|---|
| **DI realizado** (`di.db/DiHistorico`) | **2000-01-03 → 2026-07-28** (26 anos) | `scrape_di_bcb` (BCB/SGS) | ✅ já profundo — **não precisa de planilha** |
| **IPCA** (`ipca.db/IPCA`) | **1979-12 → 2026-12** (47 anos) | `scrape_ipca_ibge` | ✅ já profundo — **não precisa de planilha** |
| `AnbimaIndicativos` | 2026-02-23 → 2026-07-28 (106 dias) | scrapers deb/CRI-CRA | ⚠️ **já no limite da fonte** (ver abaixo) |
| `MtmAnbima` (NTN-B + DI1) | 2026-06-08 → 2026-07-28 (36 dias) | `scrape_anbima_ntnb` | 🎁 **3,5 meses de graça, não raspados** |
| `di.db/CurvaDi` (vértices) | 2026-06-15 → 2026-07-28 (32 pregões) | `scrape_b3_curva_di` | ❌ janela da B3 ~20 pregões; fundo só por planilha |

**3. Onde a fonte da Anbima acaba, exatamente.** Bissecção nos arquivos `.xls` diários
(`m{yy}{mmm}{dd}.xls` para NTN-B, `d{...}.xls` para debênture):

> **A fronteira é 2026-02-21 nos dois.** 20/02 dá 404, 21/02 baixa. Arquivos de 15/01/2026 e
> anteriores não existem mais. Os de 17/03, 10/06 e 28/07 baixam e têm md5 distintos (são
> arquivos de verdade, não a mesma página repetida).

Ou seja: a janela da Anbima é de **~6 meses**, não os "~4 meses" que o item supunha — e o
`AnbimaIndicativos` da base (começa 23/02) **já está colado nessa borda**. Não há nada a
raspar ali; tudo antes de 21/02/2026 só existe em planilha, se existir.

### O que fazer

**Ação imediata, e ela EXPIRA:** o `MtmAnbima` só tem 36 dias, mas a fonte de NTN-B entrega
desde 21/02. São **~3,5 meses de curva NTN-B disponíveis agora e que somem** conforme a
janela desliza. O script já existe e é idempotente:

```
python codigos\scrape_anbima_ntnb\scrape_anbima_ntnb.py --start 2026-02-23 --end 2026-06-05
```

Isso é o que o item chamava de "destravar spread histórico profundo" — e não precisa de
planilha nenhuma nem de código novo.

**O que sobra de verdade para importação manual:** só a **curva DI por vértice** (`CurvaDi`),
onde a B3 guarda ~20 pregões e não há outra fonte. E o histórico Anbima **anterior a
21/02/2026**, se e somente se aparecer uma planilha que o contenha.

**Decisões que continuam pendentes** (e só valem quando a planilha aparecer): layout real
(abas, colunas, data BR, unidade da taxa), mapa coluna→tabela, quem prevalece na sobreposição
scraper × planilha, e se o "DI projetado" cabe em `MtmAnbima` ou pede tabela própria.

**Conversa com:** [[10 - Scripts/scrape_anbima_ntnb]], [[10 - Scripts/scrape_b3_curva_di]] e
o Passo 6-B do `INSTALACAO_BANCO.md` (é o mesmo problema pela outra ponta: lá a fonte é o
SQLite de produção do banco).

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

## ~~Flag `--sem-email` para execuções em lote e validação~~ — ver o RESOLVIDO acima (29/08)

**Origem:** 23/07/2026, no fechamento da FASE 3.

**O que é:** uma flag global que suprima o envio de email no fim do script, para rodadas de teste/validação em sequência.

**Por que importa:** a convenção do projeto é que **todo script manda email no fim, sucesso ou erro** — ótimo para execução agendada, ruim para depuração. No fechamento da FASE 3 o `validar_calc_b3.py` foi rodado 4× seguidas (comparando calc nova × antiga, amostras diferentes) e o `match_referencias.py` 1×: cada uma disparou um email. O `--dry-run` protege o banco, mas não a caixa de entrada. Pior: uma das chamadas deixou uma **instância órfã de COM do Outlook** (processo sem janela, `MainWindowTitle` vazio, respondendo mas pendurado) — o gotcha de `pywin32` já conhecido no projeto. Rodar validação em lote hoje custa spam e um zumbi de processo.

**O que precisa ser decidido/feito:**
- Onde mora a flag: em cada `LerArgumentos()` ou num helper comum (o envio já é centralizado — a supressão deveria ser também, senão vira 12 implementações e volta o problema de N cópias).
- Se `--dry-run` deveria **implicar** `--sem-email` automaticamente. Provavelmente sim: quem não grava, geralmente também não quer notificar.
- Alternativa/complemento: variável de ambiente (ex.: `SEM_EMAIL=1`) para uma sessão inteira de depuração, sem repetir a flag em cada comando.
- Conferir se o caminho de erro também respeita a flag — um script que falha em modo de teste não deveria mandar email de erro.

**Conversa com:** a nota do gotcha de COM em [[Email rascunho do relatório]] e a convenção "Email no fim de cada script" no `CLAUDE.md`.
