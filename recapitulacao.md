# Retomar aqui

> Escrito em **03/09/2026, manhã**. Branch **`refactor/split-bases`**, árvore limpa.
> **Commitado em commits temáticos, e NADA foi pushado** — o push é seu. A calculadora
> vizinha tem commit próprio, também sem push. Comece por `git log --oneline -5`.
>
> A sessão fez uma coisa só, grande: **a reestruturação inteira do projeto**. Tudo mudou
> de lugar, o armazenamento da calculadora virou Parquet, o gate foi reescrito, e os 37
> documentos foram escritos do zero.
>
> **Teste de aceitação re-conferido no fim:** `--ate 2026-07-28` devolve
> **34 pregões · 2.568 ativos · R$ 52.315,17 MM** — idêntico ao número de antes da
> reestruturação. Nenhum número de negócio mudou.

---

## 1. O que mudou de lugar

A estrutura antiga (`code/Helpers/`, `code/codigos/`, `files/`) **não existe mais**. A nova
está descrita no `CLAUDE.md` §2 e é a que está no disco:

| antes | agora |
|---|---|
| `code/Helpers/` | `codigos/helpers/` |
| `code/codigos/<nome>/` | `codigos/scripts/<nome>/` |
| `files/Parquet/` | `database/parquets/` |
| `files/Database/{ipca,di}.db` | `database/parquets/{ipca,ipca_projetado,di_historico,curva_di}/` |
| `files/Database/feriados_anbima.csv` | `database/arquivos/feriados_anbima.csv` |
| `files/config.toml` · `.env` | `config/` |
| `files/{emails,debug,diagnostico}/` | `cache/` (descartável) |
| `bundle_banco.py` na raiz | `migracao-banco/` |
| 3 notebooks em `rotinas/` | `rotinas/teste-debug.ipynb` · `run-pipeline-diario.ipynb` |
| vault Obsidian | `docs/` |

**Nenhum script importa outro.** O compartilhado vive em `codigos/helpers/` (13 módulos), e
os `[paths]` são ancorados na raiz do projeto, nunca no cwd.

### Os documentos

`docs/` foi escrito **inteiro**, nos dois gabaritos fixos: 13 helpers e 24 scripts, mais
`base-de-dados.md`, `pipeline.md`, `fontes.md`, `credenciais.md` e `backlog.md`. A seção
**Armadilhas** de cada um é onde mora o conhecimento caro — se estiver vazia, é bug do
documento.

---

## 2. A calculadora vizinha agora lê Parquet

`../calculadora-renda-fixa/calculadora_rf.py` foi convertida de SQLite para Parquet (com
permissão explícita). O que mudou lá:

- saiu `import sqlite3`, entrou `import re`;
- `DIR_PARQUET` (env `CALCRF_PARQUET_DIR`) e `LerParquet(tabela)` novos;
- `DIR_ARQUIVOS` (env `CALCRF_FILES_DIR`) continua servindo só o `feriados_anbima.csv`;
- `MERCADO.__init__` ganhou o cache de curva DI.

**Isso deu à calc a primeira dependência externa da vida dela: `pyarrow`.** Antes era
stdlib pura. É o custo de não manter dois formatos.

Quem seta as duas variáveis é `codigos/helpers/calc.py` — o único módulo que sabe onde a
calc está instalada. Verificado ponta a ponta: 1.263 feriados, 565 meses de IPCA, 438
projeções, 6.698 dias de DI.

⚠️ **`calculadora-renda-fixa` é um repositório separado.** Tem commit próprio
(`4973af2`), sem push.

---

## 3. O gate novo (`validar_calc_b3`)

Implementado conforme o desenho fechado com você em 03/09. Quatro testes: PU em data
passada na taxa de emissão, PU hoje a +100 bps, PU em D+1 a +100 bps, e a taxa implícita
naquele mesmo PU. B3 primeiro, FI depois, **por teste**.

Duas correções entraram na implementação e não estavam no desenho:

