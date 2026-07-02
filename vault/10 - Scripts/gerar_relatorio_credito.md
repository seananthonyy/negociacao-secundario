# gerar_relatorio_credito.py

> Script de análise histórica que gera um **relatório HTML interativo** com todos os dados da base. Diferente do [[10 - Scripts/gerar_relatorio_html|gerar_relatorio_html.py]], que gera o boletim de um único pregão sob demanda, este script carrega **todos os pregões disponíveis** de uma vez e entrega quatro visões navegáveis numa página só.

**Arquivo:** `code/scripts/gerar_relatorio_credito.py`
**Template:** `code/templates/relatorio_secundario.html`
**Output:** `data/relatorios/relatorio_secundario.html`

---

## Como rodar

```powershell
python scripts/gerar_relatorio_credito.py
```

Não aceita argumentos CLI. Lê tudo o que já existe no banco e sobrescreve o arquivo de saída.

> **Atenção — o relatório agrupa por `dtLiquidacao`, não por `dtNegocio`.** Um trade negociado num dia liquida no dia útil seguinte (D+1). Portanto, o último dia que aparece no relatório depende de quais trades já passaram por **todo o pipeline de processamento** (`calc_taxa_negocios` → `filtrar_trades` → `calc_spread_over`), não só de quais estão crus em `NegociosBrutos`.
>
> Caso típico (visto em 23/06/2026): havia ~4.100 trades negociados em 19/06 que liquidam em 22/06 já crus em `NegociosBrutos`, mas o relatório parava em 19/06 porque esses trades ainda não tinham sido processados. **Não precisou de scraping novo** — as referências Anbima/MtM de 19/06 (D-1 de 22/06) já estavam na base. Bastou rodar a cadeia local **só para a data de liquidação**:
>
> ```powershell
> python scripts/calc_taxa_negocios.py --date 2026-06-22
> python scripts/filtrar_trades.py     --date 2026-06-22
> python scripts/calc_spread_over.py   --date 2026-06-22
> python scripts/gerar_relatorio_credito.py
> ```
>
> Resultado: período 09→22/06, 10 pregões, 1.987 ativos, R$ 8.444,34 MM (22/06 sozinho: 564 tickers, R$ 898,27 MM). Trades em forward (ex: 4 liquidando em 24/06) ficam de fora até serem processados; os negócios de 22/06 (que liquidam 23/06) ainda nem foram raspados da B3.

---

## As quatro abas

### 1. Visão Geral

Gráfico com **dois subplots verticais** empilhados (layout refatorado em 16/06/2026 — substituiu o dual-axis original):

- **Subplot superior** (`yaxis.domain = [0.44, 1.0]`): volume diário em R$ MM — área preenchida azul navy (`#003087`)
- **Subplot inferior** (`yaxis2.domain = [0, 0.38]`): spread ex. %CDI em bps — linha laranja, largura 3, marcadores tamanho 7, grid `rgba(236,112,0,0.12)`
  - Se houver ativos %CDI: `yaxis3` overlaying `y2`, linha vermelha tracejada no mesmo subplot inferior
- Ambos os subplots compartilham `xaxis:'x'`; ticks de data aparecem na base do chart

A escolha de subplots empilhados (em vez de eixo Y duplo) foi feita para eliminar a compressão visual da série de spread — com dual-axis, a escala do volume distorcia a leitura de variação do spread de pregão para pregão.

### 2. Por Ticker

Multiselect com dropdown searchable. Ao selecionar um ou mais tickers, exibe:
- Volume (R$ MM) por pregão para cada ticker selecionado
- Spread por pregão (bps para não-%CDI, %CDI para indexados em CDI)

Tickers pré-ordenados por volume total decrescente no dropdown.

### 3. Spread × Duration

Gráfico de bolhas (scatter) por data:
- Eixo X: duration (anos)
- Eixo Y: spread (bps ou %CDI conforme indexador)
- Tamanho da bolha: volume em R$ MM

Seletor de data para filtrar qual pregão visualizar. Apenas ativos com `vrDuration IS NOT NULL` aparecem.

### 4. Boletim Diário ← primeira aba visível

Tabela idêntica ao relatório gerado por [[10 - Scripts/gerar_relatorio_html|gerar_relatorio_html.py]]. Seletor de data no topo para navegar entre os pregões disponíveis.

> A ordem das abas foi invertida em 16/06/2026: **Boletim Diário aparece primeiro** e começa ativo ao carregar a página. Ordem atual: Boletim Diário → Visão Geral → Por Ticker → Spread × Duration. Motivação: o boletim é a visão mais usada no dia a dia pela mesa.

