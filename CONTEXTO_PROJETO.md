# CONTEXTO_PROJETO.md — contexto completo para agentes de IA

> Documento de contexto do projeto **negociação secundária de crédito privado**.
> Escrito para ser colado inteiro como prompt de contexto. Última verificação
> contra o código e o banco: **23/07/2026**.
>
> Regra de ouro para quem usar este arquivo: **ele descreve o que existe.** Se
> uma tarefa mencionar fonte, tabela ou script que não está aqui, provavelmente
> não existe no projeto — confirme antes de assumir. Ver §8 (o que NÃO existe).

> 🚧 **DESATUALIZADO desde 29/08/2026 quanto a ESTRUTURA e ARMAZENAMENTO.** O branch
> `refactor/split-bases` mudou três coisas que atravessam este documento inteiro:
> 1. **Pastas:** `lib/` → `Helpers/`, `scripts/` → `codigos/<nome>/<nome>.py`, `data/` →
>    `files/`. Imports achatados (`from db import X`).
> 2. **Armazenamento:** SQLite → **Parquet + DuckDB** (`Helpers/dados.py`), local ou
>    `s3://`. Os dados vão para a AWS, onde só há bucket + Athena.
> 3. **Chave dos negócios:** `idTrade` morreu; virou `cdIdentificadorNegocio`.
>
> **A lógica de negócio, o dicionário de dados e o motor de cálculo continuam válidos** —
> nomes de tabela e coluna não mudaram. O que mudou foi onde os bytes moram e como se
> chega neles. Ler `vault/17 - Armazenamento Parquet e AWS.md` junto com este arquivo.

---

## 1. O que o sistema faz

Gera relatórios HTML diários de negócios de **crédito privado brasileiro**
(Debêntures, CRIs, CRAs) negociados no mercado secundário, consolidando B3,
Anbima e FI Analytics. O produto final é o **spread over** de cada negócio —
quanto o papel pagou acima da referência de mercado (NTN-B para IPCA, DI1 para
prefixado).

Para chegar lá o sistema precisa, para cada negócio: descobrir a **taxa**
(nem sempre vem no boletim), descobrir a **referência** (qual vértice da curva
comparar) e descontar um do outro.

---

## 2. Layout do repositório

```
negociacao-secundario/
├── requirements.txt          Dependências (fonte única; code/ aponta para cá)
├── CLAUDE.md                 Convenções obrigatórias de código
├── CONTEXTO_PROJETO.md       Este arquivo
├── guia_projeto.html         Guia visual para leitura humana
├── PLANEJAMENTO_v5.md        Documento mestre de decisões de projeto
├── INSTALACAO_BANCO.md       Runbook de instalação no PC do banco
├── bundle_banco.py           Bundle auto-extraível (deploy sem git)
├── code/
│   ├── scripts/              26 scripts executáveis, cada um com CLI próprio
│   ├── lib/                  9 módulos compartilhados
│   ├── templates/            Jinja2 do relatório
│   ├── data/                 trades.db, ipca.db, di.db, CSVs de apoio, saídas
│   └── config.toml           Configuração (sem segredos)
├── docs/                     Relatórios de fase e backlogs técnicos
└── vault/                    Obsidian — fonte da verdade viva
```

**Projeto vizinho obrigatório:** `../calculadora-renda-fixa/calculadora_rf.py` —
a biblioteca de cálculo. Importada via `lib/calc.py`, localizada por
`config.toml [paths].calculadoraDir` ou pela env `CALCULADORA_DIR`.

---

## 3. Convenções de código (não negociáveis)

Detalhe completo no `CLAUDE.md`. Resumo do que mais quebra:

| elemento | convenção | exemplo |
|---|---|---|
| Tabelas SQLite | PascalCase, português | `NegociosBrutos`, `InfoAtivos` |
| Colunas SQLite | camelCase com prefixo semântico | `vrTaxaNegocio`, `cdTicker`, `dtLiquidacao` |
| Funções/classes Python | PascalCase, português | `LerArgumentos`, `ProcessarData` |
| Variáveis/parâmetros | camelCase, português | `dtRef`, `limiteTrades` |
| Constantes de módulo | UPPER_SNAKE | `SQL_UPSERT`, `MESES_PT` |
| Nome de arquivo | snake_case (única exceção) | `scrape_b3_boletim.py` |

