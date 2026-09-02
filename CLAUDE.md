# Projeto: Relatório Diário de Negociação Secundária de Crédito Privado

Este projeto gera relatórios HTML diários de negócios de crédito privado brasileiro (Debêntures, CRIs, CRAs) consolidando dados da B3, FI Analytics e Anbima.

> ▶️ **RETOMANDO O TRABALHO? Leia `RETOMAR_AQUI.md` na raiz.** É o handoff da última
> sessão: onde o projeto parou, o que mudou nos números e por quê, o que fazer a seguir, e
> as armadilhas da camada de dados nova.

> 🚧 **LEIA PRIMEIRO — branch `refactor/split-bases` (31/08/2026).** Ele reorganizou a
> estrutura de pastas e trocou o armazenamento de SQLite por **Parquet + DuckDB** (os dados
> vão para a AWS: bucket S3 + Athena, sem banco SQL). **A conversão está completa**: os 21
> scripts, os três notebooks e o `tests_fase1.py` falam Parquet, e o `db.py` foi aposentado.
> O branch **ainda não foi mergeado** e falta o acesso à AWS (depende de Quant/TI).
> **Leia `vault/17 - Armazenamento Parquet e AWS.md` antes de tocar em qualquer código.**
> Partes do vault ainda descrevem o `main` e estão marcadas.

> **Instalando/migrando para o PC do banco?** Se o usuário pedir "me diga o que fazer" / "como instalo isto", siga o runbook **`INSTALACAO_BANCO.md`** (raiz) — passo a passo de pastas, dependências, segredos, teste de fluxos e montagem da base. Contexto da migração em `vault/13 - Migracao Banco.md`.

## Documento mestre

**Toda decisão de projeto está em `PLANEJAMENTO_v5.md` na raiz.** Leia esse arquivo antes de qualquer ação. É a fonte da verdade.

**Para contexto técnico rápido (arquitetura, dicionário de dados do banco, mapa do motor de cálculo): `CONTEXTO_PROJETO.md` na raiz.** Foi escrito para ser colado inteiro como contexto e é verificado contra o código. O `guia_projeto.html` é a mesma informação em formato visual, para leitura humana.

## Estrutura

```
negociacao-secundario/
├── code/                    <- é a pasta "z antoniooliveira" no PC do banco
│   ├── Helpers/             Módulos compartilhados (era lib/). Achatado: `from db import X`
│   │   ├── dados.py         ★ A CAMADA DE DADOS: Parquet + DuckDB (substitui o db.py)
│   │   ├── config.py        Lê files/config.toml; ANCORA todo [paths] na raiz
│   │   ├── cadastro_b3.py   Leitura do getBondDetails (dois donos: scraper e validador)
│   │   ├── pipeline_core.py Orquestrador (resolve codigos/<n>/<n>.py)
│   │   ├── datas.py         Dias uteis + feriados Anbima (fonte unica; era copiado 4x)
│   │   └── calc.py, logger.py, email_outlook.py, b3_calc_api.py, fianalytics_api.py
│   ├── files/
│   │   ├── config.toml, .env
│   │   ├── Database/        ipca.db, di.db, feriados_anbima.csv — os 3 que a CALC lê,
│   │   │                    e ela exige os três na MESMA pasta (CALCRF_FILES_DIR)
│   │   ├── Parquet/         ★ A BASE. [dados] raiz — vira "s3://bucket/..." no banco
│   │   ├── templates/, relatorios/, anbima_data_raw/, logs/, emails/
│   ├── codigos/<script>/    Uma pasta por código: <script>.py + logs/ dentro
│   └── requirements.txt
├── vault/                   Obsidian — fonte da verdade viva do projeto
└── .claude/agents/          Subagents customizados (coder, documenter)
```

**Cada código roda sozinho, de qualquer diretório.** Nenhum script importa outro; o que é
compartilhado vive em `Helpers/`. Os `[paths]` são ancorados na raiz do projeto, não no cwd.

## Calculadora de renda fixa (projeto vizinho)

`D:\ItauBBA\calculadora-renda-fixa` é a **biblioteca de cálculo** (precifica: VNA, PU Par, PU de operação, duration). **Não modificar `calculadora_rf.py` sem permissão explícita do usuário.**