1. **O PU do passo 3 é reusado no passo 5** em vez de pedido de novo — economiza uma
   chamada *contada* pela B3 por ativo.
2. **A nossa calc falhar onde o oráculo respondeu conta como REPROVA.** Na primeira
   escrita, o `None` caía num caminho que dava diferença zero: o ativo que a calc *não
   conseguia* precificar passava com nota perfeita.

### ⚠️ A rodada da madrugada rebaixou 1.532 ativos — e estava ERRADA

Já foi corrigido, mas leia, porque é a armadilha mais cara da sessão.

O gate rodou às 04:18 e derrubou os validados de **2.917 para 1.408**, com **1.494 papéis
CDI+ reprovados**. Parecia realidade; não era.

**Causa:** semear o D+1 tem **duas** condições, e o código só conferia uma. A curva
projetada vem da `CurvaDi` (**B3**); mas para chegar a D+1 a calc capitaliza o DI
**realizado** dia a dia, e essa série vem da `DiHistorico` (**BCB**). O BCB publica com um
dia de atraso — então é *normal* a `CurvaDi` estar um pregão à frente. Naquele momento a
curva de 02/09 existia e o DI realizado de 02/09 não. Todo papel indexado a CDI levantava
`ValueError`, e o `except Exception` transformava isso em **divergência** em vez de
**ausência**.

**O que o desenho já mandava fazer** — "se não der para semear, o passo é PULADO e
registrado, não reprovado; a falha é da nossa base, não do ativo" — só não estava
implementado para esta segunda condição.

**Corrigido e re-rodado na base inteira:** os validados voltaram para **2.727**, com
**0 rebaixados de fato** (1.319 promovidos). Não é o mesmo 2.917 de antes, e não deveria
ser: o gate novo é mais estrito — ganhou o teste em data passada e o de taxa, que agora é
default. Os 190 de diferença reprovam de verdade.

**A lição, que virou código:** o `9.9` do CSV não distinguia "a calc levantou" de "a calc
divergiu" — foi essa indistinção que escondeu o bug por uma rodada inteira. O resumo agora
traz a seção **Por que a NOSSA calc não respondeu**, com os motivos contados. Um motivo que
domina a lista quase nunca é o ativo: é a nossa base.

---

## 4. O que rodou, e o que falhou

Pipeline completo, 63 passos, **60 OK e 3 falhas**. A base avançou de 28/07 para 03/09:

```
39 pregões | 2.667 tickers | R$ 59.238,25 MM   (2026-06-09 → 2026-09-03, com PRÉVIA)
```

| falha | de quem é |
|---|---|
| `scrape_fianalytics_planilha` | **do site.** O botão *Exportar* não é achado (timeout de 20 s) e a lista de CRI/CRA também não. O login funciona. Mesmo padrão de 19/07: o site refez o layout de novo |
| `scrape_anbima_cri_cra --date 2026-09-03` | **da fonte.** É o pregão de hoje; o portal ainda não publicou |
| `scrape_ipca_projetado_anbima` | **minha.** O `Backup()` ainda chamava `CaminhoBancoIpca()`, que morreu com o `ipca.db`. Corrigido e re-rodado com sucesso (7 dias novos, D+1 = 04/09) |

---

## 5. Cinco coisas que consertei depois do pipeline

**O backup do IPCA projetado ia para `cache/`.** Ou seja: a única tabela do projeto que
**não é reconstituível** tinha sua rede de proteção na pasta explicitamente descartável. O
`config.toml` ganhou `[paths] backupsDir = "backups"`, e a cópia que existia foi movida.

**As pastas de Parquet das siglas estavam ilegíveis.** A regex de snake_case cortava em
cada maiúscula, então `IPCAProjetado` virava `i_p_c_a_projetado`. Corrigida nos **dois**
lados (`dados.NomePasta` e o leitor da calc) para tratar sigla, e as duas pastas foram
renomeadas. Importa mais do que parece: **esses nomes viram prefixo de S3 no Athena**, então
a feiura seria permanente.

**Referências de caminho antigo em comentário e docstring** (`Helpers/`, `code/`) — varridas.

