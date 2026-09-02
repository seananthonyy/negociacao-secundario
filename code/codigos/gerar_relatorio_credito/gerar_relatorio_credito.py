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

O pregão de HOJE entra, marcado como PRÉVIA (ver CalcularDtPrevia): ele é meio dia de
dado — a perna D+1 do pregão anterior já está completa, a perna do pregão em curso
ainda está entrando. O selo viaja no payload (`dtPrevia`) e aparece no banner do topo,
na opção do seletor do Boletim e no email do dia. `--sem-previa` volta ao corte em D-1.

CLI:
    python codigos/gerar_relatorio_credito/gerar_relatorio_credito.py
    python codigos/gerar_relatorio_credito/gerar_relatorio_credito.py --sem-previa
"""

import argparse
import json
import sys
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Helpers"))

from jinja2 import Environment, FileSystemLoader

from config import cfg, ObterListaEmails
import dados as D
from datas import DiaUtilAnteriorOuIgual, DiasUteisEntre, EhDiaUtil, Feriados
from email_outlook import EnviarEmailConclusao, EnviarEmailHtml
from relatorio_execucao import RelatorioExecucao
from logger import ObterLogger

NOME_SCRIPT = "gerar_relatorio_credito"

# ---------------------------------------------------------------------------
# Helpers compartilhados com gerar_relatorio_html.py
# ---------------------------------------------------------------------------

def CalcularDtPrevia() -> str | None:
    """A data de liquidação que AINDA NÃO FECHOU, ou None se hoje não é pregão.

    Uma `dtLiquidacao = D` recebe duas pernas: os negócios do pregão de D (D+0) e os
    do pregão de D-1u (D+1). Com D = hoje, a segunda perna está completa e a primeira
    está em curso — é meio dia de dado. Em fim de semana ou feriado não há pregão
    aberto, então nada é prévia.
    """
    hoje = date.today()
    return hoje.isoformat() if EhDiaUtil(hoje) else None


def CalcularDtCorte(incluirPrevia: bool = True) -> str:
    """Última data de liquidação publicável.

    Historicamente o corte era em D-1: o pregão de hoje não fechou, e publicar a
    liquidação de hoje mostrava um dia pela metade **como se fosse fechado**. O
    problema nunca foi o dado — era a ausência do aviso.

    Com `incluirPrevia` (default), o corte passa a ser o último dia útil <= hoje e a
    liquidação de hoje entra marcada como PRÉVIA: o selo viaja no payload
    (`dtPrevia`) e o template avisa em toda aba. `--sem-previa` volta ao corte em D-1,
    e `--ate` continua vencendo os dois.
    """
    hoje = date.today()
    d = hoje if incluirPrevia else hoje - timedelta(days=1)
    return DiaUtilAnteriorOuIgual(d).isoformat()


def MediaPonderada(valores: list[tuple[float | None, float]]) -> float | None:
    num = den = 0.0
    for v, w in valores:
        if v is not None:
            num += v * w
            den += w
    return num / den if den else None


def TipoExibicao(cdInstrumento: str | None, cdIndexador: str | None) -> str | None:
    """
    Tipo usado apenas para exibição/filtro. Debênture indexada a IPCA ou
    PREFIXADO é classificada como 'DEB 12.431' (incentivada). Demais
    instrumentos inalterados.
    """
    if cdInstrumento == "DEB" and cdIndexador in ("IPCA", "PREFIXADO"):
        return "DEB 12.431"
    return cdInstrumento


def SpreadNaBanda(cdIndexador: str | None, vrSpread: float | None) -> bool:
    """
    Banda de sanidade do spread — APENAS para as médias AGREGADAS por indexador
    (Visão Mercado). NÃO altera a base nem as visões por-ticker (Por Ativo,
    Spread×Duration, Boletim): lá o usuário precisa enxergar o caso extremo.

    Erros de PU↔taxa upstream produzem spreads impossíveis (ex: CRA02400ECU a
    ~6,3 mi %) que detonam a média ponderada do indexador. A banda só decide se a
    linha ENTRA na média do spread — o volume é sempre somado integralmente.

    Faixas (calibradas sobre a distribuição real 09–19/06, ver vault/98 Backlog):
      CDI+ / IPCA / PREFIXADO  → |spread| <= 15%
      %CDI                     → 60% <= spread <= 180%  (multiplicador, bilateral)
      indexador desconhecido   → não filtra (o grupo '?' não é plotado mesmo)
    """
    if vrSpread is None:
        return False
    if cdIndexador == "%CDI":
        return 60.0 <= vrSpread <= 180.0
    if cdIndexador in ("CDI+", "IPCA", "PREFIXADO"):
        return abs(vrSpread) <= 15.0
    return True


# A partir de quantos pregoes de idade o puPar deixa de ser "referencia viva" e passa a
# ser REFERENCIA CONGELADA no relatorio.
#
# Um puPar so fica velho por dois motivos, e o limiar existe para separa-los:
#   - soluco: a API caiu num dia, ou a nossa calc falhou. Idade de 1-2 pregoes, deriva
#     de ~0,1% (o puPar acreta ~0,055%/dia util com o CDI em ~14,9%). Tolerado, e a
#     idade aparece discreta.
#   - o papel SAIU DO CADASTRO: o emissor se aproximou do default e as calculadoras
#     removeram o titulo. Ai a idade so cresce, e o mercado passa a negociar em cents on
#     the dollar contra essa referencia fixa. E o que a mesa quer ver -- para esses nomes
#     taxa e spread somem junto com o cadastro, e o %par vira o UNICO numero disponivel.
#
# Cinco pregoes e o preco de nao confundir um com o outro. Nao ha status gravado em
# lugar nenhum: a IDADE e o status, e a tabela PuPar fica so com valor real.
PREGOES_CONGELADO = 5


def PregoesEntre(dtInicio: str, dtFim: str) -> int:
    """Quantos pregoes separam duas datas ISO. 0 se forem a mesma."""
    if not dtInicio or not dtFim or dtInicio >= dtFim:
        return 0
    return len(DiasUteisEntre(dtInicio, dtFim)) - 1


def CalcularPctPar(vrPU: float | None, vrPuPar: float | None) -> float | None:
    """%par de um negocio: o preco pago como percentual do PU par.

    NAO e gravado na base -- sai daqui, na leitura, a partir do vrPU do negocio e do
    vrPuPar vigente. Se o puPar de um ativo for descartado (correcao de cadastro ou de
    fluxo, ver dados.DescartarPuPar), o %par some junto, em vez de sobrar um numero
    calculado sobre um denominador em que ninguem mais acredita."""
    if vrPU is None or not vrPuPar or vrPuPar <= 0:
        return None
    return vrPU / vrPuPar * 100.0


@dataclass
class LinhaNegocio:
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
    vrPU: float | None = None
    vrPuPar: float | None = None
    dtPuPar: str | None = None
    nrTrades: int = 1


# ---------------------------------------------------------------------------
# SQL — Por Ticker / Duration (sem filtro de período)
# ---------------------------------------------------------------------------

# NOTA (12/08/2026) — a Visão Mercado NÃO tem mais SQL próprio.
# Ela era alimentada por SQL_DIARIO/SQL_DIARIO_IDX, que filtravam
# `cdStatus = 'VALIDO'` e por isso ignoravam todo o volume BROKER — divergindo
# do Boletim Diário em ~35% do volume da base. Agora `DerivarDiario()` deriva a
# aba do MESMO dado do Boletim (VALIDO + grupos BROKER agregados por
# `idGrupoNegocio`, volume/2), o que faz os totais baterem por construção em vez
# de por coincidência. Ver vault/09 - Progresso.

SQL_TICKER = """
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
  AND t.dtLiquidacao <= ?
