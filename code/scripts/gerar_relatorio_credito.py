"""
gerar_relatorio_credito.py
==========================
Relatório HTML interativo de crédito privado com quatro visões:
  1. Visão Geral     — volume de mercado + spread médio ponderado dia a dia
  2. Por Ticker      — volume + spread por ticker(s) selecionado(s) dia a dia
  3. Spread×Duration — bolhas (X=duration, Y=spread, tamanho=volume)
  4. Boletim Diário  — tabela idêntica ao relatório diário, com seletor de data

Carrega todos os dados disponíveis na base (sem filtro de período).
Boletim replica exatamente a lógica de gerar_relatorio_html.py
(VALIDO + BROKER, D-1 Anbima, spread calculado por grupo).

CLI:
    python scripts/gerar_relatorio_credito.py
"""

import argparse
import csv
import json
import sys
import traceback
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from jinja2 import Environment, FileSystemLoader

from lib.config import cfg, get_email_list
from lib.db import get_db
from lib.email_outlook import send_completion_email, send_html_email
from lib.logger import get_logger

_SCRIPT_NAME = "gerar_relatorio_credito"

# ---------------------------------------------------------------------------
# Helpers compartilhados com gerar_relatorio_html.py
# ---------------------------------------------------------------------------

def _LoadFeriados() -> set[date]:
    """Feriados Anbima a partir de data/feriados_anbima.csv (set de date)."""
    feriadosPath = Path(cfg["paths"]["dbFile"]).parent / "feriados_anbima.csv"
    feriados: set[date] = set()
    if feriadosPath.exists():
        with feriadosPath.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    feriados.add(date.fromisoformat(row["data"].strip()))
                except ValueError:
                    pass
    return feriados


def _WeightedAvg(valores: list[tuple[float | None, float]]) -> float | None:
    num = den = 0.0
    for v, w in valores:
        if v is not None:
            num += v * w
            den += w
    return num / den if den else None


def _TipoExibicao(cdInstrumento: str | None, cdIndexador: str | None) -> str | None:
    """
    Tipo usado apenas para exibição/filtro. Debênture indexada a IPCA ou
    PREFIXADO é classificada como 'DEB 12.431' (incentivada). Demais
    instrumentos inalterados.
    """
    if cdInstrumento == "DEB" and cdIndexador in ("IPCA", "PREFIXADO"):
        return "DEB 12.431"
    return cdInstrumento


@dataclass
class _TradeRow:
    cdTicker: str
    cdEmissor: str
    cdInstrumento: str
    vrQuantidade: int
    vrVolume: float
    vrTaxaCalculada: float | None
    vrSpreadOver: float | None
    cdIndexador: str | None
    cdReferencia: str | None
    vrDuration: float | None
    dtVencimento: str | None
    vrTaxaAnbima: float | None
    vrSpreadAnbima: float | None
    nrTrades: int = 1


# ---------------------------------------------------------------------------
# SQL — Visão Geral / Por Ticker / Duration (sem filtro de período)
# ---------------------------------------------------------------------------

# Banda de sanidade do spread — APENAS para as visões AGREGADAS por indexador
# (Visão Geral). NÃO altera a base nem as visões por-ticker (Por Ativo,
# Spread×Duration, Boletim): lá o usuário precisa enxergar o caso extremo.
# Erros de PU↔taxa upstream produzem spreads impossíveis (ex: CRA02400ECU a
# ~6,3 mi %) que detonam a média ponderada do indexador. A banda só decide se o
# trade ENTRA no CASE do spread — o volume é sempre somado integralmente.
# Faixas (calibradas sobre a distribuição real 09–19/06, ver vault/98 Backlog):
#   CDI+ / IPCA / PREFIXADO  → |spread| <= 15%
#   %CDI                     → 60% <= spread <= 180%  (multiplicador, banda bilateral)
#   indexador desconhecido   → não filtra (grupo '?' não é plotado mesmo)
_BANDA_SPREAD = (
    "((ia.cdIndexador = '%CDI' AND t.vrSpreadOver BETWEEN 60.0 AND 180.0) "
    "OR (ia.cdIndexador IN ('CDI+','IPCA','PREFIXADO') AND ABS(t.vrSpreadOver) <= 15.0) "
    "OR ia.cdIndexador IS NULL "
    "OR ia.cdIndexador NOT IN ('%CDI','CDI+','IPCA','PREFIXADO'))"
)

_SQL_DIARIO = f"""
SELECT
    t.dtLiquidacao,
    SUM(t.vrVolume) / 1e6                                               AS vrVolumeM,
    SUM(CASE WHEN COALESCE(ia.cdIndexador,'') != '%CDI'
                  AND t.vrSpreadOver IS NOT NULL AND {_BANDA_SPREAD}
             THEN t.vrSpreadOver * 100.0 * t.vrVolume ELSE 0 END)
    / NULLIF(SUM(CASE WHEN COALESCE(ia.cdIndexador,'') != '%CDI'
                           AND t.vrSpreadOver IS NOT NULL AND {_BANDA_SPREAD}
                      THEN t.vrVolume ELSE 0 END), 0)   AS vrSpreadBps,
    SUM(CASE WHEN COALESCE(ia.cdIndexador,'') = '%CDI'
                  AND t.vrSpreadOver IS NOT NULL AND {_BANDA_SPREAD}
             THEN t.vrSpreadOver * t.vrVolume ELSE 0 END)
    / NULLIF(SUM(CASE WHEN COALESCE(ia.cdIndexador,'') = '%CDI'
                           AND t.vrSpreadOver IS NOT NULL AND {_BANDA_SPREAD}
                      THEN t.vrVolume ELSE 0 END), 0)   AS vrSpreadPctCdi
FROM NegociosProcessados t
LEFT JOIN InfoAtivos ia ON ia.cdTicker = t.cdTicker
WHERE t.cdStatus = 'VALIDO'
GROUP BY t.dtLiquidacao
ORDER BY t.dtLiquidacao
"""

