# scrape_anbima_cri_cra.py

**Passo 4** do pipeline.

---

## Overview

Raspa as **taxas indicativas de CRI e CRA** do portal da Anbima e grava em
`AnbimaIndicativos`.

**Precisa de navegador.** Diferente de debêntures e NTN-B, não há arquivo por URL: os
preços de certificado de recebíveis só existem no portal, atrás de JavaScript.

---

## Regras de negócio

Extrai `cdTicker`, `Taxa Indicativa` e a data de referência da tabela de preços.

Papel que a Anbima publica e que nunca negociou entra na base mesmo assim — a indicativa é
informação de mercado, independente de ter havido negócio.

---

## CLI

```powershell
python codigos\scripts\scrape_anbima_cri_cra\scrape_anbima_cri_cra.py --date 2026-09-01
```

| argumento | efeito |
|---|---|
| `--date` | Uma data de referência |
| `--start` / `--end` | Intervalo |
| `--no-headless` | Abre o navegador visível, para depuração |

---

## Interação com a base

**Grava:** `AnbimaIndicativos` (`vrTaxaAnbima` com `SOBRESCREVER`) e `InfoAtivos` (o que o
portal traz de cadastro, preenchendo buraco).

---

## Detalhes técnicos

Playwright sobre
`https://data.anbima.com.br/busca/certificado-de-recebiveis?view=precos`.

Seletores por **texto e role**, nunca por classe CSS — o portal usa Tailwind com hash, que
muda a cada build e já quebrou o scraper uma vez.

---

## Armadilhas

**O portal guarda ~5 pregões.** É a janela mais curta de todas as fontes. Perdeu, perdeu.

**Data de hoje pode não ter dado ainda.** Rodar de madrugada para o próprio dia falha
legitimamente — o portal publica ao longo do dia.

**Playwright precisa de Chromium instalado.** Numa máquina nova,
`playwright install chromium`.

**Vale testar se dá para sair do navegador.** O boletim da B3 saiu do Playwright em
02/09/2026 quando se descobriu que o endpoint era público — o navegador tinha entrado só
porque foi como ele foi descoberto. Este script pode estar no mesmo caso.
