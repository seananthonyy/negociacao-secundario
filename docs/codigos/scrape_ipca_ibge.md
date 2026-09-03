# scrape_ipca_ibge.py

**Passo 9** do pipeline.

---

## Overview

Baixa o **IPCA realizado** (número-índice) do IBGE e grava na tabela `IPCA`. É insumo da
calculadora: o VNA de papel IPCA+ é corrigido por este índice.

Migrado do `atualizar_ipca.py` do projeto `calculadora-renda-fixa` (handoff 12/07/2026).

---

## Regras de negócio

**Duas fontes, uma tabela:**

| fonte | traz | cobre |
|---|---|---|
| SIDRA tabela 1737, variável 2266 | o número-índice (base dez/1993 = 100) | desde 12/1979 |
| API Calendário do IBGE | a data de divulgação de cada mês | só de 12/2016 em diante |

A data de divulgação importa porque é ela que decide **quando** o índice do mês passa a
valer para o VNA. Antes de 12/2016 ela fica NULL, e a calc usa a convenção padrão.

**A SIDRA devolve a série INTEIRA a cada chamada.** Não há incremental — o script grava
tudo por `Upsert` e conta quantos meses são novos.

---

## CLI

```powershell
python codigos\scripts\scrape_ipca_ibge\scrape_ipca_ibge.py
```

Sem argumentos. Reprocessa a série completa toda vez, e é idempotente.

---

## Interação com a base

**Lê:** `IPCA` (só a contagem, para reportar quantos entraram).

**Grava:** `IPCA` por `Upsert` — chave `dtIPCA`, no formato `AAAA-MM`.

---

## Detalhes técnicos

```
GET https://apisidra.ibge.gov.br/values/t/1737/n1/all/v/2266/p/all/f/n
GET https://servicodados.ibge.gov.br/api/v3/calendario/?qtd=500&de={ini}&ate={fim}
```

Ambas públicas, sem autenticação. `httpx` com `trust_env=True`.

---

## Armadilhas

**`dtIPCA` é `AAAA-MM`, não `AAAA-MM-DD`.** É mês de referência, não data. Comparar com
uma data completa nunca casa.

**O número-índice não é a variação.** É o índice acumulado desde a base; a variação do mês
sai da razão entre dois índices. Confundir os dois erra o VNA por ordens de grandeza.

**O calendário do IBGE não cobre o histórico todo.** Divulgação NULL antes de 12/2016 é
esperado, não é buraco a preencher.

**A projeção do mês corrente vem de outro lugar** — `scrape_ipca_projetado_anbima`. Entre
a divulgação de um IPCA e a do seguinte, é a projeção que atualiza o VNA.