_SQL_DIARIO_IDX = f"""
SELECT
    t.dtLiquidacao,
    COALESCE(ia.cdIndexador, '?')  AS cdIndexador,
    SUM(t.vrVolume) / 1e6          AS vrVolumeM,
    SUM(CASE WHEN t.vrSpreadOver IS NOT NULL AND {_BANDA_SPREAD}
             THEN t.vrSpreadOver * t.vrVolume ELSE 0 END)
    / NULLIF(SUM(CASE WHEN t.vrSpreadOver IS NOT NULL AND {_BANDA_SPREAD}
                      THEN t.vrVolume ELSE 0 END), 0)   AS vrSpreadRaw
FROM NegociosProcessados t
LEFT JOIN InfoAtivos ia ON ia.cdTicker = t.cdTicker
WHERE t.cdStatus = 'VALIDO'
GROUP BY t.dtLiquidacao, ia.cdIndexador
ORDER BY t.dtLiquidacao
"""

_SQL_TICKER = """
SELECT
    t.dtLiquidacao,
    t.cdTicker,
    COALESCE(ia.cdEmissor,  '')    AS cdEmissor,
    COALESCE(ia.cdIndexador, '?')  AS cdIndexador,
    SUM(t.vrVolume) / 1e6          AS vrVolumeM,
    SUM(CASE WHEN t.vrSpreadOver IS NOT NULL
             THEN t.vrSpreadOver * t.vrVolume ELSE 0 END)
    / NULLIF(SUM(CASE WHEN t.vrSpreadOver IS NOT NULL
                      THEN t.vrVolume ELSE 0 END), 0)   AS vrSpreadRaw
FROM NegociosProcessados t
LEFT JOIN InfoAtivos ia ON ia.cdTicker = t.cdTicker
WHERE t.cdStatus = 'VALIDO'
GROUP BY t.dtLiquidacao, t.cdTicker, ia.cdEmissor, ia.cdIndexador
ORDER BY t.dtLiquidacao, t.cdTicker
"""

_SQL_DURATION = """
SELECT
    t.dtLiquidacao,
    t.cdTicker,
    COALESCE(ia.cdEmissor,  '')    AS cdEmissor,
    COALESCE(ia.cdIndexador, '?')  AS cdIndexador,
    ia.vrDuration,
    SUM(t.vrVolume) / 1e6          AS vrVolumeM,
    SUM(CASE WHEN t.vrSpreadOver IS NOT NULL
             THEN t.vrSpreadOver * t.vrVolume ELSE 0 END)
    / NULLIF(SUM(CASE WHEN t.vrSpreadOver IS NOT NULL
                      THEN t.vrVolume ELSE 0 END), 0)   AS vrSpreadRaw
FROM NegociosProcessados t
LEFT JOIN InfoAtivos ia ON ia.cdTicker = t.cdTicker
WHERE t.cdStatus = 'VALIDO'
  AND ia.vrDuration IS NOT NULL
GROUP BY t.dtLiquidacao, t.cdTicker, ia.cdEmissor, ia.cdIndexador, ia.vrDuration
ORDER BY t.dtLiquidacao, vrVolumeM DESC
"""

# ---------------------------------------------------------------------------
# SQL — Visão Anbima (spread Anbima ponderado por outstanding)
# ---------------------------------------------------------------------------
#
# PESO = outstanding real (varia POR DATA), vindo da tabela `Outstanding`
# (populada por `scrape_outstanding_bloomberg.py`). O JOIN casa a data:
# `AND p.dtPeso = ai.dtReferencia`, então cada (ticker, data Anbima) usa o
# outstanding daquela data. Ativos sem outstanding (> 0) na data ficam fora
# do cálculo (o JOIN interno já exclui).
#
# NOTA: enquanto `Outstanding` estiver vazia (ex.: PC pessoal, sem Bloomberg),
# a Visão Anbima fica sem dados. As demais abas não usam este CTE.
# (Histórico: até 28/06/2026 o peso era `InfoAtivos.vrQuantidadeEmissao`, um
#  proxy CONSTANTE por data, sem casa de data no JOIN.)
_PESO_CTE = """
Peso AS (
    SELECT cdTicker, dtOutstanding AS dtPeso, vrOutstanding AS vrPeso
    FROM Outstanding
    WHERE vrOutstanding IS NOT NULL AND vrOutstanding > 0
)
"""

# Linha POR TICKER (spread Anbima + peso) por (data, indexador). A agregação
# (média ponderada por quantidade de emissão) é feita no CLIENTE, só sobre os tickers
# que o usuário deixar selecionados no filtro manual por indexador da Visão Anbima —
# assim ele tira na mão os high-yield estressados sem filtro estatístico automático.
# População: ativos com peso > 0 (JOIN Peso).
_SQL_ANBIMA_IDX = f"""
WITH {_PESO_CTE}
SELECT
    ai.dtReferencia,
    COALESCE(ia.cdIndexador, '?') AS cdIndexador,
    ai.cdTicker,
    COALESCE(ia.cdEmissor, '')    AS cdEmissor,
    ia.cdInstrumento,
    ai.vrSpreadAnbima,
    p.vrPeso
FROM AnbimaIndicativos ai
JOIN Peso       p  ON p.cdTicker  = ai.cdTicker AND p.dtPeso = ai.dtReferencia
JOIN InfoAtivos ia ON ia.cdTicker = ai.cdTicker
WHERE ai.vrSpreadAnbima IS NOT NULL
ORDER BY ai.dtReferencia
"""

