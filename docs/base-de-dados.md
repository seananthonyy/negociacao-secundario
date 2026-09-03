# Base de dados

Armazenamento **Parquet**, motor de consulta **DuckDB**. Doze tabelas.

Onde os arquivos moram é uma linha do `config/config.toml`:

```toml
[dados]
raiz = "database/parquets"      # no banco: "s3://bucket/negociacao"
```

O código é o mesmo nos dois. As **views** do DuckDB têm o nome PascalCase das tabelas,
então o SQL não muda; a pasta em disco é snake_case, que é a convenção que o Athena
espera:

```
database/parquets/negocios_brutos/dtNegocio=2026-07-28/dados.parquet
database/parquets/info_ativos/dados.parquet
```

---

## Duas naturezas de tabela

| natureza | como se escreve | tabelas |
|---|---|---|
| **SÉRIE** — particionada por data | reescreve **o dia inteiro**. Reprocessar uma data é trocar a partição, nunca dar UPDATE em linha | `NegociosBrutos` · `NegociosProcessados` · `AnbimaIndicativos` · `PuPar` |
| **ESTADO** — arquivo único | reescreve o arquivo inteiro | `InfoAtivos` · `FluxoAtivos` · `MtmAnbima` · `Outstanding` · `IPCA` · `IPCAProjetado` · `DiHistorico` · `CurvaDi` |

Reescrever tudo funciona porque as tabelas de estado são pequenas: `InfoAtivos` 0,20 MB
e `FluxoAtivos` 0,35 MB. **Foi essa medição que tornou o Parquet puro viável** — a
objeção inicial (dezenas de pontos de escrita mutável no código) caiu quando ficou claro
que "reescrever a tabela" custa milissegundos.

**Só as grandes são particionadas.** Vinte mil linhas por dia já é um arquivo pequeno, e
particionar tabela miúda só multiplica arquivo à toa — o que deixa tanto o Athena quanto
o DuckDB mais **lentos**, não mais rápidos.

---

## Convenção de nomes

| elemento | forma | exemplo |
|---|---|---|
| tabela | PascalCase, português | `NegociosBrutos` |
| coluna | camelCase com prefixo | `vrTaxaCalculada` |

| prefixo | significa |
|---|---|
| `vr` | valor numérico |
| `cd` | código ou categoria |
| `dt` | data ou timestamp (texto ISO) |
| `id` | identificador de agrupamento |
| `st` | status booleano (0/1) |

**Datas são TEXTO ISO** (`AAAA-MM-DD`), não tipo data. É o que os scripts, o Athena e o
DuckDB comparam sem conversão — e o mesmo que o SQLite guardava.

**Exceção declarada:** as quatro tabelas da calculadora não seguem o prefixo. Ver o fim
deste documento.

---

## As tabelas

### `NegociosBrutos` — o boletim da B3 como veio
**Série**, particionada por `dtNegocio`. Chave: `cdIdentificadorNegocio`.

| coluna | tipo | papel |
|---|---|---|
| `cdIdentificadorNegocio` | VARCHAR | **A chave.** Vem da B3 |
| `cdInstrumento` | VARCHAR | `DEB`, `CRI` ou `CRA` |
| `cdEmissor` | VARCHAR | |
| `cdTicker` | VARCHAR | Código IF |
| `cdISIN` | VARCHAR | |
| `vrQuantidade` | BIGINT | |
| `vrPU` | DOUBLE | Preço do negócio |
| `vrVolume` | DOUBLE | Financeiro em R$ |
| `vrTaxaNegocio` | DOUBLE | Taxa quando a B3 informa — NULL na maioria |
| `dtNegocio` | VARCHAR | **A partição** |
| `dtHorarioNegocio` | VARCHAR | `HH:MM:SS` |
| `dtLiquidacao` | VARCHAR | D+0 ou D+1 |
| `cdSituacao` | VARCHAR | Ver *Vocabulário de cancelamento* |
| `dtCriacao` / `dtAtualizacao` | VARCHAR | Auditoria |

> ⚠️ **O `idTrade` não existe mais.** Era `INTEGER PRIMARY KEY AUTOINCREMENT` e só existia
> porque o SQLite o dava de graça. A chave passou a ser a natural, que a própria B3 manda.

