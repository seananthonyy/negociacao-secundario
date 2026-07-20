# match_referencias.py

> Ver também: [[08 - Match de Referencia]] | [[04 - Banco de Dados]] | [[10 - Scripts/calc_spread_over]]

Script: `code/scripts/match_referencias.py`

---

## O que faz

Atribui `cdReferencia` em `InfoAtivos` para ativos **IPCA** e **PREFIXADO** que ainda não têm referência ou cuja referência foi definida por uma rodada anterior deste mesmo script (`cdFonteReferencia = 'MatchRef'`).

O match é feito por proximidade de duration contra os candidatos disponíveis em `MtmAnbima` — a mesma tabela que já é populada pelos scripts [[10 - Scripts/scrape_anbima_ntnb|scrape_anbima_ntnb.py]] e [[10 - Scripts/scrape_b3_curva_di|scrape_b3_curva_di.py]]. Não depende do Bloomberg.

---

## Pré-passo: calcula a duration faltante (19/07/2026)

Antes do match, `PreencherDurationFaltante`/`CalcularDurationAtivo` calculam a `vrDuration` dos IPCA/PREFIXADO que **negociaram mas estão sem duration** — senão ficariam sem `cdReferencia` → sem spread (a duration só vinha do XLS da Anbima; ativo fora do indicativo ficava eternamente sem). Cascata igual à do `calc_taxa`:

- **validado** (`stFluxoValidado=1`) → **calc local** (`lib.calc.CalcularDuration`, DU → /252 = anos)
- **não-validado** → **FI** (`lib.fianalytics_api.ObterDuration`, campo `maculayDuration` já em anos, modo `rate` com a taxa em percent) e, se a FI não cobrir, **B3** (`CalcularPuGov` devolve `(pu, duration)`)

Desconto na `vrTaxaEmissao`, as-of a data da curva de benchmark mais recente (grava `dtAtualizacaoDuration=dtRef` p/ o match data-exato casar). **Resultado 1ª rodada:** IPCA/PREF negociados sem duration **520 → 41** (389 calc + 46 B3 + 44 FI; 41 = nenhuma fonte cobre); cdReferencia sem ref 1147 → 679.

---

## Regras de match

| Indexador | Prefixo buscado em `MtmAnbima` |
|---|---|
| `IPCA` | `NTN-B%` |
| `PREFIXADO` | `DI1%` |
| Outros | Pula (sem match automático) |

**Fonte dos candidatos:** `MtmAnbima` com `dtReferencia = dtAtualizacaoDuration` do ativo — match exato de data. Se não houver nenhum candidato para aquela data, o ativo fica sem ref e é logado como `SEM_REF`.

**Critério de seleção:** vértice com menor `|vrDuration - vrDuration do ativo|`.

---

## O que não sobrescreve

`match_referencias.py` **nunca** altera ativos com `cdFonteReferencia = 'Anbima'`. Refs definidas pelos scrapers de debentures ou CRI/CRA são consideradas definitivas.

Só atua em ativos com:
- `cdReferencia IS NULL`, ou
- `cdFonteReferencia = 'MatchRef'` (pode rematchar se a duration mudou)

---

## CLI

```powershell
# Sem flags obrigatórias — faz parte do pipeline normal
python scripts/match_referencias.py
```

O script não aceita `--date` / `--start --end` porque opera sobre `InfoAtivos` (informações de ativo, não de data). Processa todos os ativos elegíveis de uma só vez.

**Decisao (07/06/2026):** o `--force` documentado no plano original (PLANEJAMENTO_v5.md §8) foi descartado na implementação real. O script roda sem flags e integra o pipeline normal após F14 e antes de `calc_spread_over`.

---

## Saída / email

```
Ativos processados : 57
Matches atribuidos : 57
Sem ref na data    : 0

Detalhes:
  MATCH    DEBA11               (IPCA)      dur=3.2104 → NTN-B 28
  MATCH    CRAC11               (IPCA)      dur=7.8540 → NTN-B 35
  ...
```

---

## Resultado do campo `cdFonteReferencia`

Após a execução, ativos matchados têm:
- `cdReferencia = 'NTN-B 28'` (ou outro vértice)
- `cdFonteReferencia = 'MatchRef'`

Ver [[04 - Banco de Dados]] para descrição completa da coluna `cdFonteReferencia`.

---

## Integração no pipeline

Posição no pipeline:

```
... (pipeline de 19 passos — ver [[11 - Pipeline de Execucao]])
16. calc_spread_anbima
17. match_referencias     ← aqui (com o pré-passo de duration)
18. calc_spread_over      ← usa cdReferencia gerado aqui
19. gerar_relatorio_credito
```

---

**Decisao (07/06/2026):** implementação usa `MtmAnbima` como fonte de candidatos (não `MtmBloomberg` como estava no plano). Isso elimina a dependência do Bloomberg e permite rodar o script em qualquer PC. Match de data exato em `dtAtualizacaoDuration` — sem fallback para datas vizinhas. Teste: 57/57 ativos IPCA/PREFIXADO matchados, 0 sem ref.