# Linha POR TICKER (duration + spread + taxa nominal Anbima) para os indexadores
# SEM vértice de referência (CDI+ e %CDI). Alimenta as "curvas por duration" da
# Visão Anbima: x = duration do ativo, y = spread/nominal, uma curva por data.
# Mesma população (Peso > 0); exige duration conhecida.
_SQL_ANBIMA_DUR = f"""
WITH {_PESO_CTE}
SELECT
    ai.dtReferencia,
    ia.cdIndexador,
    ai.cdTicker,
    ia.cdInstrumento,
    ia.vrDuration,
    ai.vrSpreadAnbima,
    ai.vrTaxaAnbima
FROM AnbimaIndicativos ai
JOIN Peso       p  ON p.cdTicker  = ai.cdTicker AND p.dtPeso = ai.dtReferencia
JOIN InfoAtivos ia ON ia.cdTicker = ai.cdTicker
WHERE ai.vrSpreadAnbima IS NOT NULL
  AND ia.cdIndexador IN ('CDI+','%CDI')
  AND ia.vrDuration IS NOT NULL
ORDER BY ai.dtReferencia, ia.vrDuration
"""

# Linha POR TICKER (spread + taxa nominal Anbima + tipo de instrumento) por
# (data, referência NTN-B / DI1). A mediana por vértice é feita no CLIENTE, filtrável
# por tipo de instrumento (DEB/DEB 12.431/CRI/CRA). Mesma população do _SQL_ANBIMA_IDX.
_SQL_ANBIMA_REF = f"""
WITH {_PESO_CTE}
SELECT
    ai.dtReferencia,
    ia.cdReferencia,
    ai.cdTicker,
    ia.cdInstrumento,
    ia.cdIndexador,
    ai.vrSpreadAnbima,
    ai.vrTaxaAnbima
FROM AnbimaIndicativos ai
JOIN Peso       p  ON p.cdTicker  = ai.cdTicker AND p.dtPeso = ai.dtReferencia
JOIN InfoAtivos ia ON ia.cdTicker = ai.cdTicker
WHERE ai.vrSpreadAnbima IS NOT NULL
  AND (ia.cdReferencia LIKE 'NTN-B%' OR ia.cdReferencia LIKE 'DI1%')
ORDER BY ai.dtReferencia, ia.cdReferencia
"""

# ---------------------------------------------------------------------------
# SQL — Boletim Diário
# ---------------------------------------------------------------------------

_SQL_DATAS_BOLETIM = """
SELECT DISTINCT dtLiquidacao
FROM NegociosProcessados
WHERE cdStatus IN ('VALIDO', 'BROKER')
ORDER BY dtLiquidacao
"""

# Anbima casado por dtNegocio: cada trade compara com a indicativa mais recente
# com dtReferencia <= o próprio dtNegocio do trade (reflete o mercado no momento
# em que o negócio foi fechado). Simétrico ao MtM de calc_spread_over.
_SQL_FETCH_VALIDO = """
WITH AnbimaMatch AS (
    SELECT tp.idTrade AS idTrade, ai.vrTaxaAnbima, ai.vrSpreadAnbima,
           ROW_NUMBER() OVER (PARTITION BY tp.idTrade ORDER BY ai.dtReferencia DESC) AS rn
    FROM NegociosProcessados tp
    JOIN AnbimaIndicativos ai
      ON ai.cdTicker = tp.cdTicker AND ai.dtReferencia <= tp.dtNegocio
    WHERE tp.dtLiquidacao = ? AND tp.cdStatus = 'VALIDO'
)
SELECT tp.cdTicker, ia.cdEmissor, tr.cdInstrumento,
       tp.vrQuantidade, tp.vrVolume, tp.vrTaxaCalculada, tp.vrSpreadOver,
       ia.cdIndexador, ia.cdReferencia, ia.vrDuration, ia.dtVencimento,
       am.vrTaxaAnbima, am.vrSpreadAnbima
FROM NegociosProcessados tp
JOIN NegociosBrutos tr ON tr.idTrade = tp.idTrade
LEFT JOIN InfoAtivos ia ON ia.cdTicker = tp.cdTicker
LEFT JOIN AnbimaMatch am ON am.idTrade = tp.idTrade AND am.rn = 1
WHERE tp.dtLiquidacao = ? AND tp.cdStatus = 'VALIDO' AND tr.cdSituacao != 'Cancelado'
"""

_SQL_FETCH_BROKER = """
WITH AnbimaMatch AS (
    SELECT tp.idTrade AS idTrade, ai.vrTaxaAnbima, ai.vrSpreadAnbima,
           ROW_NUMBER() OVER (PARTITION BY tp.idTrade ORDER BY ai.dtReferencia DESC) AS rn
    FROM NegociosProcessados tp
    JOIN AnbimaIndicativos ai
      ON ai.cdTicker = tp.cdTicker AND ai.dtReferencia <= tp.dtNegocio
    WHERE tp.dtLiquidacao = ? AND tp.cdStatus = 'BROKER'
)
SELECT tp.cdTicker, ia.cdEmissor, tr.cdInstrumento, tp.idGrupoNegocio, tp.dtNegocio,
       tp.vrQuantidade, tp.vrVolume, tp.vrTaxaCalculada,
       ia.cdIndexador, ia.cdReferencia, ia.vrDuration, ia.dtVencimento,
       am.vrTaxaAnbima, am.vrSpreadAnbima
FROM NegociosProcessados tp
JOIN NegociosBrutos tr ON tr.idTrade = tp.idTrade
LEFT JOIN InfoAtivos ia ON ia.cdTicker = tp.cdTicker
LEFT JOIN AnbimaMatch am ON am.idTrade = tp.idTrade AND am.rn = 1
WHERE tp.dtLiquidacao = ? AND tp.cdStatus = 'BROKER'
  AND tp.idGrupoNegocio IS NOT NULL AND tr.cdSituacao != 'Cancelado'
"""

_SQL_MTM_RATE = "SELECT vrTaxa FROM MtmAnbima WHERE cdTicker = ? AND dtReferencia = ?"

