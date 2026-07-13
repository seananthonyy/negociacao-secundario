# conferir_pu.py

> **O portão de aceitação da precificação local.** Enquanto os PUs não baterem, não se troca a fonte de taxa do `calc_taxa_negocios`. Criado em 13/07/2026.

## O que faz

Para cada ativo com `stFluxoValidado = 1`, calcula o PU de operação com a **nossa calc** e compara com o PU que a **fonte** devolve — a mesma que validou o fluxo:

- **B3** → `GET /calcPU/{ticker}/{data}/{taxa}` → campo `"PU"`
- **FI** → `m2m` da resposta completa

Saída: `data/pu_divergencias.csv`.

## Duas taxas — e a segunda é a que importa

| teste | taxa de desconto | valida |
|---|---|---|
| **no par** | = taxa de emissão | o **fluxo** e o **VNA** |
| **fora do par** | taxa de emissão + `DELTA_FORA_PAR` (100 bps) | o **DESCONTO** |

**Testar só no par não basta, e isso quase custou caro.** O PU no par é quase cego ao desconto: capitalizar um fluxo à taxa de emissão e descontá-lo à taxa de negociação dá o **mesmo VP** quando as duas são iguais. Um ativo pode ter fluxo perfeito e desconto quebrado — e é o desconto que produz a **taxa**, que é o que vai para o relatório.

**Prova:** o TRGP13 batia o PU par a **5,7e-09** e errava **1,3%** a 100 bps do par. A causa era a incorporação ignorada no `CalcularPuOperacao` (ver [[14 - Rotinas da Calculadora]], correção 4). Numa versão anterior deste script, que só testava no par, **628 ativos passariam com o desconto errado**.

## Critério — relativo, não absoluto

Um erro de 1e-3 num PU de 1.000 é um erro relativo de 1e-6. O mesmo 1e-3 num PU de 10.000 seria um sarrafo **10× mais apertado**, e num PU de 400 seria 2,5× mais frouxo. Então o critério é o **erro relativo**:

- **≤ 1e-6** (0,0001%) — bate exato. É o que uma implementação correta atinge.
- **> 1e-3** (0,1%) — investigar. É a fila de trabalho.

## Estado em 13/07/2026 (3.023 validados, referência 10/07)

| | ativos |
|---|---|
| batem **no par E fora dele** (≤ 1e-6) | **1.947 — 64,4%** |
| erram fora do par **por um fio** (1e-6 a 1e-5 ≈ 0,02 bps em taxa) | 596 — 579 CDI+, 16 %CDI, 1 PRE, **zero IPCA** |
| erram fora do par de forma **material** (> 1e-4) | **3** |
| a investigar (> 1e-3) | 108 |

Com o sarrafo em 1e-5, **2.543 (84,1%)** passam nos dois testes. Os 596 são ruído numérico da projeção DI, não bug.

## `--desvalidar`

Tira a validação (`stFluxoValidado = 0`) de quem erra acima de `--tol-desvalida`. É o que torna *"a calc só precifica o que está validado"* **seguro por construção**: quem não reproduz o PU da fonte não é precificado localmente, cai na cascata de API.

**Não está no pipeline por padrão** — só faz sentido quando a calc for ligada (ver [[98 - Backlog]]).

## CLI

```bash
python scripts/conferir_pu.py                       # último dia útil
python scripts/conferir_pu.py --date 2026-07-10
python scripts/conferir_pu.py --tickers SSRU11,TAEE17
python scripts/conferir_pu.py --limite 60           # smoke (a rodada cheia leva ~6,5 min)
python scripts/conferir_pu.py --desvalidar          # desvalida quem erra > 1e-3
```

Rodar **depois de qualquer mudança** em cadastro, fluxo ou na calc.