Desde 12/07/2026, **este** projeto roda as rotinas de dados que ela consome (IPCA, projeção de IPCA, DI, curva DI) e valida o fluxo dos ativos que ela pode precificar. Os bancos `files/Database/ipca.db` e `di.db` são nossos; o schema deles é **contrato com a calc** (não segue o prefixo `vr/cd/dt` — não renomear). Import via `Helpers/calc.py`. Ver **[[14 - Rotinas da Calculadora]]** no vault.

⚠️ **`ipca.db`, `di.db` e `feriados_anbima.csv` têm de ficar na MESMA pasta** — a calc lê os
três de `CALCRF_FILES_DIR`, pelo nome do arquivo. Apontar para o lugar errado **não dá erro**:
o SQLite cria um `ipca.db` vazio lá e a calc passa a rodar sem série de IPCA. Já aconteceu
(29/08) e contaminou uma rodada inteira de `match_referencias`.

## Agentes customizados

- **coder** — implementa scripts em `code/codigos/<nome>/` e módulos em `code/Helpers/`. Lê o vault para contexto.
- **documenter** — mantém o vault Obsidian sincronizado com o código atual. Atualiza notas em `vault/` conforme o coder progride.

Use `/agents` no Claude Code pra ver/invocar.

## Convenções fechadas (não revisitar sem motivo forte)

- **Python 3.11+**, sem venv, sem tests automatizados
- **Armazenamento: Parquet + DuckDB** (`Helpers/dados.py`), em `files/Parquet/` ou num
  `s3://` — é **uma linha** do `config.toml` (`[dados] raiz`). O SQLite saiu: os dados
  precisam viver na AWS e lá só há bucket + Athena. Ver [[17 - Armazenamento Parquet e AWS]].
  - **O SQL continua o mesmo** — as views do DuckDB têm o nome das tabelas antigas.
  - Tabela **SÉRIE** (particionada por data) reescreve o dia inteiro; tabela **ESTADO**
    (`InfoAtivos`, `FluxoAtivos`) reescreve o arquivo inteiro. Não existe UPDATE em disco.
  - **Tipo de coluna é declarado, nunca inferido** (`ESQUEMA` em `dados.py`): coluna toda
    NULL num dia seria inferida como tipo `null` e quebraria a leitura das outras partições.
  - **Escrita parcial vai por `Mesclar()`**, com política por coluna (`SOBRESCREVER` /
    `PREFERIR_NOVO` / `PREFERIR_ATUAL`) — é o `ON CONFLICT DO UPDATE` do SQLite. `Upsert()`
    troca a linha inteira e só serve a quem é dono de todas as colunas.
  - **Grave em LOTE, nunca por ativo:** `InfoAtivos` e `FluxoAtivos` são arquivos únicos.
  - `ipca.db` e `di.db` seguem SQLite — são **contrato com a calculadora**, não nossos.
- **Chave dos negócios: `cdIdentificadorNegocio`** (a que a B3 manda). O `idTrade`
  (`AUTOINCREMENT`) morreu com o SQLite — fora dele ninguém gera esse número.
  ⚠️ O `PLANEJAMENTO_v5.md` ainda cita `idTrade` em 12 lugares — é texto histórico.
- **Paths ancorados na raiz do projeto**, nunca no cwd (`AncorarPaths` em `Helpers/config.py`).
  Antes eram relativos ao cwd: rodar um script de outra pasta fazia o SQLite criar um banco
  vazio ali e o script terminava "com sucesso", sobre nada. (O SQLite saiu, mas a âncora
  continua valendo — sem ela o Parquet nasceria numa raiz errada, com o mesmo silêncio.)
- **Tabelas**: PascalCase e **em português** (`NegociosBrutos`, `NegociosProcessados`, `InfoAtivos`, `FluxoAtivos`)
- **Colunas**: camelCase com prefixos `vr` (valor), `cd` (código/categoria), `dt` (data/hora), `id` (identificador)
- **Nomes de arquivo de script**: snake_case, sem prefixo numérico (`scrape_b3_boletim.py`, não `01_scrape_...`). Nome de arquivo é a **única** coisa em snake_case.
- **Um código, uma pasta**: `codigos/<nome>/<nome>.py`, com o `logs/` dentro. Cada um roda
  sozinho, de qualquer diretório. **Nenhum script importa outro** — o que dois precisam vai
  para `Helpers/` (foi o caso do `cadastro_b3.py`).
