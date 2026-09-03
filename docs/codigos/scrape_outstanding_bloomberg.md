# scrape_outstanding_bloomberg.py

**Passo 12** do pipeline. **Só roda no ambiente do banco.**

---

## Overview

Puxa o **saldo em circulação** (`AMT_OUTSTANDING`) de cada papel via terminal Bloomberg e
grava em `Outstanding`.

Serve de **peso** da média ponderada por indexador na aba Visão Anbima.

---

## Regras de negócio

Para cada dia, busca os tickers da união de **negociados** (`NegociosBrutos.dtNegocio`) e
**divulgados pela Anbima** (`AnbimaIndicativos.dtReferencia`), excluindo NTN-B e DI1 — que
não são papéis de crédito privado.

Valor nulo não é gravado: ausência de resposta da Bloomberg não deve virar saldo zero.

---

## CLI

```powershell
python codigos\scripts\scrape_outstanding_bloomberg\scrape_outstanding_bloomberg.py --start 2026-09-01 --end 2026-09-02
```

| argumento | efeito |
|---|---|
| `--date` | Uma data |
| `--start` / `--end` | Intervalo |

---

## Interação com a base

**Lê:** `NegociosBrutos` e `AnbimaIndicativos` (para montar a lista de tickers).

**Grava:** `Outstanding`, por `Mesclar` com `SOBRESCREVER` — o valor da Bloomberg do dia
sempre manda.

---

## Detalhes técnicos

Biblioteca `xbbg`, que fala com o terminal Bloomberg local. **Não há endpoint HTTP** — é o
único fluxo do projeto que depende de software instalado.

---

## Armadilhas

**Vazio no PC pessoal é o esperado, não bug.** Sem terminal, a tabela fica vazia — e a
Visão Anbima cai automaticamente para a **quantidade de emissão** como proxy, com
disclaimer na aba.

**A proxy distorce, e o disclaimer diz como.** Quantidade de emissão não encolhe com
amortização, então ativo já amortizado pesa mais do que deveria. A ordem de grandeza dos
spreads se mantém; o nível exato muda quando o outstanding real entrar.

**As medianas das curvas NTN-B/DI não usam peso** e portanto não são afetadas.
