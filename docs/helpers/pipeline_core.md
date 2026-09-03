# pipeline_core.py

`codigos/helpers/pipeline_core.py` — **o orquestrador**.

---

## Overview

Uma função por fluxo. **O comando de linha de cada script vive num lugar só: aqui.** Os
notebooks e qualquer rotina chamam essas funções — nunca remontam o comando.

Cada passo roda como **subprocesso**, o que mantém cada script independente, com log e
email próprios, e faz uma falha não contaminar o processo pai.

---

## API pública

### Um fluxo por função

`Boletim` · `BondDetails` · `AnbimaDeb` · `AnbimaCriCra` · `FiAnalytics` · `AnbimaData` ·
`Ntnb` · `CurvaDi` · `IpcaIbge` · `IpcaProjetado` · `DiBcb` · `Outstanding` ·
`ValidarCalcB3` · `CalcTaxa` · `Filtrar` · `PuPar` · `SpreadAnbima` · `MatchRef` ·
`SpreadOver` · `Relatorio`

Todas aceitam `resultados=lista` para acumular `(rótulo, ok)`.

### Rotinas

| função | o que faz |
|---|---|
| `RodarDia(X)` | A cadeia completa para uma liquidação |
| `RodarUltimosN(n=5)` | **O modo padrão da diária.** Reprocessa os últimos `n` pregões |
| `RodarIntervalo(inicio, fim)` | Reprocessa um período |
| `RodarSetup(inicioBoletim, ...)` | Bootstrap da base — roda uma vez |
| `RodarCadeiaDias(dias, rotulo)` | A base das duas primeiras |

### Utilitários

`CaminhoScript(script)` · `RodarPasso(script, *args)` · `ImprimirResumo(resultados)` ·
`ArgsData(inicio, fim)` · `Iso(d)`

Reexporta de `datas.py`: `Feriados`, `EhDiaUtil`, `DiaUtilAnterior`, `UltimosNDiasUteis`,
`DiasUteisEntre` — porque os notebooks chamam `pc.DiaUtilAnterior(...)` direto.

---

## Invariantes

**1. O CLI vive só aqui.** Se o CLI de um script mudar, altera-se só a função
correspondente. Duplicar a chamada no notebook cria dois lugares para manter — e um deles
fica velho na primeira mudança de flag.

**2. Um passo que falha não derruba os outros.** `RodarPasso` registra e segue.
`pararEmErro=True` inverte, para depuração.

**3. A ordem das dependências não é negociável.** Ver `pipeline.md` — cada uma já custou
um bug.

**4. `RodarUltimosN` reprocessa os últimos N, não só o último.** A B3 revisa o boletim
depois do pregão; a re-raspagem pega essas revisões.

---

## Quem consome

`rotinas/run-pipeline-diario.ipynb` e `rotinas/teste-debug.ipynb`.

---

## Armadilhas

**Não existe atalho de intervalo no boletim.** Já houve um POST único para
`--start`/`--end` na API da B3, e ela devolvia apenas as **duas pontas** do intervalo — sem
os pregões do meio, e com status 200. O fallback nunca disparava e os dias sumiam em
silêncio. Cada pregão é baixado individualmente.

**Pregão sem nenhum negócio quase sempre é falha, não ausência.** A B3 sempre tem negócio
em dia útil.

**Ler o resumo do fim é obrigatório.** Como um passo que falha não interrompe, a única
forma de saber é o `N/M passos OK`.

**`RAIZ` aqui é `parent.parent.parent`** — este arquivo mora em `codigos/helpers/`. Errar
o nível faz o `CaminhoScript` não achar script nenhum (falha alta, ao menos).
