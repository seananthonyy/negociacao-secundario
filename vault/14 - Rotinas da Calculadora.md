# Rotinas da Calculadora de Renda Fixa

> **O que é:** desde **12/07/2026**, este projeto passou a **rodar as rotinas de dados** que a calculadora de renda fixa consome, e a **validar o fluxo** dos ativos que ela pode precificar. Handoff original: `D:\ItauBBA\calculadora-renda-fixa\docs\MIGRACAO.md`.

## A divisão de responsabilidades

|  | `calculadora-renda-fixa` | **este projeto** (`negociacao-secundario`) |
|---|---|---|
| **Cálculo** (VNA, PU Par, PU de operação, duration, taxa) | ✅ é a **biblioteca** — `calculadora_rf.py`, `di.py` | consome via `lib/calc.py` |
| **Coleta de dados** (IPCA, projeção, DI) | — | ✅ **roda as rotinas** (passos 9–11 do pipeline) |
| **Validação de fluxo** | — | ✅ **roda** (passo 12) |

A calc continua morando em `D:\ItauBBA\calculadora-renda-fixa`. **Não se mexe nela sem autorização explícita** (regra do projeto dela) — já são 4 mudanças autorizadas, listadas abaixo. Aqui ficam os dados e as rotinas.

## Onde vivem os dados

Em `code/data/`, junto do `trades.db` — este projeto é quem os coleta; a calc só lê:

| Arquivo | Tabelas | Alimentado por | Serve para |
|---|---|---|---|
| `data/ipca.db` | `IPCA`, `IPCAProjetado` | `scrape_ipca_ibge`, `scrape_ipca_projetado_anbima` | VNA de IPCA+ |
| `data/di.db` | `DiHistorico`, `CurvaDi` | `scrape_di_bcb`, `scrape_b3_curva_di` | %CDI e CDI+ (realizado + projeção) |
| `data/feriados_anbima.csv` | — | (estático) | régua de dias úteis — **fonte única**, a calc passou a ler a nossa |
| `data/trades.db` | `InfoAtivos` (4 colunas de validação), `FluxoAtivos` | ingestor + `validar_calc_b3` | a calc só precifica `stFluxoValidado = 1` |

Os schemas dessas 4 tabelas são **contrato com a calc** — por isso os nomes de coluna não seguem o prefixo `vr/cd/dt` do `trades.db`. Não renomear.

## Como a calc enxerga os nossos dados (`lib/calc.py`)

A calc resolve `feriados_anbima.csv`, `ipca.db` e `di.db` por uma constante `DIR_ARQUIVOS`. Em 12/07/2026 (com autorização do usuário) ela ganhou **uma** alteração:

```python
DIR_ARQUIVOS = Path(os.environ.get('CALCRF_FILES_DIR') or Path(__file__).parent / 'files')
```

- **Sem** a env var → comportamento antigo (o `files/` dela). É o que o add-in do Excel usa: **nada quebrou lá**.
- **Com** a env var → lê os nossos dados. Quem seta é o `lib/calc.py`, o **único** módulo que sabe onde a calc está instalada (`config.toml [paths] calculadoraDir`, sobrescrevível pela variável de ambiente `CALCULADORA_DIR` no banco).

```python
from lib.calc import ImportarCalc, ObterBancoIpca, ObterBancoDi

C = ImportarCalc()                       # módulo calculadora_rf, apontado p/ code/data/
du = C.ProximoDu(d, C.FERIADOS_ANBIMA)
```

`ImportarCalc()` cria os dois bancos (vazios, com as tabelas) **antes** de importar a calc — ela carrega feriados/IPCA/projeção no `import`, e sem isso quebraria numa máquina nova.

## As 4 rotinas

### 1. `scrape_ipca_ibge` — IPCA realizado
SIDRA (tabela 1737, variável 2266 = número-índice, base dez/1993) + API Calendário do IBGE (datas de divulgação, só cobre dez/2016+; antes disso fica NULL, e a calc trata como mês fechado). Sem argumentos: o SIDRA devolve a série inteira (desde 1979) a cada chamada, e o UPSERT com `COALESCE` nunca apaga o que já existe.