**Prefixos de coluna:** `vr` = valor · `cd` = código/categoria · `dt` = data/hora
· `id` = identificador · `st` = status/flag booleana.

**PROIBIDO: `_` no início de qualquer nome** — nem função "privada", nem
constante, nem variável. (O `calculadora_rf.py` do projeto vizinho tem helpers
legados com `_`; é exceção histórica dele, não padrão a seguir.)

**Jargão que fica em inglês:** `vrSpreadOver`, `vrDuration`, `vrPU`, `cdISIN`,
`Mtm*`, `Outstanding`, `Broker`, `Yield`, e APIs de terceiros (`parse_args`,
`status_code`).

**`dest=` explícito no argparse** sempre que a flag tiver hífen
(`--email-dia` → `dest="emailDia"`), senão o argparse gera snake_case e quebra a
convenção — e o erro só aparece em runtime.

---

## 4. Dicionário de dados

### 4.1 `code/data/trades.db` — banco principal

#### `NegociosBrutos` (556.885 linhas) — PK `idTrade`
Negócios crus do boletim da B3, sem tratamento. Fonte: `scrape_b3_boletim.py`.

| coluna | tipo | papel |
|---|---|---|
| `idTrade` | INTEGER | PK autoincremento (interno) |
| `cdIdentificadorNegocio` | TEXT | Identificador do negócio no boletim B3 |
| `cdInstrumento` | TEXT | `DEB`, `CRI` ou `CRA` |
| `cdEmissor` | TEXT | Nome do emissor como veio da B3 |
| `cdTicker` | TEXT | Código do papel (chave de junção com `InfoAtivos`) |
| `vrQuantidade` | INTEGER | Quantidade negociada |
| `vrPU` | REAL | Preço unitário do negócio |
| `vrVolume` | REAL | Financeiro (quantidade × PU) |
| `vrTaxaNegocio` | REAL | Taxa **direta do boletim** — frequentemente NULL; é a razão de existir a cascata de taxa |
| `dtHorarioNegocio` | TEXT | Timestamp do negócio |
| `dtNegocio` | TEXT | Data do pregão |
| `cdISIN` | TEXT | ISIN quando o boletim traz |
| `dtLiquidacao` | TEXT | **Data de liquidação (D+1)** — é por ela que o relatório agrupa, não por `dtNegocio` |
| `cdSituacao` | TEXT | Situação do negócio no boletim |
| `dtCriacao` / `dtAtualizacao` | TEXT | Auditoria do UPSERT |

#### `NegociosProcessados` (522.670 linhas) — PK `idTrade`
Resultado do tratamento: taxa resolvida, duplicados classificados, spread calculado.

| coluna | tipo | papel |
|---|---|---|
| `idTrade` | INTEGER | FK para `NegociosBrutos` |
| `cdTicker`, `cdEmissor`, `dtNegocio`, `dtLiquidacao`, `vrQuantidade`, `vrPU`, `vrVolume` | — | Copiados do bruto |
| `vrTaxaCalculada` | REAL | **A taxa final do negócio**, venha de onde vier |
| `cdFonteTaxa` | TEXT | De onde veio: `Calc` (calculadora local), `FiAnalytics`, `B3`, ou NULL |
| `vrDuration` | REAL | Duration do papel na data (anos, base 252) |
| `vrSpreadOver` | REAL | **Entrega final**: taxa do negócio − taxa da referência |
| `idGrupoNegocio` | TEXT | Grupo do union-find do filtro de duplicados |
| `cdStatus` | TEXT | `VALIDO`, `BROKER`, `FUNDO` ou `PF` (ver §6) |
| `dtProcessamento` | TEXT | Quando foi processado |

#### `InfoAtivos` (5.102 linhas) — PK `cdTicker`
Cadastro do papel. **É a tabela mais importante do sistema** — quase todo bug de
precificação nasce de campo errado aqui.

