# Mapa das Fontes de Dados

> Levantado em **02/09/2026**, lendo os scripts. Detalhe de cada fonte nas notas irmãs
> (Anbima, B3 Boletim, FI Analytics) e em [pipeline](pipeline.md).

Doze scrapers, quatro instituições. Sete falam HTTP direto (`httpx`), quatro precisam de
navegador (`Playwright`) e um depende do terminal Bloomberg.

---

## 1. Negócios — o que a mesa efetivamente negociou

| fonte | transporte | endpoint | grava em |
|---|---|---|---|
| **B3 — Boletim Diário, tabela "Negócio a negócio"** | `httpx` POST | `arquivos.b3.com.br/bdi/table/export/csv?lang=pt-BR`<br>corpo `{"Name":"Trade","Date":D,"FinalDate":D,"ClientId":"","Filters":{}}` | `NegociosBrutos` |

É a **única fonte de negócio** do projeto — todo o resto é cadastro, curva ou preço de
referência. Uma linha por operação; `Name: "Trade"` é o que pede negócio-a-negócio em vez
do agregado por ativo. Endpoint público: sem login, cookie ou token.

Ver [scrape_b3_boletim](codigos/scrape_b3_boletim.md).

---

## 2. Cadastro do ativo — características e fluxo de caixa

| fonte | transporte | endpoint | grava em |
|---|---|---|---|
| **B3 `getBondDetails`** ⭐ **primária** | `httpx` GET | `api.calculadorarendafixa.com.br/getBondDetails/{ticker}` | `InfoAtivos` + `FluxoAtivos` |
| **Anbima Data** — fallback | Playwright | `data.anbima.com.br/{debentures\|certificado-de-recebiveis}/{ticker}/caracteristicas`<br>`.../agenda?page={n}` | `InfoAtivos` + `FluxoAtivos` |
| **FI Analytics** — planilha | Playwright + login | `fi-analytics.com.br/analytics-hub/hub?type=deb` → CSV | `InfoAtivos` (só preenche buraco) |

⚠️ **`vrVNE` + `dtInicioRentabilidade` + `FluxoAtivos` são um pacote indivisível por ativo.**
A B3 pré-capitaliza a carência dentro do VNE e não emite evento de incorporação; a Anbima
traz o VNE cru e a incorporação como evento. Misturar conta a capitalização **duas vezes**,
em silêncio. A coluna `cdFonteCadastro` diz de quem é o pacote.

Ver [scrape_b3_bond_details](codigos/scrape_b3_bond_details.md).

---

## 3. Taxas indicativas — o preço de referência do secundário

| fonte | transporte | endpoint | grava em |
|---|---|---|---|
| **Anbima — debêntures** | `httpx` GET | `anbima.com.br/informacoes/merc-sec-debentures/arqs/d{yy}{mmm}{dd}.xls` | `AnbimaIndicativos` |
| **Anbima — CRI/CRA** | Playwright | `data.anbima.com.br/busca/certificado-de-recebiveis?view=precos` | `AnbimaIndicativos` |

A URL do `.xls` é previsível (`d26jul28.xls`), por isso a debênture dispensa navegador. O
CRI/CRA só existe no portal, atrás de JS — daí o Playwright.

---

## 4. Curvas de referência — contra o que o spread é medido

| fonte | transporte | endpoint | grava em |
|---|---|---|---|
| **Anbima — NTN-B (MtM)** | `httpx` GET | `anbima.com.br/informacoes/merc-sec/arqs/m{yy}{mmm}{dd}.xls` | `MtmAnbima` |
| **B3 — curva DI** | `httpx` GET | `sistemaswebb3-derivativos.b3.com.br/referenceRatesProxy/` | `MtmAnbima` (contratos `DI1`) **e** `di.db/CurvaDi` |

O `scrape_b3_curva_di` tem **dois destinos num download só**: os vértices `DI1` vão para o
`MtmAnbima` (o relatório usa como referência de PREFIXADO) e a curva inteira vai para o
`di.db`, que é o que a calculadora lê.

---

## 5. Insumos da calculadora de renda fixa

Não alimentam o relatório direto — alimentam os dois SQLite que a `calculadora_rf` lê.