### 2. `scrape_ipca_projetado_anbima` — projeção de IPCA
Raspa a aba IPCA (`#profile`) da página da Anbima. Cada projeção tem uma **Data de Validade** e vale até a véspera da validade seguinte (carry-forward); o script expande isso em registros **diários** até D+1. Sem a projeção **não se precifica IPCA+ no mês corrente** (entre uma divulgação e a seguinte, o VNA anda pela projeção).

- **⚠️ Rodar antes das 17h30** — a Anbima republica por volta desse horário nos dias de divulgação.
- Playwright **com proxy explícito** (`ObterProxyPlaywright()`). O script original não passava proxy: no banco teria voltado vazio, como aconteceu com os 4 scrapers em 03/07.
- **A janela da fonte é curta (~13 meses).** O histórico mais antigo **não é reconstruível** — o `ipca.db` copiado da calc (392 dias de projeção) é patrimônio, não se apaga.

### 3. `scrape_di_bcb` — DI realizado
API SGS do BCB, **duas séries da mesma taxa**: a 4389 (anualizada, base 252, 2 casas) e a **12** (fator diário, 6 casas). O acúmulo do PU Par usa a série 12 — o fator de 6 casas reproduz a calculadora da B3 a ~1e-5; a 4389 perde precisão ao virar taxa diária. Incremental (retoma do último dia gravado); base vazia → backfill desde 2000.

### 4. `scrape_b3_curva_di` — curva DI (já existia, agora com dois destinos)
O script **já baixava** o CSV completo do produto `PRE` da B3 e **jogava fora todos os vértices**, guardando só os 8 contratos DI1 em `MtmAnbima`. Agora arquiva também a curva inteira (du a du) em `di.db/CurvaDi` — **zero requisição a mais**.

Isso importa porque **a B3 não guarda histórico**: a API só expõe ~20 pregões. Rodando todo dia, acumulamos os snapshots que ela descarta — é o que permite reprecificar uma data passada. **Data fora da janela é WARNING com exit 0** (é "a fonte não tem", não "o scraper quebrou"); qualquer outra falha é exit 1.

### 5. `validar_calc_b3` — quais ativos a calc pode precificar

> ⚠️ **Mudou em 24/08/2026.** O `validar_fluxos` foi **removido** e o `validar_calc_b3` é o **único validador**. Ele não compara a agenda evento a evento: pergunta se a **nossa calc reproduz a B3** (primária) ou a **FI** (se a B3 não cobrir), em PU no par e fora do par, em 3 datas. O PU no par já valida fluxo+VNA; fora do par valida o desconto — então o teste de agenda era redundante. O `scrape_b3_bond_details` também deixou de marcar `stFluxoValidado = 1` sozinho: "veio da B3" não é o mesmo que "a calc precifica certo". Ver **[[10 - Scripts/validar_calc_b3]]** e **[[15 - Cadastro dos Ativos]]**.

O que continua valendo:

- **Régua:** divergente **ou não-confirmável** ⇒ **não valida** (rigor > cobertura).
- **FI é proibida** para ativo com incorporação: ela omite esses eventos, então nunca poderia confirmá-los. Fica não-validável — o que **não** é divergência.
- **Fila com throttle de 10 dias.** A invalidação do ingestor **zera** `dtUltimaTentativa`, então ativo cujo fluxo mudou volta pro topo da fila na hora.
- **Saída:** `data/divergencias_fluxo.csv`.

Estado em 13/07/2026: **3.027 validados** de 4.840 ativos (2.967 de fonte B3).

## Ordem no pipeline

Passos **9–12** de [[11 - Pipeline de Execucao]]. Não dependem da liquidação X → rodam uma vez por ciclo, no bloco global do `pipeline_core`. O `validar_calc_b3` vem **depois** do `scrape_anbima_data_ativos` (6): é ele quem mexe em `FluxoAtivos`/`InfoAtivos` e zera a validação quando o fluxo muda de verdade.

## Rede — hosts novos (conferir no banco)

`apisidra.ibge.gov.br` · `servicodados.ibge.gov.br` (calendário) · `api.bcb.gov.br` (SGS) · `www.anbima.com.br` (página de projeção). O `referenceRatesProxy` da B3 e as APIs B3 Calculator / FI Analytics já eram usados. **Testar esses 4 antes de confiar na rotina no banco.**