GROUP BY t.dtLiquidacao, t.cdTicker, ia.cdEmissor, ia.cdIndexador
ORDER BY t.dtLiquidacao, t.cdTicker
"""

SQL_DURATION = """
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
  AND t.dtLiquidacao <= ?
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
# FONTE DO PESO — escolhida UMA vez por relatório, em EscolherFontePeso():
#
#   `Outstanding` populada  → outstanding real (PC do banco, via Bloomberg).
#   `Outstanding` vazia     → `InfoAtivos.vrQuantidadeEmissao` como PROXY
#                             (PC pessoal, sem terminal Bloomberg).
#
# A escolha é GLOBAL de propósito: outstanding e quantidade de emissão têm
# escalas diferentes, e misturá-las na mesma média ponderada corromperia o
# resultado em silêncio. Nunca há fallback por ativo — ou tudo é outstanding,
# ou tudo é emissão, e o relatório diz qual foi (disclaimer na aba).
#
# Os dois CTEs expõem a MESMA forma (cdTicker, dtPeso, vrPeso), então os três
# SQLs abaixo não mudam. A emissão é constante por ticker: o CROSS JOIN a
# expande contra as datas da Anbima só para casar o `p.dtPeso = ai.dtReferencia`.
PESO_CTE_OUTSTANDING = """
Peso AS (
    SELECT cdTicker, dtOutstanding AS dtPeso, vrOutstanding AS vrPeso
    FROM Outstanding
    WHERE vrOutstanding IS NOT NULL AND vrOutstanding > 0
)
"""

PESO_CTE_EMISSAO = """
Peso AS (
    SELECT ia.cdTicker, d.dtReferencia AS dtPeso, ia.vrQuantidadeEmissao AS vrPeso
    FROM InfoAtivos ia
    CROSS JOIN (SELECT DISTINCT dtReferencia FROM AnbimaIndicativos) d
    WHERE ia.vrQuantidadeEmissao IS NOT NULL AND ia.vrQuantidadeEmissao > 0
)
"""

# Linha POR TICKER (spread Anbima + peso) por (data, indexador). A agregação
# (média ponderada por quantidade de emissão) é feita no CLIENTE, só sobre os tickers
# que o usuário deixar selecionados no filtro manual por indexador da Visão Anbima —
# assim ele tira na mão os high-yield estressados sem filtro estatístico automático.
# População: ativos com peso > 0 (JOIN Peso).
SQL_ANBIMA_IDX = """
WITH {peso}
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
  AND ai.dtReferencia <= ?
ORDER BY ai.dtReferencia
"""

# Linha POR TICKER (duration + spread + taxa nominal Anbima) para os indexadores
# SEM vértice de referência (CDI+ e %CDI). Alimenta as "curvas por duration" da
# Visão Anbima: x = duration do ativo, y = spread/nominal, uma curva por data.
# Mesma população (Peso > 0); exige duration conhecida.
SQL_ANBIMA_DUR = """
WITH {peso}
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
  AND ai.dtReferencia <= ?
  AND ia.cdIndexador IN ('CDI+','%CDI')
  AND ia.vrDuration IS NOT NULL
ORDER BY ai.dtReferencia, ia.vrDuration
"""

# Linha POR TICKER (spread + taxa nominal Anbima + tipo de instrumento) por
# (data, referência NTN-B / DI1). A mediana por vértice é feita no CLIENTE, filtrável
# por tipo de instrumento (DEB/DEB 12.431/CRI/CRA). Mesma população do SQL_ANBIMA_IDX.
SQL_ANBIMA_REF = """
WITH {peso}
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
  AND ai.dtReferencia <= ?
  AND (ia.cdReferencia LIKE 'NTN-B%' OR ia.cdReferencia LIKE 'DI1%')
ORDER BY ai.dtReferencia, ia.cdReferencia
"""

# ---------------------------------------------------------------------------
# SQL — Boletim Diário
# ---------------------------------------------------------------------------

SQL_DATAS_BOLETIM = """
SELECT DISTINCT dtLiquidacao
FROM NegociosProcessados
WHERE cdStatus IN ('VALIDO', 'BROKER')
  AND dtLiquidacao <= ?
ORDER BY dtLiquidacao
"""