- Agrupa VALIDO + BROKER por ticker
- Taxa e spread BROKER calculados por grupo (`idGrupoNegocio`)
- **Taxa/Spread Anbima casados por `dtNegocio`** (desde 02/07/2026): cada trade compara com a indicativa Anbima mais recente com `dtReferencia <= dtNegocio` (CTE `AnbimaMatch`, `ROW_NUMBER` por `idTrade`); no ticker, ponderado por volume. Reflete o mercado no momento do negócio e é simétrico ao MtM de `calc_spread_over`. **Substituiu** o antigo D-1 chapado (`AnbimaLatest`/`_CalcDMenos1`, removido). Ver [[13 - Migracao Banco]] §7.
- Filtros por coluna + filtro global de texto + dropdown por instrumento (DEB/CRI/CRA)
- Sort clicável em todas as colunas
- Footer com totais

---

## Diferenças em relação a `gerar_relatorio_html.py`

| Aspecto | `gerar_relatorio_html.py` | `gerar_relatorio_credito.py` |
|---|---|---|
| Escopo temporal | Um único `--date` | Todos os pregões da base |
| CLI | `--date` obrigatório | Sem argumentos |
| Output | `relatorio_YYYY-MM-DD.html` | `relatorio_secundario.html` (fixo) |
| Abas | Nenhuma (página única) | 4 abas navegáveis |
| Gráficos | Nenhum | Visão Geral, Por Ticker, Scatter Duration |
| Dados em memória | Apenas um pregão | Todos os pregões (JSON embutido no HTML) |

A lógica do Boletim Diário é próxima à de `gerar_relatorio_html.py` (mesma agregação BROKER por `idGrupoNegocio`), **exceto o casamento Anbima**: aqui é por `dtNegocio` (CTE `AnbimaMatch`, ver acima); o `gerar_relatorio_html.py` ainda usa o D-1 chapado (`AnbimaLatest`/`_CalcDMenos1`) por ser caminho secundário de validação.

---

## Estrutura de dados injetada no HTML

O template recebe um único JSON (`data_json`) embutido numa `<script>` tag. Campos principais:

```json
{
  "dtStart": "2026-06-09",
  "dtEnd":   "2026-06-15",
  "diario":  [
    { "dt": "2026-06-09", "volume": 812.4, "spreadBps": 142.3, "spreadPctCdi": null },
    ...
  ],
  "ticker":  [
    { "dt": "2026-06-09", "ticker": "PETR13", "emissor": "Petrobras", "indexador": "CDI+", "volume": 45.2, "spreadRaw": 0.0123 },
    ...
  ],
  "duration": [
    { "dt": "2026-06-09", "ticker": "PETR13", "emissor": "Petrobras", "indexador": "CDI+", "duration": 2.45, "volume": 45.2, "spreadRaw": 0.0123 },
    ...
  ],
  "tickers":     ["PETR13", "VALE14", ...],
  "datas":       ["2026-06-09", "2026-06-10", ...],
  "diasBoletim": ["2026-06-09", "2026-06-10", ...],
  "boletim": {
    "2026-06-09": {
      "tickers": [ { "cdTicker": "...", "vrVolumeTotal": 123.45, ... } ],
      "resumo":  [ { "cdInstrumento": "DEB", "nrTickers": 80, "vrVolume": 2100000.0, "nrTrades": 500 } ],
      "nrTickers": 350, "nrTrades": 2100, "vrVolumeTotal": 4053000000.0, "vrQtdTotal": 1800000
    },
    ...
  }
}
```

`spreadRaw` é o spread bruto em fração (ex: `0.0123` = 1,23%). O template Jinja2/JavaScript multiplica por 100 para exibição em bps (ou mantém como-é para %CDI).

---

## Visual do template

`code/templates/relatorio_secundario.html` é standalone (todo CSS/JS inline, sem dependências locais):
- Cores: `#003087` (navy Itaú), `#EC7000` (laranja Itaú), `#002370` (hover)
- Gráficos via Plotly CDN
- Pills fora do card: total tickers, total negócios, volume total, breakdown DEB/CRI/CRA
- Vencimento formatado como `mmm/aa` (ex: `jan/28`)
- Spread não-%CDI exibido em bps (`× 100`); %CDI exibido como-é

### Melhorias de UX implementadas em 16/06/2026