## Mudanças feitas na calc (todas autorizadas)

A calc **não é repositório git** — há uma cópia de segurança em `calculadora_rf.py.bak-AAAAMMDD` ao lado dela.

1. **`DIR_ARQUIVOS` via `CALCRF_FILES_DIR`** (12/07/2026) — descrita acima. Sem a env var, comportamento inalterado.
2. **`FatorDi`: contador de DU incremental** (12/07/2026) — era **quadrático**. Ele chamava `ContarDu(dataCalc, d)` a cada dia útil projetado, e o `ContarDu` varre dia a dia desde `dataCalc`; num CDI+ longo isso dava **19s por PU**, e o `CalcularTaxaNegociacao` faz até 100 PUs → **~30 min por negócio**. Agora `z` acumula (`z += 1`) em vez de ser recontado — é exatamente o mesmo número. Depois do fix: **8 negócios DI em 18,7s**.
3. **Aniversário por ativo** (12/07/2026) — `DIA_ANIV = 15` era **constante de módulo**, e o `CalcularVna` (ramo IPCA) só aplica evento que caia **exatamente** na data de aniversário calculada (`if anivAtual in eventos`, busca por data exata). Dia 15 é convenção de **NTN-B**; a debênture aniversaria no dia das **suas** datas de pagamento. Num papel de aniversário 28 (SSRU11), **os 25 eventos eram silenciosamente descartados** — o VNA ficava sem amortizar e o PU dava **15.403 contra 9.738** da B3. Agora `CalcularAniv`, `ObterUltimoProximoAniv`, `CalcularVna`, `CalcularPupar`, `CalcularPuOperacao` e `CalcularTaxaNegociacao` aceitam `diaAniversario`, **sempre por último na assinatura** e com **default 15** — o add-in do Excel e os gabaritos não mudam de comportamento. O `CalcularAniv` também passou a truncar dia 31 em mês de 30 (antes: `ValueError`).

4. **`CalcularPuOperacao`: a incorporação era IGNORADA** (13/07/2026) — o laço dos eventos futuros desempacotava `pctIncorp` e **nunca o usava**. Num evento de incorporação o juro **não é pago** (vira principal), mas a calc **pagava em dinheiro** e **não fazia o VNA crescer**. O erro é **invisível no par** — capitalizar um fluxo a `jEmi` e descontá-lo a `jNeg` tem VP idêntico quando as duas taxas são iguais — e só aparece **fora do par**. Foi por isso que o TRGP13 batia o PU par a **5,7e-09** e errava **1,3% a 100 bps do par**. Corrigido: `juros *= (1 - pctIncorp/100)`, `vnaVigente *= fatorIncorp`, e a face original cresce junto (mesma álgebra do `CalcularVna`). O ramo DI (`_PuOperacaoDi`) **já tratava certo** — o bug era exclusivo do IPCA.

**Gabaritos depois de tudo: 11 OK, 1 FAIL** — igual a antes. O FAIL (PALF38, VNA de 2025-09-15) é **anterior** e não passa por nenhuma das funções mexidas (verificado por instrumentação: `FatorDi` é chamado **zero** vezes nesse caso).

## O gate de aceitação — e por que o PU par não bastava

O `scripts/validar_calc_b3.py` compara o PU da calc com o da fonte em **duas** taxas:

1. **no par** (taxa de negociação = taxa de emissão) → valida o **fluxo** e o **VNA**
2. **a 100 bps do par** → valida o **desconto**

**Só no par não basta**, e isso custou caro: o TRGP13 batia o par a 5,7e-09 e errava 1,3% fora dele. Um ativo pode ter fluxo perfeito e desconto quebrado — e é o desconto que produz a **taxa** que vai para o relatório.

Estado em 13/07/2026 (3.023 ativos validados, referência 10/07):

| | ativos |
|---|---|
| batem **no par E fora dele** (≤ 1e-6) | **1.947 — 64,4%** |
| erram fora do par **por um fio** (1e-6 a 1e-5 ≈ 0,02 bps em taxa) | 596 — **579 CDI+, 16 %CDI, 1 PRE, zero IPCA** |
| erram fora do par de forma **material** (> 1e-4) | **3** |
| a investigar (> 1e-3) | 108 |

