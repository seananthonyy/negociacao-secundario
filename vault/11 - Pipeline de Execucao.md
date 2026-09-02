# Pipeline de Execução — Relatório de Liquidação X

> ℹ️ **02/09/2026 — o `calc_pu_par` entrou como passo 15;** os seguintes andaram um.
> ℹ️ **29/08/2026 — a ORDEM dos passos continua exatamente esta.** O que mudou foi onde
> os scripts moram (`codigos/<nome>/<nome>.py`) e o armazenamento (Parquet, não SQLite).
> O `pipeline_core` já resolve o caminho novo. Ver [[17 - Armazenamento Parquet e AWS]].


> Ordem canônica para gerar o relatório de uma data de liquidação. Fonte da verdade do **fluxo operacional**. Ver também [[09 - Progresso]] (histórico de cargas) e [[04 - Banco de Dados]] (schema).

## Conceito

O relatório **agrupa por `dtLiquidacao`** (não `dtNegocio`). Para uma data de liquidação `X`:

- `X-1u` = **dia útil anterior** a X (pula fim de semana e feriados Anbima).
- Os negócios que liquidam em `X` foram negociados em:
  - **`X-1u`** → liquidam em X por **D+1** (a maioria);
  - **`X`** → liquidam em X por **D+0** (mesmo dia);
  - ocasionalmente datas anteriores (trades *forward*), cujo MtM já costuma estar na base.

## Os 19 passos

| # | Script | Parâmetro | Data(s) | Rede? | Por quê |
|---|---|---|---|---|---|
| 1 | `scrape_b3_boletim` | `--start X-1u --end X` | X-1u, X | ✅ Playwright | negócios das 2 pontas que liquidam em X |
| 2 | `scrape_b3_bond_details` | `--start X-1u --end X` | X-1u, X | ✅ httpx | **FONTE PRIMÁRIA do cadastro.** Cadastro + fluxo dos tickers que negociaram e têm campo faltante. **Não valida** — quem marca `stFluxoValidado` é só o passo 12 |
| 3 | `scrape_anbima_debentures` | `--date X-1u` | X-1u | ✅ httpx | taxa indicativa deb — o relatório usa a Anbima de **D-1** (ver §"Por que D-1") |
| 4 | `scrape_anbima_cri_cra` | `--date X-1u` | X-1u | ✅ Playwright | idem CRI/CRA (UI instável — ver nota) |
| 5 | `scrape_fianalytics_planilha` | *(sem data)* | snapshot | ✅ Playwright | características (indexador, duration, venc.) de tickers novos |
| 6 | `scrape_anbima_data_ativos` | `--start X-1u --end X` | X-1u, X | ✅ Playwright | **FALLBACK.** Só o que a B3 (2) não cobriu. Não sobrescreve cadastro de fonte B3 |
| 7 | `scrape_anbima_ntnb` | `--start X-1u --end X` | X-1u **e** X | ✅ httpx | MtM NTN-B nas 2 datas de **negócio** (não é D-1!) |
| 8 | `scrape_b3_curva_di` | `--date` (rodar **2×**) | X-1u **e** X | ✅ httpx | MtM curva DI (contratos DI1 → `MtmAnbima`) **e** curva inteira → `di.db/CurvaDi` |
| 9 | `scrape_ipca_ibge` | *(sem data)* | série toda | ✅ httpx | IPCA realizado (IBGE) → `ipca.db/IPCA` — insumo da calculadora |
| 10 | `scrape_ipca_projetado_anbima` | *(sem data)* | janela da fonte | ✅ Playwright | projeção de IPCA (Anbima) → `ipca.db/IPCAProjetado` — **rodar antes das 17h30** |
| 11 | `scrape_di_bcb` | *(sem data)* | incremental | ✅ httpx | DI realizado (BCB) → `di.db/DiHistorico` — insumo da calculadora |
| 12 | `validar_calc_b3` | *(sem args — base inteira)* | 3 datas c/ curva + D+1 | ✅ B3/FI | **ÚNICO VALIDADOR.** Gate de confiança: a calc reproduz a B3 (ou FI) em PU (par+fora, ≤1e-5)? Promove/rebaixa `stFluxoValidado`; não-confirmável → inválido. Revalida a cada 15d. `--negociados-dias N` encurta a rodada. Ver [[16 - Confianca nos Validados (WIP)]] |
| 13 | `calc_taxa_negocios` | `--date X` | X | ✅ APIs FI/B3 | taxa por trade. Cascata: direta → **calc local (LIGADA p/ CDI+/IPCA/PREFIXADO validados)** → FI → B3. %CDI e não-validados seguem em FI/B3 |
| 14 | `filtrar_trades` | `--date X` | X | ❌ local | classifica VALIDO / FUNDO / BROKER / PF |
| 15 | `calc_pu_par` | `--date X` | X | ✅ APIs B3/FI | **PU par por (ativo, data)** em `PuPar`. Cascata calc local → B3 → FI. Idempotente: par que já existe não custa chamada. O `%par` do negócio **não é gravado** — sai na leitura do relatório |
| 16 | `calc_spread_anbima` | `--date X-1u` | X-1u | ❌ local | spread Anbima das indicativas (mesma data que o relatório exibe) |
| 17 | `match_referencias` | *(sem data)* | — | ❌ local | preenche `cdReferencia` faltante via duration vs MtmAnbima |
| 18 | `calc_spread_over` | `--date X` | X | ❌ local | spread dos trades — **casa MtM por `dtNegocio`** |
| 19 | `gerar_relatorio_credito` | *(sem args)* | — | ❌ local | regenera `files/relatorios/relatorio_secundario.html` (toda a base). Calcula o `%par` na leitura, a partir de `PuPar` |