**1. Spread sem casas decimais**
Todos os valores de spread (bps e %CDI) são exibidos como inteiros arredondados. No template JS, a função `bFmt(v, 0)` (formato Itaú com separador de milhar) é chamada com `decimals=0`, e os hovertemplates Plotly usam `Math.round` antes de compor o texto. Aplica-se à coluna "Spread Méd. (bps)" do Boletim, aos pills da Visão Geral e a todos os traces dos gráficos Plotly.

**2. Datas em formato BR (DD/MM/AAAA) em todos os filtros**
Os filtros de período que antes usavam `<input type="date">` (formato `YYYY-MM-DD` nativo do browser, dependente de locale) foram substituídos por `<select class="fsel">` populados via JS com a função `buildDateSelect(id, dates, selected)`. Cada opção exibe a data no formato `${d}/${m}/${y}`. Afeta:
- Filtros de período Visão Geral: `g-start`, `g-end`
- Filtros de período Por Ticker: `t-start`, `t-end`
- Filtro de data Spread×Duration: `d-date`
- O boletim (`b-date`) já usava esse formato desde a implementação original

**3. Volume com separador de milhar nos pills**
Nos pills da Visão Geral e do Boletim, o volume total passou de `.toFixed(0)` para `bFmt(totVol, 0)` — exibe ponto como separador de milhar (ex: `4.053 MM` em vez de `4053 MM`).

**4. Boletim Diário como primeira aba**
Ver seção "As quatro abas" acima.

**5. Visão Geral com subplots verticais**
Ver seção "As quatro abas → Visão Geral" acima.

**Detalhe técnico — redimensionamento de charts ao trocar aba:**
`switchTab` chama `Plotly.Plots.resize()` com `setTimeout(..., 10)` após mostrar uma aba. Necessário porque o DOM do chart fica `display:none` enquanto a aba está inativa — o Plotly perde a referência de tamanho e o gráfico fica comprimido na primeira exibição sem o resize forçado.

### Contagem de ativos por vértice nas curvas NTN-B/DI1 (23/06/2026)

Na aba **Visão Anbima**, as curvas por referência (term-structure: x = vértice NTN-B/DI1, y = spread/nominal, uma curva por data) passaram a mostrar **quantos ativos compõem cada ponto**. Motivação da mesa: vértices com poucos ativos têm viés — ex: o ponto **NTN-B 50** é formado por **1 único ativo**, enquanto **NTN-B 35** agrega 122. Sem essa informação, um ponto solto distorce a leitura da curva sem aviso.

- **Python (`gerar_relatorio_credito.py`):** `_SQL_ANBIMA_REF` ganhou `COUNT(DISTINCT ai.cdTicker) AS nAtivos`; `_LoadAnbimaRef` expõe o campo `n` em cada item de `anbimaRef`.
- **Template (`relatorio_secundario.html`, função `renderAnbimaRef`):** cada ponto ganhou um rótulo `n=X` (texto posicionado em "top center") e o hover passou a mostrar `… · X ativo(s)`. O rótulo fica na **curva de spread**; se apenas Nominal estiver marcado, ele migra para a curva nominal — evita duplicar o número quando as duas escalas estão visíveis.
- **Contagens reais (último dia, NTN-B):** B35=122, B30=98, B33=96 … B45=2, B50=1.

Apenas apresentação — nenhuma coluna nova no banco.

> **Atualizado em 25/06/2026:** o rótulo `n=X` **saiu do plot** e virou uma **tabela abaixo de cada gráfico** (`tbl-anb-ntnb` / `tbl-anb-di1`): linhas = vértice, colunas = cada data selecionada, célula = nº de ativos. O `n` continua no hover. Ver a seção "Sessão 25/06/2026" abaixo.

---

## Funções internas