# Anbima casado por dtNegocio: cada trade compara com a indicativa mais recente
# com dtReferencia <= o próprio dtNegocio do trade (reflete o mercado no momento
# em que o negócio foi fechado). Simétrico ao MtM de calc_spread_over.
SQL_BUSCAR_VALIDO = f"""
WITH AnbimaMatch AS (
    SELECT tp.cdIdentificadorNegocio AS cdIdentificadorNegocio, ai.vrTaxaAnbima, ai.vrSpreadAnbima,
           ROW_NUMBER() OVER (PARTITION BY tp.cdIdentificadorNegocio ORDER BY ai.dtReferencia DESC) AS rn
    FROM NegociosProcessados tp
    JOIN AnbimaIndicativos ai
      ON ai.cdTicker = tp.cdTicker AND ai.dtReferencia <= tp.dtNegocio
    WHERE tp.dtLiquidacao = ? AND tp.cdStatus = 'VALIDO'
),
PuParVigente AS (
    -- O puPar VIGENTE de cada ticker para esta liquidacao: o mais recente com
    -- dtReferencia <= a data do boletim. Mesmo padrao do AnbimaMatch acima.
    --
    -- Particiona por cdTicker e nao por negocio: todos os negocios do mesmo ticker
    -- nesta mesma dtLiquidacao compartilham o denominador, entao resolver por ticker
    -- devolve uma linha em vez de uma por negocio -- e a consulta nao cresce com o
    -- historico de PuPar.
    --
    -- O `<=` (em vez de `=`) e o que atende o papel que SAIU DO CADASTRO das
    -- calculadoras: quando o emissor se aproxima do default elas removem o titulo, e o
    -- mercado passa a usar o ultimo puPar disponivel como referencia, negociando em
    -- cents on the dollar. Aqui isso acontece sozinho -- nao ha linha nova para a data,
    -- entao vem a ultima que existe. Quem diz se a referencia esta viva ou congelada e
    -- a IDADE dela (dtPuPar), calculada em pregoes e exibida sempre. Ver PREGOES_CONGELADO.
    SELECT cdTicker, vrPuPar, dtReferencia AS dtPuPar, cdFontePuPar,
           ROW_NUMBER() OVER (PARTITION BY cdTicker ORDER BY dtReferencia DESC) AS rn
    FROM PuPar
    WHERE dtReferencia <= ?
)
SELECT tp.cdTicker, ia.cdEmissor, tr.cdInstrumento,
       tp.vrQuantidade, tp.vrPU, tp.vrVolume, tp.vrTaxaCalculada, tp.vrSpreadOver,
       ia.cdIndexador, ia.cdReferencia, ia.vrDuration, ia.dtVencimento,
       am.vrTaxaAnbima, am.vrSpreadAnbima,
       pv.vrPuPar, pv.dtPuPar, pv.cdFontePuPar
FROM NegociosProcessados tp
JOIN NegociosBrutos tr ON tr.cdIdentificadorNegocio = tp.cdIdentificadorNegocio
LEFT JOIN InfoAtivos ia ON ia.cdTicker = tp.cdTicker
LEFT JOIN AnbimaMatch am ON am.cdIdentificadorNegocio = tp.cdIdentificadorNegocio AND am.rn = 1
LEFT JOIN PuParVigente pv ON pv.cdTicker = tp.cdTicker AND pv.rn = 1
WHERE tp.dtLiquidacao = ? AND tp.cdStatus = 'VALIDO' AND {D.NaoCancelado('tr.')}
"""

SQL_BUSCAR_BROKER = f"""
WITH AnbimaMatch AS (
    SELECT tp.cdIdentificadorNegocio AS cdIdentificadorNegocio, ai.vrTaxaAnbima, ai.vrSpreadAnbima,
           ROW_NUMBER() OVER (PARTITION BY tp.cdIdentificadorNegocio ORDER BY ai.dtReferencia DESC) AS rn
    FROM NegociosProcessados tp
    JOIN AnbimaIndicativos ai
      ON ai.cdTicker = tp.cdTicker AND ai.dtReferencia <= tp.dtNegocio
    WHERE tp.dtLiquidacao = ? AND tp.cdStatus = 'BROKER'
),
PuParVigente AS (
    -- O puPar VIGENTE de cada ticker para esta liquidacao: o mais recente com
    -- dtReferencia <= a data do boletim. Mesmo padrao do AnbimaMatch acima.
    --
    -- Particiona por cdTicker e nao por negocio: todos os negocios do mesmo ticker
    -- nesta mesma dtLiquidacao compartilham o denominador, entao resolver por ticker
    -- devolve uma linha em vez de uma por negocio -- e a consulta nao cresce com o
    -- historico de PuPar.
    --
    -- O `<=` (em vez de `=`) e o que atende o papel que SAIU DO CADASTRO das
    -- calculadoras: quando o emissor se aproxima do default elas removem o titulo, e o
    -- mercado passa a usar o ultimo puPar disponivel como referencia, negociando em
    -- cents on the dollar. Aqui isso acontece sozinho -- nao ha linha nova para a data,
    -- entao vem a ultima que existe. Quem diz se a referencia esta viva ou congelada e
    -- a IDADE dela (dtPuPar), calculada em pregoes e exibida sempre. Ver PREGOES_CONGELADO.
    SELECT cdTicker, vrPuPar, dtReferencia AS dtPuPar, cdFontePuPar,
           ROW_NUMBER() OVER (PARTITION BY cdTicker ORDER BY dtReferencia DESC) AS rn
    FROM PuPar
    WHERE dtReferencia <= ?
)
SELECT tp.cdTicker, ia.cdEmissor, tr.cdInstrumento, tp.idGrupoNegocio, tp.dtNegocio,
       tp.vrQuantidade, tp.vrPU, tp.vrVolume, tp.vrTaxaCalculada,
       ia.cdIndexador, ia.cdReferencia, ia.vrDuration, ia.dtVencimento,
       am.vrTaxaAnbima, am.vrSpreadAnbima,
       pv.vrPuPar, pv.dtPuPar, pv.cdFontePuPar
FROM NegociosProcessados tp
JOIN NegociosBrutos tr ON tr.cdIdentificadorNegocio = tp.cdIdentificadorNegocio
LEFT JOIN InfoAtivos ia ON ia.cdTicker = tp.cdTicker
LEFT JOIN AnbimaMatch am ON am.cdIdentificadorNegocio = tp.cdIdentificadorNegocio AND am.rn = 1
LEFT JOIN PuParVigente pv ON pv.cdTicker = tp.cdTicker AND pv.rn = 1
WHERE tp.dtLiquidacao = ? AND tp.cdStatus = 'BROKER'
  AND tp.idGrupoNegocio IS NOT NULL AND {D.NaoCancelado('tr.')}
"""

