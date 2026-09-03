# scrape_ipca_projetado_anbima.py

**Passo 10** do pipeline.

---

## Overview

Raspa a **projeção de IPCA** que a Anbima publica e grava na tabela `IPCAProjetado`.

Entre a divulgação de um IPCA e a do seguinte, o VNA dos papéis IPCA+ é atualizado pela
**projeção** — sem ela, não se precifica IPCA+ no mês corrente. É o insumo mais frágil do
projeto: ver Armadilhas.

Migrado do `atualizar_projecao.py` do projeto `calculadora-renda-fixa` (12/07/2026).

---

## Regras de negócio

**Data de validade, não data de publicação.** A Anbima publica uma projeção que passa a
valer a partir de uma data. O script grava uma linha por **dia corrido** em que aquela
projeção vale — é assim que a calc consulta, pela data do cálculo.

**Backup antes de gravar.** É a única tabela do projeto com rede de proteção própria,
porque é a única **não reconstituível**: a página da Anbima só mostra a projeção corrente
e ~13 meses para trás. Um dia perdido é perdido para sempre. O backup vai para `backups/`
e mantém os N mais recentes.

---

## CLI

```powershell
python codigos\scripts\scrape_ipca_projetado_anbima\scrape_ipca_projetado_anbima.py
```

Sem argumentos.

---

## Interação com a base

**Lê:** `IPCAProjetado` (contagem, para reportar novos).

**Grava:** `IPCAProjetado` por `Upsert` — chave `dtIPCAProjetado`.

**Copia:** `database/parquets/ipca_projetado/dados.parquet` → `backups/`, antes de escrever.

---

## Detalhes técnicos

Playwright sobre `https://www.anbima.com.br/pt_br/informar/estatisticas/`, com parse por
BeautifulSoup. Não há API — o número só existe na página.

---

## Armadilhas

**Rodar ANTES das 17h30.** A Anbima republica a projeção ao longo do dia. Rodar depois
pega um valor que pode ser revisado, e a linha do dia fica com o número errado.

**O histórico não é reconstituível.** Se a tabela for perdida e não houver backup, os meses
anteriores não voltam. É a razão do backup automático — não o remova.

**Projeção negativa é normal e é sinal.** Deflação acontece (a projeção de 27/08/2026 era
`-0,28%`). E há um risco dormindo aí: alguns papéis têm **piso de deflação** — o VNA não
cai em mês de IPCA negativo — e a nossa calc **não modela isso**. A divergência só aparece
no primeiro mês negativo de verdade. Ver o campo `note` em `helpers/b3_calc_api.md`.

**Playwright depende de Chromium instalado.** Numa máquina nova,
`playwright install chromium` antes da primeira rodada.