| Função | Papel |
|---|---|
| `_LoadFeriados()` | Set de feriados de `data/feriados_anbima.csv` (usado nos `rangebreaks` dos gráficos) |
| `_WeightedAvg(valores)` | Média ponderada por volume, ignora valores `None` (taxa/spread do trade **e do Anbima**) |
| `_TradeRow` (dataclass) | Linha de trade antes de agregar por ticker |
| `_FetchTrades(conn, dt)` | Busca VALIDO para um pregão; Anbima casado por `dtNegocio` (CTE `AnbimaMatch`) |
| `_FetchBrokerGroups(conn, dt)` | Busca BROKER, agrega por `idGrupoNegocio`, calcula taxa e spread do grupo |
| `_AggregateTicker(cdTicker, grupo)` | Agrega lista de `_TradeRow` em dict por ticker |
| `_LoadBoletim(conn, log)` | Itera todos os pregões e chama as funções acima |
| `_LoadDiario(conn)` | Executa `_SQL_DIARIO` → lista de dicts para aba Visão Geral |
| `_LoadTicker(conn)` | Executa `_SQL_TICKER` → lista de dicts para aba Por Ticker |
| `_LoadDuration(conn)` | Executa `_SQL_DURATION` → lista de dicts para aba Duration |
| `_LoadAnbimaRef(conn)` | Executa `_SQL_ANBIMA_REF` → curvas por referência NTN-B/DI1 da aba Visão Anbima; expõe `n` (`COUNT(DISTINCT cdTicker)` de ativos por vértice) |
| `_GetTickersByVolume(tickerRows)` | Ordena tickers por volume total decrescente |
| `_RenderHtml(...)` | Monta o payload JSON e renderiza o template Jinja2 |
| `_BuildSummary(diario, ticker)` | Texto de resumo para o email e stdout |
| `_ParseArgs()` | Sem argumentos; presente por convenção de projeto |
| `Main()` | Ponto de entrada: lê banco → renderiza → salva HTML → envia email |

---

## Email e logs

- Logs em: `data/logs/gerar_relatorio_credito/{YYYY-MM-DD_HHMMSS}.log`
- Email ao final: assunto `[OK] gerar_relatorio_credito` ou `[ERROR] gerar_relatorio_credito`
- Corpo do email de sucesso: período, número de pregões, tickers únicos e volume total

---

## Dependências

- `lib/db.py`, `lib/config.py`, `lib/logger.py`, `lib/email_outlook.py`
- `jinja2` (já em `requirements.txt`)
- Tabelas lidas: `NegociosProcessados`, `NegociosBrutos`, `InfoAtivos`, `AnbimaIndicativos`, `MtmAnbima`
- Arquivo auxiliar: `data/feriados_anbima.csv` (para `_LoadFeriados` → `rangebreaks` dos gráficos)
- Deve rodar **depois** de todo o pipeline estar executado: [[10 - Scripts/gerar_relatorio_html]] é o script de relatório diário; este é o script de análise histórica

---

**Decisão (15/06/2026) — relatório histórico separado do diário:** o relatório de análise histórica (`relatorio_secundario.html`) é gerado por um script distinto e tem output fixo. Não substitui o `gerar_relatorio_html.py` (que continua gerando boletins diários por data); os dois coexistem. O script histórico não aceita `--date` pois sua proposta é sempre mostrar tudo que há na base.

**Decisão (16/06/2026) — subplots em vez de dual-axis na Visão Geral:** dual-axis comprimia a curva de spread visualmente porque a escala do volume dominava. Subplots empilhados dão espaço independente a cada série, tornando variações de spread legíveis mesmo quando o volume é muito maior em magnitude.

**Decisão (16/06/2026) — `<select>` em vez de `<input type="date">` nos filtros:** o `<input type="date">` exibe a data no formato do locale do sistema operacional do usuário. Em Windows configurado como pt-BR, pode exibir `DD/MM/AAAA` na UI mas o `.value` continua sendo `YYYY-MM-DD`. Para eliminar ambiguidade e garantir consistência visual, todos os filtros de data passaram a usar `<select>` com opções formatadas explicitamente em JS.

---

## Sessão 25/06/2026 — mudanças nas abas

> Esta nota acima descreve o estado "4 abas" original. O relatório hoje tem **6 abas**: **Boletim Diário · Visão Mercado · Por Ativo · Spread × Duration · Info Ativos · Visão Anbima**. As mudanças abaixo são as desta sessão.

**Rename: "Visão Geral" → "Visão Mercado".** Só o rótulo do botão da aba (`switchTab('geral', ...)` mantém o id interno `geral`/`renderGeral`/`pills-geral`).

**Visão Mercado — quebra do "Volume Total" por indexador.** Além da pill "Volume Total", agora há uma pill por indexador (CDI+/%CDI/IPCA/Prefixado) com **volume (R$ MM) e % do total** (com quadradinho colorido `IDX_COLOR`). Calculado em `renderGeral` somando `byIdx[key][dt].volume` por indexador no período.

