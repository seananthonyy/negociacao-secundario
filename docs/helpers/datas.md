# datas.py

`codigos/helpers/datas.py` — **dias úteis pelo calendário da Anbima**.

---

## Overview

Fonte única do que era copiado em quatro lugares.

Até 01/09/2026 a leitura do `feriados_anbima.csv` estava escrita quatro vezes:
`pipeline_core`, `gerar_relatorio_credito`, `scrape_b3_curva_di` e um bloco solto dentro
de uma função do relatório diário. Todas liam o mesmo arquivo e faziam a mesma coisa —
menos numa diferença que importa: **três devolviam conjunto vazio quando o arquivo não
existia, e só uma levantava.**

Aqui vale a versão que **levanta**.

O calendário não muda durante uma rodada, então o arquivo é lido **uma vez por processo**.

---

## API pública

| função | devolve |
|---|---|
| `Feriados()` | `frozenset[date]` — lê o CSV na primeira chamada |
| `Recarregar()` | Relê o arquivo e substitui o cache |
| `EmData(d)` | Aceita `date` ou ISO `'AAAA-MM-DD'`, devolve sempre `date` |
| `EhDiaUtil(d, feriados=None)` | Se é dia útil |
| `DiaUtilAnteriorOuIgual(d, feriados=None)` | O próprio `d` se for útil; senão o anterior |
| `DiaUtilAnterior(d, feriados=None)` | O dia útil **anterior** a `d` — nunca o próprio |
| `UltimosNDiasUteis(n, ref=None)` | Os `n` mais recentes até `ref`, em ordem cronológica |
| `DiasUteisEntre(inicio, fim)` | Dias úteis no intervalo, inclusive nas duas pontas |

Todas aceitam `date` ou string ISO.

---

## Invariantes

**1. Feriado ausente LEVANTA.** Conjunto vazio não dá erro: faz todo sábado, domingo e
feriado virarem dia útil. O efeito é um relatório com pregão que não existiu e uma janela
de datas maior que a real — errado em silêncio, que é o modo de falha que este projeto já
pagou caro.

**2. Linha suja no CSV não derruba o calendário.** Uma data ilegível é ignorada; só o
arquivo inteiro ausente é fatal.

**3. O cache é por processo.** Quem precisa reler depois de o CSV mudar chama
`Recarregar()`.

---

## Quem consome

`pipeline_core` (reexporta os nomes, porque os notebooks chamam `pc.DiaUtilAnterior(...)`)
· `gerar_relatorio_credito` (corte, prévia, idade da referência de PU par) ·
`scrape_b3_curva_di` (vencimento dos contratos DI1).

---

## Armadilhas

**A calculadora tem o próprio `FERIADOS_ANBIMA`**, carregado do **mesmo arquivo** — ela lê
por `CALCRF_FILES_DIR`, pelo nome. São dois caches do mesmo conteúdo, não duas verdades. O
dela é contrato da calc e não se mexe.

**`DiaUtilAnterior` nunca devolve o próprio dia.** Se você quer "hoje, se for útil", é
`DiaUtilAnteriorOuIgual`. Trocar os dois desloca todo o corte do relatório em um pregão.

**O CSV mora em `database/arquivos/`, não em `config/`.** Não é configuração do usuário: é
dado, e é lido também pela calculadora.
