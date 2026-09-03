# Relatório Diário de Negociação Secundária — Crédito Privado

Gera um relatório HTML diário dos negócios de crédito privado brasileiro (debêntures,
CRIs e CRAs), consolidando B3, Anbima, FI Analytics, IBGE, BCB e Bloomberg.

---

## 1. O que o projeto faz

A pergunta que ele responde é: **o que negociou ontem, a que preço, e caro ou barato
em relação a quê?**

Nenhuma fonte responde isso sozinha. A B3 publica os negócios, mas sem taxa na maioria
deles — só preço. Transformar preço em taxa exige conhecer o papel: fluxo de pagamentos,
indexador, valor nominal. Esse cadastro está espalhado entre B3, Anbima e FI Analytics,
cada uma com convenção diferente. E para saber se a taxa é alta ou baixa é preciso
compará-la com uma curva de referência — NTN-B para papel IPCA, contrato DI para
prefixado — que vem de outras fontes ainda.

Então, todo dia, nesta ordem:

1. **Baixa os negócios** do Boletim Diário da B3, um a um.
2. **Busca o cadastro** de cada papel que negociou. A B3 é primária; a Anbima cobre o resto.
3. **Valida se consegue precificar** cada papel: roda a calculadora local e confere se
   ela reproduz o que a B3 ou a FI devolvem. Só quem passa é precificado localmente.
4. **Calcula a taxa de cada negócio** — direta do boletim quando vem, senão pela calc
   local, senão por API.
5. **Filtra duplicados** — passagem de fundo e operação de corretor aparecem duas vezes.
6. **Calcula o PU par** de cada ativo que negociou, para o `%par` do relatório.
7. **Casa cada papel com uma referência** de curva, por duration, e calcula o spread.
8. **Gera o relatório HTML** com seis abas.

### Duas convicções que explicam quase toda decisão do projeto

**Errado em silêncio é pior que quebrado.** Um script que roda, sai com sucesso e grava
número errado custa semanas até alguém notar. Por isso os caminhos são ancorados na raiz
e não no diretório atual; o leitor de feriados levanta em vez de devolver conjunto vazio;
a calculadora recusa calcular sem projeção em vez de inventar; e negócio sem
identificador é descartado em vez de guardado.

**Fonte também apodrece.** Cadastro que a B3 publicou hoje pode estar aditado amanhã.
Por isso o gate revalida periodicamente, o fluxo tem refresco por idade, e o boletim
marca como cancelado o negócio que sumiu numa re-raspagem.

---

## 2. Onde as coisas moram

```
<raiz>/                          irmã de calculadora-renda-fixa/
├── CLAUDE.md                    este arquivo
├── recapitulacao.md             onde paramos na última sessão
├── requirements.txt
│
├── config/                      config.toml · .env (não versionado) · .env.example
│
├── codigos/
│   ├── helpers/                 13 módulos compartilhados
│   └── scripts/<nome>/          <nome>.py · logs/ · dados do próprio script
│
├── docs/
│   ├── base-de-dados.md         schema das tabelas, colunas, tipos, relações
│   ├── pipeline.md              ordem de execução e argumentos
│   ├── backlog.md · fontes.md · credenciais.md
│   ├── codigos/<nome>.md        um por script
│   └── helpers/<nome>.md        um por helper
│
├── database/
│   ├── parquets/                A BASE — vira "s3://bucket/..." no banco
│   └── arquivos/                feriados_anbima.csv
│
├── migracao-banco/              make_bundle.py · bundle_banco.py
├── relatorios/                  HTML final
├── cache/<script>/              saída descartável: emails, JSON cru, CSV de debug
├── backups/                     só o IPCA projetado, que não é reconstituível
└── rotinas/                     teste-debug.ipynb · run-pipeline-diario.ipynb
```

**Cada script roda sozinho, de qualquer diretório.** Nenhum script importa outro; o que
é compartilhado vive em `codigos/helpers/`. Os `[paths]` são ancorados na raiz do
projeto, nunca no cwd.

---

## 3. A camada de dados

`codigos/helpers/dados.py` é o centro. O armazenamento é **Parquet**, o motor de consulta
é **DuckDB** — uma biblioteca, não um servidor. O SQLite saiu porque os dados precisam
viver na AWS, onde há bucket S3 + Athena e nenhum banco SQL.