> **Ordem do passo 15 (`calc_pu_par`):** depois do `validar_calc_b3` (12), que decide
> em quais ativos a calc local vale, e do `calc_taxa_negocios` (13), que cria as linhas
> em `NegociosProcessados` de onde sai a fila. **Não depende** do `match_referencias`
> nem do spread — é preço, não curva —, então poderia rodar antes; fica aqui só para
> manter o bloco por-liquidação junto.

> **Ordem do passo 5:** `scrape_anbima_data_ativos` roda **depois** de boletim (1) e Anbima deb/cri (2-3) — porque monta a fila de tickers a partir da união `NegociosBrutos.dtNegocio` + `AnbimaIndicativos.dtReferencia` — e **antes** de `match_referencias` (17), que usa `InfoAtivos.vrDuration` que este passo pode preencher. Rodar o FI Analytics (4) antes reduz o trabalho dele (menos tickers "incompletos").

> **Passo 2 (cadastro pela B3, adotado em 13/07/2026):** roda logo depois do boletim e **antes** do `scrape_anbima_data_ativos` — a B3 é a fonte **primária** do cadastro e do fluxo; a Anbima só preenche o que ela não cobriu. O fluxo da B3 **não nasce mais validado** (mudou em 24/08/2026): quem concede `stFluxoValidado = 1` é só o `validar_calc_b3`. Tem gate próprio (só chama a API para ticker com campo faltante): sem ele seriam ~2.900 chamadas por rodada. Ver [[15 - Cadastro dos Ativos]].

> **Passos 9–12 (insumos da calculadora, adotados em 12/07/2026):** não dependem da liquidação X — rodam **uma vez por ciclo**, no bloco global do `pipeline_core`. Os passos 9–11 alimentam os bancos que a **calculadora de renda fixa** lê (`data/ipca.db` e `data/di.db`, ver [[14 - Rotinas da Calculadora]]); o passo 12 marca quais ativos ela pode precificar. O `validar_calc_b3` roda **depois do `scrape_anbima_data_ativos` (6)**: é ele que atualiza `FluxoAtivos`/`InfoAtivos` e, quando o fluxo muda de verdade, **zera a validação** — o ativo volta pro topo da fila. Rodar antes validaria um fluxo prestes a mudar.