**O `make_bundle.py` tinha escapado da faxina inteira.** Três defeitos: o `ROOT` apontava
para `migracao-banco/`, e o `git ls-files` rodado de lá geraria um bundle **sem código, com
sucesso**; o `EXCLUIR` deixou de bater no caminho novo e o bundle passou a **empacotar a si
mesmo**, crescendo ~2 MB por regeração; e `_arquivos_versionados` / `_protegido` / `main`
violavam duas convenções fechadas. Regenerado e **testado de verdade**: extrai 92 arquivos
numa pasta limpa, com os 24 scripts, os 37 docs e os 2 notebooks, sem segredo nenhum, e
todo `.py` extraído compila.

**O `INSTALACAO_BANCO.md` foi apagado na reestruturação e três docs ainda apontavam para
ele.** Metade dele já era falsa (falava de `code/`, `setup_teste.ipynb`,
`setup_inicial.ipynb`). Reescrevi o que sobrevive para a estrutura nova e dobrei na Parte A
do item de migração do backlog — sem criar arquivo fora do plano.

---

## 6. Onde continuar

### Imediato

1. **Revisar os 4 commits e dar o push.** **Nada foi pushado** — isso é seu. O
   `check_no_secrets` passa limpo (98 arquivos nos dois repos), e o git **não enxerga**
   `config/.env`, `codigos/helpers/destinatarios.py`, `database/parquets/`, `backups/`,
   `cache/` nem `relatorios/`.
2. **A calculadora tem commit próprio, também sem push.** É outro repositório.
3. **Consertar o `scrape_fianalytics_planilha`.** O site mudou. A receita já conhecida:
   seletor por texto/role, **nunca por classe CSS** neste site.
4. **A migração dos dados**, que é o item grande: converter o SQLite de produção do banco
   em vez de raspar o histórico de novo. O passo a passo de instalação está na Parte A do
   item de migração do backlog; o conversor é o `migrar_para_parquet`.

### Decisões que são suas, não do código

- **A régua do %CDI.** Medido três vezes agora: a taxa reproduz a B3 a **0,06 bps**, e o
  ativo é reprovado por PU a `3e-05`. A `TOL_TAXA_BPS` do mesmo gate permitiria 5 bps —
  as duas réguas discordam sobre o que é material, e no %CDI decide a mais apertada. Está
  no backlog com os três caminhos.
- **Onde mais exibir o `%par`.** Boletim e email do dia já mostram; você ia apontar os
  outros lugares.
- **Rotina de backup.** A pasta existe e está vazia. Item novo no backlog com as perguntas
  (frequência, destino, formato, se entra como passo 0 do pipeline).
- **Re-rodar `filtrar_trades` na base toda.** Segue de pé desde 31/08: 33 pregões ainda têm
  `cdStatus` **gravado** com os cancelados dentro do pareamento. O relatório sai certo
  (filtra na leitura), mas a classificação gravada não. Muda número histórico.

### Valor que evapora

O `MtmAnbima` tem só 36 dias, mas a fonte de NTN-B entrega desde **21/02/2026** — são ~3,5
meses de curva disponíveis **agora** e que somem conforme a janela desliza:

```bash
NEGSEC_SEM_EMAIL=1 python codigos/scripts/scrape_anbima_ntnb/scrape_anbima_ntnb.py \
    --start 2026-02-23 --end 2026-06-05
```

---

## 7. O que eu NÃO fiz, de propósito

- **Não pushei nada**, nem aqui nem na calculadora. Commitei nos dois; o push é seu.
- **Não mergeei o branch.**
- **Não mexi na régua do gate** (`TOL_PU`) nem liguei o `%CDI` na calc local — muda número
  de produção, e é decisão sua.
- **Não rodei o backfill do `MtmAnbima`** nem o `filtrar_trades` na base toda: os dois
  escrevem na base e mudam número histórico.
- **Não apaguei os `.db` antigos** que estavam fora da nova estrutura. Apagar é
  irreversível; confira a base Parquet primeiro.