Com o sarrafo em 1e-5, **2.543 (84,1%)** passam nos dois testes. Os 596 são ruído numérico da projeção DI, não bug.

## Faxina de cadastro (14/07/2026) — 96,3% → 97,5%

Rodando o gate contra a régua certa (o `calcYield`/`calcPU` da B3, não a taxa gravada na base — ver [[project_calc_taxa_nao_fecha]]), os erros >1e-3 caíram de **114 para 76** ativos corrigindo **só dado nosso** (nada na `calculadora_rf.py`). Três bugs de cadastro, todos com a **mesma raiz**: scrapers secundários **sobrescreviam** o que a B3 (fonte primária) já tinha gravado certo.

1. **`vrTaxaEmissao` (cupom) errado — 20 ativos.** A planilha da **FI Analytics** sobrescrevia o `yield` da B3, às vezes em unidade diferente (RED711 gravado **250** em vez de 2,5 → PU errava **702%**; 24B0013203 como IPCA+1,4% em vez de 6,4%). Raiz: `scrape_fianalytics_planilha.py` fazia `vrTaxaEmissao = COALESCE(excluded, vrTaxaEmissao)` (FI vence) e roda **depois** da B3 no `RodarDia`. **Corrigido** para `COALESCE(vrTaxaEmissao, excluded)` (fill-only, igual a B3/Anbima).

2. **`cdIndexador` errado — 4 ativos.** FI e o scraper CRI/CRA sobrescreviam o `method` da B3. **CRA02300MJ8** era **%CDI** marcado como CDI+ → PU errava **565%**; BHIAC0 (CDI+↔%CDI), HSEI11 e SRGI12 (PRE↔IPCA). **Corrigido**: `cdIndexador` virou fill-only em `scrape_fianalytics_planilha.py` e `scrape_anbima_cri_cra.py`.

3. **`FluxoAtivos` defasado/espúrio — 19 ativos.** Dois padrões, ambos corrigidos regravando o fluxo a partir dos eventos **atuais** da B3: (a) **fluxo defasado** — datas de cupom desalinhadas do que a B3 emite hoje (ENEVA0 errava 3,2%; RENTE2/ITSA17/VERT13 ~10-15%); (b) **evento antes do `dtInicioRentabilidade`** — âncora de juros espúria em papéis que pagam juro no vencimento (RIOS21, IBIP11). **609 ativos** têm evento pré-início, mas ele só atrapalha nessas estruturas — a poda foi aplicada **só onde o gate melhora**, por ativo.
   > ⚠️ Raiz não fechada: `scrape_b3_bond_details.py` só re-scrapeia quem tem **info faltando** (o gate `faltando`); um fluxo que já existe **nunca é atualizado**, então ele **deriva** com o tempo. Item de backlog: refresh periódico do fluxo da B3.

**Gate depois da faxina (10/07, 3.047 validados): 76 ruins, 97,5% OK.**

## Snap da amortização no aniversário (14/07/2026) — 97,5% → 97,8%, e mata os erros catastróficos

O 5º bug da calc (autorizado). `CalcularVna` casava evento com aniversário por **data exata** (`if anivAtual in eventos`). Mas a B3 **fixa o montante da amortização no aniversário e liquida alguns DU depois** (lag de liquidação): 22D1226341 aniversaria dia 17 e paga dia 19; TPER11 aniversaria (segundo a B3) dia 15 mas amortiza em fim de mês. O evento não casava e era **silenciosamente descartado** → VNA ficava inteiro (TPER11 PU **653 vs 385**; 22D1226341 VNA **+30%**).

**Fix:** ao montar o dict `eventos`, cada evento é **encostado no aniversário mais próximo** (por dias corridos) em vez de exigir data exata. Evento já no aniversário encosta em si (no-op), então os ~1.226 IPCA que já batiam **não mudam** — confirmado: gabaritos seguem **11 OK / 1 FAIL** (o FAIL do PALF38 é anterior) e nenhuma regressão no gate. Prova de que a B3 fixa no aniversário: o snap leva 22D1226341 a **5,9e-8** (precisão de máquina), não a um ~0,01% de aproximação.

