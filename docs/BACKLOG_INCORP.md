# Backlog — inconsistência latente de incorporação (passado × futuro)

> Aberto em 23/07/2026, durante a FASE 3 / Tarefa 2 (unificação do walk de fluxo).
> Status: **latente** — não afeta nenhum resultado hoje, mas vai afetar. Tratar no Passo 4.

## O problema

A calc tem **duas semânticas diferentes** para um evento de incorporação de juros
(`pctIncorp > 0`), e elas divergem exatamente quando o evento também amortiza ou quando
a incorporação é **parcial**.

**Semântica A — o walk IPCA/PREFIXADO** (hoje no `GerarFluxosFuturos`, para eventos
FUTUROS de qualquer indexador):

```python
fatorIncorp = 1 + pctIncorp / 100 * (fatorJuros - 1)
juros      *= (1 - pctIncorp / 100)   # a parcela NÃO incorporada é paga em dinheiro
vna        *= fatorIncorp
face       *= fatorIncorp
# ... e a amortização do mesmo evento incide sobre a base já crescida
```

**Semântica B — o walk DI** (`_CaminharVnaDi`, para eventos PASSADOS):

```python
if pctIncorp > 0:
    ...                      # capitaliza
elif pctAmort > 0:           # <-- elif: se incorporou, NÃO amortiza
    ...
# e nada de juros é pago, mesmo com pctIncorp < 100
```

Na semântica B, um evento que incorpora **e** amortiza tem a amortização
**silenciosamente descartada**, e uma incorporação parcial não paga a fração não
incorporada. A semântica A é a correta.

## Por que não quebra hoje

Levantamento na base (`FluxoAtivos` × `InfoAtivos`, 23/07/2026):

| indexador | eventos c/ `pctIncorp > 0` | parciais (`0 < pct < 100`) | incorp **e** amort na mesma data |
|-----------|---------------------------:|---------------------------:|---------------------------------:|
| %CDI      | 30                         | 0                          | 0                                |
| CDI+      | 369                        | 23                         | 39                               |
| IPCA      | 290                        | 14                         | 20                               |
| PREFIXADO | 40                         | 1                          | 0                                |

Só que **todos os eventos divergentes já são passados**. Eventos passados são aplicados
pelo `VnaNaData` — que, no `EstrategiaCdi`, delega ao `_CaminharVnaDi` (semântica B) e,
no `EstrategiaTaxaFixa`, ao `CalcularVna`. Eventos futuros, esses sim, passam pela
semântica A do gerador. Consulta de controle:

```sql
SELECT i.cdIndexador, COUNT(DISTINCT f.cdTicker)
FROM FluxoAtivos f JOIN InfoAtivos i ON i.cdTicker = f.cdTicker
WHERE f.vrPctIncorporacao > 0
  AND (f.vrPctIncorporacao < 100 OR f.vrPctAmortizacao > 0)
  AND f.dtEvento > date('now')
GROUP BY 1;
-- 23/07/2026: só IPCA, 4 tickers (que já usavam a semântica A). Zero DI.
```

Por isso a unificação saiu **bit-a-bit** sem regressão: nenhum papel DI tem evento
divergente no futuro.

## Por que vai quebrar

É uma bomba-relógio de calendário, não de código. Quando um dos papéis CDI+ com
incorporação parcial (ou incorporação+amortização na mesma data) tiver um desses eventos
**à frente** da data de cálculo, o mesmo evento vai ser tratado por A enquanto futuro e
por B depois de virar passado. O PU vai dar um salto artificial na data do evento, e o
gate `validar_calc_b3` vai acusar o ativo como inválido sem explicar por quê — que é
precisamente o modo de falha que originou esta tarefa.

## O que fazer (Passo 4)

1. Portar o `_CaminharVnaDi` para a semântica A: trocar o `elif pctAmort > 0` por um `if`
   independente, e aplicar `juros *= (1 - pctIncorp/100)` na parcela não incorporada.
   Como o `_CaminharVnaDi` não gera caixa (só carrega o VNA), o efeito é: (a) a
   amortização deixa de ser descartada; (b) o VNA passa a refletir a fração paga.
2. Idem no `CalcularVna` para IPCA — lá o `if pctIncorp > 0: ... elif tipoAmort ...`
   tem o mesmo `elif`. Conferir com um papel IPCA-I que amortize na carência.
3. Rodar o gate completo e comparar contra a B3 os 39 + 23 papéis CDI+ afetados, **em
   datas de cálculo anteriores ao evento**, para provar qual semântica a B3 usa antes de
   mexer. Esta é a etapa que decide — nada de mudar as duas pontas por simetria estética.

## Anexo — o pseudocódigo do plano original já trazia a semântica errada

Preservado aqui do `docs/HANDOFF_FASE3.md` (removido em 23/07/2026, depois que a
FASE 3 fechou; este era o único trecho ainda citado). É o desenho que o plano da
Tarefa 2 propunha para o gerador unificado:

```python
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

Repare no `continue` da linha de incorporação: é **exatamente a semântica B**. Se
tivesse sido implementado como escrito, o gerador unificado teria propagado para
*todos* os indexadores o descarte da amortização e a não-remuneração da fração não
incorporada — inclusive para o IPCA, que já estava certo.

O plano nasceu assim porque foi desenhado olhando o walk do DI (`_EventosOperacaoDi`),
que era o mais recente. A implementação final divergiu do plano de propósito, adotando
a semântica A. **Lição para o Passo 4:** ao unificar duas implementações que discordam,
a que serviu de molde para o desenho não é necessariamente a correta — decidir qual é
exige oráculo externo, não leitura de código.

## Referências

- `calculadora_rf.py` → `GerarFluxosFuturos` (semântica A), `_CaminharVnaDi` (B), `CalcularVna` (B).
- `docs/RELATORIO_FASE3_FINAL.md` — resultado da unificação e evidência contra a B3.
- `CONTEXTO_PROJETO.md` §7.4 — resumo desta pendência para agentes de IA.