**O SQL não mudou:** as views do DuckDB têm o nome das tabelas antigas. Onde os arquivos
moram é **uma linha** do config (`[dados] raiz`): pasta local hoje, `s3://` no banco.

| natureza | tabelas | como se escreve |
|---|---|---|
| **SÉRIE** (particionada por data) | `NegociosBrutos`, `NegociosProcessados`, `AnbimaIndicativos`, `PuPar` | reescreve **o dia inteiro** |
| **ESTADO** (arquivo único) | `InfoAtivos`, `FluxoAtivos`, `MtmAnbima`, `Outstanding`, `IPCA`, `IPCAProjetado`, `DiHistorico`, `CurvaDi` | reescreve o arquivo inteiro |

Não existe UPDATE em disco. Reprocessar uma data é **trocar a partição**.

### Regras que, esquecidas, causam bug silencioso

- **Tipo de coluna é declarado, nunca inferido** (`ESQUEMA`). Coluna toda NULL num dia
  seria inferida como tipo `null` e quebraria a leitura das outras partições.
- **Escrita parcial vai por `Mesclar()`**, com política por coluna (`SOBRESCREVER` /
  `PREFERIR_NOVO` / `PREFERIR_ATUAL`). É o `ON CONFLICT DO UPDATE` do SQLite.
  **`Upsert()` troca a linha inteira** e só serve a quem é dono de todas as colunas.
- **Grave em LOTE, nunca por ativo** — `InfoAtivos` e `FluxoAtivos` são arquivos únicos.
- **Evoluir o `ESQUEMA` exige `Reconformar(tabela)`**: a view é `SELECT *` sobre os
  parquets, então coluna nova não existe até alguém reescrever os arquivos.
- **Não use `SUM()` de float como checksum de regressão.** Reconformar deu diferença de
  R$ 0,01 em R$ 163 bi — ordem de acumulação, não perda. A conferência certa é diferença
  simétrica linha a linha.

O que era **trigger no banco** virou código dentro do `Mesclar()`: mudança de cadastro ou
de fluxo zera `stFluxoValidado` **e descarta o `PuPar`** do ativo. Mora ali porque
`InfoAtivos` tem cinco escritores e bastava um esquecer.

### As quatro tabelas da calculadora

`IPCA`, `IPCAProjetado`, `DiHistorico` e `CurvaDi` eram `ipca.db` e `di.db`. Viraram
Parquet em 03/09/2026. **Os nomes de coluna delas são contrato com a `calculadora_rf`** e
por isso não seguem o prefixo `vr`/`cd`/`dt` — renomear qualquer um quebra a precificação
inteira, em silêncio.

A calc as lê por `CALCRF_PARQUET_DIR`; o `feriados_anbima.csv` continua arquivo e vai por
`CALCRF_FILES_DIR`. Quem seta as duas é o `codigos/helpers/calc.py`.

---

## 4. Convenções fechadas

Não revisitar sem motivo forte.

- **Python 3.11+**, sem venv.
- **Funções e classes: PascalCase, em português** (`ProcessarData`, `MontarUrl`).
- **Variáveis e parâmetros: camelCase, em português** (`dtRef`, `cdTicker`, `limiteTrades`).
- **Constantes de módulo: UPPER_SNAKE** (`TOL_PU`, `MESES_PT`) — `_` interno é permitido.
- **NUNCA `_` no início de nome nenhum.** Nem função "privada", nem constante, nem
  variável, nem em JavaScript de template.
- **Tabelas: PascalCase, em português.** **Colunas: camelCase** com prefixo `vr` (valor),
  `cd` (código), `dt` (data), `id` (identificador), `st` (status).
- **Nome de arquivo de script: snake_case**, sem prefixo numérico. É a única coisa em
  snake_case.
- **Ficam em inglês:** jargão de mercado (`vrSpreadOver`, `vrDuration`, `vrPU`, `cdISIN`,
  `Outstanding`), API de terceiros (`parse_args`, `status_code`), nomes de módulo, e
  chaves de contrato (colunas da base, variáveis do template Jinja como `data_json`).
- **`dest=` explícito no argparse** sempre que a flag tiver hífen (`--email-dia` →
  `dest="emailDia"`) — senão o argparse gera snake_case e o mismatch só aparece em runtime.