---

### `NegociosProcessados` — o negócio depois do tratamento
**Série**, particionada por `dtLiquidacao`. Chave: `cdIdentificadorNegocio`.

| coluna | tipo | papel |
|---|---|---|
| `cdIdentificadorNegocio` | VARCHAR | Liga a `NegociosBrutos` |
| `cdTicker` · `cdEmissor` · `dtNegocio` · `dtLiquidacao` · `vrQuantidade` · `vrPU` · `vrVolume` | — | Copiados do bruto |
| `vrTaxaCalculada` | DOUBLE | **A taxa final do negócio**, venha de onde vier |
| `cdFonteTaxa` | VARCHAR | `Calc`, `FiAnalytics`, `B3`, ou NULL quando veio direta do boletim |
| `vrDuration` | DOUBLE | Duration do papel na data, em anos base 252 |
| `vrSpreadOver` | DOUBLE | **Entrega final:** taxa do negócio − taxa da referência |
| `idGrupoNegocio` | VARCHAR | Grupo do union-find do filtro de duplicados |
| `cdStatus` | VARCHAR | `VALIDO`, `BROKER`, `FUNDO` ou `PF` |
| `dtProcessamento` | VARCHAR | |

> O **`%par` não é coluna daqui.** Ele é calculado na leitura do relatório
> (`vrPU / vrPuPar × 100`), com o denominador vindo da tabela `PuPar`. Gravá-lo deixaria
> um número velho de pé quando o `PuPar` do ativo fosse descartado.

---

### `InfoAtivos` — o cadastro do papel
**Estado.** Chave: `cdTicker`. **É a tabela mais importante do sistema** — quase todo bug
de precificação nasce de campo errado aqui.

| coluna | tipo | papel |
|---|---|---|
| `cdTicker` | VARCHAR | A chave |
| `cdInstrumento` · `cdEmissor` · `cdISIN` | VARCHAR | Identificação |
| `vrQuantidadeEmissao` | DOUBLE | Usada como proxy de outstanding quando a Bloomberg não roda |
| `dtEmissao` · `dtVencimento` | VARCHAR | |
| `cdIndexador` | VARCHAR | `CDI+`, `%CDI`, `IPCA`, `PREFIXADO` |
| `vrTaxaEmissao` | DOUBLE | A taxa contratada. **É ela que define o "par"** |
| `vrVNE` | DOUBLE | Valor nominal de emissão |
| `dtInicioRentabilidade` | VARCHAR | |
| `vrAniversario` | BIGINT | Dia do mês do aniversário — conceito de IPCA |
| `cdTipoAmortizacao` | VARCHAR | Convenção da amortização |
| `cdFonteCadastro` | VARCHAR | `B3` ou `AnbimaData` — **de quem é o pacote** |
| `vrDuration` · `dtAtualizacaoDuration` | | Duration e quando foi apurada |
| `cdReferencia` · `cdFonteReferencia` · `dtAtualizacaoReferencia` | | A curva contra a qual o spread é medido |
| `stTemFluxo` | BIGINT | 0/1 — tem agenda em `FluxoAtivos` |
| `stFluxoValidado` | BIGINT | 0/1 — **a calc local pode precificar este papel** |
| `dtValidacaoFluxo` · `cdFonteValidacaoFluxo` | | Quando e contra quem validou |

> ⚠️ **`vrVNE` + `dtInicioRentabilidade` + `FluxoAtivos` são um pacote indivisível.** A B3
> pré-capitaliza a carência dentro do VNE e não emite evento de incorporação; a Anbima traz
> o VNE cru e a incorporação como evento. Misturar conta a capitalização **duas vezes**, em
> silêncio. `cdFonteCadastro` diz de quem é o pacote, e o `SincronizarFluxos` se recusa a
> escrever agenda por cima de ativo cuja fonte é `B3`.

---

### `FluxoAtivos` — a agenda de pagamentos
**Estado.** Chave: `cdTicker` + `dtEvento`.

| coluna | tipo | papel |
|---|---|---|
| `cdTicker` · `dtEvento` | VARCHAR | A chave |
| `vrPctAmortizacao` | DOUBLE | % do principal amortizado no evento |
| `vrPctIncorporacao` | DOUBLE | % de juros incorporados ao principal |
| `dtAtualizacao` | VARCHAR | |

