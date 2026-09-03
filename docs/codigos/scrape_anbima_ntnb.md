# scrape_anbima_ntnb.py

**Passo 7** do pipeline.

---

## Overview

Baixa a **curva de NTN-B** que a Anbima publica — a curva de juro real do dia — e grava em
`MtmAnbima`. É a referência contra a qual o spread dos papéis IPCA é medido.

---

## Regras de negócio

Um vértice por vencimento de NTN-B, com a **taxa indicativa** (coluna 5 do arquivo).

**A duration não vem no arquivo.** Ela é calculada a partir do vencimento e da taxa, via FI
Analytics (`govbondcalculator`) ou, se a FI não responder, via `calcPU` da B3. É a duration
que casa o papel com o vértice no `match_referencias` — sem ela o vértice não serve.

O ticker é normalizado para `NTN-B AA` (ex.: `NTN-B 35`), que é como o relatório o exibe.

**Política de escrita:** `vrTaxa` com `SOBRESCREVER` — a taxa do dia sempre manda; e
`vrDuration` com `PREFERIR_NOVO` — não apaga uma duration já apurada quando a nova falha.

---

## CLI

```powershell
python codigos\scripts\scrape_anbima_ntnb\scrape_anbima_ntnb.py --date 2026-09-01
python codigos\scripts\scrape_anbima_ntnb\scrape_anbima_ntnb.py --start 2026-02-23 --end 2026-06-05
```

| argumento | efeito |
|---|---|
| `--date` | Uma data |
| `--start` / `--end` | Intervalo |
| `--workers N` | Threads do cálculo de duration |

---

## Interação com a base

**Grava:** `MtmAnbima`, por `Mesclar`.

---

## Detalhes técnicos

```
GET https://www.anbima.com.br/informacoes/merc-sec/arqs/m{aa}{mmm}{dd}.xls
    ex.: m26jul28.xls
```

Mesmo padrão do arquivo de debêntures, com prefixo `m` em vez de `d`. Público.

**Alcance: desde 21/02/2026**, igual ao de debêntures.

---

## Armadilhas

**A duration depende de rede.** Um vértice sem duration não casa com papel nenhum, e o
sintoma aparece lá na frente, como spread NULL — não aqui.

**Há ~3,5 meses de curva recuperáveis.** A base tem menos histórico de `MtmAnbima` do que a
fonte ainda entrega. É backfill de graça, e **expira** conforme a janela desliza.

**404 em dia sem pregão é normal.**
