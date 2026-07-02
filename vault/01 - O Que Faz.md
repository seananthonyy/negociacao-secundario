# O Que Faz

> Ver também: [[00 - Inicio]] | [[02 - Como Rodar]] | [[09 - Progresso]]

## Visão geral

O sistema coleta e consolida os negócios de crédito privado brasileiro (Debêntures, CRIs e CRAs) que ocorreram no mercado secundário durante o dia. A fonte principal é o Boletim Diário da B3, que publica todas as operações registradas em formato CSV. Esses dados são baixados automaticamente, filtrados para os instrumentos relevantes, e armazenados em um banco SQLite local.

## O que o relatório mostra

O produto final são dois arquivos HTML gerados ao fim de cada dia útil: uma **prévia** (trades do próprio dia, ainda sujeitos a complementos tardios) e um **definitivo** (trades do dia anterior, consolidados). Cada relatório agrupa os negócios por ticker e mostra volume negociado, taxa calculada e spread sobre uma referência de mercado (NTN-B para indexados ao IPCA, DI Futuro para prefixados). A mesa de crédito usa esses relatórios para acompanhar o nível de atividade e as taxas praticadas no mercado secundário.

## Por que `dtLiquidacao` é a chave do relatório

O boletim B3 registra duas datas para cada negócio: `dtNegocio` (quando foi feito o trade) e `dtLiquidacao` (quando vai liquidar financeiramente, geralmente D+1). O problema é que boletas feitas num dia podem aparecer no boletim do dia seguinte. Por exemplo: um trade feito em 26/05 que liquida em 27/05 pode ser publicado no boletim de 27/05. Se o relatório agrupasse por `dtNegocio`, esse trade ficaria "perdido". Agrupando por `dtLiquidacao`, ele cai corretamente na janela do dia 27/05 — tanto na prévia quanto no definitivo.

## Enriquecimento dos dados

Além das colunas brutas do boletim B3, cada trade é enriquecido com:

- **Taxa calculada**: quando o boletim não traz taxa (campo vazio), o sistema tenta calculá-la via cascata de APIs: primeiro FI Analytics, depois B3 Calculator. Ver [[06 - Calculadoras/FI Analytics API]] e [[06 - Calculadoras/B3 Calculator API]].
- **Informações estáticas do ativo**: indexador, duration, vencimento — extraídas da planilha do FI Analytics. Ver [[05 - Fontes/FI Analytics]].
- **Taxas indicativas Anbima**: publicadas diariamente como referência de mercado. Ver [[05 - Fontes/Anbima]].
- **Spread over**: diferença entre a taxa do trade e a taxa da referência (NTN-B ou DI Futuro). Ver [[08 - Match de Referencia]].

## Filtro de duplicados

O boletim B3 registra os dois lados de uma operação casada (corretagem) e às vezes lança passagens de fundo como trades separados, gerando linhas que não refletem o preço real de mercado. O sistema aplica três filtros sequenciais com um algoritmo union-find, classificando cada trade como `VALIDO`, `FUNDO`, `BROKER` ou `PF`. O relatório exibe apenas os trades `VALIDO`. Ver [[07 - Filtro de Duplicados]].

---

## Glossário

| Termo | Definição |
|---|---|
| **trade** | Um registro de negócio no boletim B3; classificado como `VALIDO`, `FUNDO`, `BROKER` ou `PF`. |
| **ticker** | Código do ativo (ex: `DEBA11`, `CRIA12`). |
| **spread over** | Diferença em bps entre a taxa do trade e a taxa da referência de mercado. |
| **ref** | Ativo de referência para cálculo de spread: NTN-B (IPCA) ou DI Futuro (prefixado). Armazenado em `cdReferencia` na tabela `InfoAtivos`. |
| **indexador** | Índice de correção do ativo: `IPCA`, `PREFIXADO`, `CDI+`, `IGPM`, etc. |
| **NTN-B** | Título público federal indexado ao IPCA. Usado como referência para debêntures e CRIs/CRAs IPCA. Tickers no Bloomberg como "NTN-B 32". |
| **DI Futuro** | Contrato futuro de taxa DI na B3. Usado como referência para ativos prefixados. Tickers como "DI1F31". |
| **D+0 / D+1** | Prazo de liquidação: D+0 liquida no mesmo dia do trade; D+1 liquida no dia útil seguinte. |
| **prévia** | Relatório gerado no fechamento do dia com trades cuja `dtLiquidacao` = hoje. Pode ter complementos do boletim B3 chegando tarde. |
| **definitivo** | Relatório do dia anterior, com o boletim já completo e consolidado. |
| **`dtLiquidacao`** | Data de liquidação financeira do trade. Chave do agrupamento nos relatórios. |
| **`dtNegocio`** | Data em que o trade foi registrado na B3. Pode diferir de `dtLiquidacao`. |