| coluna | tipo | papel |
|---|---|---|
| `cdTicker` | TEXT | PK |
| `cdInstrumento` | TEXT | `DEB` (4.115) / `CRI` (478) / `CRA` (509) |
| `cdEmissor` | TEXT | Emissor |
| `dtVencimento` | TEXT | Vencimento |
| `vrDuration` | REAL | Duration em **anos base 252**. Ver armadilha em §7.3 |
| `dtAtualizacaoDuration` | TEXT | Data de referência da duration gravada |
| `cdIndexador` | TEXT | `IPCA` (1.695) / `CDI+` (2.761) / `%CDI` (292) / `PREFIXADO` (354) |
| `cdReferencia` | TEXT | Vértice de comparação: `NTN-B 35`, `DI1F29`… |
| `cdFonteReferencia` | TEXT | `Anbima` (1.630) ou `MatchRef` (560). O match **nunca sobrescreve** `Anbima` |
| `vrTaxaEmissao` | REAL | Taxa do cupom de emissão (% a.a. base 252) — insumo da calculadora |
| `vrVNE` | REAL | Valor Nominal de Emissão |
| `dtInicioRentabilidade` | TEXT | Data em que a rentabilidade começa a correr |
| `cdISIN` | TEXT | ISIN |
| `vrQuantidadeEmissao` | REAL | Quantidade emitida — **proxy de outstanding** na Visão Anbima |
| `dtEmissao` | TEXT | Data de emissão |
| `stTemFluxo` | INTEGER | 1 se há agenda em `FluxoAtivos` |
| `stFluxoValidado` | INTEGER | **1 = a calculadora local reproduz a B3** neste papel. Gate para usar a calc na cascata de taxa |
| `dtValidacaoFluxo` | TEXT | Quando foi validado (revalida a cada 15 dias) |
| `cdFonteValidacaoFluxo` | TEXT | Qual oráculo confirmou: `B3` ou `FiAnalytics` |
| `dtUltimaTentativa` | TEXT | Anti-retry: evita re-scrape eterno de quem a fonte não cobre |
| `vrAniversario` | INTEGER | **Dia do mês em que o papel aniversaria.** Default 15 (NTN-B). Se errado, os eventos do fluxo não casam e são silenciosamente ignorados no VNA |
| `cdFonteCadastro` | TEXT | `B3` (3.062) ou `AnbimaData` (17). Ver §7.1 |
| `cdTipoAmortizacao` | TEXT | `saldo_original` ou `saldo_restante` — base sobre a qual o % de amortização incide |

#### `FluxoAtivos` (102.253 linhas) — PK (`cdTicker`, `dtEvento`)
Agenda de eventos do papel. É o insumo direto do motor de cálculo.

| coluna | tipo | papel |
|---|---|---|
| `cdTicker` | TEXT | FK |
| `dtEvento` | TEXT | Data do evento |
| `vrPctAmortizacao` | REAL | % amortizado neste evento |
| `vrPctIncorporacao` | REAL | % dos juros do período **incorporado ao principal** (não pago em caixa) |
| `dtAtualizacao` | TEXT | Auditoria |

#### `AnbimaIndicativos` (136.070 linhas) — PK (`cdTicker`, `dtReferencia`)
Taxa indicativa diária da Anbima por papel.

| coluna | papel |
|---|---|
| `vrTaxaAnbima` | Taxa indicativa publicada |
| `vrSpreadAnbima` | Spread da taxa indicativa contra a referência (calculado por `calc_spread_anbima.py`) |

#### `MtmAnbima` (667 linhas) — PK (`cdTicker`, `dtReferencia`)
**Curvas de referência. Duas origens diferentes convivem aqui** — o nome da
tabela engana:

| `cdTicker` | origem real | script |
|---|---|---|
| `NTN-B ...` | Anbima (mercado secundário de títulos públicos) | `scrape_anbima_ntnb.py` |
| `DI1F...` | **B3** (curva pré × DI) | `scrape_b3_curva_di.py` |

Colunas: `vrTaxa`, `vrDuration`.

#### `Outstanding` (0 linhas) — PK (`cdTicker`, `dtOutstanding`)
Saldo em circulação via Bloomberg. **Vazia fora do PC do banco.** Quando vazia,
a Visão Anbima cai automaticamente no proxy `vrQuantidadeEmissao`.