SQL_MTM_TAXA = "SELECT vrTaxa FROM MtmAnbima WHERE cdTicker = ? AND dtReferencia = ?"

# ---------------------------------------------------------------------------
# SQL — Info Ativos (uma linha por ticker negociado)
# ---------------------------------------------------------------------------

SQL_INFO_ATIVOS = """
WITH UltTrade AS (
    SELECT cdTicker, MAX(dtLiquidacao) AS dtUltimo
    FROM NegociosProcessados
    WHERE cdStatus = 'VALIDO'
      AND dtLiquidacao <= ?
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
      AND tp.dtLiquidacao <= ?
    GROUP BY tp.cdTicker, ut.dtUltimo
),
TaxaAnb AS (
    SELECT cdTicker, vrTaxaAnbima, dtReferencia FROM (
        SELECT cdTicker, vrTaxaAnbima, dtReferencia,
               ROW_NUMBER() OVER (PARTITION BY cdTicker ORDER BY dtReferencia DESC) AS rn
        FROM AnbimaIndicativos
        WHERE vrTaxaAnbima IS NOT NULL
          AND dtReferencia <= ?
    ) WHERE rn = 1
)
SELECT
    tt.cdTicker,
    COALESCE(ia.cdEmissor, '')     AS cdEmissor,
    COALESCE(ia.cdInstrumento, '') AS cdInstrumento,
    ia.vrDuration,
    COALESCE(ia.cdIndexador, '')   AS cdIndexador,
    ia.cdReferencia,
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

def EscolherFontePeso() -> tuple[str, str]:
    """Escolhe a fonte do peso da Visão Anbima: outstanding real quando a tabela
    `Outstanding` está populada (PC do banco, via Bloomberg); senão a quantidade
    de emissão como proxy. Escolha global — as escalas nunca se misturam."""
    temOutstanding = D.Escalar(
        "SELECT EXISTS(SELECT 1 FROM Outstanding WHERE vrOutstanding > 0)"
    )
    if temOutstanding:
        return PESO_CTE_OUTSTANDING, "outstanding"
    return PESO_CTE_EMISSAO, "emissao"


def DerivarDiario(boletim: dict) -> tuple[list[dict], list[dict]]:
    """
    Deriva os dados da Visão Mercado a partir do MESMO dado do Boletim Diário —
    isto é, VALIDO + grupos BROKER agregados por `idGrupoNegocio` (volume/2,
    taxa (max+min)/2), já consolidados por ticker em `CarregarBoletim`.

    Antes esta aba tinha SQL próprio filtrando `cdStatus = 'VALIDO'`, o que
    deixava de fora ~35% do volume da base (todo o BROKER) e fazia as duas abas
    do relatório mostrarem números diferentes para o mesmo pregão. Derivar do
    boletim faz os totais baterem por construção.

    Retorna `(diario, diarioIdx)`:
      diario     — 1 linha por pregão. `volume` inclui TODOS os ativos, também os
                   sem cadastro em InfoAtivos (indexador '?') — é o total que a
                   pill "Volume Total" exibe e tem de casar com o Boletim.
      diarioIdx  — 1 linha por (pregão, indexador), inclusive o balde '?'. O
                   template plota só os 4 indexadores classificados; o '?' entra
                   apenas no total (ver nota de rodapé da aba).

    Unidades preservadas do formato antigo: volume em R$ MM; `spreadBps` já em
    bps; `spreadRaw`/`spreadPctCdi` em % (o template multiplica por 100 nos
    não-%CDI).
    """
    diario: list[dict] = []
    diarioIdx: list[dict] = []

    for dt in sorted(boletim.keys()):
        tickers = boletim[dt]["tickers"]

        porIdx: dict[str, dict] = {}
        numBps = denBps = numCdi = denCdi = 0.0

        for t in tickers:
            cdIndexador = t["cdIndexador"]
            idx         = cdIndexador or "?"
            vrVolume    = t["vrVolumeTotal"] or 0.0
            vrSpread    = t["vrSpreadOverMedio"]

            acc = porIdx.setdefault(idx, {"volume": 0.0, "num": 0.0, "den": 0.0})
            acc["volume"] += vrVolume

            if not SpreadNaBanda(cdIndexador, vrSpread):
                continue
            acc["num"] += vrSpread * vrVolume
            acc["den"] += vrVolume
            if idx == "%CDI":
                numCdi += vrSpread * vrVolume
                denCdi += vrVolume
            else:
                numBps += vrSpread * vrVolume
                denBps += vrVolume

        diario.append({
            "dt":           dt,
            "volume":       round(sum(a["volume"] for a in porIdx.values()) / 1e6, 2),
            "spreadBps":    round(numBps / denBps * 100.0, 2) if denBps else None,
            "spreadPctCdi": round(numCdi / denCdi, 4)         if denCdi else None,
        })

        for idx in sorted(porIdx):
            acc = porIdx[idx]
            diarioIdx.append({
                "dt":        dt,
                "indexador": idx,
                "volume":    round(acc["volume"] / 1e6, 4),
                "spreadRaw": round(acc["num"] / acc["den"], 6) if acc["den"] else None,
            })

    return diario, diarioIdx


def CarregarAnbimaIdx(ctePeso: str, dtCorte: str) -> list[dict]:
    """Linha por ticker (spread Anbima + peso) por (dia, indexador). A média ponderada
    pelo peso é feita no cliente, sobre os tickers selecionados no filtro manual.
    spreadRaw em % para os não-%CDI (×100 = bps no template); direto para %CDI."""
    return [
        {
            "dt":        r[0],
            "indexador": r[1],
            "ticker":    r[2],
            "emissor":   r[3] or "",
            "tipo":      TipoExibicao(r[4], r[1]) or "—",
            "spreadRaw": round(r[5], 6) if r[5] is not None else None,
            "peso":      r[6],
        }
        for r in D.Tuplas(SQL_ANBIMA_IDX.format(peso=ctePeso), (dtCorte,))
    ]


def CarregarAnbimaDur(ctePeso: str, dtCorte: str) -> list[dict]:
    """Linha por ticker (duration + spread + taxa nominal Anbima) para CDI+ e %CDI.
    Alimenta as curvas por duration da Visão Anbima (x = duration, y = spread/nominal,
    uma curva por data). spreadRaw em % para CDI+ (×100 = bps no template); direto
    (multiplicador) para %CDI."""
    return [
        {
            "dt":        r[0],
            "indexador": r[1],
            "ticker":    r[2],
            "tipo":      TipoExibicao(r[3], r[1]) or "—",
            "duration":  round(r[4], 4) if r[4] is not None else None,
            "spreadRaw": round(r[5], 6) if r[5] is not None else None,
            "taxa":      round(r[6], 6) if r[6] is not None else None,
        }
        for r in D.Tuplas(SQL_ANBIMA_DUR.format(peso=ctePeso), (dtCorte,))
    ]


def CarregarAnbimaRef(ctePeso: str, dtCorte: str) -> list[dict]:
    """Linha por ticker (spread + taxa nominal Anbima + tipo de instrumento) por
    (dia, ref NTN-B/DI1). A mediana por vértice é feita no cliente (robusta a outlier),
    filtrável por tipo de instrumento (DEB/DEB 12.431/CRI/CRA)."""
    return [
        {
            "dt":        r[0],
            "ref":       r[1],
            "ticker":    r[2],
            "tipo":      TipoExibicao(r[3], r[4]) or "—",
            "spreadRaw": round(r[5], 6) if r[5] is not None else None,
            "taxa":      round(r[6], 6) if r[6] is not None else None,
        }
        for r in D.Tuplas(SQL_ANBIMA_REF.format(peso=ctePeso), (dtCorte,))
    ]


def CarregarTicker(dtCorte: str) -> list[dict]:
    rows = D.Tuplas(SQL_TICKER, (dtCorte,))
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


def CarregarDuration(dtCorte: str) -> list[dict]:
    rows = D.Tuplas(SQL_DURATION, (dtCorte,))
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


def CarregarInfoAtivos(dtCorte: str) -> list[dict]:
    """Uma linha por ticker negociado: cadastro + taxa Anbima e taxa de trade mais recentes."""
    rows = D.Linhas(SQL_INFO_ATIVOS, (dtCorte, dtCorte, dtCorte))
    return [
        {
            "ticker":     r["cdTicker"],
            "emissor":    r["cdEmissor"] or "",
            "tipo":       TipoExibicao(r["cdInstrumento"], r["cdIndexador"]) or "",
            "duration":   round(r["vrDuration"], 4)  if r["vrDuration"]  is not None else None,
            "indexador":  r["cdIndexador"] or "",
            "referencia": r["cdReferencia"] or "",
            "taxaAnbima": round(r["vrTaxaAnbima"], 4) if r["vrTaxaAnbima"] is not None else None,
            "dtAnbima":   r["dtAnbima"],
            "taxaTrade":  round(r["vrTaxaTrade"], 6)  if r["vrTaxaTrade"]  is not None else None,
            "dtTrade":    r["dtTrade"],
        }
        for r in rows
    ]


def TickersPorVolume(tickerRows: list[dict]) -> list[str]:
    vol: dict[str, float] = {}
    for r in tickerRows:
        vol[r["ticker"]] = vol.get(r["ticker"], 0.0) + r["volume"]
    return [t for t, _ in sorted(vol.items(), key=lambda x: -x[1])]


# ---------------------------------------------------------------------------
# Boletim Diário — agrega por pregão, replica lógica de gerar_relatorio_html
# ---------------------------------------------------------------------------

def BuscarNegocios(dtLiquidacao: str) -> list[LinhaNegocio]:
    rows = D.Linhas(SQL_BUSCAR_VALIDO, (dtLiquidacao, dtLiquidacao, dtLiquidacao))
    return [
        LinhaNegocio(
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
            vrPU=r["vrPU"],
            vrPuPar=r["vrPuPar"],
            dtPuPar=r["dtPuPar"],
        )
        for r in rows
    ]


def BuscarGruposBroker(dtLiquidacao: str) -> list[LinhaNegocio]:
    rows = D.Linhas(SQL_BUSCAR_BROKER, (dtLiquidacao, dtLiquidacao, dtLiquidacao))
    if not rows:
        return []
    grupos: dict[str, list] = {}
    for r in rows:
        grupos.setdefault(r["idGrupoNegocio"], []).append(r)
    result: list[LinhaNegocio] = []
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
            mtm = D.Linha(SQL_MTM_TAXA, (cdReferencia, rep["dtNegocio"]))
            if mtm:
                vrSpreadOver = ((1 + taxaMedia / 100) / (1 + mtm["vrTaxa"] / 100) - 1) * 100
        result.append(LinhaNegocio(
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
            # O par corretor e o MESMO papel na MESMA data, entao as duas pernas
            # dividem o mesmo puPar; so o PU difere. Media do PU pelas duas pontas, do
            # mesmo jeito que a taxa do grupo — o %par sai disso na agregacao.
            vrPU=MediaPonderada([(t["vrPU"], t["vrVolume"]) for t in trades]),
            vrPuPar=rep["vrPuPar"],
            dtPuPar=rep["dtPuPar"],
            nrTrades=len(trades),
        ))
    return result


def AgregarTicker(cdTicker: str, grupo: list[LinhaNegocio], dtBoletim: str) -> dict:
    p            = grupo[0]
    vrVolumeTotal  = sum(t.vrVolume for t in grupo)
    vrTaxaMedia    = MediaPonderada([(t.vrTaxaCalculada, t.vrVolume) for t in grupo])
    vrSpreadOver   = MediaPonderada([(t.vrSpreadOver,    t.vrVolume) for t in grupo])
    # Anbima também ponderado por volume: trades do mesmo ticker podem ter
    # dtNegocio (logo indicativa Anbima) diferentes dentro da mesma liquidação.
    vrTaxaAnbima   = MediaPonderada([(t.vrTaxaAnbima,   t.vrVolume) for t in grupo])
    vrSpreadAnbima = MediaPonderada([(t.vrSpreadAnbima, t.vrVolume) for t in grupo])
    # O %par sai do PU MEDIO ponderado, e nao da media dos %par de cada negocio. Da no
    # mesmo numero (o denominador e constante dentro do ticker-dia), mas por este
    # caminho o puPar aparece uma vez so — e e ele que carrega a data que o relatorio
    # precisa exibir. Ponderar por volume, e nao pela media simples dos tickets, e a
    # mesma regra das taxas: interessa onde o DINHEIRO negociou.
    vrPuMedio      = MediaPonderada([(t.vrPU,           t.vrVolume) for t in grupo])
    vrPuPar        = next((t.vrPuPar for t in grupo if t.vrPuPar), None)
    dtPuPar        = next((t.dtPuPar for t in grupo if t.vrPuPar), None)
    vrPctPar       = CalcularPctPar(vrPuMedio, vrPuPar)
    nrPregoes      = PregoesEntre(dtPuPar, dtBoletim) if dtPuPar else 0
    return {
        "cdTicker":          cdTicker,
        "cdEmissor":         p.cdEmissor,
        "cdInstrumento":     p.cdInstrumento or "—",
        "cdTipo":            TipoExibicao(p.cdInstrumento, p.cdIndexador) or "—",
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
        "vrPctPar":          round(vrPctPar, 2)       if vrPctPar       is not None else None,
        # Idade da referencia, em pregoes, e o selo de congelada. Viajam prontos para o
        # template: ele nao tem calendario de feriados para contar dia util.
        "dtPuPar":           dtPuPar,
        "nrPregoesPuPar":    nrPregoes,
        "stPuParCongelado":  1 if nrPregoes >= PREGOES_CONGELADO else 0,
    }


def CarregarBoletim(log, dtCorte: str) -> dict:
    """Retorna {dtLiquidacao: {tickers, resumo, totais}} para os pregões até dtCorte."""
    datas     = [r[0] for r in D.Tuplas(SQL_DATAS_BOLETIM, (dtCorte,))]
    ordemInstr = {"DEB": 0, "DEB 12.431": 1, "CRI": 2, "CRA": 3}
    boletim: dict[str, dict] = {}

    for dt in datas:
        linhas   = BuscarNegocios(dt)
        linhas.extend(BuscarGruposBroker(dt))
        if not linhas:
            continue

        gruposTicker: dict[str, list] = {}
        for ln in linhas:
            gruposTicker.setdefault(ln.cdTicker, []).append(ln)

        tickers = sorted(
            [AgregarTicker(tk, grp, dt) for tk, grp in gruposTicker.items()],
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
            "nrCongelados":  sum(1 for t in tickers if t["stPuParCongelado"]),
        }
        log.info("  boletim %s: %d tickers | R$ %.2f MM", dt, len(tickers), vrVolumeTotal / 1e6)

    return boletim


# ---------------------------------------------------------------------------
# Email — top 20 trades (por volume) de um pregão (data de liquidação)
# ---------------------------------------------------------------------------

# Destinatários do rascunho do relatório do dia: resolvidos em runtime via
# config.ObterListaEmails("destinatarios") (destinatarios.py → var EMAIL_DESTINATARIOS).

# Paleta Itaú (mesma do template relatorio_secundario.html)
ITAU_NAVY   = "#003087"
ITAU_NAVY2  = "#002370"
ITAU_ORANGE = "#EC7000"
ITAU_LITE   = "#fff3e8"


def FmtBr(v: float | None, dec: int) -> str:
    """Número no padrão BR (milhar '.', decimal ','). None → travessão."""
    if v is None:
        return "—"
    s = f"{v:,.{dec}f}"                       # ex: 1,234.56 (US)
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def DataBr(iso: str) -> str:
    """YYYY-MM-DD → DD/MM/AAAA."""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return iso


def RefDisplayEmail(ref: str | None) -> str:
    """NTN-B 35 → B35+; demais inalterados (espelha bRefDisp do template)."""
    if not ref:
        return "—"
    if ref.startswith("NTN-B"):
        return "B" + ref.replace("NTN-B", "").replace(" ", "") + "+"
    return ref


def PctParEmail(ticker: dict) -> str:
    """%par para o email: numero, e um asterisco quando a referencia esta congelada.

    O email vira print no celular de alguem, entao o aviso tem de caber na propria
    celula — o rodape explica o asterisco. Ver PREGOES_CONGELADO."""
    if ticker["vrPctPar"] is None:
        return "—"
    marca = " *" if ticker["stPuParCongelado"] else ""
    return FmtBr(ticker["vrPctPar"], 2) + marca


def SpreadEmail(v: float | None, cdIndexador: str | None) -> str:
    """%CDI → multiplicador direto; demais → ×100 = bps (espelha bSpreadDisp)."""
    if v is None:
        return "—"
    if cdIndexador == "%CDI":
        return FmtBr(v, 0)
    return FmtBr(v * 100, 0) + " bps"


def MontarEmailHtml(dtX: str, top: list[dict], diaInfo: dict, ehPrevia: bool = False) -> str:
    """Tabela HTML formatada (tons Itaú) com os top 20 ativos por volume do pregão.

    `ehPrevia` marca o email quando dtX é o pregão de hoje, que ainda não fechou — o
    email sai da máquina e vira print no celular de alguém, então o aviso tem de estar
    no corpo, não só no relatório em anexo."""
    cols = [
        ("Instrumento",          "left"),
        ("Ticker",               "left"),
        ("Emissor",              "left"),
        ("Volume Negociado",     "right"),
        ("% Par",                "right"),
        ("Indexador",            "left"),
        ("Duration",             "right"),
        ("Ref",                  "left"),
        ("Taxa Negócio Média",   "right"),
        ("Spread Negócio Média", "right"),
        ("Taxa Anbima Média",    "right"),
        ("Spread Anbima Média",  "right"),
    ]
    thStyle = (
        f"background:{ITAU_NAVY};color:#fff;font-size:11px;font-weight:700;"
        "text-transform:uppercase;letter-spacing:.4px;padding:8px 10px;"
        "border-bottom:2px solid " + ITAU_ORANGE + ";white-space:nowrap;text-align:"
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
            (FmtBr(vol / 1e6 if vol is not None else None, 2) + " MM",                "right"),
            (PctParEmail(t),                                                         "right"),
            (idx or "—",                                                               "left"),
            (FmtBr(t["vrDuration"], 2),                                               "right"),
            (RefDisplayEmail(t["cdReferencia"]),                                                "left"),
            ((FmtBr(t["vrTaxaMedia"], 2) + "%") if t["vrTaxaMedia"] is not None else "—",       "right"),
            (SpreadEmail(t["vrSpreadOverMedio"], idx),                                "right"),
            ((FmtBr(t["vrTaxaAnbima"], 2) + "%") if t["vrTaxaAnbima"] is not None else "—",      "right"),
            (SpreadEmail(t["vrSpreadAnbima"], idx),                                   "right"),
        ]
        tds = "".join(
            f'<td style="padding:7px 10px;font-size:12px;color:#222;'
            f'border-bottom:1px solid #e6e9ef;text-align:{align};'
            f'{"font-family:Consolas,monospace;font-weight:600;color:" + ITAU_NAVY + ";" if j == 1 else ""}'
            f'white-space:nowrap;">{val}</td>'
            for j, (val, align) in enumerate(cells)
        )
        bodyRows.append(f'<tr style="background:{bg};">{tds}</tr>')

    nrTickers = diaInfo["nrTickers"]
    volTotMM  = (diaInfo["vrVolumeTotal"] or 0) / 1e6

    avisoPrevia = f"""
    <div style="background:#fff8e1;border-left:4px solid #c47f00;padding:10px 16px;
                font-size:12px;color:#6d4c00;">
      <b>PRÉVIA — o pregão de {DataBr(dtX)} ainda não fechou.</b> Estes números são
      parciais: a liquidação de hoje já contém a perna D+1 do pregão anterior, mas a
      perna do pregão em curso ainda está entrando. Volume e média de taxa vão subir
      até o fechamento. Para o dado consolidado, use o pregão anterior.
    </div>""" if ehPrevia else ""

    return f"""\
