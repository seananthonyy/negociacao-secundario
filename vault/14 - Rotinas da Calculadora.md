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

## Mudanças feitas na calc (as duas únicas, ambas autorizadas)

1. **`DIR_ARQUIVOS` via `CALCRF_FILES_DIR`** (12/07/2026) — descrita acima. Sem a env var, comportamento inalterado.
2. **`FatorDi`: contador de DU incremental** (12/07/2026) — era **quadrático**. Ele chamava `ContarDu(dataCalc, d)` a cada dia útil projetado, e o `ContarDu` varre dia a dia desde `dataCalc`; num CDI+ longo isso dava **19s por PU**, e o `CalcularTaxaNegociacao` faz até 100 PUs → **~30 min por negócio**. Agora `z` acumula (`z += 1`) em vez de ser recontado — é exatamente o mesmo número. Depois do fix: **8 negócios DI em 18,7s**. Gabaritos da calc inalterados (o único FAIL é anterior e não chama `FatorDi` — verificado por instrumentação).

## O que ficou de fora (fase seguinte)

**Trocar a precificação daqui pela calc importada** — hoje o `calc_taxa_negocios` obtém a taxa de cada trade batendo em API (cascata FI Analytics → B3). Com a calc local isso vira cálculo em memória: no banco, onde cada chamada paga proxy, o passo leva ~25 min/dia. Depende de `stFluxoValidado = 1` (a calc só precifica fluxo confirmado) e exige repensar a cascata (calc → FI → B3 → NULL) + revalidar as taxas já gravadas. Item 5 do `MIGRACAO.md`, ver [[98 - Backlog]].
