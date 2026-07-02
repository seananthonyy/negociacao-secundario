# filtrar_trades.py

Arquivo: `code/scripts/filtrar_trades.py`
Dependências de lib: [[10 - Scripts/libs#lib/db.py|db]], [[10 - Scripts/libs#lib/config.py|config]], [[10 - Scripts/libs#lib/logger.py|logger]], [[10 - Scripts/libs#lib/email_outlook.py|email_outlook]]

Ver também: [[07 - Filtro de Duplicados]] (regras conceituais dos três filtros)

---

## O que faz

Para cada `dtLiquidacao` na janela informada, aplica três filtros sequenciais em `NegociosProcessados`:

1. **FUNDO** — identifica passagens de fundo via critério de PU; marca ambas as pontas como `FUNDO`
2. **CORRETOR** — identifica pares de corretagem via diferença de taxa; menor volume → `VALIDO`, maior → `BROKER`
3. **PF** — identifica operações de pessoa física via diferença de taxa e taxa de referência (Anbima ou mediana ponderada por volume); mais próximo da referência → `VALIDO`, outro → `PF`

Faz UPDATE em `NegociosProcessados` gravando `cdStatus` e `idGrupoNegocio` para todos os trades da data.

---

## De onde lê / onde escreve

**Lê de:** `NegociosProcessados` JOIN `NegociosBrutos` + `AnbimaIndicativos` para taxa de referência do filtro PF

```sql
SELECT tp.idTrade, tp.cdTicker, tp.dtLiquidacao,
       tp.vrQuantidade, tp.vrVolume, tp.vrTaxaCalculada,
       ia.cdIndexador
FROM NegociosProcessados tp
JOIN NegociosBrutos tr ON tr.idTrade = tp.idTrade
LEFT JOIN InfoAtivos ia ON ia.cdTicker = tp.cdTicker
WHERE tp.dtLiquidacao = ?
  AND tr.cdSituacao != 'Cancelado'
```

**Por que JOIN NegociosBrutos?** `cdSituacao` (para filtrar cancelados) está apenas em `NegociosBrutos`.

**Por que LEFT JOIN InfoAtivos?** `cdIndexador` é necessário para detectar ativos %CDI e aplicar os thresholds corretos.

**Taxa Anbima:**
```sql
SELECT cdTicker, vrTaxaAnbima
FROM AnbimaIndicativos
WHERE dtReferencia = ?
  AND vrTaxaAnbima IS NOT NULL
```

**Escreve em:** `NegociosProcessados` — UPDATE de `cdStatus` e `idGrupoNegocio` para todos os trades da data.

---

## Status possíveis

| `cdStatus` | Significado |
|---|---|
| `VALIDO` | Trade legítimo — aparece no relatório HTML |
| `FUNDO` | Passagem de fundo — ambas as pontas |
| `BROKER` | Perna interna de corretagem — maior volume do par |
| `PF` | Operação de pessoa física — taxa mais distante do mercado |

---

## Idempotência

A cada rodada, o script **recalcula do zero** para a `dtLiquidacao` — sobrescreve `cdStatus` e `idGrupoNegocio` de todos os trades daquele dia. Necessário para cobrir cancelamentos entre rodadas.

---

## Trades com `vrTaxaCalculada = NULL`

- Filtro FUNDO: participam (PU = `vrVolume / vrQuantidade` sempre existe).
- Filtros CORRETOR e PF: excluídos (sem taxa para comparar).
- Resultado: sempre ficam `VALIDO` isolados. Contados em `nullTaxa` no email.

---

## CLI

```powershell
# Data única (por dtLiquidacao)
python scripts/filtrar_trades.py --date 2026-05-27

# Janela histórica
python scripts/filtrar_trades.py --start 2026-05-01 --end 2026-05-27

# Parâmetros customizados
python scripts/filtrar_trades.py --date 2026-05-27 --fundo-max 80 --corretor-bps 1.0
python scripts/filtrar_trades.py --date 2026-05-27 --pf-min-bps 15 --pf-min-pctcdi 1.5
```

| Flag | Padrão | Descrição |
|---|---|---|
| `--date` | — | Data única (`dtLiquidacao`) |
| `--start` / `--end` | — | Intervalo de datas |
| `--fundo-max N` | 100.0 | R$/MM de notional para FUNDO |
| `--corretor-bps N` | 1.5 | bps máx para CORRETOR (não-%CDI) |
| `--corretor-pctcdi N` | 0.15 | unidades %CDI máx para CORRETOR |
| `--pf-min-bps N` | 20.0 | bps mín para PF (não-%CDI) |
| `--pf-min-pctcdi N` | 2.0 | unidades %CDI mín para PF |

---

## Resumo do email

```
Data          Total   VALIDO  FUNDO   BROKER   PF  NullTaxa
----------    -----   ------  -----   ------   --  --------
2026-06-03    10200     9939    142       87   29         3
----------    -----   ------  -----   ------   --  --------
TOTAL         10200     9939    142       87   29         3
```

---

## Logs e email

- Logs em: `data/logs/filtrar_trades/{YYYY-MM-DD_HHMMSS}.log`
- Email ao final — assunto `[OK] filtrar_trades` ou `[ERROR] filtrar_trades`