### 4.2 `code/data/ipca.db` — insumo da calculadora
**Schema é contrato com a calculadora — não renomear** (não segue o padrão `vr/cd/dt`).

| tabela | PK | colunas |
|---|---|---|
| `IPCA` (565) | `dtIPCA` | `vrIndiceIPCA`, `dtDivulgacaoIPCA` |
| `IPCAProjetado` (399) | `dtIPCAProjetado` | `vrProjecaoIPCA` |

### 4.3 `code/data/di.db` — insumo da calculadora

| tabela | PK | colunas | papel |
|---|---|---|---|
| `CurvaDi` (6.881) | (`dtReferencia`, `du`) | `diasCorridos`, `vrTaxa` | Curva pré×DI inteira, vértice a vértice — projeção do DI futuro |
| `DiHistorico` (6.665) | `dtReferencia` | `vrTaxaDiAnual`, `vrTaxaDiDiaria` | DI **realizado** (BCB série 12) |

### 4.4 Arquivos de apoio (não-banco)

**Entradas versionadas** — fazem parte do código, editadas à mão:

| Arquivo | Quem lê / grava | Finalidade |
|---|---|---|
| `code/data/feriados_anbima.csv` | **lê:** `lib/calc.py`, `scrape_b3_curva_di.py`, `gerar_relatorio_credito.py`, `gerar_relatorio_html.py`, `pipeline_core.py` | Calendário de feriados ANBIMA. Base de todo `ContarDu` — **é o insumo mais crítico do motor** |
| `code/data/anbima_skip_tickers.csv` | **lê:** `scrape_anbima_data_ativos.py` | Skip-list manual: tickers que a Anbima Data não cobre, para não re-scrapear eternamente |
| `code/config.toml` | **lê:** `lib/config.py` (todos os scripts) | Paths, thresholds do filtro, cascata de taxa, mapa de nomes de env vars |
| `requirements.txt` | pip | Dependências (raiz; `code/requirements.txt` aponta para cá) |

**Saídas de diagnóstico** — regeneráveis, gitignored, centralizadas em
`code/data/diagnosticos/`:

| Arquivo | Quem grava | Finalidade |
|---|---|---|
| `diagnosticos/validar_calc_b3.csv` | `validar_calc_b3.py` | Veredito do gate por ativo: erro de PU, oráculo que confirmou |

**Diretórios de saída** (todos gitignored e regeneráveis): `data/logs/`,
`data/relatorios/`, `data/emails/`, `data/debug/`, `data/anbima_data_raw/`,
`data/api_samples/`, `data/backups/`.

**Órfãos identificados em 23/07/2026** — existem no disco, **nenhum código os
referencia**. Deixados no lugar para o dono decidir:

| Arquivo | Situação |
|---|---|
| `code/data/cobertura_fontes.csv` | Sobra de diagnóstico ad-hoc (12/07). Sem leitor nem escritor no código |
| `code/data/fianalytics_resposta.json` | Amostra de resposta da API (22/07). Sem leitor no código |
| `code/data/trades_backup_pre_ptbr_20260629.db` | Backup de 164 MB anterior à renomeação PT-BR. Avaliar se ainda é necessário |

---

## 5. Fontes de dados — mapa verificado