- **Cada script é independente e idempotente**, com CLI próprio (`--date` ou `--start --end`).
- **Email no fim de cada script**, sucesso ou erro. `NEGSEC_SEM_EMAIL=1` desliga o Outlook
  e grava o corpo em `cache/emails/` — **usar sempre em teste e em rodada de lote.**
- **Chave dos negócios: `cdIdentificadorNegocio`**, a que a B3 manda. O `idTrade`
  (`AUTOINCREMENT`) morreu com o SQLite.
- **Sem CHECK constraints** no schema.

### Regras de negócio que atravessam vários scripts

- **Cadastro:** a B3 (`getBondDetails`) é a fonte **primária**; a Anbima é fallback por
  demanda. `vrVNE` + `dtInicioRentabilidade` + `FluxoAtivos` são um **pacote indivisível**
  por ativo — `cdFonteCadastro` diz de quem é, e misturar as duas fontes conta a carência
  duas vezes, em silêncio.
- **Cascata de taxa:** taxa direta do boletim → **calc local** (só `stFluxoValidado = 1`
  e indexador em `["CDI+","IPCA","PREFIXADO"]`) → FI Analytics → B3 → NULL.
- **%par:** `vrPU / vrPuPar × 100`, **calculado na leitura**, nunca gravado. O `vrPuPar`
  vem da tabela `PuPar`, indexada por **(ativo, data)** — o PU par acreta todo dia útil.
- **Filtro de duplicados:** union-find; status `VALIDO`, `BROKER`, `FUNDO` ou `PF`.
- **Match de referência** (`IPCA→NTN-B`, `PREFIXADO→DI1`): script separado, sem args,
  idempotente sobre a base toda.
- **Relatório agrupa por `dtLiquidacao`**, não `dtNegocio`, e **inclui o pregão de hoje
  marcado como PRÉVIA** (é meio dia de dado; o selo aparece em toda aba).

---

## 5. A calculadora de renda fixa (projeto vizinho)

`../calculadora-renda-fixa` é a **biblioteca de cálculo** (VNA, PU par, PU de operação,
duration). **Não modificar `calculadora_rf.py` sem permissão explícita do usuário.**

Este projeto roda as rotinas de dados que ela consome (IPCA, projeção de IPCA, DI, curva
DI) e valida o fluxo dos ativos que ela pode precificar. Import via
`codigos/helpers/calc.py`, que é o **único** módulo que sabe onde ela está instalada.

⚠️ **A calc recusa calcular sem a projeção da data** — levanta `ValueError` em vez de
inventar número. É o comportamento certo: foi ele que deixou 825 ativos sem `vrPuPar` em
vez de dar-lhes um denominador falso.

---

## 6. Como trabalhar neste projeto

**Sempre que alterar código, atualize o `.md` correspondente.** Um documento que descreve
o que o código já não faz é pior que documento nenhum.

- `docs/codigos/<script>.md` — Overview · Regras de negócio · CLI · Interação com a base ·
  Detalhes técnicos · **Armadilhas**
- `docs/helpers/<helper>.md` — Overview · API pública · Invariantes · Quem consome ·
  **Armadilhas**

A seção **Armadilhas** é onde mora o conhecimento mais caro do projeto: o que falha em
silêncio e como perceber. Se o script não tem armadilha conhecida, escreva isso — não
deixe a seção vazia.

**Ao terminar uma sessão, atualize o `recapitulacao.md`**: onde paramos, o que mudou nos
números e por quê, o que fazer a seguir.

**Ao validar mudanças, rode o relatório geral** e compare com o número de aceitação. Não
rode o pipeline de rede inteiro só para conferir código.

**Antes de qualquer `git push`:** `python codigos/scripts/check_no_secrets/check_no_secrets.py`.

---

## 7. Estado atual

**Branch `refactor/split-bases`.**

Teste de aceitação do relatório, medido sobre a base até 28/07/2026: **34 pregões ·
2.568 ativos · R$ 52.315,17 MM**. É contra esse número que se confere uma mudança que
não deveria mexer em nada.

Itens abertos em `docs/backlog.md`. **Leia e mencione ao usuário no início de cada sessão
se houver item relevante ao trabalho em curso.**