<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#eef0f3;font-family:Segoe UI,Arial,sans-serif;">
  <div style="max-width:1100px;margin:0 auto;padding:18px;">
    <div style="background:{ITAU_NAVY};color:#fff;padding:16px 22px;border-radius:6px 6px 0 0;">
      <div style="font-size:17px;font-weight:700;letter-spacing:.3px;">
        Relatório Diário de Negociação — Crédito Privado
      </div>
      <div style="font-size:12px;color:#cdd6ea;margin-top:3px;">
        Top 20 ativos por <b style="color:#fff;">volume negociado</b> ·
        data de <b style="color:#fff;">liquidação {DataBr(dtX)}</b>{
        ' · <b style="color:#ffcf6b;">PRÉVIA</b>' if ehPrevia else ''}
      </div>
    </div>{avisoPrevia}
    <div style="background:{ITAU_LITE};border-left:4px solid {ITAU_ORANGE};
                padding:10px 16px;font-size:12px;color:{ITAU_NAVY};">
      Ranking pelos 20 maiores volumes negociados no pregão de liquidação
      <b>{DataBr(dtX)}</b> ({nrTickers} ativos no dia · R$ {FmtBr(volTotMM, 2)} MM no total).
      O relatório completo e interativo (todas as abas e pregões) segue em anexo
      (<b>relatorio_secundario.html</b>). Taxas em % a.a.; spreads em bps
      (exceto %&nbsp;CDI, que é multiplicador). <b>% Par</b> = preço ÷ PU par × 100
      (100 = no par, acima é ágio, abaixo é deságio); travessão onde não há PU par de
      nenhuma fonte. <b>*</b> = o PU par usado é de pregão anterior. Em poucos papéis com
      datas distintas, é o caso real: o título saiu do cadastro das calculadoras (emissor
      perto do default) e o mercado passa a medir contra o último PU par conhecido, em
      <i>cents on the dollar</i> — aí taxa e spread também não existem. Em muitos papéis
      com a mesma data, é só o <code>calc_pu_par</code> que não rodou para o dia.
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