# ---------------------------------------------------------------------------
# SQL — Info Ativos (uma linha por ticker negociado)
# ---------------------------------------------------------------------------

_SQL_INFO_ATIVOS = """
WITH UltTrade AS (
    SELECT cdTicker, MAX(dtLiquidacao) AS dtUltimo
    FROM NegociosProcessados
    WHERE cdStatus = 'VALIDO'
    GROUP BY cdTicker
),
TaxaTrade AS (
    SELECT tp.cdTicker,
           ut.dtUltimo AS dtTrade,
           SUM(CASE WHEN tp.vrTaxaCalculada IS NOT NULL
                    THEN tp.vrTaxaCalculada * tp.vrVolume ELSE 0 END)
             / NULLIF(SUM(CASE WHEN tp.vrTaxaCalculada IS NOT NULL
                               THEN tp.vrVolume ELSE 0 END), 0) AS vrTaxaTrade
    FROM NegociosProcessados tp
    JOIN UltTrade ut ON ut.cdTicker = tp.cdTicker AND tp.dtLiquidacao = ut.dtUltimo
    WHERE tp.cdStatus = 'VALIDO'
    GROUP BY tp.cdTicker, ut.dtUltimo
),
TaxaAnb AS (
    SELECT cdTicker, vrTaxaAnbima, dtReferencia FROM (
        SELECT cdTicker, vrTaxaAnbima, dtReferencia,
               ROW_NUMBER() OVER (PARTITION BY cdTicker ORDER BY dtReferencia DESC) AS rn
        FROM AnbimaIndicativos
        WHERE vrTaxaAnbima IS NOT NULL
    ) WHERE rn = 1
)
SELECT
    tt.cdTicker,
    COALESCE(ia.cdEmissor, '')     AS cdEmissor,
    COALESCE(ia.cdInstrumento, '') AS cdInstrumento,
    ia.vrDuration,
    COALESCE(ia.cdIndexador, '')   AS cdIndexador,
    ta.vrTaxaAnbima,
    ta.dtReferencia                AS dtAnbima,
    tt.vrTaxaTrade,
    tt.dtTrade
FROM TaxaTrade tt
LEFT JOIN InfoAtivos ia ON ia.cdTicker = tt.cdTicker
LEFT JOIN TaxaAnb   ta ON ta.cdTicker = tt.cdTicker
ORDER BY tt.cdTicker
"""

# ---------------------------------------------------------------------------
# Data loading — Visão Geral / Por Ticker / Duration
# ---------------------------------------------------------------------------

def _LoadDiario(conn) -> list[dict]:
    rows = conn.execute(_SQL_DIARIO).fetchall()
    return [
        {
            "dt":           r[0],
            "volume":       round(r[1], 2) if r[1] is not None else 0.0,
            "spreadBps":    round(r[2], 2) if r[2] is not None else None,
            "spreadPctCdi": round(r[3], 4) if r[3] is not None else None,
        }
        for r in rows
    ]


def _LoadDiarioIdx(conn) -> list[dict]:
    """Volume e spread ponderado por volume, quebrados por (dia, indexador).
    spreadRaw fica em % para CDI+/IPCA/PREFIXADO (multiplicar por 100 = bps no
    template) e já é o spread direto para %CDI."""
    rows = conn.execute(_SQL_DIARIO_IDX).fetchall()
    return [
        {
            "dt":        r[0],
            "indexador": r[1],
            "volume":    round(r[2], 4) if r[2] is not None else 0.0,
            "spreadRaw": round(r[3], 6) if r[3] is not None else None,
        }
        for r in rows
    ]


def _LoadAnbimaIdx(conn) -> list[dict]:
    """Linha por ticker (spread Anbima + peso) por (dia, indexador). A média ponderada
    por emissão é feita no cliente, sobre os tickers selecionados no filtro manual.
    spreadRaw em % para os não-%CDI (×100 = bps no template); direto para %CDI."""
    return [
        {
            "dt":        r[0],
            "indexador": r[1],
            "ticker":    r[2],
            "emissor":   r[3] or "",
            "tipo":      _TipoExibicao(r[4], r[1]) or "—",
            "spreadRaw": round(r[5], 6) if r[5] is not None else None,
            "peso":      r[6],
        }
        for r in conn.execute(_SQL_ANBIMA_IDX).fetchall()
    ]


def _LoadAnbimaDur(conn) -> list[dict]:
    """Linha por ticker (duration + spread + taxa nominal Anbima) para CDI+ e %CDI.
    Alimenta as curvas por duration da Visão Anbima (x = duration, y = spread/nominal,
    uma curva por data). spreadRaw em % para CDI+ (×100 = bps no template); direto
    (multiplicador) para %CDI."""
    return [
        {
            "dt":        r[0],
            "indexador": r[1],
            "ticker":    r[2],
            "tipo":      _TipoExibicao(r[3], r[1]) or "—",
            "duration":  round(r[4], 4) if r[4] is not None else None,
            "spreadRaw": round(r[5], 6) if r[5] is not None else None,
            "taxa":      round(r[6], 6) if r[6] is not None else None,
        }
        for r in conn.execute(_SQL_ANBIMA_DUR).fetchall()
    ]


def _LoadAnbimaRef(conn) -> list[dict]:
    """Linha por ticker (spread + taxa nominal Anbima + tipo de instrumento) por
    (dia, ref NTN-B/DI1). A mediana por vértice é feita no cliente (robusta a outlier),
    filtrável por tipo de instrumento (DEB/DEB 12.431/CRI/CRA)."""
    return [
        {
            "dt":        r[0],
            "ref":       r[1],
            "ticker":    r[2],
            "tipo":      _TipoExibicao(r[3], r[4]) or "—",
            "spreadRaw": round(r[5], 6) if r[5] is not None else None,
            "taxa":      round(r[6], 6) if r[6] is not None else None,
        }
        for r in conn.execute(_SQL_ANBIMA_REF).fetchall()
    ]


