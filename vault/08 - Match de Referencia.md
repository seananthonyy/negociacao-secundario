# Match de Referência

> Ver também: [[00 - Inicio]] | [[04 - Banco de Dados]] | [[01 - O Que Faz]]

Script responsável: `code/scripts/match_referencias.py` — ver também [[10 - Scripts/match_referencias]].

---

## O que faz

Associa cada ativo de crédito privado a um ativo de referência de mercado para cálculo de spread over. A referência é armazenada na coluna `cdReferencia` da tabela `InfoAtivos` e é usada depois pelo `calc_spread_over.py`. Ver [[04 - Banco de Dados]].

---

## `--force` descartado na implementação real

O plano original previa `--force` como obrigatório, mas a implementação final **não requer a flag**. O script integra o pipeline normal e roda a cada fechamento. A proteção contra sobrescrita de refs corretas é feita por `cdFonteReferencia`: o script só atua em ativos com `cdReferencia IS NULL` ou `cdFonteReferencia = 'MatchRef'` — nunca sobrescreve `cdFonteReferencia = 'Anbima'`.

---

## Regras de match

| Indexador do ativo | Prefixo da referência buscada | Onde buscar |
|---|---|---|
| `IPCA` | `NTN-B` | `MtmAnbima` |
| `PREFIXADO` | `DI1` | `MtmAnbima` |
| Outros (`CDI+`, `IGPM`, etc.) | — | Pula (sem match automático) |

**Importante:** a implementação real usa `MtmAnbima` (não `MtmBloomberg` como estava no plano). `MtmAnbima` já é populada pelos scripts F13 e F14, sem dependência de Bloomberg.

Para indexadores além de IPCA e PREFIXADO, a taxa já representa um spread direto — não há referência de mercado a subtrair. O `calc_spread_over.py` trata esses casos como `vrSpreadOver = vrTaxaCalculada * 100` (conversão para bps).

---

## Algoritmo de match por duration

```python
# Ativos elegíveis: sem cdReferencia ou com cdFonteReferencia = 'MatchRef' (nunca 'Anbima')
ativos = SELECT cdTicker, cdIndexador, vrDuration, dtAtualizacaoDuration FROM InfoAtivos
         WHERE (cdReferencia IS NULL OR cdFonteReferencia = 'MatchRef')
           AND cdIndexador IN ('IPCA', 'PREFIXADO')
           AND vrDuration IS NOT NULL
           AND dtAtualizacaoDuration IS NOT NULL

for ativo in ativos:
    prefix = 'NTN-B%' if ativo.cdIndexador == 'IPCA' else 'DI1%'

    # Candidatos em MtmAnbima — match exato de data
    candidatos = SELECT cdTicker, vrDuration FROM MtmAnbima
                 WHERE cdTicker LIKE prefix
                   AND dtReferencia = ativo.dtAtualizacaoDuration
                   AND vrDuration IS NOT NULL

    if not candidatos:
        # loga SEM_REF — ativo fica sem ref nesta rodada
        continue

    melhor = min(candidatos, key=lambda c: abs(c.vrDuration - ativo.vrDuration))
    UPDATE InfoAtivos
    SET cdReferencia = melhor.cdTicker, cdFonteReferencia = 'MatchRef'
    WHERE cdTicker = ativo.cdTicker
```

O match usa `dtAtualizacaoDuration` do ativo como data de referência — a data em que a duration do ativo foi inserida no banco. Match exato (sem fallback para D-1).

---

## Como refazer um ticker específico

Se o match automático errou para um ativo ou se a duration mudou significativamente:

1. Zere o `cdReferencia` do ativo específico:
   ```sql
   UPDATE InfoAtivos SET cdReferencia = NULL WHERE cdTicker = 'DEBA11';
   ```
2. Rode o match novamente:
   ```powershell
   python scripts/match_referencias.py --force
   ```

Para sobrescrever manualmente (sem usar o match automático):
```sql
UPDATE InfoAtivos SET cdReferencia = 'NTN-B 32' WHERE cdTicker = 'DEBA11';
```

---

## Integração com `calc_spread_over.py`

Após o match, o [[10 - Scripts/calc_spread_over|calc_spread_over.py]] usa `cdReferencia` para buscar a taxa da referência em `MtmAnbima`:

```sql
SELECT vrTaxa FROM MtmAnbima
WHERE cdTicker    = <cdReferencia do ativo>
  AND dtReferencia = <dtLiquidacao do trade>
```

Match exato de data — se não houver taxa de referência para `dtLiquidacao`, o `vrSpreadOver` fica NULL.

---

## CLI

```powershell
# Sem flags — roda no pipeline normal após calc_spread_anbima
python scripts/match_referencias.py
```
