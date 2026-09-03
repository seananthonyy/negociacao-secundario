# calc.py

`codigos/helpers/calc.py` — **a ponte para a calculadora de renda fixa**.

---

## Overview

`../calculadora-renda-fixa` é a biblioteca que precifica: VNA, PU par, PU de operação,
duration, DV01. Este módulo é o **único** que sabe onde ela está instalada e o único que
traduz o cadastro da nossa base para o formato que ela consome.

**A divisão de responsabilidades:** este projeto **coleta** os insumos (IPCA, projeção de
IPCA, DI realizado, curva DI) e mantém o cadastro dos ativos; a calc só **lê**. Ela não
sabe da existência de `NegociosBrutos`, de spread ou de relatório.

---

## API pública

### Localização e import

| função | devolve |
|---|---|
| `DirCalculadora()` | Onde a calc está instalada. Variável de ambiente (banco) > config |
| `DirArquivos()` | Pasta do `feriados_anbima.csv` → `CALCRF_FILES_DIR` |
| `DirParquet()` | Pasta das tabelas de insumo → `CALCRF_PARQUET_DIR` |
| `ImportarCalc()` | Importa e devolve o módulo `calculadora_rf`, já apontado |
| `RecarregarMercado()` | Descarta os caches de mercado da calc |

### Cadastro

| função | devolve |
|---|---|
| `IndiceAtivos()` | `{cdTicker: {cadastro, fluxo}}`, reconstruído quando a base muda |
| `CarregarAtivo(cdTicker)` | O ativo no formato que a calc consome, ou `None` |
| `ArgumentosCalc(ativo)` | Os argumentos posicionais comuns às funções de preço |

`CarregarAtivo` devolve `None` quando **não dá para precificar**: falta taxa de emissão,
início de rentabilidade, fluxo ou indexador suportado. É a primeira peneira do gate.

### Cálculo

| função | devolve |
|---|---|
| `CalcularPu(ativo, dtCalc, vrTaxa)` | PU de operação, descontado por `vrTaxa` (% a.a. base 252) |
| `CalcularTaxa(ativo, dtCalc, vrPU)` | Taxa implícita num PU. Newton-Raphson |
| `CalcularVnaAtivo(ativo, dtCalc)` | VNA — saldo devedor atualizado |
| `CalcularDuration(ativo, dtCalc, vrTaxa)` | Duration de Macaulay, em **anos** base 252 |
| `CalcularDurationModificada(...)` | Macaulay ÷ (1 + y) |
| `CalcularDv01(ativo, dtCalc, vrTaxa)` | R$ por face a cada +1 bp, por *bump-and-reprice* |

**O PU par é `CalcularPu(ativo, data, ativo["vrTaxaEmissao"])`** — precificar na própria
taxa de emissão. É a definição de "no par", e é o que o `calc_pu_par` e o gate usam.

---

## Invariantes

**1. As quatro tabelas de insumo são contrato.** `IPCA`, `IPCAProjetado`, `DiHistorico` e
`CurvaDi` têm nomes de coluna que a `calculadora_rf` lê literalmente. Por isso não seguem
o prefixo `vr`/`cd`/`dt` do projeto. **Renomear qualquer uma quebra a precificação
inteira, em silêncio.**

**2. Duas variáveis de ambiente, duas pastas.** `CALCRF_FILES_DIR` aponta para o
`feriados_anbima.csv`; `CALCRF_PARQUET_DIR` para as tabelas. São pastas diferentes desde
que o `ipca.db` e o `di.db` viraram Parquet (03/09/2026). Quem as seta é o `ImportarCalc`.

**3. O índice de ativos tem cache por geração.** `IndiceAtivos()` compara
`dados.Geracao("InfoAtivos")` e `Geracao("FluxoAtivos")` com o que guardou, e só reconstrói
quando alguém gravou. É o único jeito de um cache aqui ser seguro: no SQLite cada leitura
enxergava a escrita anterior de graça, e um cache mudo devolveria dado velho.

**4. Aniversário é conceito de IPCA.** Nos demais indexadores a calc ignora o parâmetro;
`CarregarAtivo` passa o default para evitar um `if` em cada chamador.

**5. Não modificar `calculadora_rf.py` sem permissão explícita do usuário.** É projeto
vizinho, com outros consumidores.

---

## Quem consome

`calc_taxa_negocios` (degrau 1 da cascata) · `calc_pu_par` (degrau 1) · `validar_calc_b3`
(é ela que o gate confere) · `match_referencias` (duration) · os quatro scripts de insumo
(via `ImportarCalc`, para o calendário de dias úteis).

---

## Armadilhas

**A calc RECUSA calcular sem a projeção da data.** Levanta `ValueError`:

```
Curva DI x pre indisponivel para {data} (CALCRF_PARQUET_DIR)
projecao nao encontrada para {dataCalc}
```

Isso é o comportamento **certo** e não deve ser "consertado": foi ele que deixou 825
ativos sem `vrPuPar` no backfill, em vez de dar-lhes um denominador inventado. Quem chama
tem de tratar a exceção e registrar o ativo como não-calculável.

**A curva DI só é necessária para papel indexado a CDI.** `CDI+` e `%CDI` precisam da
`CurvaDi` para projetar o CDI futuro; `IPCA` precisa da projeção da Anbima, que é mensal;
`PREFIXADO` não precisa de nenhuma das duas. Um erro de "curva indisponível" num papel
prefixado indica outro problema.

**D+1 não tem curva própria.** A B3 só publica pregão fechado. Quem precisa precificar em
D+1 semeia o cache da calc com a curva de hoje sob a chave de amanhã
(`C.MERCADO.SemearAccProj`) — em memória, sem escrever na base. É o que o gate faz.

**Falha de `CalcularTaxa` não é ausência de medida.** O Newton pode não convergir num PU
não-bracketável. Quem chama deve distinguir "não deu para medir" de "mediu e bateu" — no
gate, uma falha da nossa calc onde o oráculo respondeu conta como **falha**, não como
acerto perfeito.

**A duration sai em ANOS, não em dias úteis.** Desde a FASE 2 a calc já devolve em anos;
não dividir por 252 de novo.