| Dado | Fonte real | Script | Destino |
|---|---|---|---|
| Negócios do secundário | **B3** — Boletim Diário (Playwright, iframe `arquivos.b3.com.br/bdi`) | `scrape_b3_boletim.py` | `NegociosBrutos` |
| Cadastro + agenda de eventos | **B3** — `getBondDetails/{ticker}` (**fonte primária**) | `scrape_b3_bond_details.py` | `InfoAtivos`, `FluxoAtivos` |
| Cadastro + agenda (fallback) | **Anbima Data** (Playwright) | `scrape_anbima_data_ativos.py` | `InfoAtivos`, `FluxoAtivos` |
| Taxa indicativa debêntures | **Anbima** — XLS diário | `scrape_anbima_debentures.py` | `AnbimaIndicativos` |
| Taxa indicativa CRI/CRA | **Anbima** — portal (blob URL, exige browser) | `scrape_anbima_cri_cra.py` | `AnbimaIndicativos` |
| Curva NTN-B | **Anbima** — XLS mercado secundário, aba NTN-B | `scrape_anbima_ntnb.py` | `MtmAnbima` (`NTN-B *`) |
| Curva DI futuro | **B3** — API de derivativos (`referenceRatesProxy`) | `scrape_b3_curva_di.py` | `MtmAnbima` (`DI1F*`) **e** `di.db/CurvaDi` |
| DI realizado | **BCB** — série 12 | `scrape_di_bcb.py` | `di.db/DiHistorico` |
| IPCA número-índice | **IBGE** | `scrape_ipca_ibge.py` | `ipca.db/IPCA` |
| Projeção de IPCA | **Anbima** (HTML) | `scrape_ipca_projetado_anbima.py` | `ipca.db/IPCAProjetado` |
| Cadastro/duration complementar | **FI Analytics** — CSV via login | `scrape_fianalytics_planilha.py` | `InfoAtivos` |
| Taxa/PU (fallback) | **FI Analytics** API e **B3** `calcPU`/`calcYield` | `lib/fianalytics_api.py`, `lib/b3_calc_api.py` | usado na cascata |
| Outstanding | **Bloomberg** (`xbbg`, só no banco) | `scrape_outstanding_bloomberg.py` | `Outstanding` |

**Janelas de retenção das fontes (crítico):** a B3 só expõe **~20 pregões** da
curva DI; Anbima CRI/CRA ~5 pregões. O arquivamento diário é o que permite
reprecificar o passado — **buraco não é recuperável depois**.

---

## 6. Pipeline — ordem de execução

Definido em `scripts/pipeline_core.py`. Para a liquidação **X** (com `Xant` = dia
útil anterior):

```
 1. Boletim(Xant, X)              → NegociosBrutos
 2. BondDetails(Xant, X)          → cadastro B3 (PRIMÁRIO, roda antes da Anbima)
 3. AnbimaDeb + AnbimaCriCra      → taxas indicativas (Xant e X)
 4. FiAnalytics                   → cadastro complementar
 5. AnbimaData(Xant, X)           → cadastro fallback
 6. Ntnb(Xant, X) + CurvaDi       → curvas de referência
 7. IpcaIbge + IpcaProjetado + DiBcb   → insumos da calculadora
 8. ValidarFluxos                 → confere a agenda
 9. ValidarCalcB3                 → GATE: a calc reproduz a B3? senão desvalida
10. CalcTaxa(X)                   → cascata de taxa
11. Filtrar(X)                    → duplicados (union-find)
12. SpreadAnbima(Xant, X)         → spread da indicativa
13. MatchRef                      → duration-match → cdReferencia
14. SpreadOver(X)                 → entrega final
15. Relatorio                     → HTML
```

### Cascata de taxa (passo 10)
```
taxa direta do boletim  →  calculadora LOCAL  →  FI Analytics  →  B3  →  NULL
```
A calc local só entra com `stFluxoValidado = 1` e indexador em
`["CDI+", "IPCA", "PREFIXADO"]`. **`%CDI` e não-validados seguem direto para
FI→B3** — a calc não reproduz o desconto de %CDI fora do par.

Distribuição real: `FiAnalytics` 132.920 · `Calc` 21.215 · `B3` 1.324 · NULL 367.211.

### Filtro de duplicados (passo 11)
Union-find sobre (ticker, quantidade). Status resultante:
`VALIDO` (138.568) · `FUNDO` (313.518) · `BROKER` (34.057) · `PF` (36.527).
Thresholds em `config.toml [filtro]`.

---

## 7. Armadilhas conhecidas (leia antes de mexer)

### 7.1 Cadastro é pacote indivisível
`vrVNE` + `dtInicioRentabilidade` + `FluxoAtivos` de um papel **têm que vir da
mesma fonte**. `cdFonteCadastro` registra qual. Misturar B3 com Anbima conta a
carência duas vezes, em silêncio.

