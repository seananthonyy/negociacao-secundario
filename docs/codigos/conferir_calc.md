# conferir_calc.py

**Ferramenta**, não passo de pipeline. Era o `tests_fase1.py`.

---

## Overview

Confere o **contrato da ponte com a calculadora de renda fixa** — em paralelo ao
`conferir_dados`, que confere o contrato da camada de dados.

Renomeado em 03/09/2026: o nome antigo dizia *quando* foi escrito (a FASE 1), não *o que*
confere.

---

## Regras de negócio

| # | confere |
|---|---|
| 1 | Extrapolação flat-forward da curva DI, e o mínimo de 2 vértices |
| 2 | `ResolverTipoAmort`: cadastro vence heurística, e o override muda o PU |
| 3 | `CalcularTaxaNegociacao`: round-trip PU→taxa→PU, e levanta em PU não-bracketável |
| 4 | `MERCADO`: cache preguiçoso e `Recarregar()` |
| 5 | Métricas de risco: duration em **anos**, modificada < Macaulay, DV01 positivo |
| 6 | Integração: `cdTipoAmortizacao` no schema e a propagação via `helpers/calc` |

---

## CLI

```powershell
python codigos\scripts\conferir_calc\conferir_calc.py
```

Sem argumentos. Sai `0` se tudo passa, `1` se algo falha.

---

## Interação com a base

**Nenhuma.** Aponta `[dados] raiz` para uma pasta temporária e monta ali as tabelas de
que precisa.

---

## Detalhes técnicos

Importa a `calculadora_rf` pelo `helpers/calc`, então exercita o caminho real de
resolução de caminho e de variáveis de ambiente — não uma versão de teste.

---

## Armadilhas

**Ele importa a calc de verdade.** Se a `calculadora-renda-fixa` não estiver na pasta irmã
(ou `CALCULADORA_DIR` não estiver setada), falha na importação — o que é informação útil,
não bug.

**Duration em anos, não em dias úteis.** Desde a FASE 2 a calc devolve em anos; o teste #5
existe justamente para pegar uma regressão que voltasse a dividir por 252.