def _LoadTicker(conn) -> list[dict]:
    rows = conn.execute(_SQL_TICKER).fetchall()
    return [
        {
            "dt":        r[0],
            "ticker":    r[1],
            "emissor":   r[2] or "",
            "indexador": r[3],
            "volume":    round(r[4], 4) if r[4] is not None else 0.0,
            "spreadRaw": round(r[5], 6) if r[5] is not None else None,
        }
        for r in rows
    ]


def _LoadDuration(conn) -> list[dict]:
    rows = conn.execute(_SQL_DURATION).fetchall()
    return [
        {
            "dt":        r[0],
            "ticker":    r[1],
            "emissor":   r[2] or "",
            "indexador": r[3],
            "duration":  round(r[4], 4) if r[4] is not None else None,
            "volume":    round(r[5], 4) if r[5] is not None else 0.0,
            "spreadRaw": round(r[6], 6) if r[6] is not None else None,
        }
        for r in rows
    ]


def _LoadInfoAtivos(conn) -> list[dict]:
    """Uma linha por ticker negociado: cadastro + taxa Anbima e taxa de trade mais recentes."""
    rows = conn.execute(_SQL_INFO_ATIVOS).fetchall()
    return [
        {
            "ticker":     r["cdTicker"],
            "emissor":    r["cdEmissor"] or "",
            "tipo":       _TipoExibicao(r["cdInstrumento"], r["cdIndexador"]) or "",
            "duration":   round(r["vrDuration"], 4)  if r["vrDuration"]  is not None else None,
            "indexador":  r["cdIndexador"] or "",
            "taxaAnbima": round(r["vrTaxaAnbima"], 4) if r["vrTaxaAnbima"] is not None else None,
            "dtAnbima":   r["dtAnbima"],
            "taxaTrade":  round(r["vrTaxaTrade"], 6)  if r["vrTaxaTrade"]  is not None else None,
            "dtTrade":    r["dtTrade"],
        }
        for r in rows
    ]


def _GetTickersByVolume(tickerRows: list[dict]) -> list[str]:
    vol: dict[str, float] = {}
    for r in tickerRows:
        vol[r["ticker"]] = vol.get(r["ticker"], 0.0) + r["volume"]
    return [t for t, _ in sorted(vol.items(), key=lambda x: -x[1])]


# ---------------------------------------------------------------------------
# Boletim Diário — agrega por pregão, replica lógica de gerar_relatorio_html
# ---------------------------------------------------------------------------

def _FetchTrades(conn, dtLiquidacao: str) -> list[_TradeRow]:
    rows = conn.execute(_SQL_FETCH_VALIDO, (dtLiquidacao, dtLiquidacao)).fetchall()
    return [
        _TradeRow(
            cdTicker=r["cdTicker"],
            cdEmissor=r["cdEmissor"] or "",
            cdInstrumento=r["cdInstrumento"],
            vrQuantidade=r["vrQuantidade"],
            vrVolume=r["vrVolume"],
            vrTaxaCalculada=r["vrTaxaCalculada"],
            vrSpreadOver=r["vrSpreadOver"],
            cdIndexador=r["cdIndexador"],
            cdReferencia=r["cdReferencia"],
            vrDuration=r["vrDuration"],
            dtVencimento=r["dtVencimento"],
            vrTaxaAnbima=r["vrTaxaAnbima"],
            vrSpreadAnbima=r["vrSpreadAnbima"],
        )
        for r in rows
    ]


def _FetchBrokerGroups(conn, dtLiquidacao: str) -> list[_TradeRow]:
    rows = conn.execute(_SQL_FETCH_BROKER, (dtLiquidacao, dtLiquidacao)).fetchall()
    if not rows:
        return []
    grupos: dict[str, list] = {}
    for r in rows:
        grupos.setdefault(r["idGrupoNegocio"], []).append(r)
    result: list[_TradeRow] = []
    for trades in grupos.values():
        taxas = [t["vrTaxaCalculada"] for t in trades if t["vrTaxaCalculada"] is not None]
        if not taxas:
            continue
        taxaMedia   = (max(taxas) + min(taxas)) / 2.0
        volumeGrupo = sum(t["vrVolume"] for t in trades) / 2.0
        qtdGrupo    = sum(t["vrQuantidade"] for t in trades) // 2
        rep         = max(trades, key=lambda t: t["vrVolume"])
        cdReferencia       = rep["cdReferencia"]
        vrSpreadOver: float | None = None
        if cdReferencia == "FUNDING":
            vrSpreadOver = taxaMedia
        elif cdReferencia is not None:
            mtm = conn.execute(_SQL_MTM_RATE, (cdReferencia, rep["dtNegocio"])).fetchone()
            if mtm:
                vrSpreadOver = ((1 + taxaMedia / 100) / (1 + mtm["vrTaxa"] / 100) - 1) * 100
        result.append(_TradeRow(
            cdTicker=rep["cdTicker"],
            cdEmissor=rep["cdEmissor"] or "",
            cdInstrumento=rep["cdInstrumento"],
            vrQuantidade=qtdGrupo,
            vrVolume=volumeGrupo,
            vrTaxaCalculada=taxaMedia,
            vrSpreadOver=vrSpreadOver,
            cdIndexador=rep["cdIndexador"],
            cdReferencia=cdReferencia,
            vrDuration=rep["vrDuration"],
            dtVencimento=rep["dtVencimento"],
            vrTaxaAnbima=rep["vrTaxaAnbima"],
            vrSpreadAnbima=rep["vrSpreadAnbima"],
            nrTrades=len(trades),
        ))
    return result