| fonte | transporte | endpoint | grava em |
|---|---|---|---|
| **IPCA realizado** — IBGE/SIDRA | `httpx` GET | `apisidra.ibge.gov.br/values/t/1737/n1/all/v/2266/p/all/f/n`<br>calendário: `servicodados.ibge.gov.br/api/v3/calendario/` | `ipca.db/IPCA` |
| **IPCA projetado** — Anbima | Playwright | `anbima.com.br/pt_br/informar/estatisticas/` | `ipca.db/IPCAProjetado` |
| **DI realizado** — BCB/SGS | `httpx` GET | `api.bcb.gov.br/dados/serie/bcdata.sgs.{serie}/dados` | `di.db/DiHistorico` |

⚠️ `ipca.db`, `di.db` e `feriados_anbima.csv` têm de ficar na **mesma pasta** — a calc lê os
três de `CALCRF_FILES_DIR`, pelo nome do arquivo. Ver [calc](helpers/calc.md).

---

## 6. Outstanding — saldo em circulação

| fonte | transporte | campo | grava em |
|---|---|---|---|
| **Bloomberg** | `xbbg` (terminal) | `AMT_OUTSTANDING` | `Outstanding` |

**Só roda no PC do banco.** No PC pessoal a tabela fica vazia, e a Visão Anbima cai
automaticamente para a quantidade de emissão como proxy, com disclaimer no relatório.

---

## 7. Precificação sob demanda — não são scrapers

Estas não raspam nada: são chamadas **por ativo/negócio**, durante o cálculo.

| API | endpoints | usada por |
|---|---|---|
| **B3 Calculator** | `api.calculadorarendafixa.com.br` — `/calcPU`, `/calcYield`, `/getBondDetails` | `calc_taxa_negocios`, `validar_calc_b3`, `calc_pu_par`, `scrape_b3_bond_details` |
| **FI Analytics** | `endpoint.fi-analytics.com.br` — `/deb/debenturecalculator`, `/cr/cricracalculator`, `/bb/bondbuildercalculator` | as mesmas |

⚠️ **As chamadas de cálculo da B3 são CONTADAS.** Existe a rota
`GET /consumo/pacotes/{dataInicial}/{dataFinal}` para acompanhar. É por isso que o
`calc_pu_par` é idempotente por (ticker, data) e o `scrape_b3_bond_details` tem teto de
refresco por rodada.

---

## Janela histórica de cada fonte — o que dá para recuperar

Medido em 01–02/09/2026. **Isto determina quanto histórico é recuperável e quanto só existe
se alguém tiver guardado.**

| fonte | alcance | observação |
|---|---|---|
| **DI realizado** (BCB) | **desde 2000-01-03** | 26 anos. Não precisa de importação manual |
| **IPCA** (IBGE) | **desde 1979-12** | 47 anos. Idem |
| **Anbima deb / NTN-B** | **desde 2026-02-21** | Bissectado: 20/02 dá 404, 21/02 baixa. ~6 meses deslizantes |
| **Anbima CRI/CRA** | ~5 pregões | Portal só mantém os últimos |
| **B3 curva DI** | ~20 pregões | O fundo só existe em planilha, se existir |
| **B3 boletim** | não medido | Testado com sucesso em datas de jun–jul/2026 |
| **Anbima Data** (cadastro) | universo completo | Não é série temporal |

**Consequência prática:** tudo anterior a **21/02/2026** nas indicativas da Anbima está fora
de alcance para sempre. Ver o item de histórico em [backlog](backlog.md).

---

## Resumo por transporte

**`httpx` direto (7):** boletim B3, bond_details B3, curva DI B3, Anbima debêntures,
Anbima NTN-B, IPCA IBGE, DI BCB.

**Playwright (4):** Anbima CRI/CRA, Anbima Data, IPCA projetado Anbima, FI Analytics.

**Terminal (1):** Outstanding Bloomberg.

O boletim da B3 **saiu do Playwright em 02/09/2026** — o navegador nunca foi necessário, era
como o endpoint tinha sido descoberto. Vale conferir se os outros três estão no mesmo caso.
