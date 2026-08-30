# Início — Relatório Secundário de Crédito Privado

> 🚧 **29/08/2026:** estrutura de pastas e armazenamento mudaram no branch
> `refactor/split-bases` (SQLite → Parquet + DuckDB; `lib/`+`scripts/`+`data/` →
> `Helpers/`+`codigos/`+`files/`). **Leia [[17 - Armazenamento Parquet e AWS]] primeiro.**


Sistema Python local que gera relatórios HTML diários consolidando negócios secundários de Debêntures, CRIs e CRAs da B3, com taxa, spread sobre referência, e filtro de duplicados.

> **Onde estou no projeto?** → [[09 - Progresso]]
>
> **Documento mestre** (decisões fechadas, schema completo, contratos de scripts): ver `PLANEJAMENTO_v5.md` na raiz.

## Mapa de notas

### Visão e operação
- [[01 - O Que Faz]]
- [[02 - Como Rodar]]
- [[03 - Estrutura de Pastas]]

### Técnico
- [[04 - Banco de Dados]]
- [[07 - Filtro de Duplicados]]
- [[08 - Match de Referencia]]
- [[11 - Pipeline de Execucao]] — os 19 passos e a ordem entre eles

### Precificação (leia estas antes de mexer em cadastro, fluxo ou calc)
- [[15 - Cadastro dos Ativos]] — **a B3 é a fonte primária**; o pacote indivisível; as 3 armadilhas do fluxo da B3
- [[14 - Rotinas da Calculadora]] — o que a calc é, as 4 mudanças autorizadas nela, e **o que ainda não fecha**

### Fontes de dados
- [[05 - Fontes/B3 Boletim]]
- [[05 - Fontes/FI Analytics]]
- [[05 - Fontes/Anbima]]

### Calculadoras
- [[06 - Calculadoras/FI Analytics API]]
- [[06 - Calculadoras/B3 Calculator API]]

### Scripts
- [[10 - Scripts/scrape_b3_boletim]]
- [[10 - Scripts/scrape_anbima_debentures]]
- [[10 - Scripts/scrape_anbima_cri_cra]]
- [[10 - Scripts/calc_taxa_negocios]]
- [[10 - Scripts/filtrar_trades]]
- [[10 - Scripts/scrape_anbima_ntnb]]
- [[10 - Scripts/scrape_b3_curva_di]]
- [[10 - Scripts/calc_spread_anbima]]
- [[10 - Scripts/match_referencias]]
- [[10 - Scripts/calc_spread_over]]
- [[10 - Scripts/gerar_relatorio_credito]]
- [[10 - Scripts/libs]]

### Estado
- [[09 - Progresso]]

### Configuração
- [[99 - Credenciais e Links]]