def _AggregateTicker(cdTicker: str, grupo: list[_TradeRow]) -> dict:
    p            = grupo[0]
    vrVolumeTotal  = sum(t.vrVolume for t in grupo)
    vrTaxaMedia    = _WeightedAvg([(t.vrTaxaCalculada, t.vrVolume) for t in grupo])
    vrSpreadOver   = _WeightedAvg([(t.vrSpreadOver,    t.vrVolume) for t in grupo])
    # Anbima também ponderado por volume: trades do mesmo ticker podem ter
    # dtNegocio (logo indicativa Anbima) diferentes dentro da mesma liquidação.
    vrTaxaAnbima   = _WeightedAvg([(t.vrTaxaAnbima,   t.vrVolume) for t in grupo])
    vrSpreadAnbima = _WeightedAvg([(t.vrSpreadAnbima, t.vrVolume) for t in grupo])
    return {
        "cdTicker":          cdTicker,
        "cdEmissor":         p.cdEmissor,
        "cdInstrumento":     p.cdInstrumento or "—",
        "cdTipo":            _TipoExibicao(p.cdInstrumento, p.cdIndexador) or "—",
        "cdIndexador":       p.cdIndexador,
        "cdReferencia":             p.cdReferencia,
        "vrDuration":        round(p.vrDuration, 4)  if p.vrDuration  is not None else None,
        "dtVencimento":      p.dtVencimento,
        "nrTrades":          sum(t.nrTrades for t in grupo),
        "vrQuantidadeTotal": sum(t.vrQuantidade for t in grupo),
        "vrVolumeTotal":     round(vrVolumeTotal, 2),
        "vrTaxaMedia":       round(vrTaxaMedia, 6)   if vrTaxaMedia   is not None else None,
        "vrSpreadOverMedio": round(vrSpreadOver, 6)  if vrSpreadOver  is not None else None,
        "vrTaxaAnbima":      round(vrTaxaAnbima, 4)   if vrTaxaAnbima   is not None else None,
        "vrSpreadAnbima":    round(vrSpreadAnbima, 4) if vrSpreadAnbima is not None else None,
    }


def _LoadBoletim(conn, log) -> dict:
    """Retorna {dtLiquidacao: {tickers, resumo, totais}} para todos os pregões."""
    datas     = [r[0] for r in conn.execute(_SQL_DATAS_BOLETIM).fetchall()]
    ordemInstr = {"DEB": 0, "DEB 12.431": 1, "CRI": 2, "CRA": 3}
    boletim: dict[str, dict] = {}

    for dt in datas:
        linhas   = _FetchTrades(conn, dt)
        linhas.extend(_FetchBrokerGroups(conn, dt))
        if not linhas:
            continue

        gruposTicker: dict[str, list] = {}
        for ln in linhas:
            gruposTicker.setdefault(ln.cdTicker, []).append(ln)

        tickers = sorted(
            [_AggregateTicker(tk, grp) for tk, grp in gruposTicker.items()],
            key=lambda t: (ordemInstr.get(t["cdTipo"] or "", 99), -(t["vrVolumeTotal"] or 0)),
        )

        resumoMap: dict[str, dict] = {}
        for t in tickers:
            tipo = t["cdTipo"] or "OUTRO"
            if tipo not in resumoMap:
                resumoMap[tipo] = {"cdTipo": tipo, "nrTickers": 0, "vrVolume": 0.0, "nrTrades": 0}
            resumoMap[tipo]["nrTickers"] += 1
            resumoMap[tipo]["vrVolume"]  += t["vrVolumeTotal"] or 0.0
            resumoMap[tipo]["nrTrades"]  += t["nrTrades"]

        ordemSecoes = ["DEB", "DEB 12.431", "CRI", "CRA"]
        resumo = [resumoMap[i] for i in ordemSecoes if i in resumoMap]
        resumo += [v for k, v in resumoMap.items() if k not in ordemSecoes]

        vrVolumeTotal = round(sum(t["vrVolumeTotal"] for t in tickers), 2)
        boletim[dt] = {
            "tickers":       tickers,
            "resumo":        resumo,
            "nrTickers":     len(tickers),
            "nrTrades":      sum(t["nrTrades"] for t in tickers),
            "vrVolumeTotal": vrVolumeTotal,
            "vrQtdTotal":    sum(t["vrQuantidadeTotal"] for t in tickers),
        }
        log.info("  boletim %s: %d tickers | R$ %.2f MM", dt, len(tickers), vrVolumeTotal / 1e6)

    return boletim


# ---------------------------------------------------------------------------
# Email — top 20 trades (por volume) de um pregão (data de liquidação)
# ---------------------------------------------------------------------------

# Destinatários do rascunho do relatório do dia: resolvidos em runtime via
# lib.config.get_email_list("destinatarios") (destinatarios.py → var EMAIL_DESTINATARIOS).

# Paleta Itaú (mesma do template relatorio_secundario.html)
_ITAU_NAVY   = "#003087"
_ITAU_NAVY2  = "#002370"
_ITAU_ORANGE = "#EC7000"
_ITAU_LITE   = "#fff3e8"


def _FmtBR(v: float | None, dec: int) -> str:
    """Número no padrão BR (milhar '.', decimal ','). None → travessão."""
    if v is None:
        return "—"
    s = f"{v:,.{dec}f}"                       # ex: 1,234.56 (US)
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _DataBR(iso: str) -> str:
    """YYYY-MM-DD → DD/MM/AAAA."""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return iso


def _RefDispEmail(ref: str | None) -> str:
    """NTN-B 35 → B35+; demais inalterados (espelha bRefDisp do template)."""
    if not ref:
        return "—"
    if ref.startswith("NTN-B"):
        return "B" + ref.replace("NTN-B", "").replace(" ", "") + "+"
    return ref


