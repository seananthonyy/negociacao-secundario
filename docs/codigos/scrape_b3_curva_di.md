# scrape_b3_curva_di.py

**Passo 8** do pipeline. **Dois destinos num download só.**

---

## Overview

Baixa a curva DI×pré da B3 e a grava em **dois lugares**:

| destino | o quê | para quê |
|---|---|---|
| `MtmAnbima` | os contratos `DI1F27` … `DI1F34` | referência de spread de papel **prefixado** |
| `CurvaDi` | a curva **inteira**, vértice a vértice | insumo da calculadora, para projetar CDI |

São usos diferentes do mesmo arquivo, e por isso um script só.

---

## Regras de negócio

**Os contratos DI1 viram vértices de referência.** O vencimento de cada contrato é
calculado pelo código (`F27` = janeiro/2027, primeiro dia útil), e a duration sai dos dias
úteis até lá.

**A curva inteira vai para `CurvaDi`** com `du` (dias úteis), `diasCorridos` e a taxa —
é a partir dela que a calc monta o fator de projeção do CDI.

**Só aceita data única.** Rodar uma vez por pregão; a B3 publica um arquivo por dia.

---

## CLI

```powershell
python codigos\scripts\scrape_b3_curva_di\scrape_b3_curva_di.py --date 2026-09-02
```

| argumento | efeito |
|---|---|
| `--date` | O pregão. **Obrigatório** — não aceita intervalo |

---

## Interação com a base

**Lê:** o CSV da B3 e o calendário de feriados (para o cálculo de dias úteis).

**Grava:**
- `CurvaDi`, por `Upsert` — o script é dono das quatro colunas;
- `MtmAnbima`, por `Mesclar` com `SOBRESCREVER` em `vrTaxa` e `vrDuration`.

---

## Detalhes técnicos

```
GET https://sistemaswebb3-derivativos.b3.com.br/referenceRatesProxy/
```

Público. A B3 devolve as ~20 datas mais recentes; o script escolhe a pedida.

---

## Armadilhas

**A B3 guarda ~20 pregões.** O histórico profundo da curva não existe em lugar nenhum
online — é a única série do projeto que precisaria mesmo de importação de planilha.

**Sem a curva do dia, papel CDI não precifica.** A calc levanta
`Curva DI x pre indisponivel para {data}` — e é o comportamento certo. Um pregão sem curva
derruba o `calc_pu_par` e o gate para todo CDI+ e %CDI daquela data.

**O vencimento do contrato é calculado, não lido.** `DI1F30` vence no primeiro dia útil de
janeiro/2030. Se a B3 mudar a convenção de código, o cálculo erra em silêncio.

**Vértice ausente para um contrato é avisado, não fatal.** O log registra
`du=N nao encontrado`; o contrato fica sem duration naquele dia.