### 7.2 `vrAniversario` errado apaga eventos
A calculadora casa evento do fluxo com o aniversário do papel. Se `vrAniversario`
estiver errado, os eventos não caem no aniversário e são **descartados sem
aviso** — o VNA sai inteiro e o PU explode.

### 7.3 `vrDuration` não é recalculada
O pré-passo do `match_referencias.py` roda `WHERE ia.vrDuration IS NULL`. Quem já
tem duration gravada **nunca é atualizado** — melhoria no motor não alcança esses
papéis. A fonte da duration não é persistida, então não dá para saber quais
vieram da calc. Item aberto no backlog (`--forcar-duration`).

### 7.4 Semântica de incorporação diverge (latente)
Eventos **futuros** usam a semântica correta (`GerarFluxosFuturos`: paga a fração
não incorporada e amortiza sobre a base já crescida). Eventos **passados**
(`_CaminharVnaDi`, `CalcularVna`) usam `elif` e **descartam a amortização** do
evento. Hoje é inerte — todos os eventos divergentes já são passados. Quebra por
calendário. Ver `docs/BACKLOG_INCORP.md`.

### 7.5 Relatório corta em D-1
Nunca publicar o pregão de hoje: não fechou e é meia perna D+1.

---

## 8. O que **não** existe neste projeto

Registrado porque já foi assumido por engano:

- **CVM** — nenhum informe diário, nenhuma agenda. Zero código.
- **Selic** — o BCB é consultado apenas para o **DI realizado** (série 12).
- **Ofertas primárias / re-indexações da B3** — não coletadas.
- **pandas, numpy, requests, lxml, openpyxl** — não são importados por nenhum
  script (estavam no `requirements.txt` antigo sem uso).
- **Testes automatizados além de `code/tests_fase1.py`** — não há pytest, não há CI.
- **venv** — decisão de projeto: instalação direta.

---

## 9. Motor de cálculo — `calculadora_rf.py`

Repo vizinho `calculadora-renda-fixa`. **Não alterar sem OK explícito do usuário.**

### 9.1 Arquitetura: um gerador + Strategy por indexador

Desde a FASE 3 (23/07/2026) existe **um único ponto** com a máquina de estado do
fluxo. Antes eram 7 cópias, o que custou dois bugs do mesmo tipo (incorporação
ignorada no PU e depois na Duration).

```python
GerarFluxosFuturos(dataCalc, dataInicioRent, taxaEmissao, fluxo, vne,
                   estrategia, tipoAmort, ctx)  ->  yield (dataEv, du, valorFuturo)
```

Estado carregado por evento:
- `vna` — saldo devedor corrente (base dos juros e da amortização `saldo_restante`)
- `face` — face original atualizada (base da `saldo_original`; não cai com a
  amortização, mas **cresce** com incorporação)
- `ancora` — data do último evento, de onde correm os juros do próximo período

Regra de incorporação (`pctIncorp > 0`): a fração `pctIncorp` dos juros não é
paga e vira principal (`vna` e `face` crescem por `1 + pctIncorp/100 × (FJ−1)`);
o restante `(1 − pctIncorp/100)` é pago em caixa. Com `pctIncorp = 100` o evento
não gera caixa e o valor futuro sai 0.

### 9.2 O que varia por indexador (a Strategy)

| método | IPCA / PREFIXADO | %CDI / CDI+ |
|---|---|---|
| `VnaNaData` | `CalcularVna` (indexado ou nominal) | `_CaminharVnaDi` |
| `FatorPeriodo` | `round((1+j)^(du/252), 9)` | `_FatorPeriodoDi` |
| `FatorDescontoBase` | `1.0` | Fator DI puro (`CDI+`) / `None` (`%CDI`) |
| `AplicarTaxaDesconto` | `round((1+j)^(du/252), 9)` | `base × (1+spread)^(du/252)` ou `FatorDi` |

Classes: `EstrategiaIpca` e `EstrategiaPrefixado` derivam de
`EstrategiaTaxaFixa`; `EstrategiaCdi` cobre `%CDI` e `CDI+`.
Fábrica: `ResolverEstrategia(cdIndexador)` + `MontarContexto(...)`.