def _SpreadEmail(v: float | None, cdIndexador: str | None) -> str:
    """%CDI → multiplicador direto; demais → ×100 = bps (espelha bSpreadDisp)."""
    if v is None:
        return "—"
    if cdIndexador == "%CDI":
        return _FmtBR(v, 0)
    return _FmtBR(v * 100, 0) + " bps"


def _BuildEmailHtml(dtX: str, top: list[dict], diaInfo: dict) -> str:
    """Tabela HTML formatada (tons Itaú) com os top 20 ativos por volume do pregão."""
    cols = [
        ("Instrumento",          "left"),
        ("Ticker",               "left"),
        ("Emissor",              "left"),
        ("Volume Negociado",     "right"),
        ("Indexador",            "left"),
        ("Duration",             "right"),
        ("Ref",                  "left"),
        ("Taxa Negócio Média",   "right"),
        ("Spread Negócio Média", "right"),
        ("Taxa Anbima Média",    "right"),
        ("Spread Anbima Média",  "right"),
    ]
    thStyle = (
        f"background:{_ITAU_NAVY};color:#fff;font-size:11px;font-weight:700;"
        "text-transform:uppercase;letter-spacing:.4px;padding:8px 10px;"
        "border-bottom:2px solid " + _ITAU_ORANGE + ";white-space:nowrap;text-align:"
    )
    head = "".join(f'<th style="{thStyle}{align};">{name}</th>' for name, align in cols)

    bodyRows = []
    for i, t in enumerate(top):
        bg  = "#ffffff" if i % 2 == 0 else "#f4f6fa"
        idx = t["cdIndexador"]
        vol = t["vrVolumeTotal"]
        cells = [
            (t["cdTipo"] or "—",                                                       "left"),
            (t["cdTicker"],                                                            "left"),
            (t["cdEmissor"] or "—",                                                    "left"),
            (_FmtBR(vol / 1e6 if vol is not None else None, 2) + " MM",                "right"),
            (idx or "—",                                                               "left"),
            (_FmtBR(t["vrDuration"], 2),                                               "right"),
            (_RefDispEmail(t["cdReferencia"]),                                                "left"),
            ((_FmtBR(t["vrTaxaMedia"], 2) + "%") if t["vrTaxaMedia"] is not None else "—",       "right"),
            (_SpreadEmail(t["vrSpreadOverMedio"], idx),                                "right"),
            ((_FmtBR(t["vrTaxaAnbima"], 2) + "%") if t["vrTaxaAnbima"] is not None else "—",      "right"),
            (_SpreadEmail(t["vrSpreadAnbima"], idx),                                   "right"),
        ]
        tds = "".join(
            f'<td style="padding:7px 10px;font-size:12px;color:#222;'
            f'border-bottom:1px solid #e6e9ef;text-align:{align};'
            f'{"font-family:Consolas,monospace;font-weight:600;color:" + _ITAU_NAVY + ";" if j == 1 else ""}'
            f'white-space:nowrap;">{val}</td>'
            for j, (val, align) in enumerate(cells)
        )
        bodyRows.append(f'<tr style="background:{bg};">{tds}</tr>')

    nrTickers = diaInfo["nrTickers"]
    volTotMM  = (diaInfo["vrVolumeTotal"] or 0) / 1e6

    return f"""\
<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#eef0f3;font-family:Segoe UI,Arial,sans-serif;">
  <div style="max-width:1100px;margin:0 auto;padding:18px;">
    <div style="background:{_ITAU_NAVY};color:#fff;padding:16px 22px;border-radius:6px 6px 0 0;">
      <div style="font-size:17px;font-weight:700;letter-spacing:.3px;">
        Relatório Diário de Negociação — Crédito Privado
      </div>
      <div style="font-size:12px;color:#cdd6ea;margin-top:3px;">
        Top 20 ativos por <b style="color:#fff;">volume negociado</b> ·
        data de <b style="color:#fff;">liquidação {_DataBR(dtX)}</b>
      </div>
    </div>
    <div style="background:{_ITAU_LITE};border-left:4px solid {_ITAU_ORANGE};
                padding:10px 16px;font-size:12px;color:{_ITAU_NAVY};">
      Ranking pelos 20 maiores volumes negociados no pregão de liquidação
      <b>{_DataBR(dtX)}</b> ({nrTickers} ativos no dia · R$ {_FmtBR(volTotMM, 2)} MM no total).
      O relatório completo e interativo (todas as abas e pregões) segue em anexo
      (<b>relatorio_secundario.html</b>). Taxas em % a.a.; spreads em bps
      (exceto %&nbsp;CDI, que é multiplicador).
    </div>
    <table style="width:100%;border-collapse:collapse;background:#fff;
                  box-shadow:0 1px 5px rgba(0,0,0,.08);border-radius:0 0 6px 6px;overflow:hidden;">
      <thead><tr>{head}</tr></thead>
      <tbody>{''.join(bodyRows)}</tbody>
    </table>
    <div style="font-size:10.5px;color:#8a93a3;margin-top:10px;">
      Gerado automaticamente por <code>gerar_relatorio_credito.py</code>.
      Taxa/spread médios ponderados por volume (VALIDO + BROKER agregado por grupo);
      taxa/spread Anbima referentes a D-1 da liquidação.
    </div>
  </div>
</body></html>"""