- **Email**: `NEGSEC_SEM_EMAIL=1` no ambiente roda qualquer script sem tocar no Outlook,
  gravando o corpo em `files/emails/`. Usar sempre em teste e em rodada de lote.
- **Python funções e classes**: PascalCase, **em português** (`LerArgumentos`, `ProcessarData`, `MontarUrl`, `AnalisarCsv`, `Principal`)
- **Python variáveis e parâmetros**: camelCase, em português (`dtRef`, `cdTicker`, `anbimaRows`, `limiteTrades`)
- **Constantes de módulo**: UPPER_SNAKE (`SQL_UPSERT`, `MESES_PT`) — o `_` **interno** é permitido
- **NUNCA `_` no início de nome nenhum** (nem função "privada", nem constante, nem variável). `_ParseArgs`, `_SQL_UPSERT`, `_smoke` são todos proibidos.
- **Exceções que ficam em inglês**: jargão de mercado (`vrSpreadOver`, `vrDuration`, `vrPU`, `cdISIN`, `Mtm*`, `Outstanding`, `Broker`, `Yield`), API de terceiros (`parse_args`, `status_code`, `format_exc`), nomes de módulo/arquivo, e **chaves de contrato** (colunas do banco, variáveis do template Jinja como `data_json`).
- **`dest=` explícito no argparse** sempre que a flag tiver hífen (`--email-dia` → `dest="emailDia"`), senão o argparse gera `email_dia` em snake_case e quebra a convenção — e o mismatch só aparece em runtime.
- **Sem CHECK constraints** no schema
- **Cada script é independente**, idempotente, com CLI próprio (`--date` ou `--start --end`)
- **Email no fim de cada script** via Outlook (`pywin32`), sucesso ou erro
- **Filtro de duplicados**: union-find, status só `PRIMARY` ou `DUPLICATE`
- **Cascata de taxa** (19/07/2026): taxa direta do boletim → **calc local** (`[calc].usarCalcTaxa=true`, só `stFluxoValidado=1` e indexador em `["CDI+","IPCA","PREFIXADO"]`) → FI Analytics → B3 → NULL (só Deb/CRI/CRA). A confiança da calc é garantida pelo gate **`validar_calc_b3`** (passo 13 do pipeline). **%CDI e não-validados seguem em FI→B3.** ⚠️ **A justificativa do %CDI foi re-medida em 01/09/2026 e não se sustenta mais**: em 40 ativos de maior volume, a taxa da calc reproduz a B3 com mediana de 0,05 bps e pior caso de 0,46 bps (40/40 dentro de 1 bp). O que os reprova é a régua de PU (`TOL_PU = 1e-5`), mais apertada em termos econômicos que a própria `TOL_TAXA_BPS = 5,0` do mesmo gate. Decisão pendente do usuário — ver o backlog. Ver [[16 - Confianca nos Validados (WIP)]] e [[14 - Rotinas da Calculadora]].
- **Cadastro dos ativos**: a **B3** (`getBondDetails`) é a fonte **primária**; a Anbima Data é fallback, só para o que negociou e a B3 não cobriu. `vrVNE` + `dtInicioRentabilidade` + `FluxoAtivos` são um **pacote indivisível** por ativo — a coluna `cdFonteCadastro` diz de quem é, e misturar as duas fontes conta a carência duas vezes, em silêncio.
- **Match de referência** (`IPCA→NTN-B`, `PREFIXADO→DI1`): script separado (`match_referencias.py`), sem args — roda idempotente sobre a base toda a cada ciclo do pipeline. Duration-match data-exata contra `MtmAnbima`; não sobrescreve refs da Anbima (`cdFonteReferencia='Anbima'`)
- **Relatório agrupa por `dtLiquidacao`**, não `dtNegocio`
- **O relatório geral inclui o pregão de HOJE, marcado como PRÉVIA** (01/09/2026). Ele é meio
  dia de dado — a perna D+1 do pregão anterior fechou, a do pregão em curso não. O selo viaja
  no payload (`dtPrevia`) e aparece no banner do topo, na opção do seletor do Boletim e no
  email do dia; `--sem-previa` volta ao corte em D-1. Mesma paleta do `badge-previa` que o
  relatório diário (`--mode previa`) já usava.