def EnviarEmailDia(dtX: str, boletim: dict, htmlPath: Path, log,
                   dtPrevia: str | None = None) -> None:
    """Monta e envia o email do dia X (top 20 por volume) com o HTML em anexo."""
    try:
        date.fromisoformat(dtX)
    except ValueError:
        log.error("%s: --email-dia invalido (use YYYY-MM-DD): %s", NOME_SCRIPT, dtX)
        return

    diaInfo = boletim.get(dtX)
    if not diaInfo or not diaInfo.get("tickers"):
        log.warning("%s: sem negocios para liquidacao %s — email do dia nao enviado.",
                    NOME_SCRIPT, dtX)
        return

    destinatarios = ObterListaEmails("destinatarios")
    if not destinatarios:
        log.warning("%s: sem destinatarios (destinatarios.py / EMAIL_DESTINATARIOS) — rascunho nao salvo.",
                    NOME_SCRIPT)
        return

    ehPrevia = dtX == dtPrevia
    top = sorted(diaInfo["tickers"], key=lambda t: -(t["vrVolumeTotal"] or 0))[:20]
    html = MontarEmailHtml(dtX, top, diaInfo, ehPrevia)
    subject = (f"[PRÉVIA] " if ehPrevia else "") + \
              f"Relatório Crédito Privado — Top 20 volume · liquidação {DataBr(dtX)}"
    EnviarEmailHtml(
        subject, html,
        attachments=[str(htmlPath)],
        logger=log,
        to=destinatarios,
        draft=True,
    )
    log.info("%s: rascunho do dia %s salvo (top %d de %d ativos) para %s",
             NOME_SCRIPT, dtX, len(top), diaInfo["nrTickers"], "; ".join(destinatarios))


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def RenderizarHtml(
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
    pesoFonte:   str,
    dtPrevia:    str | None,
) -> str:
    tickers = TickersPorVolume(ticker)
    datas   = sorted({r["dt"] for r in ticker})

    payload = {
        "dtStart":     dtStart,
        "dtEnd":       dtEnd,
        "pesoFonte":   pesoFonte,
        "dtPrevia":    dtPrevia,
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
        "feriados":    sorted(d.isoformat() for d in Feriados()),
    }

    templatesDir = Path(cfg["paths"].get("templatesDir", "templates"))
    env  = Environment(loader=FileSystemLoader(str(templatesDir)))
    tmpl = env.get_template("relatorio_secundario.html")
    return tmpl.render(
        data_json=json.dumps(payload, ensure_ascii=False),
        dtStart=dtStart,
        dtEnd=dtEnd,
        pesoFonte=pesoFonte,
        dtPrevia=dtPrevia,
    )