def _EnviarEmailDia(dtX: str, boletim: dict, htmlPath: Path, log) -> None:
    """Monta e envia o email do dia X (top 20 por volume) com o HTML em anexo."""
    try:
        date.fromisoformat(dtX)
    except ValueError:
        log.error("%s: --email-dia invalido (use YYYY-MM-DD): %s", _SCRIPT_NAME, dtX)
        return

    diaInfo = boletim.get(dtX)
    if not diaInfo or not diaInfo.get("tickers"):
        log.warning("%s: sem negocios para liquidacao %s — email do dia nao enviado.",
                    _SCRIPT_NAME, dtX)
        return

    destinatarios = get_email_list("destinatarios")
    if not destinatarios:
        log.warning("%s: sem destinatarios (destinatarios.py / EMAIL_DESTINATARIOS) — rascunho nao salvo.",
                    _SCRIPT_NAME)
        return

    top = sorted(diaInfo["tickers"], key=lambda t: -(t["vrVolumeTotal"] or 0))[:20]
    html = _BuildEmailHtml(dtX, top, diaInfo)
    subject = f"Relatório Crédito Privado — Top 20 volume · liquidação {_DataBR(dtX)}"
    send_html_email(
        subject, html,
        attachments=[str(htmlPath)],
        logger=log,
        to=destinatarios,
        draft=True,
    )
    log.info("%s: rascunho do dia %s salvo (top %d de %d ativos) para %s",
             _SCRIPT_NAME, dtX, len(top), diaInfo["nrTickers"], "; ".join(destinatarios))


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def _RenderHtml(
    diario:      list[dict],
    diarioIdx:   list[dict],
    anbimaIdx:   list[dict],
    anbimaRef:   list[dict],
    anbimaDur:   list[dict],
    ticker:      list[dict],
    duration:    list[dict],
    boletim:     dict,
    diasBoletim: list[str],
    infoAtivos:  list[dict],
    dtStart:     str,
    dtEnd:       str,
) -> str:
    tickers = _GetTickersByVolume(ticker)
    datas   = sorted({r["dt"] for r in ticker})

    payload = {
        "dtStart":     dtStart,
        "dtEnd":       dtEnd,
        "diario":      diario,
        "diarioIdx":   diarioIdx,
        "anbimaIdx":   anbimaIdx,
        "anbimaRef":   anbimaRef,
        "anbimaDur":   anbimaDur,
        "ticker":      ticker,
        "duration":    duration,
        "tickers":     tickers,
        "datas":       datas,
        "boletim":     boletim,
        "diasBoletim": diasBoletim,
        "infoAtivos":  infoAtivos,
        "feriados":    sorted(d.isoformat() for d in _LoadFeriados()),
    }

    templatesDir = Path(cfg["paths"].get("templatesDir", "templates"))
    env  = Environment(loader=FileSystemLoader(str(templatesDir)))
    tmpl = env.get_template("relatorio_secundario.html")
    return tmpl.render(
        data_json=json.dumps(payload, ensure_ascii=False),
        dtStart=dtStart,
        dtEnd=dtEnd,
    )


def _BuildSummary(diario: list[dict], ticker: list[dict]) -> str:
    totalVol     = sum(r["volume"] for r in diario)
    totalTickers = len({r["ticker"] for r in ticker})
    dtStart = diario[0]["dt"]  if diario else "?"
    dtEnd   = diario[-1]["dt"] if diario else "?"
    lines = [
        f"Período  : {dtStart} → {dtEnd}",
        f"Pregões  : {len(diario)}",
        f"Tickers  : {totalTickers}",
        f"Volume   : R$ {totalVol:,.2f} MM",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI / Main
# ---------------------------------------------------------------------------

def _ParseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gera relatório HTML interativo de crédito privado (todos os dados da base)."
    )
    parser.add_argument(
        "--email-dia",
        metavar="YYYY-MM-DD",
        default=None,
        help="Envia email com o top 20 ativos por volume da data de LIQUIDAÇÃO informada "
             "(corpo HTML formatado) e o relatório completo em anexo.",
    )
    return parser.parse_args()


def Main() -> None:
    log     = get_logger(_SCRIPT_NAME)
    args    = _ParseArgs()
    summary = ""
    success = True

    try:
        log.info("%s: iniciando", _SCRIPT_NAME)

        conn = get_db()
        try:
            diario    = _LoadDiario(conn)
            diarioIdx = _LoadDiarioIdx(conn)
            anbimaIdx = _LoadAnbimaIdx(conn)
            anbimaRef = _LoadAnbimaRef(conn)
            anbimaDur = _LoadAnbimaDur(conn)
            ticker    = _LoadTicker(conn)
            duration = _LoadDuration(conn)
            log.info("%s: %d pregões | %d ticker-dias | %d duration-rows",
                     _SCRIPT_NAME, len(diario), len(ticker), len(duration))
            log.info("%s: carregando boletim por pregão...", _SCRIPT_NAME)
            boletim  = _LoadBoletim(conn, log)
            infoAtivos = _LoadInfoAtivos(conn)
            log.info("%s: %d ativos em Info Ativos", _SCRIPT_NAME, len(infoAtivos))
        finally:
            conn.close()

        diasBoletim = sorted(boletim.keys())
        dtStart = diario[0]["dt"]  if diario else date.today().isoformat()
        dtEnd   = diario[-1]["dt"] if diario else date.today().isoformat()

        html = _RenderHtml(diario, diarioIdx, anbimaIdx, anbimaRef, anbimaDur, ticker, duration,
                           boletim, diasBoletim, infoAtivos, dtStart, dtEnd)

        relDir = Path(cfg["paths"]["relatoriosDir"])
        relDir.mkdir(parents=True, exist_ok=True)
        outPath = relDir / "relatorio_secundario.html"
        outPath.write_text(html, encoding="utf-8")

        summary = _BuildSummary(diario, ticker)
        log.info("%s: HTML salvo em %s\n%s", _SCRIPT_NAME, outPath, summary)
        print(f"Arquivo: {outPath}\n{summary}".encode("ascii", errors="replace").decode())

        if args.email_dia:
            _EnviarEmailDia(args.email_dia, boletim, outPath, log)

    except Exception:
        success = False
        summary = traceback.format_exc()
        log.exception("%s: erro inesperado", _SCRIPT_NAME)

    finally:
        send_completion_email(_SCRIPT_NAME, success, summary, logger=log)


if __name__ == "__main__":
    Main()
