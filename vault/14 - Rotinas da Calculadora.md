# Rotinas da Calculadora de Renda Fixa

> **O que é:** desde **12/07/2026**, este projeto passou a **rodar as rotinas de dados** que a calculadora de renda fixa consome, e a **validar o fluxo** dos ativos que ela pode precificar. Handoff original: `D:\ItauBBA\calculadora-renda-fixa\docs\MIGRACAO.md`.

## A divisão de responsabilidades

|  | `calculadora-renda-fixa` | **este projeto** (`negociacao-secundario`) |
|---|---|---|
| **Cálculo** (VNA, PU Par, PU de operação, duration, taxa) | ✅ é a **biblioteca** — `calculadora_rf.py`, `di.py` | consome via `lib/calc.py` |
| **Coleta de dados** (IPCA, projeção, DI) | — | ✅ **roda as rotinas** (passos 8–10 do pipeline) |
| **Validação de fluxo** | — | ✅ **roda** (passo 11) |

A calc continua morando em `D:\ItauBBA\calculadora-renda-fixa` e **não se mexe nela** (regra do projeto dela). Aqui ficam os dados e as rotinas.

## Onde vivem os dados

Em `code/data/`, junto do `trades.db` — este projeto é quem os coleta; a calc só lê:

| Arquivo | Tabelas | Alimentado por | Serve para |
|---|---|---|---|
| `data/ipca.db` | `IPCA`, `IPCAProjetado` | `scrape_ipca_ibge`, `scrape_ipca_projetado_anbima` | VNA de IPCA+ |
| `data/di.db` | `DiHistorico`, `CurvaDi` | `scrape_di_bcb`, `scrape_b3_curva_di` | %CDI e CDI+ (realizado + projeção) |
| `data/feriados_anbima.csv` | — | (estático) | régua de dias úteis — **fonte única**, a calc passou a ler a nossa |
| `data/trades.db` | `InfoAtivos` (5 colunas de validação), `FluxoAtivos` | ingestor + `validar_fluxos` | a calc só precifica `stFluxoValidado = 1` |

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

### 5. `validar_fluxos` — quais ativos a calc pode precificar
Ver §"Validação de fluxo" em [[04 - Banco de Dados]] para o contrato das 5 colunas. Aqui, o essencial:

- **Régua:** divergente **ou não-confirmável** ⇒ **não valida** (rigor > cobertura). Validado = zero divergência.
- **Cascata:** B3 (`getBondDetails`, via `lib/b3_calc_api.ObterDetalhesAtivo`) → FI Analytics (`lib/fianalytics_api.ChamarCompleto`) → não-validável.
- **FI é proibida** para ativo com incorporação na nossa base: ela omite esses eventos, então nunca poderia confirmá-los. Fica não-validável — o que **não** é divergência.
- **Fila com throttle de 10 dias:** `stFluxoValidado <> 1 AND (dtUltimaTentativa IS NULL OR < hoje-10d)`. Como a invalidação do ingestor **zera** `dtUltimaTentativa`, ativo cujo fluxo mudou volta pro topo da fila na hora. Rodar todo dia é barato.
- **Saídas:** `data/divergencias_fluxo.csv` (ticker, fonte, campo, situação, nosso, fonte) e `data/carencia_conferir_pu.csv` (os carência-100%, validados sem conferir a %incorporação — conferir por PU depois).
- Estado em 11/07/2026 (rodado ainda do lado da calc, sobre a mesma base): **2.040 validados** de 4.197 com fluxo. O grosso dos não-validados é `sem_fonte` (nem B3 nem FI cobrem) e agenda **truncada** na nossa base.

## Ordem no pipeline

Passos **8–11** de [[11 - Pipeline de Execucao]]. Não dependem da liquidação X → rodam uma vez por ciclo, no bloco global do `pipeline_core`. O `validar_fluxos` vem **depois** do `scrape_anbima_data_ativos` (5): é ele quem mexe em `FluxoAtivos`/`InfoAtivos` e zera a validação quando o fluxo muda de verdade.

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

O `scripts/conferir_pu.py` compara o PU da calc com o da fonte em **duas** taxas:

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

## ⚠️ O que ainda NÃO fecha: a taxa fora do par

A calc reproduz o **PU par** das fontes com precisão (**86,4%** dos 2.861 ativos validados batem a 1e-6 — ver `scripts/conferir_pu.py`), mas **não reproduz a taxa implícita num PU fora do par**.

Triangulação em 4 negócios de 16/06/2026 (`--date` do `conferir_pu` não pega isso; foi teste manual):

| ativo | calc | FI Analytics | B3 | calc − B3 | FI − B3 |
|---|---|---|---|---|---|
| TRGP13 (IPCA) | 7,3838 | 7,3620 | 7,3620 | **+2,18 bps** | 0,00 |
| CRA02300MJ7 (%CDI) | 99,0308 | 98,9105 | 98,8934 | **+13,74 bps** | +1,71 |
| 22J0346710 (%CDI) | 91,9606 | 92,0145 | 92,0015 | **−4,09 bps** | +1,30 |

**FI e B3 concordam entre si; a calc discorda das duas.** Quando duas fontes independentes batem e a nossa diverge, o erro é nosso. Como os fluxos e o VNA estão certos (o PU par fecha, e o TRGP13 bate a 1e-6), a suspeita é a convenção de **desconto**.

**Lição de método:** o gate de PU par **não basta**. Ele valida o fluxo, não o desconto. O teste que falta é o **round-trip da taxa**: dado o PU que a fonte devolve para uma taxa **fora do par**, a calc tem que reproduzir aquela taxa.

Por isso o `calc_taxa_negocios` tem a calc implementada como 1º degrau da cascata mas **desligada** (`config.toml [calc] usarCalcTaxa = false`). Ligar é uma linha — mas só depois de fechar o round-trip. Ver [[98 - Backlog]].

## O que ficou de fora (fase seguinte)

**Trocar a precificação daqui pela calc importada** — hoje o `calc_taxa_negocios` obtém a taxa de cada trade batendo em API (cascata FI Analytics → B3). Com a calc local isso vira cálculo em memória: no banco, onde cada chamada paga proxy, o passo leva ~25 min/dia. Depende de `stFluxoValidado = 1` (a calc só precifica fluxo confirmado) e exige repensar a cascata (calc → FI → B3 → NULL) + revalidar as taxas já gravadas. Item 5 do `MIGRACAO.md`, ver [[98 - Backlog]].
