# Script: validar_calc_b3.py

> Ver também: [[../16 - Confianca nos Validados (WIP)]] | [[../10 - Scripts/calc_taxa_negocios]] | [[../10 - Scripts/validar_fluxos]] | [[../15 - Cadastro dos Ativos]]

**Arquivo:** `code/scripts/validar_calc_b3.py` · **Pipeline:** passo 13 (entre `validar_fluxos` e `calc_taxa_negocios`)

---

## O que faz

**Gate de confiança da precificação local.** Responde: *"a NOSSA calc reproduz uma calculadora de mercado?"* Se sim, `stFluxoValidado=1` e a calc pode precificar o ativo no `calc_taxa`. É **bidirecional**: promove quem passa, rebaixa quem falha. Foi o que fechou o buraco do `validar_fluxos` (que marcava o fluxo da B3 como "nasce validado" sem nunca conferir se a calc precifica certo).

## Como valida um ativo

Testa em **3 datas** (`DatasPadrao`): 8 pregões atrás, mais recente com curva, e **D+1**. O **D+1 é data futura, sem curva própria** — precificada por **carry-forward** (`SemearCurvaCarryForward` semeia o cache de projeção da calc com a curva mais recente, em memória, sem escrever no `di.db` nem tocar na `calculadora_rf.py`). Verificado: a B3 forward-precifica e a calc bate o D+1 a 1e-8.

**Dois oráculos, B3 primária:**
- **B3** (`CalcularPuGov`): compara o PU da calc contra o da B3 em **dois pontos** — no par (valida fluxo+VNA) e a **+100 bps** (valida o desconto). Régua: **erro relativo de PU ≤ 1e-5** (= 0,001% = R$0,01 num PU de R$1.000). Como é PU, **%CDI usa a mesma métrica**.
- **FI Analytics** (`ChamarPrimaria`): só se a B3 não bater. Round-trip de taxa (o PU que a calc gera na taxa T tem que voltar T pela FI, ≤ 5 bps). Resgata ativos que a B3 não cobre.

**Oráculo mudo ≠ falha:** HTTP 500/timeout/não-cobre = não-confirma (pula). Só é falha quando o oráculo **responde e diverge**.

**Refresh-on-fail:** quem falha ganha um cadastro fresco da B3 (`GravarAtivo`) e é re-testado antes de rebaixar — cura a deriva de fluxo.

## Vereditos (CSV `data/validar_calc_b3.csv`)

| veredito | efeito |
|---|---|
| `confiavel` | algum oráculo bateu → `stFluxoValidado=1` |
| `REPROVADO` | oráculo respondeu e a calc divergiu → `stFluxoValidado=0` |
| `nao_confirmavel` | nem B3 nem FI responderam → `stFluxoValidado=0` (inválido — decisão 19/07) |

## Revalidação (`--revalidar-dias`, default 15)

Ativo já validado é **pulado** enquanto a `dtValidacaoFluxo` tiver < 15 dias — só revalida quando vence. Fluxo que muda de verdade zera `stFluxoValidado` pelo trigger e é re-testado na hora. `--revalidar-dias 0` re-testa tudo (usar após mudar a régua). `--tickers` ignora a janela.

## Colunas (InfoAtivos)

- **Lê** (via `lib.calc.CarregarAtivo`): `cdIndexador`, `vrTaxaEmissao`, `vrVNE`, `dtInicioRentabilidade`, `vrAniversario`, `stFluxoValidado`, `cdInstrumento`; `FluxoAtivos` (`dtEvento`, `vrPctAmortizacao`, `vrPctIncorporacao`); `di.db/CurvaDi` (escolha das datas).
- **Escreve** (fora de `--dry-run`): `stFluxoValidado`, `dtValidacaoFluxo`, `cdFonteValidacaoFluxo` (`'B3'`/`'FiAnalytics'`); no refresh-on-fail reescreve o pacote (`vrVNE`, `dtInicioRentabilidade`, `FluxoAtivos`) via `GravarAtivo`.

> ⚠️ **Fora de `--dry-run`, MUTA a base** (valida/desvalida + refresh reescreve cadastro). Rodar "pra ver" → sempre `--dry-run` (só gera o CSV).

## CLI

```powershell
python scripts/validar_calc_b3.py                    # audita e (des)valida; revalida vencidos
python scripts/validar_calc_b3.py --dry-run          # só reporta, não grava
python scripts/validar_calc_b3.py --negociados-dias 120   # rotina (só o que negociou)
python scripts/validar_calc_b3.py --revalidar-dias 0      # re-testa tudo
python scripts/validar_calc_b3.py --tickers A,B      # testa esses (ignora janela de revalidação)
```

## Estado (19/07/2026)

Base validada: **2.806** (IPCA 1.234 / CDI+ 1.222 / PREFIXADO 201 / %CDI 149); fonte B3 1.705 / FI 1.101. Achado: a régua de 1e-5 empurrou ~889 CDI+ da confirmação-B3 pra confirmação-FI (o erro fora-do-par é ~0,25 bps de yield, ruído da curva DI). Ver [[../16 - Confianca nos Validados (WIP)]].