`FatorDescontoBase` / `AplicarTaxaDesconto` separam a fatia do desconto que **não**
depende da taxa negociada — é o que permite ao solver PU→taxa caminhar o fluxo
uma vez só. Em `%CDI` a base é `None` porque o percentual entra *dentro* do fator
diário e não há o que separar.

### 9.3 Todos os consumidores são map/reduce sobre o mesmo gerador

| função | redução |
|---|---|
| `CalcularPuOperacao` | `Σ Trunca(FV / FatorDesconto, 6)` |
| `CalcularDuration` | `[Σ(du·VP) / Σ VP] / 252` → **anos** |
| `CalcularDv01` | materializa o fluxo 1×, desconta 2× (taxa e taxa+1bp) |
| `CalcularTaxaNegociacao` | walk 1×, Newton só redesconta; bisseção como fallback |

Fora do gerador (VNA/PU par): `CalcularVna`, `CalcularPupar`.

### 9.4 Convenções numéricas (ANBIMA)

| item | regra |
|---|---|
| Base de dias | **BUS/252** (dias úteis, calendário ANBIMA) |
| Fator C acumulado (VNA) | **truncado** em 8 casas |
| VNA final | **arredondado** em 6 casas |
| Fator de juros / desconto | **`round`** em 9 casas |
| PU Par / PU Operação | **truncados** em 6 casas |
| Somatório do PU | `sum()` — compensação de Neumaier (CPython ≥ 3.12) |

**Truncar ≠ arredondar, e a escolha alterna por etapa** — é a fonte nº 1 de
divergência de centavos ao reimplementar.

### 9.5 Precisão validada

- PU vs `/calcPU` da B3: **~1e-8** relativo nos tickers de controle.
- Duration vs `cashFlowList` da B3: bate a **4 casas** (6/6 papéis testados).
- Gate `validar_calc_b3.py`: régua **1e-5** relativo (R$ 0,01 por 1.000 de face).

---

## 10. Segredos e configuração

Nunca há valor de segredo no repositório. `config.toml [env]` mapeia cada segredo
para **nomes** de variáveis de ambiente, e `lib.config.get_secret` tenta cada
candidato em ordem:

| segredo | candidatos |
|---|---|
| `b3CalcToken` | `token_calc_B3`, `B3_CALC_TOKEN` |
| `fianalyticsApiKey` | `token_fianalytics`, `FIANALYTICS_API_KEY` |
| `fianalyticsUser` / `Pass` | `user_fianalytics` / `password_fianalytics` |
| proxy | `proxy_http` / `HTTP_PROXY` / `http_proxy` |
| `calculadoraDir` | `CALCULADORA_DIR` |

No PC pessoal os valores vêm de `code/.env` (gitignored); no banco, do ambiente.
`scripts/check_no_secrets.py` valida antes de publicar.

---

## 11. Como rodar

```bash
cd code
pip install -r requirements.txt && playwright install chromium

python run_diario.py --start 2026-07-20 --end 2026-07-22   # pipeline completo
python scripts/scrape_b3_boletim.py --date 2026-07-22      # um passo isolado
python tests_fase1.py                                       # testes do motor
python scripts/validar_calc_b3.py --dry-run --limite 50     # gate, sem gravar
```

Todo script é **idempotente** (UPSERT), tem CLI próprio e **manda email ao final**
(sucesso ou erro). Para rodadas em lote isso incomoda — ver item `--sem-email` no
backlog; hoje o contorno é `email.ativo=false` no config ou `NEGSEC_SEM_EMAIL=1`.

---

## 12. Onde continuar lendo

| assunto | arquivo |
|---|---|
| Decisões de projeto (mestre) | `PLANEJAMENTO_v5.md` |
| Convenções de código | `CLAUDE.md` |
| Instalação no banco | `INSTALACAO_BANCO.md` |
| Refatoração do motor | `docs/RELATORIO_FASE3_FINAL.md` |
| Pendência de incorporação | `docs/BACKLOG_INCORP.md` |
| Backlog geral | `vault/98 - Backlog.md` |
| Estado da implementação | `vault/09 - Progresso.md` |
| Guia visual | `guia_projeto.html` |