> ⚠️ **Data de cupom entra como evento de amortização ZERO.** A calc ancora o juro de cada
> período no evento anterior; sem as datas de cupom ela acha que o papel acumula juros por
> anos sem pagar. Guardar só os eventos `A` derruba a aderência do modelo B3 de 92% para 25%.

---

### `AnbimaIndicativos` — a taxa indicativa por papel e dia
**Série**, particionada por `dtReferencia`. Chave: `cdTicker` + `dtReferencia`.

| coluna | tipo | papel |
|---|---|---|
| `cdTicker` · `dtReferencia` | VARCHAR | A chave |
| `vrTaxaAnbima` | DOUBLE | A taxa indicativa publicada |
| `vrSpreadAnbima` | DOUBLE | Spread dela contra a referência |
| `dtCriacao` | VARCHAR | |

---

### `MtmAnbima` — as curvas de referência
**Estado.** Chave: `cdTicker` + `dtReferencia`. Guarda os vértices de NTN-B (`NTN-B 35`)
e os contratos DI1 (`DI1F30`).

| coluna | tipo | papel |
|---|---|---|
| `cdTicker` · `dtReferencia` | VARCHAR | A chave |
| `vrTaxa` | DOUBLE | Taxa do vértice |
| `vrDuration` | DOUBLE | Duration do vértice — é ela que casa com o papel |

---

### `PuPar` — o PU no par, por ativo e data
**Série**, particionada por `dtReferencia`. Chave: `cdTicker` + `dtReferencia`.

| coluna | tipo | papel |
|---|---|---|
| `cdTicker` · `dtReferencia` | VARCHAR | A chave |
| `vrPuPar` | DOUBLE | O PU do papel precificado na própria taxa de emissão |
| `cdFontePuPar` | VARCHAR | `Calc`, `B3` ou `FI` |
| `dtCriacao` | VARCHAR | |

**A data está na chave porque o PU par acreta todo dia útil** — não é propriedade do
ativo, é do par (ativo, data). Medido em 249 ativos validados, reusar um valor por 7 dias
úteis erra **0,368% na mediana**, sempre para cima, e ignora evento de fluxo na janela:
5% dos ativos negociados tiveram evento em 7 dias, e um deles amortizou 100%.

Guarda **só valor real** — nada sintético. Papel que sai do cadastro das calculadoras
simplesmente para de ganhar linha; a leitura pega o último com
`dtReferencia <= dtLiquidacao` e exibe a **idade** dessa referência.

---

### `Outstanding` — saldo em circulação
**Estado.** Chave: `cdTicker` + `dtOutstanding`. Vem da Bloomberg (`AMT_OUTSTANDING`),
**só no ambiente do banco**. Vazia no PC pessoal, e aí a Visão Anbima cai para a
quantidade de emissão como proxy, com aviso na tela.

| coluna | tipo |
|---|---|
| `cdTicker` · `dtOutstanding` | VARCHAR |
| `vrOutstanding` | DOUBLE |

---

## As quatro tabelas da calculadora

Eram `ipca.db` e `di.db`, dois SQLite à parte. Viraram Parquet em **03/09/2026**: a base
inteira passa a viver num lugar só, e no banco vai para o mesmo bucket.

> ⚠️ **Os nomes de coluna destas quatro são CONTRATO com a `calculadora_rf`.** Ela as lê
> pelo nome. Por isso não seguem o prefixo `vr`/`cd`/`dt` do projeto — e renomear qualquer
> um quebra a precificação inteira, em silêncio.

| tabela | colunas | o que guarda |
|---|---|---|
| `IPCA` | `dtIPCA` (AAAA-MM) · `vrIndiceIPCA` · `dtDivulgacaoIPCA` | Número-índice do IBGE, desde 12/1979 |
| `IPCAProjetado` | `dtIPCAProjetado` · `vrProjecaoIPCA` | Projeção da Anbima, % do mês |
| `DiHistorico` | `dtReferencia` · `vrTaxaDiAnual` · `vrTaxaDiDiaria` | DI realizado (BCB), desde 01/2000 |
| `CurvaDi` | `dtReferencia` · `du` · `diasCorridos` · `vrTaxa` | Curva pré da B3, vértice a vértice |