- **PU par e %par** (02/09/2026). A tabela **`PuPar`** guarda o PU par por **(ativo, data)**,
  escrita pelo `calc_pu_par` logo depois do `filtrar_trades`, em cascata
  **calc local → B3 → FI**. O **`%par` do negócio NÃO é gravado** — sai na leitura do
  relatório (`vrPU / vrPuPar × 100`), para que corrigir um cadastro não deixe um %par velho
  de pé sobre um denominador descartado.
  - **A chave inclui a data porque o PU par acreta todo dia útil.** Reusar um valor por
    alguns dias injeta erro sistemático (mediana 0,368% em 7 dias úteis, medido em 249
    ativos) e ignora evento de fluxo na janela (5% dos ativos negociados; o MATD23
    amortizou 100%). Indexar por data também é **mais barato**: cada par se calcula uma vez.
  - **Idempotente:** par `(ticker, data)` que existe não é recalculado nem consultado em API.
    Quem invalida é `dados.DescartarPuPar`, chamado de dentro do `Mesclar`/`Sincronizar*`.
  - **Papel que sai do cadastro** (emissor perto do default): as calculadoras removem o
    título e o mercado passa a usar o **último PU par conhecido**, em *cents on the dollar*.
    A leitura pega o último com `dtReferencia <= dtLiquidacao` e mostra a **idade**. Não há
    status "congelado" gravado — a idade é o status (`PREGOES_CONGELADO = 5`).
  - TODOS os indexadores, **%CDI inclusive**: no par não há desconto envolvido. A coluna
    `% PU Par` da FI Analytics **não** serve de fonte — numerador e denominador diferentes.
- **Dias úteis e feriados vêm de `Helpers/datas.py`** — e ele LEVANTA se o CSV faltar, em vez
  de devolver conjunto vazio (o que faria todo sábado virar pregão, em silêncio).
- **Evoluir o `ESQUEMA` exige `dados.Reconformar(tabela)`**: a view é `SELECT *` sobre os
  parquets, então coluna nova não existe até alguém reescrever os arquivos, e todo `Ler()`
  quebra com "column not found" nesse meio-tempo.

## Estado atual

**Branch `refactor/split-bases`, não mergeado. A conversão para Parquet está COMPLETA.**
O detalhe está em **`vault/17 - Armazenamento Parquet e AWS.md`** — leia antes de continuar.
Resumo: `Helpers/dados.py` é a camada de dados, a base real vive em `files/Parquet`
(1.582.200 linhas, 388 MB → 41,6 MB), e nada mais fala SQLite fora do `ipca.db`/`di.db`.

- **O trigger `trgInfoAtivosInvalidaFluxo` virou código**, dentro de `dados.Mesclar()` — e
  mora lá pelo mesmo motivo que morava no banco: é o único caminho de escrita coluna a
  coluna de `InfoAtivos`, então nenhum dos cinco escritores pode esquecer.
- **Teste de aceitação:** o relatório geral sai em **34 pregões, 2.568 ativos,
  R$ 52.315,17 MM**. Esse número **não** é o R$ 53.265,83 MM que o vault citava antes — ele
  mudou três vezes, por motivo verificado. A tabela com os três passos está em
  `RETOMAR_AQUI.md` §4.

Veja `vault/09 - Progresso.md` para saber em que fase está a implementação e o que falta.

Veja `vault/98 - Backlog.md` para itens pendentes de decisão. **Leia e mencione ao usuário no início de cada sessão se houver itens relevantes ao trabalho em curso.**

## Workflow de implementação

O projeto é dividido em **fases** (uma por script principal). Em cada fase:

1. Coder lê PLANEJAMENTO_v5.md + notas relevantes do vault
2. Coder implementa o script + dependências em `lib/`
3. Documenter atualiza as notas do vault e o Progresso.md
4. Usuário revisa, aprova, próxima fase

## Como começar

**Retomando de uma sessão anterior** (o caso normal hoje), diga:
**"Leia `RETOMAR_AQUI.md`, `vault/17 - Armazenamento Parquet e AWS.md` e
`vault/98 - Backlog.md`. Depois me diga o que você faria primeiro."**

Para uma fase nova do zero: **"Leia PLANEJAMENTO_v5.md e vault/09 - Progresso.md, depois proponha a próxima fase de implementação"**.

Se for a primeira vez (vault ainda vazio), diga: **"Despache o documenter para popular o vault baseado em PLANEJAMENTO_v5.md, depois comece a Fase 1 com o coder"**.