## Por que D-1?

A coluna Anbima do relatório vem do CTE `AnbimaLatest` em `gerar_relatorio_credito`:

```sql
WITH AnbimaLatest AS (
  SELECT cdTicker, vrTaxaAnbima, vrSpreadAnbima,
         ROW_NUMBER() OVER (PARTITION BY cdTicker ORDER BY dtReferencia DESC) AS rn
  FROM AnbimaIndicativos WHERE dtReferencia <= ?    -- ? = X-1u (dia útil anterior à liquidação)
)
```

Ou seja: por ticker, mostra a indicativa **mais recente com `dtReferencia ≤ X-1u`**. Dois motivos para ser D-1:

1. **Disponibilidade:** a Anbima publica a indicativa do dia D só na manhã de D+1. No dia da liquidação X, a mais recente disponível é a de X-1u.
2. **Comparação correta:** X-1u é a **data de negócio** do fluxo dominante (D+1). Comparar a taxa do trade contra a Anbima do dia em que foi negociado é o apples-to-apples certo.

Por isso `scrape_anbima_debentures`/`cri_cra` (3-4) e `calc_spread_anbima` (16) rodam em **X-1u** — é a data das indicativas que o relatório vai exibir.

**Cuidado:** isso vale só para deb/CRI/CRA (`AnbimaIndicativos`). NTN-B e curva DI (`MtmAnbima`, passos 7-8) **não são D-1** — são MtM casado por `dtNegocio`, logo precisam de X-1u **E** X (ver Regra 1).

## Regras que NÃO podem ser esquecidas

1. **MtM precisa de X-1u E X** (passos 7-8). `calc_spread_over` (passo 18) busca a taxa de referência em `MtmAnbima WHERE dtReferencia = dtNegocio` — não pela liquidação. Como os negócios de X liquidam tanto pela ponta X-1u quanto X, ambas as datas de MtM têm de existir. **Sintoma de erro:** `nullSemMtm > 0` no resumo do passo 18.
2. **AnbimaIndicativos só precisa de X-1u** (passos 3-4) para ESTE relatório — ver §"Por que D-1". Numa cadência diária, a Anbima de X que você raspar hoje vira o D-1 do relatório de amanhã (liq X+1u).
3. **Pular o que já está na base.** Se `X-1u` já foi processado num dia anterior, boletim/MtM/Anbima de `X-1u` já existem — não re-scrapar. Só rode os passos cujos dados faltam + a cadeia de cálculo (13-19).
4. **Ordem obrigatória:** `calc_taxa_negocios` (13) **antes** de `filtrar_trades` (14) — o filtro só atualiza `cdStatus` em linhas já existentes em `NegociosProcessados`. E `match_referencias` (17) **antes** de `calc_spread_over` (18) — o spread depende de `cdReferencia`. E `scrape_anbima_data_ativos` (6) **antes** de `validar_calc_b3` (12) — quem valida um fluxo que o ingestor vai mudar em seguida perde a validação. E `validar_calc_b3` (12) **antes** de `calc_taxa_negocios` (13) — o gate decide quais ativos a calc local pode precificar.
5. **Relatório usa o geral** (`gerar_relatorio_credito`, passo 19) — cobre toda a base e tem aba/filtro por data. O relatório diário (`gerar_relatorio_html --date X`) existe mas não é o caminho padrão de validação.
6. **Projeção de IPCA antes das 17h30** (passo 9). A Anbima republica as projeções por volta desse horário nos dias de divulgação do IPCA/IPCA-15 — rodar depois pega o valor certo, mas o ciclo diário costuma rodar de manhã.

## Exemplos concretos (validados em 30/06/2026)

