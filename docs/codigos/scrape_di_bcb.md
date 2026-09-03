# scrape_di_bcb.py

**Passo 11** do pipeline.

---

## Overview

Baixa a **taxa DI realizada** — a que o Banco Central divulga dia a dia — e grava na
tabela `DiHistorico`. É insumo da calculadora de renda fixa: sem ela não se precifica
papel indexado a CDI, porque o acúmulo do CDI passado sai daqui.

Migrado do `atualizar_di.py` do projeto `calculadora-renda-fixa` (handoff 12/07/2026). A
outra metade daquele script — a **curva** DI×pré, que é projeção e não realizado — vive no
`scrape_b3_curva_di`.

---

## Regras de negócio

**Duas séries da MESMA taxa**, porque a calculadora usa as duas:

| série SGS | o que é | coluna |
|---|---|---|
| 4389 | DI anualizado, % a.a. base 252 | `vrTaxaDiAnual` |
| 12 | DI ao dia, fator diário | `vrTaxaDiDiaria` |

**Zero dia devolvido é falha, não ausência.** A SGS sempre cobre a janela pedida, nem que
seja com um pregão. Se voltar vazio, o script levanta — é fonte quebrada, e sair com
sucesso sobre nada seria pior.

**Incremental por padrão.** Sem argumento, retoma do último `dtReferencia` gravado. Com
intervalo, força a janela (backfill).

---

## CLI

```powershell
python codigos\scripts\scrape_di_bcb\scrape_di_bcb.py
python codigos\scripts\scrape_di_bcb\scrape_di_bcb.py --start 2026-01-01 --end 2026-09-01
```

| argumento | efeito |
|---|---|
| *(nenhum)* | Incremental: do último dia gravado até hoje |
| `--start` / `--end` | Força a janela. Use para backfill |

---

## Interação com a base

**Lê:** `DiHistorico` (só o `MAX(dtReferencia)`, para saber onde retomar).

**Grava:** `DiHistorico`, por `Upsert` — o script é dono das três colunas da tabela, então
substituir a linha inteira é correto aqui.

---

## Detalhes técnicos

```
GET https://api.bcb.gov.br/dados/serie/bcdata.sgs.{serie}/dados
    ?formato=json&dataInicial=DD/MM/AAAA&dataFinal=DD/MM/AAAA
```

`httpx` com `trust_env=True` — lê o proxy do ambiente sozinho. Sem autenticação: a API do
BCB é pública.

**Alcance: desde 03/01/2000.** É a série mais profunda do projeto, e por isso o histórico
de DI **não precisa** de importação manual de planilha.

---

## Armadilhas

**As datas da API do BCB são `DD/MM/AAAA`**, não ISO. A conversão é na montagem da URL; um
formato errado devolve lista vazia em vez de erro — que é justamente por isso que o "zero
dia = falha" existe.

**A janela máxima da SGS é de ~10 anos por requisição.** Um backfill desde 2000 precisa ser
fatiado. O script não fatia sozinho: peça períodos menores.

**Fim de semana e feriado não vêm.** A série só tem dia útil. Não confunda com buraco.
