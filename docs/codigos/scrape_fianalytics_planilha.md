# scrape_fianalytics_planilha.py

**Passo 5** do pipeline.

---

## Overview

Faz login no portal da FI Analytics, baixa os CSVs de **debêntures** e de **CRI/CRA**, e
completa `InfoAtivos` com o que falta.

É a **terceira** fonte de cadastro, atrás da B3 e da Anbima: ela só preenche buraco, nunca
sobrescreve o que as outras duas já apuraram.

---

## Regras de negócio

**Campos usados:** `Ticker`, `Indexador`, `Emissor`, `Vencimento`, `Duration`,
`Taxa Emissão (%)`.

**A duration já vem em anos** — não dividir por 252.

**Política de escrita:** identificação (instrumento, emissor, vencimento) e duration são
reescritas; `cdIndexador` e `vrTaxaEmissao` são `PREFERIR_ATUAL` — só preenchem o buraco
que a Anbima ou a B3 deixaram.

**A FI não fornece `cdReferencia`.** Esse campo sai daqui sempre NULL.

---

## CLI

```powershell
python codigos\scripts\scrape_fianalytics_planilha\scrape_fianalytics_planilha.py
python codigos\scripts\scrape_fianalytics_planilha\scrape_fianalytics_planilha.py --no-headless
```

| argumento | efeito |
|---|---|
| *(nenhum)* | Baixa as duas planilhas |
| `--no-headless` | Navegador visível, para depurar mudança de layout |

Sem data: a planilha é o retrato corrente do cadastro, não uma série.

---

## Interação com a base

**Grava:** `InfoAtivos`, por `Mesclar` com a política mista descrita acima.

---

## Detalhes técnicos

Playwright com login (`fianalyticsUser` / `fianalyticsPass`). O fluxo do layout de jul/2026:
login (`name=email`/`password`, botão *Entrar*) cai na lista de debêntures → botão
*Exportar* baixa o CSV; para CRI/CRA, clica no item de menu *Lista* (o de CRI/CRA é o
**último** dos dois) e no mesmo *Exportar*.

**Seletores por texto e role, nunca por classe CSS** — Tailwind com hash muda a cada build
e já quebrou o scraper.

CSV: UTF-8 com BOM, separador `;`, decimal vírgula.

---

## Armadilhas

**É o script mais frágil do projeto.** Depende de login e de layout. Já quebrou em
19/07/2026 (layout novo) e voltou a quebrar em 03/09/2026 — o login funciona, mas o botão
*Exportar* não é encontrado. Falha aqui **não derruba o pipeline**: a FI é a terceira fonte,
e o cadastro já veio da B3 e da Anbima.

**"0 tickers em TODAS as planilhas" é falha (exit 1), de propósito.** Sair com sucesso
sobre zero linha esconderia a quebra.

**A coluna `% PU Par` existe e é ignorada de propósito.** Ela mede `preço indicativo da FI ÷
puPar na data do download` — não é o `%par` do negócio, que tem numerador e denominador
diferentes. Ver `calc_pu_par.md`.

**Playwright precisa de Chromium instalado.**
