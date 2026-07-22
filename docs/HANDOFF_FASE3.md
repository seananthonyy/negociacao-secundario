# Handoff — Auditoria Quantitativa (Fases 1–3) e ponto de partida da FASE 3 / Tarefa 2

> Documento de encerramento do dia. Ponto de partida da próxima sessão está na seção 3.
> Motor de cálculo: repo **`calculadora-renda-fixa`** (`calculadora_rf.py`). Orquestração/dados: **`negociacao-secundario`**.

---

## 1. Status atual do projeto (o que foi concluído)

Auditoria de rigor quantitativo do motor de renda fixa e do pipeline, dividida em fases.

### FASE 1 — correções críticas (✅ commitada e pushada nos 2 repos)
- **#1 Curva DI — extrapolação flat-forward.** Além do último vértice a curva **mantém o forward do último trecho** (`fwd = (F_ult/F_penult)^(1/Δdu)`) em vez de congelar o fator acumulado (que era forward zero, subprojetando o DI em papéis longos). Exige ≥2 vértices. — `_MontarAccProjDi`.
- **#2 Convenção de amortização cadastral.** Nova coluna `InfoAtivos.cdTipoAmortizacao` (`saldo_original`/`saldo_restante`). `ResolverTipoAmort` prioriza o cadastro; a heurística "soma ≈ 100%" (ambígua) vira só *fallback*. Threadada por 12 funções do motor + `lib/calc.py`.
- **#3 Newton robusto (PU→taxa).** Tolerância em PU, *clamp* anti-fuga + confinamento ao domínio `[-20, 400]%` (base de desconto > 0, evita número complexo) e **fallback de bisseção monotônica**. PU não-bracketável **levanta** em vez de gravar taxa espúria.
- **#4 Cache de mercado lazy.** `_MercadoCache`/`MERCADO` (feriados, IPCA, projeção, DI) carregado sob demanda e recarregável (`MERCADO.Recarregar()`), sem leitura de banco no *import*; compat de `FERIADOS_ANBIMA`/`IPCA`/`IPCA_PROJETADO` via `__getattr__`. Helper `lib.calc.RecarregarMercado()`.

### FASE 2 — validação, observabilidade e risco (✅ commitada e pushada)
- **Métricas de acurácia** no `validar_calc_b3.py`: seção "Acurácia PU calc vs B3 por indexador" (distribuição do erro em faixas + % global ≤ 1e-5).
- **#8 Visibilidade:** falha do calc local em `calc_taxa_negocios.py` passou de `DEBUG` → `WARNING` (1×/ticker) + contador `calcFalhas` no relatório/email diário.
- **#6 Métricas de risco:** `CalcularDurationModificada` e `CalcularDv01` (bump-and-reprice +1bp) no motor; **Duration padronizada em ANOS base 252** (removido o `÷252` dos consumidores).
- **Bug de regressão da FASE 1 capturado e corrigido:** o `#4` removeu `_ACCPROJ_CACHE`, que o gate usava no carry-forward do D+1 → adicionado `MERCADO.SemearAccProj()`.

### FASE 3 / Tarefa 1 — diagnóstico termo-a-termo IPCA (✅ concluída — ver seção 2)
- Provado que o "fora do par" do IPCA **está resolvido** (precisão 1e-8 vs B3). Item 🔴 PRIORIDADE do backlog **pode ser fechado**.
- Entregável: `scripts/comparar_calcpu_b3.py` (`--ticker`/`--batch`/`--date`/`--delta`).

### FASE 3 / Tarefa 2 — unificação do walk de fluxo (📋 desenhada — ver seção 3)

**Testes/harness:** `code/tests_fase1.py` (unit das correções, todos verdes) + gabaritos VNA/PUPAR do `calculadora_rf.py` (14/14 OK) + `comparar_calcpu_b3.py` (régua 1e-8 vs B3).

---

## 2. Evidência do IPCA — precisão 1e-8 contra a B3

Comparação do **PU da calc local vs `/calcPU` da B3** em 2026-07-17, no par e a +100 bps. São exatamente os papéis que o backlog listava errando ~1,3% fora do par.

| ticker | PU par (B3) | rel. par | PU +100bps (B3) | rel. +100bps |
|--------|-------------|----------|-----------------|--------------|
| TRGP13 | 1061,9752   | 1,0e-08  | 987,4162        | 1,0e-08      |
| SABP13 | 1257,4243   | 8,5e-08  | 1140,4463       | 8,8e-08      |
| PLAC23 | 1301,5045   | 9,3e-08  | 1213,1039       | 9,1e-08      |
| RALM11 | 1239,2728   | 6,5e-08  | 1145,8456       | 6,6e-08      |
| BARU11 | 1272,1743   | 9,3e-08  | 1187,6653       | 9,4e-08      |
| MNAU18 | 1081,0643   | 1,9e-08  | 1000,3674       | 2,3e-08      |