### Liquidação 29/06 (segunda; X-1u = 26/06 sexta) — pipeline completo
Nada de 26 ou 29 estava na base → rodei os passos de scraping + cálculo:
`boletim(26→29)` → `deb(26)` → `cri_cra(26)` → `fianalytics` → `ntnb(26,29)` → `curva_di(26)`+`(29)` → `calc_taxa(29)` → `filtrar(29)` → `calc_spread_anbima(26)` → `match` → `calc_spread_over(29)` → `relatorio`.
Resultado: liq 29/06 = 1.091 ativos, R$ 1.820,66 MM.
⚠️ Este run **NÃO** incluiu `scrape_anbima_data_ativos` (passo 5) — ele foi formalizado no pipeline depois. Características vieram do FI Analytics; o **fluxo de caixa** de tickers novos pode estar faltando até rodar o passo 5.

### Liquidação 26/06 (sexta; X-1u = 25/06 quinta) — caminho enxuto
Boletim (25 e 26) e MtM (25 e 26) **já estavam na base** → pulei passos 1, 6 e 7. Rodei só:
`deb(25)` → `cri_cra(25)` → `calc_taxa(26)` → `filtrar(26)` → `calc_spread_anbima(25)` → `match` → `calc_spread_over(26)` → `relatorio`.
⚠️ Idem: passo 5 (`scrape_anbima_data_ativos`) não foi rodado.

## Scripts que existem mas NÃO entram no pipeline diário

Inventário completo: **20 scripts** em `code/scripts/`. Os 17 acima + estes 3:

| Script | Papel | Por que fora do pipeline |
|---|---|---|
| `scrape_outstanding_bloomberg` | popula `Outstanding` (saldo em circulação via Bloomberg) | só roda no **PC do banco** (terminal Bloomberg). Alimenta o peso da aba **Visão Anbima**. No PC pessoal a tabela fica vazia e a aba sem dados. Rodar quando estiver no banco. |
| `gerar_relatorio_html` | relatório **diário** por data (boletim de um único dia) | alternativa ao geral; o caminho padrão é o `gerar_relatorio_credito`. Mesma lógica de agregação. |
| `dump_api_samples` | utilitário de **debug**: chama FI Analytics + B3 Calculator e dumpa o JSON cru | ferramenta de desenvolvimento, não pipeline. **Sem nota no vault** (única lacuna de documentação). |

`code/lib/` (7 módulos: `db`, `config`, `logger`, `email_outlook`, `b3_calc_api`, `fianalytics_api`, `__init__`) é documentado coletivamente em [[10 - Scripts/libs]].

## Notas operacionais

- **Emails de conclusão (Outlook):** cada script tenta enviar um email `[OK]/[ERROR]` no fim. No PC pessoal isso costuma dar timeout de 20s (COM zombie segurando o STA do Outlook). É **não-fatal** — o script já gravou tudo antes. Reiniciar o PC resolve.
- **CRI/CRA (passo 3):** a UI do portal Anbima (`data.anbima.com.br/busca/certificado-de-recebiveis?view=precos`) é instável via Playwright — `goto` com `networkidle` ou o clique no menu CSV podem dar timeout. Normalmente é só **timing**: re-rodar resolve. Se persistir, validar a página na mão e conferir o seletor `ul.anbima-ui-toolbar__menu-files a`.
- **Janela de arquivamento das fontes:** debêntures Anbima ~4 meses; **CRI/CRA só ~5 pregões** (portal); curva DI da B3 ~20 pregões. Para datas mais antigas que isso, a fonte não devolve dados.

## Bug histórico relacionado

30/06/2026 — `scrape_b3_boletim.py` quebrava na largada pós-padronização PT-BR: a conversão F12 (snake_case → PascalCase) renomeou indevidamente `p.parse_args()` (método do `argparse`) para `p._ParseArgs()`. Corrigido para `p.parse_args()`. Lição: ao converter nomes de função, **não** tocar em métodos de stdlib/libs com mesmo nome.