**Spread × Duration — filtro de instrumento por gráfico.** Cada um dos 4 gráficos (por indexador) ganhou um grupo de checkboxes **CRI/CRA/DEB/DEB 12.431** independente (`dfilt-cdi/pctcdi/ipca/pre`; vazio = todos), populado por `durBuildFilters()` com os tipos presentes naquele indexador. `renderDuration` filtra as bolhas por `TICKER_TIPO[ticker]` via `durActiveFilter(cfg)`. `DUR_CHARTS` ganhou campo `filt`.

**Por Ativo — colunas da tabela de negócios (`renderTkTrades`).** Removidas **Taxa Méd. Negócio** e **Taxa Méd. Anbima**; adicionada **Volume (R$M)** (`vrVolumeTotal/1e6`) após Indexador. Tabela passou de 9 → 8 colunas (colspan e placeholder de `tkTradesSort` ajustados).

**Visão Anbima — duas agregações distintas (mudança central da sessão):**

1. **Gráficos por indexador (CDI+/%CDI/IPCA/Pré): média ponderada por OUTSTANDING (date-matched) dos tickers SELECIONADOS num filtro MANUAL.** Cada gráfico tem um dropdown de ticker (busca por ticker/emissor, multi-seleção, **default todos**). O usuário tira na mão os high-yield estressados — decisão dele de preferir filtro manual a filtro estatístico (testamos winsor/MAD/2-estágios e ele optou por curadoria manual). `_SQL_ANBIMA_IDX`/`_LoadAnbimaIdx` mandam **linha por ticker** (dt, indexador, ticker, emissor, spread, peso); a média ponderada é feita no JS (`renderAnbimaIdxChart`; estado `ANB_TK`/`ANB_TK_UNIV`/`ANB_EMISSOR`; funções `anbTk*`).

   > **Peso = outstanding real (mudança 28/06/2026).** `_PESO_CTE` agora seleciona de `Outstanding (cdTicker, dtOutstanding, vrOutstanding)` em vez de `InfoAtivos.vrQuantidadeEmissao`; os 3 JOINs da Visão Anbima (`_SQL_ANBIMA_IDX`/`_DUR`/`_REF`) casam a data (`AND p.dtPeso = ai.dtReferencia`). Troca **limpa, sem fallback** para emissão (misturar outstanding em R$/face com contagem de unidades na mesma média corromperia a escala). **Consequência:** com `Outstanding` vazia (PC pessoal, sem Bloomberg), a aba Visão Anbima fica sem dados até rodar `scrape_outstanding_bloomberg.py` no banco e trazer o `trades.db`. Ver [[scrape_outstanding_bloomberg]].

2. **Curvas por referência NTN-B/DI1: MEDIANA + filtro de tipo de instrumento.** `_SQL_ANBIMA_REF`/`_LoadAnbimaRef` mandam **linha por ticker** (com `tipo` via `_TipoExibicao`); a **mediana** por (dia, ref) é feita no JS (`_anbRefAgg`/`median`/`renderAnbimaRef`). A barra das curvas tem checkboxes **DEB/DEB 12.431/CRI/CRA** (`#anb-ref-tipo`, default todos, vazio=todos) que filtram ambas as curvas antes da mediana. **Por que mediana:** uns poucos high-yield grandes puxavam a média ponderada do vértice — ex.: NTN-B 31 em 24/06 = média pond. **337 bps** vs **mediana 36 bps** (CSNAB4 a 1143, CMIN11 633). Mediana ponderada e trim p10-p90 NÃO resolviam (≈337, os papéis grandes são os largos); só mediana/MAD contornam.

> **Importante (NULL de taxa não entra nas curvas):** ativos sem taxa indicativa (`vrTaxaAnbima = NULL`) têm `vrSpreadAnbima = NULL` e são excluídos pelas duas queries (`WHERE vrSpreadAnbima IS NOT NULL`) — nunca entram no payload das curvas nem na contagem `n`. Verificado: 0 linhas com spread NOT NULL e taxa NULL.

> **Nota de payload:** `anbimaIdx` e `anbimaRef` agora vêm **por ticker** (antes vinham pré-agregados). Isso aumentou o HTML de ~9 MB para ~13 MB — aceitável (arquivo local).

**A Visão Mercado (spread de trade) NÃO mudou a agregação** — continua **ponderada por volume** + gate de plausibilidade (`_BANDA_SPREAD`). Discutimos aplicar robustez (winsor/MAD) nela mas o usuário pediu pra deixar como está; a curadoria manual ficou só na Visão Anbima. Ver [[../09 - Progresso]].

---

Ver também: [[10 - Scripts/filtrar_trades]], [[10 - Scripts/calc_spread_over]], [[04 - Banco de Dados]]
