# scrape_b3_boletim.py

**Passo 1** do pipeline. A **única fonte de negócio** do projeto.

---

## Overview

Baixa o Boletim Diário da B3 — tabela **"Negócio a negócio"** de crédito privado — e grava
em `NegociosBrutos`. Uma linha por operação, com preço, quantidade, horário e liquidação.

**Não é scraping: é um POST.** O dado sai de um endpoint público do BDI que responde CSV
direto. Até 02/09/2026 este script subia um Chromium via Playwright para chegar nesse
mesmo POST — o navegador entrou porque foi **como o endpoint foi descoberto** (interceptando
o clique no botão CSV), e ninguém voltou para conferir se ele ainda fazia falta. Não fazia.

---

## Regras de negócio

**Filtra DEB, CRI e CRA** (`config.toml → instrumentosAceitos`). O resto do boletim é
ignorado.

**Descarta negócio sem identificador.** A B3 manda `"-"` nesse campo em parte das
operações (25 num pregão medido). Ele é a chave da base desde que o `idTrade` foi
aposentado, e guardar esses negócios não é opção: todos os `"-"` de um pregão colapsam
numa linha só, e — pior — a chave só é única *dentro* da partição, então um `"-"` por
pregão se acumula e o JOIN do relatório passa a contar o mesmo negócio várias vezes.
Medido: um negócio de R$ 3,24 MM contado em dobro.

**O UPSERT reescreve só três colunas:** `vrTaxaNegocio`, `cdSituacao`, `dtAtualizacao`.
Negócio já gravado não muda de ticker, PU nem volume — o que a B3 revisa depois do pregão
é a taxa e a situação.

**Soft-cancel.** Negócio que estava na base para aquele pregão e **sumiu** do arquivo numa
re-raspagem vira `cdSituacao = 'Cancelado'` (preserva o rastro) e é removido de
`NegociosProcessados`. Não é hipotético: re-raspar um pregão um mês depois trouxe 926
negócios que eram `Confirmado` e viraram `Cancelado B3`.

**Uma requisição por pregão, de propósito.** Já houve um atalho que pedia o intervalo
inteiro num POST só (`Date` ≠ `FinalDate`), e a API devolvia apenas as **duas pontas** —
sem os pregões do meio, e com status 200. Os dias sumiam em silêncio.

---

## CLI

```powershell
python codigos\scripts\scrape_b3_boletim\scrape_b3_boletim.py --date 2026-09-02
python codigos\scripts\scrape_b3_boletim\scrape_b3_boletim.py --start 2026-09-01 --end 2026-09-02
python codigos\scripts\scrape_b3_boletim\scrape_b3_boletim.py --date 2026-09-02 --sem-gravar
```

| argumento | efeito |
|---|---|
| `--date` | Um pregão |
| `--start` / `--end` | Intervalo; um POST por dia |
| `--salvar-csv` | Grava o CSV cru em `cache/scrape_b3_boletim/` antes de parsear |
| `--sem-gravar` | Baixa e parseia sem escrever. **É o teste do endpoint num ambiente novo** — valida o proxy do banco sem tocar na base |

---

## Interação com a base

**Lê:** `NegociosBrutos` (quais identificadores já existem no dia) e, no soft-cancel, quais
não vieram no download.

**Grava:**
- `NegociosBrutos` — linha nova entra inteira (`SOBRESCREVER`); linha existente leva só as
  três colunas atualizáveis.
- `NegociosProcessados` — o soft-cancel **apaga** as linhas dos negócios que sumiram.

O lote é partido em dois porque `Mesclar` aplica a política a toda coluna presente no
DataFrame.

---

## Detalhes técnicos

```
POST https://arquivos.b3.com.br/bdi/table/export/csv?lang=pt-BR
Content-Type: application/json

{"Name": "Trade", "Date": "2026-09-02", "FinalDate": "2026-09-02",
 "ClientId": "", "Filters": {}}
```

Endpoint **público**: sem login, cookie, token ou header especial.

| campo | o que faz |
|---|---|
| `Name` | `"Trade"` seleciona a tabela **negócio a negócio**, em vez do agregado por ativo |
| `Date` / `FinalDate` | iguais = um pregão |
| `ClientId` / `Filters` | sempre vazios; o filtro de DEB/CRI/CRA é nosso, no parse |

**O CSV:** delimitador `;`, BOM UTF-8, e algumas linhas de preâmbulo descritivo antes do
header real. O parser **não conta linhas fixas** — procura a primeira que contenha uma
coluna conhecida, o que sobrevive à B3 reescrever o texto do topo.

Treze colunas mapeadas em `COLUMN_MAP`. `Origem negócio` é ignorada (é sempre
"Pre-registro - Voice").

---

## Armadilhas

**Pregão com zero negócio quase sempre é falha, não ausência.** A B3 sempre tem negócio em
dia útil. O script avisa no resumo — foi assim que um bug de proxy passou batido.

**200 com HTML não é CSV.** A B3 responde 200 com uma página quando não gosta do corpo. O
`content-type` é o único jeito de distinguir, e o script aborta nesse caso.

**A B3 repete identificador.** Num pregão medido, `#1009650476` veio **quatro vezes**. Por
isso o `UpsertLinhas` conta **identificadores distintos**, não linhas do CSV — senão o
contador de inseridos mentiria.

**Soft-cancel exige reprocessar.** Quando ele dispara, as linhas saem de
`NegociosProcessados` e é preciso rodar `calc_taxa` → `filtrar_trades` para aquela data de
novo. O aviso sai no log.