def MontarResumo(diario: list[dict], ticker: list[dict], dtPrevia: str | None = None) -> str:
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
    if dtPrevia and dtPrevia == dtEnd:
        volPrevia = next((r["volume"] for r in diario if r["dt"] == dtPrevia), 0.0)
        lines.append(f"PRÉVIA   : {dtPrevia} — pregão em curso, R$ {volPrevia:,.2f} MM "
                     "parciais no total acima")
    elif dtPrevia:
        # O pregão de hoje está no corte mas não tem negócio na base ainda (rodada da
        # manhã, antes do boletim do dia). Dizer isso evita a leitura de que sumiu.
        lines.append(f"PRÉVIA   : {dtPrevia} — sem negócio na base ainda")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI / Main
# ---------------------------------------------------------------------------

def LerArgumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gera relatório HTML interativo de crédito privado (todos os dados da base)."
    )
    parser.add_argument(
        "--email-dia",
        metavar="YYYY-MM-DD",
        default=None,
        dest="emailDia",
        help="Envia email com o top 20 ativos por volume da data de LIQUIDAÇÃO informada "
             "(corpo HTML formatado) e o relatório completo em anexo.",
    )
    parser.add_argument(
        "--ate",
        metavar="YYYY-MM-DD",
        default=None,
        dest="ate",
        help="Última data de LIQUIDAÇÃO a publicar. Default: hoje, se for pregão "
             "(entra marcada como PRÉVIA). Vence --sem-previa.",
    )
    parser.add_argument(
        "--sem-previa",
        action="store_true",
        dest="semPrevia",
        help="Volta ao corte em D-1: a liquidação de hoje fica FORA do relatório. "
             "Use quando o destino não puder exibir o selo de prévia (ex.: print "
             "colado num email de terceiro).",
    )
    return parser.parse_args()


