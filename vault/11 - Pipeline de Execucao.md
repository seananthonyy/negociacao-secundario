# Pipeline de Execução — Relatório de Liquidação X

> Ordem canônica para gerar o relatório de uma data de liquidação. Fonte da verdade do **fluxo operacional**. Ver também [[09 - Progresso]] (histórico de cargas) e [[04 - Banco de Dados]] (schema).

## Conceito

O relatório **agrupa por `dtLiquidacao`** (não `dtNegocio`). Para uma data de liquidação `X`:

- `X-1u` = **dia útil anterior** a X (pula fim de semana e feriados Anbima).
- Os negócios que liquidam em `X` foram negociados em:
  - **`X-1u`** → liquidam em X por **D+1** (a maioria);
  - **`X`** → liquidam em X por **D+0** (mesmo dia);
  - ocasionalmente datas anteriores (trades *forward*), cujo MtM já costuma estar na base.

## Os 13 passos

| # | Script | Parâmetro | Data(s) | Rede? | Por quê |
|---|---|---|---|---|---|
| 1 | `scrape_b3_boletim` | `--start X-1u --end X` | X-1u, X | ✅ Playwright | negócios das 2 pontas que liquidam em X |
| 2 | `scrape_anbima_debentures` | `--date X-1u` | X-1u | ✅ httpx | taxa indicativa deb — o relatório usa a Anbima de **D-1** (ver §"Por que D-1") |
| 3 | `scrape_anbima_cri_cra` | `--date X-1u` | X-1u | ✅ Playwright | idem CRI/CRA (UI instável — ver nota) |
| 4 | `scrape_fianalytics_planilha` | *(sem data)* | snapshot | ✅ Playwright | características (indexador, duration, venc.) de tickers novos |
| 5 | `scrape_anbima_data_ativos` | `--start X-1u --end X` | X-1u, X | ✅ Playwright | características + **fluxo de caixa** dos tickers negociados/divulgados que tenham info faltante na base (modo incremental) |
| 6 | `scrape_anbima_ntnb` | `--start X-1u --end X` | X-1u **e** X | ✅ httpx | MtM NTN-B nas 2 datas de **negócio** (não é D-1!) |
| 7 | `scrape_b3_curva_di` | `--date` (rodar **2×**) | X-1u **e** X | ✅ httpx | MtM curva DI — script só aceita `--date` |
| 8 | `calc_taxa_negocios` | `--date X` | X | ✅ APIs FI/B3 | taxa por trade (cascata FI Analytics → B3) |
| 9 | `filtrar_trades` | `--date X` | X | ❌ local | classifica VALIDO / FUNDO / BROKER / PF |
| 10 | `calc_spread_anbima` | `--date X-1u` | X-1u | ❌ local | spread Anbima das indicativas (mesma data que o relatório exibe) |
| 11 | `match_referencias` | *(sem data)* | — | ❌ local | preenche `cdReferencia` faltante via duration vs MtmAnbima |
| 12 | `calc_spread_over` | `--date X` | X | ❌ local | spread dos trades — **casa MtM por `dtNegocio`** |
| 13 | `gerar_relatorio_credito` | *(sem args)* | — | ❌ local | regenera `data/relatorios/relatorio_secundario.html` (toda a base) |

> **Ordem do passo 5:** `scrape_anbima_data_ativos` roda **depois** de boletim (1) e Anbima deb/cri (2-3) — porque monta a fila de tickers a partir da união `NegociosBrutos.dtNegocio` + `AnbimaIndicativos.dtReferencia` — e **antes** de `match_referencias` (11), que usa `InfoAtivos.vrDuration` que este passo pode preencher. Rodar o FI Analytics (4) antes reduz o trabalho dele (menos tickers "incompletos").

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

Por isso `scrape_anbima_debentures`/`cri_cra` (2-3) e `calc_spread_anbima` (10) rodam em **X-1u** — é a data das indicativas que o relatório vai exibir.

**Cuidado:** isso vale só para deb/CRI/CRA (`AnbimaIndicativos`). NTN-B e curva DI (`MtmAnbima`, passos 6-7) **não são D-1** — são MtM casado por `dtNegocio`, logo precisam de X-1u **E** X (ver Regra 1).

## Regras que NÃO podem ser esquecidas

1. **MtM precisa de X-1u E X** (passos 6-7). `calc_spread_over` (passo 12) busca a taxa de referência em `MtmAnbima WHERE dtReferencia = dtNegocio` — não pela liquidação. Como os negócios de X liquidam tanto pela ponta X-1u quanto X, ambas as datas de MtM têm de existir. **Sintoma de erro:** `nullSemMtm > 0` no resumo do passo 12.
2. **AnbimaIndicativos só precisa de X-1u** (passos 2-3) para ESTE relatório — ver §"Por que D-1". Numa cadência diária, a Anbima de X que você raspar hoje vira o D-1 do relatório de amanhã (liq X+1u).
3. **Pular o que já está na base.** Se `X-1u` já foi processado num dia anterior, boletim/MtM/Anbima de `X-1u` já existem — não re-scrapar. Só rode os passos cujos dados faltam + a cadeia de cálculo (8-13).
4. **Ordem obrigatória:** `calc_taxa_negocios` (8) **antes** de `filtrar_trades` (9) — o filtro só atualiza `cdStatus` em linhas já existentes em `NegociosProcessados`. E `match_referencias` (11) **antes** de `calc_spread_over` (12) — o spread depende de `cdReferencia`.
5. **Relatório usa o geral** (`gerar_relatorio_credito`, passo 13) — cobre toda a base e tem aba/filtro por data. O relatório diário (`gerar_relatorio_html --date X`) existe mas não é o caminho padrão de validação.

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

Inventário completo: **16 scripts** em `code/scripts/`. Os 13 acima + estes 3:

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
