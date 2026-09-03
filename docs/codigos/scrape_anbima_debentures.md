# scrape_anbima_debentures.py

**Passo 3** do pipeline.

---

## Overview

Baixa o arquivo diário de **taxas indicativas de debêntures** da Anbima e grava em
`AnbimaIndicativos`. É o preço de referência do mercado para cada papel naquele dia.

Download direto por URL previsível — **sem Playwright**.

---

## Regras de negócio

**A Taxa Indicativa é a coluna 6**, não a 5. A coluna 5 é a Taxa de Venda. Trocá-las dá um
spread sistematicamente enviesado, e é um erro difícil de perceber porque os dois números
são plausíveis.

Também aproveita do mesmo arquivo: emissor, vencimento, indexador, duration e o vértice de
NTN-B que a própria Anbima associa — este último vira `cdReferencia` com
`cdFonteReferencia = 'Anbima'`, que o `match_referencias` **não sobrescreve**.

Linha sem ticker, com espaço no ticker, ou começando com `Obs` é ignorada — é rodapé.

---

## CLI

```powershell
python codigos\scripts\scrape_anbima_debentures\scrape_anbima_debentures.py --date 2026-09-01
python codigos\scripts\scrape_anbima_debentures\scrape_anbima_debentures.py --start 2026-08-01 --end 2026-09-01
```

| argumento | efeito |
|---|---|
| `--date` | Uma data de referência |
| `--start` / `--end` | Intervalo, um arquivo por dia útil |

---

## Interação com a base

**Grava:** `AnbimaIndicativos` (`vrTaxaAnbima` com `SOBRESCREVER` — a taxa do dia sempre
manda) e `InfoAtivos` (emissor, vencimento, indexador, duration e referência, com política
de preencher-buraco).

---

## Detalhes técnicos

```
GET https://www.anbima.com.br/informacoes/merc-sec-debentures/arqs/d{aa}{mmm}{dd}.xls
    ex.: d26jul28.xls
```

O mês é abreviado em português (`jan`, `fev`, ..., `dez`). Arquivo `.xls` binário, lido com
`xlrd`. Público, sem autenticação.

**Alcance: desde 21/02/2026** — janela deslizante de ~6 meses, verificada por bissecção
(20/02 devolve 404, 21/02 baixa). Tudo anterior está fora de alcance.

---

## Armadilhas

**404 em dia sem pregão é normal.** Feriado e fim de semana não têm arquivo.

**A janela desliza.** O que não for raspado nos ~6 meses seguintes some para sempre. Se o
histórico da base começa depois do que a fonte ainda entrega, há dado recuperável parado.

**O ticker vem com espaço em algumas linhas de rodapé.** O filtro por espaço é o que separa
papel de texto livre.