def Principal() -> None:
    log     = ObterLogger(NOME_SCRIPT)
    args    = LerArgumentos()
    rel     = RelatorioExecucao(NOME_SCRIPT)
    erro    = None
    summary = ""
    success = True

    try:
        log.info("%s: iniciando", NOME_SCRIPT)

        dtCorte = args.ate or CalcularDtCorte(incluirPrevia=not args.semPrevia)
        # Só é prévia o pregão de hoje, e só se ele estiver DENTRO do corte: com --ate
        # ou --sem-previa apontando para trás, o que entra já fechou.
        dtPrevia = CalcularDtPrevia()
        if dtPrevia is not None and dtPrevia > dtCorte:
            dtPrevia = None
        log.info("%s: corte em dtLiquidacao <= %s%s", NOME_SCRIPT, dtCorte,
                 f" | PRÉVIA: {dtPrevia} (o pregão de hoje não fechou)" if dtPrevia
                 else " (só pregão fechado)")

        ctePeso, pesoFonte = EscolherFontePeso()
        log.info("%s: peso da Visão Anbima = %s", NOME_SCRIPT,
                 "outstanding real (tabela Outstanding)" if pesoFonte == "outstanding"
                 else "quantidade de emissão (PROXY — Outstanding vazia)")

        anbimaIdx = CarregarAnbimaIdx(ctePeso, dtCorte)
        anbimaRef = CarregarAnbimaRef(ctePeso, dtCorte)
        anbimaDur = CarregarAnbimaDur(ctePeso, dtCorte)
        ticker    = CarregarTicker(dtCorte)
        duration = CarregarDuration(dtCorte)
        log.info("%s: carregando boletim por pregão...", NOME_SCRIPT)
        boletim  = CarregarBoletim(log, dtCorte)
        # Visão Mercado deriva do boletim (VALIDO + BROKER) — ver DerivarDiario
        diario, diarioIdx = DerivarDiario(boletim)
        log.info("%s: %d pregões | %d ticker-dias | %d duration-rows",
                 NOME_SCRIPT, len(diario), len(ticker), len(duration))
        infoAtivos = CarregarInfoAtivos(dtCorte)
        log.info("%s: %d ativos em Info Ativos", NOME_SCRIPT, len(infoAtivos))

        diasBoletim = sorted(boletim.keys())
        dtStart = diario[0]["dt"]  if diario else date.today().isoformat()
        dtEnd   = diario[-1]["dt"] if diario else date.today().isoformat()

        html = RenderizarHtml(diario, diarioIdx, anbimaIdx, anbimaRef, anbimaDur, ticker, duration,
                           boletim, diasBoletim, infoAtivos, dtStart, dtEnd, pesoFonte, dtPrevia)

        relDir = Path(cfg["paths"]["relatoriosDir"])
        relDir.mkdir(parents=True, exist_ok=True)
        outPath = relDir / "relatorio_secundario.html"
        outPath.write_text(html, encoding="utf-8")

        summary = MontarResumo(diario, ticker, dtPrevia)
        log.info("%s: HTML salvo em %s\n%s", NOME_SCRIPT, outPath, summary)
        print(f"Arquivo: {outPath}\n{summary}".encode("ascii", errors="replace").decode())

        if args.emailDia:
            EnviarEmailDia(args.emailDia, boletim, outPath, log, dtPrevia)

    except Exception:
        success = False
        erro = traceback.format_exc()
        rel.Erro("A rodada abortou — ver traceback.")
        log.exception("%s: erro inesperado", NOME_SCRIPT)

    finally:
        if summary:
            rel.Secao("Resumo", ["saida"], [[l] for l in summary.splitlines() if l.strip()])
        EnviarEmailConclusao(NOME_SCRIPT, success, rel, tracebackErro=erro, logger=log)

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    Principal()