**Termo-a-termo (TRGP13):** o `presentValue` de cada evento bate a B3 à precisão de display. Cupons (`J`) idênticos; nas datas de amortização a B3 lança 2 linhas (cupom + principal) e a nossa calc combina num evento — somando as linhas da B3, casa 1:1 (`41,2901 == 41,2901`). Mesma economia, granularidade diferente.

**Causa-raiz (confirmada):** o erro fora-do-par vinha da **incorporação de juros ao principal não aplicada em eventos futuros**, já corrigida no `CalcularPuOperacao` (bloco `if pctIncorp>0`) antes deste audit. A hipótese de "re-indexação do VNA por evento" **não se confirmou e não é necessária**. Resíduo real: apenas **%CDI** (multiplicador dentro do `FatorDi`, inerente, ~2–4 bps), que segue fora da calc local por decisão.

---

## 3. Ponto de partida de amanhã — FASE 3 / Tarefa 2 (`GerarFluxosFuturos`, #9)

**Problema:** há **7 implementações** do mesmo walk (VNA → incorporação → amortização → desconto): `CalcularVna`, `_CaminharVnaDi`, `CalcularPuOperacao`, `_FluxosDescontaveis`, `CalcularDuration`, `_EventosOperacaoDi`, `_FluxosDescontaveisDi`. O bug de incorporação da FASE 1/backlog nasceu justamente de existir num caminho e faltar noutro.

**Objetivo:** um **gerador único** de fluxos futuros + **Strategy por indexador**, com a máquina de estado (incorporação/amortização/taxa) num **único ponto**.

```python
class EstrategiaIndexador(Protocol):
    def VnaNaData(self, dataCalc, inicio, vne, fluxo, taxaEmi, tipoAmort, ctx) -> tuple[vna, face, ancora]
    def FatorPeriodo(self, ancora, dataEv, taxa, dataCalc, ctx) -> float   # crescimento do periodo
    def FatorDesconto(self, dataCalc, dataEv, taxaNeg, du, ctx) -> float    # desconto do VP

def GerarFluxosFuturos(dataCalc, inicio, taxaEmi, fluxo, vne, estrat, tipoAmort, ctx):
    """ÚNICO ponto com a maquina de estado. yield (dataEv, du, FV) por evento futuro."""
    vna, face, ancora = estrat.VnaNaData(...)          # aplica eventos <= dataCalc
    for dataEv, pctAmort, pctIncorp in futuros:
        fj = estrat.FatorPeriodo(ancora, dataEv, taxaEmi, dataCalc, ctx)
        if pctIncorp > 0:                              # capitaliza no VNA, sem caixa
            f = 1 + pctIncorp/100*(fj-1); vna*=f; face*=f; ancora=dataEv; continue
        juros = vna*(fj-1)
        amort = (face if tipoAmort=='saldo_original' else vna) * pctAmort/100
        yield dataEv, ContarDu(dataCalc, dataEv), Trunca(juros+amort, 6)
        if pctAmort > 0: vna = vna-amort if saldo_original else vna*(1-pctAmort/100)
        ancora = dataEv
```

Com isso: **PU op = Σ desconto(FV)** · **Duration = Σ(du·VP)/ΣVP / 252** · **Newton/DV01** reusam o mesmo gerador.

**Estratégias:** `IPCA` (VNA indexado por NIk; fatores `round((1+j)^(du/252),9)`) · `PREFIXADO` (idem, VNA sem indexação) · `%CDI`/`CDI+` (VNA nominal; fatores via `FatorDi`).

### Plano faseado (baixo risco)
1. **Introduzir** `GerarFluxosFuturos` + as estratégias **sem remover nada** (código legado intacto).
2. **Reimplementar `CalcularPuOperacao`** por cima do gerador e validar contra os gabaritos **+ `comparar_calcpu_b3.py` nos 6 tickers** (régua 1e-8).
3. **Portar** `CalcularDuration` / `CalcularTaxaNegociacao` / `CalcularDv01` para o gerador.
4. **Remover** as duplicatas mortas (`_FluxosDescontaveis`, `_EventosOperacaoDi`, `_CaminharVnaDi`, `_FluxosDescontaveisDi`).

### ➡️ Primeiro passo prático da próxima sessão
**Executar o passo 1**: criar `GerarFluxosFuturos` + `EstrategiaIndexador` (IPCA/PREFIXADO/%CDI/CDI+) no `calculadora_rf.py`, **sem tocar nas funções existentes**, e escrever um teste que compare `Σ desconto(GerarFluxosFuturos)` com o `CalcularPuOperacao` atual em IPCA, PREFIXADO e CDI+ (deve bater bit-a-bit). Só depois de verde, seguir para o passo 2.

**Regra permanente:** não alterar `calculadora_rf.py` sem OK explícito; toda mudança passa pelos gabaritos (zero regressão no par) + `comparar_calcpu_b3.py`.