**Resultado: erros catastróficos zerados** (TPER11 70%→0,3%, 22D1226341 30%→0), IPCA de 65→57 ruins. Gate: **68 ruins, 97,8%** (CDI+ 99,5% · IPCA 95,6% · PREF 98,0% · %CDI 100%).

### Os 68 que sobram — pequenos, e por quê

| # | grupo | erro | causa |
|---|---|---|---|
| **57** | IPCA pro-rata/índice | 0,1-2,8% (quase tudo <1%) | **metodologia fina** — cadastro confere com a B3; o erro **oscila em torno de 1e-3 conforme a data** (SUZBC1 passa em 08/07 e 30/06, falha em 10/07 por um fio). É ruído da projeção/pró-rata do IPCA do mês corrente, não bug. **Corrigir `vrAniversario` por minimização num dia é SUPERAJUSTE** (testado: o "melhor aniversário" muda de dia pra dia) — não fazer. Poucos maiores (24G1674104 2,8%: fluxo com 121 eventos vs 151 da B3) são fluxo incompleto. |
| **7** | CDI+ residual | até 3,8% | **batem no par** (MATD23 1e-9), erram **fora do par** → é o problema conhecido da [[#⚠️ O que ainda NÃO fecha a taxa fora do par\|taxa fora do par]], não novo |
| **4** | PREFIXADO | — | a B3 devolve `getBondDetails` **vazio** hoje (TSSS15/VAMOA4/RDORE7/CEPEA5, `method=None`) — gap do lado da B3, não nosso |

Ou seja: **não sobra bug catastrófico** — o resto é pró-rata fino do IPCA, a taxa-fora-do-par (já mapeada abaixo) e buracos pontuais da B3.

## ⚠️ O que ainda NÃO fecha: a taxa fora do par

A calc reproduz o **PU par** das fontes com precisão (**86,4%** dos 2.861 ativos validados batem a 1e-6 — medido com o gate da época), mas **não reproduz a taxa implícita num PU fora do par**.

Triangulação em 4 negócios de 16/06/2026 (teste manual — o gate não pega isso):

| ativo | calc | FI Analytics | B3 | calc − B3 | FI − B3 |
|---|---|---|---|---|---|
| TRGP13 (IPCA) | 7,3838 | 7,3620 | 7,3620 | **+2,18 bps** | 0,00 |
| CRA02300MJ7 (%CDI) | 99,0308 | 98,9105 | 98,8934 | **+13,74 bps** | +1,71 |
| 22J0346710 (%CDI) | 91,9606 | 92,0145 | 92,0015 | **−4,09 bps** | +1,30 |

**FI e B3 concordam entre si; a calc discorda das duas.** Quando duas fontes independentes batem e a nossa diverge, o erro é nosso. Como os fluxos e o VNA estão certos (o PU par fecha, e o TRGP13 bate a 1e-6), a suspeita é a convenção de **desconto**.

**Lição de método:** o gate de PU par **não basta**. Ele valida o fluxo, não o desconto. O teste que falta é o **round-trip da taxa**: dado o PU que a fonte devolve para uma taxa **fora do par**, a calc tem que reproduzir aquela taxa.

**ATUALIZAÇÃO 15-19/07:** a calc foi **LIGADA** (`config.toml [calc] usarCalcTaxa = true`) para **CDI+/IPCA/PREFIXADO** validados, depois que o gate `validar_calc_b3` passou a garantir que a calc reproduz a B3/FI (ver [[16 - Confianca nos Validados (WIP)]]). **%CDI ficou de fora** — nele a calc erra o desconto fora do par (o "round-trip" que faltava). E a calc foi **otimizada (~100×)** com a memoização do Newton (19/07). Então o texto abaixo sobre "desligada" é histórico.

## O que ficou de fora (fase seguinte)

**Trocar a precificação daqui pela calc importada** — hoje o `calc_taxa_negocios` obtém a taxa de cada trade batendo em API (cascata FI Analytics → B3). Com a calc local isso vira cálculo em memória: no banco, onde cada chamada paga proxy, o passo leva ~25 min/dia. Depende de `stFluxoValidado = 1` (a calc só precifica fluxo confirmado) e exige repensar a cascata (calc → FI → B3 → NULL) + revalidar as taxas já gravadas. Item 5 do `MIGRACAO.md`, ver [[98 - Backlog]].