A calc as lê por `CALCRF_PARQUET_DIR`. O `feriados_anbima.csv` continua arquivo, em
`database/arquivos/`, e vai por `CALCRF_FILES_DIR`. Quem seta as duas variáveis é o
`codigos/helpers/calc.py`.

> **`IPCAProjetado` é a única tabela do projeto que não é reconstituível.** A página da
> Anbima só mostra a projeção corrente e ~13 meses para trás, então um dia perdido é
> perdido para sempre. Por isso é a única com backup próprio, em `backups/`.

---

## Vocabulário de cancelamento

A B3 não escreve "Cancelado". Ela escreve:

| valor | origem |
|---|---|
| `Cancelado B3` | cancelamento pela B3 |
| `Cancelado Parcial B3` | cancelamento parcial |
| `Cancelado` | **nosso** — o que o soft-cancel grava quando um negócio some do arquivo |

O projeto inteiro filtrava com `cdSituacao != 'Cancelado'`, comparação exata — então só
pegava os nossos. Os cancelados pela B3 passavam por bons, e eram milhares. O predicado
correto é `dados.NaoCancelado()`, que casa por **prefixo** e não quebra se a B3 inventar
um quarto valor.

O número cresce a cada re-raspagem: a B3 revisa o boletim depois do pregão. Re-raspar um
pregão um mês depois trouxe 926 negócios que eram `Confirmado` e viraram `Cancelado B3`.

---

## Regras de escrita

**`Mesclar(tabela, df, politica, data)`** — escrita **coluna a coluna**. Coluna que não
vier no `df` fica intacta. É o que permite cinco escritores parciais na mesma tabela, e é
o `ON CONFLICT DO UPDATE` do SQLite.

| política | efeito |
|---|---|
| `SOBRESCREVER` | o novo vence sempre, inclusive NULL |
| `PREFERIR_NOVO` | o novo vence, mas NULL não apaga o que existe |
| `PREFERIR_ATUAL` | só preenche buraco — era o `COALESCE` invertido |

**`Upsert(tabela, df, data)`** — troca a **linha inteira**. Só serve a quem é dono de
todas as colunas da tabela.

**`Reconformar(tabela)`** — reescreve todos os arquivos no esquema atual. É o caminho
único de evolução de esquema: a view é `SELECT *` sobre os parquets, então uma coluna nova
não existe em arquivo nenhum até alguém reescrever, e todo `Ler()` estoura com *column not
found* nesse meio-tempo.

**`DescartarPuPar(tickers)`** — apaga o histórico de PU par dos ativos cujo cadastro ou
fluxo mudou. Chamado de dentro do `Mesclar`/`Sincronizar*`, pelo mesmo motivo que a
invalidação de validação mora lá: são cinco escritores, e bastava um esquecer.

---

## Armadilhas

**Tipo é declarado, nunca inferido.** Uma coluna que venha toda NULL num dia faz o pyarrow
inferir o tipo `null`, e a leitura do conjunto quebra ao juntar essa partição com as
outras. O `ESQUEMA` resolve de uma vez.

**Partição é VARCHAR, sempre.** Sem `hive_types`, o DuckDB deduz o tipo da coluna de
partição pelo texto da pasta: `dtReferencia=2026-07-28` viraria DATE enquanto as demais
datas são VARCHAR, e qualquer comparação entre as duas quebra.

**Grave em lote.** `InfoAtivos` e `FluxoAtivos` são arquivos únicos — gravar por ativo num
laço de mil reescreve o arquivo mil vezes.

**`SUM()` de float não é checksum.** Reconformar `NegociosProcessados` deu diferença de
R$ 0,01 em R$ 163 bi. Não é perda de dado, é ordem de acumulação. Para conferir uma
migração, use diferença simétrica linha a linha (`EXCEPT` nos dois sentidos).

**Coluna a mais some em silêncio.** `Conformar()` descarta o que não está no esquema e
cria como NULL o que falta, sem erro e sem log. É o comportamento certo no uso diário e
veneno numa migração — por isso o `migrar_para_parquet` compara coluna a coluna e aborta.
