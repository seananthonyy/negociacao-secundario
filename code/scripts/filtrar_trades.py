"""
filtrar_trades.py
=================
Aplica três filtros sequenciais em NegociosProcessados por dtLiquidacao,
atribuindo cdStatus e idGrupoNegocio. cdStatus assume quatro valores:

  VALIDO  — trade legítimo que aparece no relatório HTML. Inclui qualquer
             trade que não se enquadre nos critérios abaixo. Trades com
             vrTaxaCalculada = NULL são sempre VALIDO.

  FUNDO   — passagem de fundo em três sub-critérios:
             (a) 2+ negócios com exatamente o mesmo (cdTicker,
             dtLiquidacao, vrQuantidade, vrVolume) — todos FUNDO;
             (b) par com mesma (cdTicker, dtLiquidacao, vrQuantidade)
             cujo |PU_i - PU_j| <= fundoMaxReaisPorMilhao * PU_medio /
             1_000_000; ou
             (c) 1 bloco (qty=Q) + N splits (sum(qty)=Q, N>=2) onde
             |PU_split - PU_bloco| <= fundoMaxReaisPorMilhao *
             (PU_split+PU_bloco)/2 / 1_000_000 para cada split.
             Trades sem taxa também entram (PU = vrVolume/vrQuantidade).

  BROKER  — operação de corretagem em dois sub-casos:
             (a) par com mesma (cdTicker, dtLiquidacao, vrQuantidade) e
             diferença de taxa <= tolCorretor; ou
             (b) 1 bloco (qty=Q) + N splits (sum(qty)=Q, N>=2) com
             |taxa_split - taxa_bloco| <= tolCorretor para cada split.
             TODOS os trades do grupo recebem BROKER. No relatório são
             exibidos agregados por idGrupoNegocio: taxa=(max+min)/2,
             volume=sum/2, spread calculado da média.
             Trades com vrTaxaCalculada = NULL são excluídos.

  PF      — operação de pessoa física: par com mesma (cdTicker,
             dtLiquidacao, vrQuantidade) e diferença de taxa > tolPF.
             O trade mais próximo da taxa de referência (vrTaxaAnbima se
             disponível; senão mediana ponderada por volume dos trades
             não-FUNDO/não-BROKER após os filtros 1-2) recebe VALIDO; o
             outro recebe PF. Trades com vrTaxaCalculada = NULL excluídos.

Ordem de execução por data:
  1. FUNDO    — critério de PU; classifica ambas as pontas
  2. CORRETOR — entre os não-FUNDO; taxa; classifica em BROKER/VALIDO
  3. PF       — entre os não-pareados restantes; taxa e taxa de referência

Cada rodada recalcula do zero para a dtLiquidacao — idempotente.
Trades com cdSituacao = 'Cancelado' em NegociosBrutos são ignorados.

CLI:
    python scripts/filtrar_trades.py --date 2026-05-27
    python scripts/filtrar_trades.py --start 2026-05-01 --end 2026-05-27
    python scripts/filtrar_trades.py --date 2026-05-27 --fundo-max 80 --corretor-bps 1.0
"""

import argparse
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.config import cfg
from lib.db import ObterBanco
from lib.logger import ObterLogger
from lib.email_outlook import EnviarEmailConclusao


# ---------------------------------------------------------------------------
# Tipos internos
# ---------------------------------------------------------------------------

@dataclass
class Negocio:
    idTrade: int
    cdTicker: str
    dtLiquidacao: str
    vrQuantidade: int
    vrVolume: float
    vrPU: float                          # = vrVolume / vrQuantidade
    vrTaxaCalculada: float | None
    cdIndexador: str | None              # de InfoAtivos; None se não cadastrado
    cdStatus: str = 'VALIDO'
    idGrupoNegocio: str | None = None
    pareados: bool = field(default=False, repr=False)  # True após inclusão em qualquer grupo


@dataclass
class EstatisticasData:
    dtLiquidacao: str
    total: int = 0
    valido: int = 0
    fundo: int = 0
    broker: int = 0
    pf: int = 0
    nullTaxa: int = 0                    # trades VALIDO sem vrTaxaCalculada (sem dedup possível)


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

SQL_BUSCAR_NEGOCIOS = """
SELECT tp.idTrade, tp.cdTicker, tp.dtLiquidacao,
       tp.vrQuantidade, tp.vrVolume, tp.vrTaxaCalculada,
       ia.cdIndexador
FROM NegociosProcessados tp
JOIN NegociosBrutos tr ON tr.idTrade = tp.idTrade
LEFT JOIN InfoAtivos ia ON ia.cdTicker = tp.cdTicker
WHERE tp.dtLiquidacao = ?
  AND tr.cdSituacao != 'Cancelado'
"""

SQL_BUSCAR_ANBIMA = """
SELECT cdTicker, vrTaxaAnbima
FROM AnbimaIndicativos
WHERE dtReferencia = ?
  AND vrTaxaAnbima IS NOT NULL
"""

SQL_ATUALIZAR_STATUS = """
UPDATE NegociosProcessados
SET cdStatus     = ?,
    idGrupoNegocio = ?
WHERE idTrade = ?
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def EhPctCdi(cdIndexador: str | None, vrTaxaCalculada: float | None) -> bool:
    """True se o ativo é indexado a %CDI.
    Usa cdIndexador de InfoAtivos quando disponível; fallback: taxa > 70
    (CDI+ e IPCA+ ficam abaixo de 20, %CDI fica na faixa de 80-120).
    """
    if cdIndexador is not None:
        return cdIndexador == '%CDI'
    return vrTaxaCalculada is not None and vrTaxaCalculada > 70


def MedianaPonderadaVolume(trades: list) -> float | None:
    """Mediana ponderada por vrVolume da lista de trades com taxa não-NULL.
    Retorna None se a lista for vazia.
    """
    candidates = [t for t in trades if t.vrTaxaCalculada is not None]
    if not candidates:
        return None
    volumeTotal = sum(t.vrVolume for t in candidates)
    if volumeTotal <= 0:
        return candidates[len(candidates) // 2].vrTaxaCalculada
    tOrdenados = sorted(candidates, key=lambda t: t.vrTaxaCalculada)
    cumulative = 0.0
    for t in tOrdenados:
        cumulative += t.vrVolume
        if cumulative >= volumeTotal / 2:
            return t.vrTaxaCalculada
    return tOrdenados[-1].vrTaxaCalculada


# ---------------------------------------------------------------------------
# Filtro 1 — Passagem de Fundo
# ---------------------------------------------------------------------------

def AplicarFiltroFundo(
    trades: list[Negocio],
    fundoMaxReaisPorMilhao: float,
) -> int:
    """
    Identifica passagens de fundo por dois critérios (union-find transitivo):

    (a) Exatos: 2+ negócios com mesmo (cdTicker, vrQuantidade, vrVolume)
        → todos FUNDO, independente de threshold.
    (b) Threshold de PU: par com mesmo (cdTicker, vrQuantidade) cujo
        |PU_i - PU_j| <= fundoMaxReaisPorMilhao * PU_medio / 1_000_000.

    Trades sem vrTaxaCalculada participam pois PU = vrVolume / vrQuantidade existe.
    Retorna o número de trades marcados FUNDO.
    """
    n = len(trades)
    parent = list(range(n))

    def Achar(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def Unir(a: int, b: int) -> None:
        ra, rb = Achar(a), Achar(b)
        if ra != rb:
            parent[rb] = ra

    grupos: dict[tuple, list[int]] = {}
    for i, t in enumerate(trades):
        grupos.setdefault((t.cdTicker, t.vrQuantidade), []).append(i)

    for indices in grupos.values():
        if len(indices) < 2:
            continue

        # (a) Negócios exatamente iguais: mesmo PU → FUNDO direto
        exatos: dict[float, list[int]] = {}
        for idx in indices:
            exatos.setdefault(trades[idx].vrPU, []).append(idx)
        for same in exatos.values():
            if len(same) >= 2:
                for idx in same[1:]:
                    Unir(same[0], idx)

        # (b) Threshold de PU (critério original)
        for ii in range(len(indices)):
            for jj in range(ii + 1, len(indices)):
                i, j = indices[ii], indices[jj]
                ti, tj = trades[i], trades[j]
                puMedio = (ti.vrPU + tj.vrPU) / 2
                if puMedio <= 0:
                    continue
                threshold = fundoMaxReaisPorMilhao * puMedio / 1_000_000
                if abs(ti.vrPU - tj.vrPU) <= threshold:
                    Unir(i, j)

    componentes: dict[int, list[int]] = {}
    for i in range(n):
        componentes.setdefault(Achar(i), []).append(i)

    marcados = 0
    for membros in componentes.values():
        if len(membros) < 2:
            continue
        idGroup = str(uuid4())
        for idx in membros:
            trades[idx].cdStatus = 'FUNDO'
            trades[idx].idGrupoNegocio = idGroup
            trades[idx].pareados = True
            marcados += 1

    return marcados


def AplicarFiltroFundoSplits(
    trades: list[Negocio],
    fundoMaxReaisPorMilhao: float,
) -> int:
    """
    Pass (c) do filtro FUNDO: bloco + splits.
    1 trade bloco (qty=Q) + N splits (sum(qty)=Q, N >= 2) onde
    |PU_split - PU_bloco| <= fundoMaxReaisPorMilhao * (PU_split+PU_bloco)/2
    / 1_000_000 para cada split.
    Roda sobre trades ainda não-pareados após as passes (a) e (b).
    Todos os trades elegíveis (PU = vrVolume/vrQuantidade existe sempre).
    Retorna número de trades marcados FUNDO.
    """
    eligible = [(i, t) for i, t in enumerate(trades) if not t.pareados]
    if not eligible:
        return 0

    grupos: dict[str, list[tuple[int, Negocio]]] = {}
    for i, t in eligible:
        grupos.setdefault(t.cdTicker, []).append((i, t))

    nFundo = 0

    for membros in grupos.values():
        if len(membros) < 3:
            continue

        membrosOrdenados = sorted(membros, key=lambda x: x[1].vrQuantidade, reverse=True)
        usados: set[int] = set()

        for blocoI, blocoT in membrosOrdenados:
            if blocoI in usados:
                continue

            blocoQty = blocoT.vrQuantidade
            blocoPu  = blocoT.vrPU

            candidatos: list[tuple[int, int]] = sorted(
                [
                    (t.vrQuantidade, i)
                    for i, t in membrosOrdenados
                    if i != blocoI
                    and i not in usados
                    and t.vrQuantidade < blocoQty
                    and abs(t.vrPU - blocoPu)
                        <= fundoMaxReaisPorMilhao * (t.vrPU + blocoPu) / 2 / 1_000_000
                ],
                reverse=True,
            )

            if len(candidatos) < 2:
                continue

            idxSplits = SubsetSumDFS(candidatos, blocoQty)
            if idxSplits is None:
                continue

            idGroup = str(uuid4())
            for idx in [blocoI] + idxSplits:
                trades[idx].cdStatus = 'FUNDO'
                trades[idx].idGrupoNegocio = idGroup
                trades[idx].pareados = True
                usados.add(idx)
                nFundo += 1

    return nFundo


# ---------------------------------------------------------------------------
# Filtro 2 — Corretor
# ---------------------------------------------------------------------------

def AplicarFiltroCorretor(
    trades: list[Negocio],
    corretorMaxBps: float,
    corretorMaxPctCdi: float,
) -> int:
    """
    Entre os não-FUNDO com taxa não-NULL, identifica pares com mesma
    (cdTicker, vrQuantidade) e diferença de taxa <= tolCorretor:
      - não-%CDI: corretorMaxBps / 100 (p.p.)
      - %CDI:     corretorMaxPctCdi (ex: 0.15 → aceita 100 a 100.15)

    Dentro de cada componente union-find: menor vrVolume → VALIDO,
    todos os demais → BROKER. Mesmo idGrupoNegocio para todos do par.

    Retorna o número de trades marcados BROKER.
    """
    idxElegiveis = [i for i, t in enumerate(trades)
                    if not t.pareados and t.vrTaxaCalculada is not None]
    if not idxElegiveis:
        return 0

    parent = {i: i for i in idxElegiveis}

    def Achar(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def Unir(a: int, b: int) -> None:
        ra, rb = Achar(a), Achar(b)
        if ra != rb:
            parent[rb] = ra

    grupos: dict[tuple, list[int]] = {}
    for i in idxElegiveis:
        t = trades[i]
        grupos.setdefault((t.cdTicker, t.vrQuantidade), []).append(i)

    for indices in grupos.values():
        if len(indices) < 2:
            continue
        for ii in range(len(indices)):
            for jj in range(ii + 1, len(indices)):
                i, j = indices[ii], indices[jj]
                ti, tj = trades[i], trades[j]
                if EhPctCdi(ti.cdIndexador, ti.vrTaxaCalculada):
                    tol = corretorMaxPctCdi
                else:
                    tol = corretorMaxBps / 100.0
                if abs(ti.vrTaxaCalculada - tj.vrTaxaCalculada) <= tol:
                    Unir(i, j)

    componentes: dict[int, list[int]] = {}
    for i in idxElegiveis:
        componentes.setdefault(Achar(i), []).append(i)

    nBroker = 0
    for membros in componentes.values():
        if len(membros) < 2:
            continue
        idGroup = str(uuid4())
        for idx in membros:
            trades[idx].idGrupoNegocio = idGroup
            trades[idx].pareados = True
            trades[idx].cdStatus = 'BROKER'
            nBroker += 1

    return nBroker


def SubsetSumDFS(candidatos: list[tuple[int, int]], alvo: int) -> list[int] | None:
    """
    Busca subconjunto de candidatos (list de (qty, trade_idx), sorted desc por qty)
    cuja soma de qty seja exatamente alvo e tenha >= 2 elementos.
    Usa soma de sufixo para pruning. Retorna lista de trade_idx ou None.
    """
    n = len(candidatos)
    sufixo = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        sufixo[i] = sufixo[i + 1] + candidatos[i][0]

    resultado: list[int] = []

    def Dfs(pos: int, restante: int) -> bool:
        if restante == 0:
            return len(resultado) >= 2
        if pos >= n or sufixo[pos] < restante:
            return False
        qty, idx = candidatos[pos]
        if qty <= restante:
            resultado.append(idx)
            if Dfs(pos + 1, restante - qty):
                return True
            resultado.pop()
        return Dfs(pos + 1, restante)

    return list(resultado) if Dfs(0, alvo) else None


def AplicarFiltroCorretorSplits(
    trades: list[Negocio],
    corretorMaxBps: float,
    corretorMaxPctCdi: float,
) -> int:
    """
    Pass 2 do filtro corretor: bloco + splits.
    Para cada trade não-pareado com qty=Q (bloco), tenta encontrar N >= 2
    outros trades não-pareados com sum(qty) = Q e |taxa_split - taxa_bloco|
    <= tolCorretor para cada split. Blocos tentados do maior para o menor.
    Retorna número de trades marcados BROKER.
    """
    eligible = [(i, t) for i, t in enumerate(trades)
                if not t.pareados and t.vrTaxaCalculada is not None]
    if not eligible:
        return 0

    grupos: dict[str, list[tuple[int, Negocio]]] = {}
    for i, t in eligible:
        grupos.setdefault(t.cdTicker, []).append((i, t))

    nBroker = 0

    for membros in grupos.values():
        if len(membros) < 3:  # mínimo: 1 bloco + 2 splits
            continue

        membrosOrdenados = sorted(membros, key=lambda x: x[1].vrQuantidade, reverse=True)
        usados: set[int] = set()

        for blocoI, blocoT in membrosOrdenados:
            if blocoI in usados:
                continue

            blocoQty = blocoT.vrQuantidade
            blocoTaxa = blocoT.vrTaxaCalculada  # not None (eligible filter)
            if EhPctCdi(blocoT.cdIndexador, blocoTaxa):
                tol = corretorMaxPctCdi
            else:
                tol = corretorMaxBps / 100.0

            candidatos: list[tuple[int, int]] = sorted(
                [
                    (t.vrQuantidade, i)
                    for i, t in membrosOrdenados
                    if i != blocoI
                    and i not in usados
                    and t.vrQuantidade < blocoQty
                    and abs(t.vrTaxaCalculada - blocoTaxa) <= tol  # type: ignore[operator]
                ],
                reverse=True,
            )

            if len(candidatos) < 2:
                continue

            idxSplits = SubsetSumDFS(candidatos, blocoQty)
            if idxSplits is None:
                continue

            idGroup = str(uuid4())
            for idx in [blocoI] + idxSplits:
                trades[idx].cdStatus = 'BROKER'
                trades[idx].idGrupoNegocio = idGroup
                trades[idx].pareados = True
                usados.add(idx)
                nBroker += 1

    return nBroker


# ---------------------------------------------------------------------------
# Filtro 3 — Pessoa Física
# ---------------------------------------------------------------------------

def AplicarFiltroPF(
    trades: list[Negocio],
    pfMinBps: float,
    pfMinPctCdi: float,
    taxaAnbima: dict[str, float],
) -> int:
    """
    Entre os trades não-pareados (não-FUNDO, não-BROKER, não-VALIDO-de-par)
    com taxa não-NULL, identifica grupos com mesma (cdTicker, vrQuantidade)
    onde ao menos um par tem diferença de taxa > tolPF:
      - não-%CDI: pfMinBps / 100 (p.p.)
      - %CDI:     pfMinPctCdi (ex: 2.0 → aceita diferença máxima 2 p.p.)

    Elege o trade mais próximo da taxa de referência como VALIDO.
    Os demais mais que tolPF afastados do eleito recebem PF.
    Tiebreak de proximidade: menor vrVolume (consistente com critério CORRETOR).

    Taxa de referência por ticker (ordem de prioridade):
      1. vrTaxaAnbima de AnbimaIndicativos para a data (melhor, externa)
      2. Mediana ponderada por volume dos trades não-FUNDO/não-BROKER com taxa

    Retorna o número de trades marcados PF.
    """
    # Pool para mediana: não-FUNDO, não-BROKER, com taxa — inclui VALIDO-de-CORRETOR
    # e trades ainda não-classificados. Reflete o "válido até aqui".
    poolPorTicker: dict[str, list] = {}
    for t in trades:
        if t.cdStatus not in ('FUNDO', 'BROKER') and t.vrTaxaCalculada is not None:
            poolPorTicker.setdefault(t.cdTicker, []).append(t)

    taxaRef: dict[str, float] = {}
    for cdTicker, pool in poolPorTicker.items():
        if cdTicker in taxaAnbima:
            taxaRef[cdTicker] = taxaAnbima[cdTicker]
        else:
            mediana = MedianaPonderadaVolume(pool)
            if mediana is not None:
                taxaRef[cdTicker] = mediana

    # Elegíveis: não-pareados com taxa
    eligible: dict[tuple, list[int]] = {}
    for i, t in enumerate(trades):
        if not t.pareados and t.vrTaxaCalculada is not None:
            eligible.setdefault((t.cdTicker, t.vrQuantidade), []).append(i)

    nPf = 0
    for (cdTicker, _), indices in eligible.items():
        if len(indices) < 2:
            continue

        ref = taxaRef.get(cdTicker)
        if ref is None:
            continue  # sem referência disponível — não classifica PF, mantém VALIDO

        t0 = trades[indices[0]]
        tol = pfMinPctCdi if EhPctCdi(t0.cdIndexador, t0.vrTaxaCalculada) else pfMinBps / 100.0

        # Elege o mais próximo da referência; tiebreak por menor volume
        indices.sort(key=lambda i: (abs(trades[i].vrTaxaCalculada - ref), trades[i].vrVolume))
        idxVencedor = indices[0]
        winner = trades[idxVencedor]

        idGroup = str(uuid4())
        grupoPf = 0
        for idx in indices[1:]:
            t = trades[idx]
            if abs(t.vrTaxaCalculada - winner.vrTaxaCalculada) > tol:
                t.cdStatus = 'PF'
                t.idGrupoNegocio = idGroup
                t.pareados = True
                grupoPf += 1
                nPf += 1

        if grupoPf > 0:
            winner.idGrupoNegocio = idGroup
            winner.pareados = True

    return nPf


# ---------------------------------------------------------------------------
# Processamento por data
# ---------------------------------------------------------------------------

def ProcessarData(
    conn,
    dtLiquidacao: str,
    fundoMaxReaisPorMilhao: float,
    corretorMaxBps: float,
    corretorMaxPctCdi: float,
    pfMinBps: float,
    pfMinPctCdi: float,
    log,
) -> EstatisticasData:
    """
    Lê todos os trades válidos de uma dtLiquidacao, aplica os três filtros
    sequencialmente e faz UPDATE em NegociosProcessados com cdStatus e idGrupoNegocio.
    Retorna _DateStats com os contadores.
    """
    stats = EstatisticasData(dtLiquidacao=dtLiquidacao)

    rows = conn.execute(SQL_BUSCAR_NEGOCIOS, (dtLiquidacao,)).fetchall()
    stats.total = len(rows)

    if stats.total == 0:
        log.info("filtrar_trades: dtLiquidacao=%s — nenhum trade em NegociosProcessados", dtLiquidacao)
        return stats

    trades = [
        Negocio(
            idTrade=row["idTrade"],
            cdTicker=row["cdTicker"],
            dtLiquidacao=row["dtLiquidacao"],
            vrQuantidade=row["vrQuantidade"],
            vrVolume=row["vrVolume"],
            vrPU=row["vrVolume"] / row["vrQuantidade"] if row["vrQuantidade"] else 0.0,
            vrTaxaCalculada=row["vrTaxaCalculada"],
            cdIndexador=row["cdIndexador"],
        )
        for row in rows
    ]

    # Taxas indicativas Anbima para a data (referência do filtro PF)
    linhasAnbima = conn.execute(SQL_BUSCAR_ANBIMA, (dtLiquidacao,)).fetchall()
    taxaAnbima = {r["cdTicker"]: r["vrTaxaAnbima"] for r in linhasAnbima}

    log.info(
        "filtrar_trades: dtLiquidacao=%s — %d trade(s), %d taxa(s) Anbima disponíveis",
        dtLiquidacao, stats.total, len(taxaAnbima),
    )

    # --- Filtro 1: Passagem de Fundo (passes a+b pairwise; pass c bloco+splits) ---
    nFundo = AplicarFiltroFundo(trades, fundoMaxReaisPorMilhao)
    nFundo += AplicarFiltroFundoSplits(trades, fundoMaxReaisPorMilhao)
    log.info("filtrar_trades: %s — FUNDO: %d trade(s) (threshold=%.1f R$/MM)",
             dtLiquidacao, nFundo, fundoMaxReaisPorMilhao)

    # --- Filtro 2: Corretor (pass 1: pares mesma qty; pass 2: bloco + splits) ---
    nBroker = AplicarFiltroCorretor(trades, corretorMaxBps, corretorMaxPctCdi)
    nBroker += AplicarFiltroCorretorSplits(trades, corretorMaxBps, corretorMaxPctCdi)
    log.info("filtrar_trades: %s — CORRETOR: %d trade(s) BROKER (maxBps=%.2f, maxPctCdi=%.3f)",
             dtLiquidacao, nBroker, corretorMaxBps, corretorMaxPctCdi)

    # --- Filtro 3: Pessoa Física ---
    nPf = AplicarFiltroPF(trades, pfMinBps, pfMinPctCdi, taxaAnbima)
    log.info("filtrar_trades: %s — PF: %d trade(s) (minBps=%.1f, minPctCdi=%.2f)",
             dtLiquidacao, nPf, pfMinBps, pfMinPctCdi)

    # Contagem de stats
    for t in trades:
        if t.cdStatus == 'VALIDO':
            stats.valido += 1
            if t.vrTaxaCalculada is None:
                stats.nullTaxa += 1
        elif t.cdStatus == 'FUNDO':
            stats.fundo += 1
        elif t.cdStatus == 'BROKER':
            stats.broker += 1
        elif t.cdStatus == 'PF':
            stats.pf += 1

    # UPDATE atômico: um executemany para todos os trades da data
    updates = [(t.cdStatus, t.idGrupoNegocio, t.idTrade) for t in trades]
    conn.executemany(SQL_ATUALIZAR_STATUS, updates)
    conn.commit()

    log.info(
        "filtrar_trades: %s — VALIDO=%d FUNDO=%d BROKER=%d PF=%d NullTaxa=%d",
        dtLiquidacao, stats.valido, stats.fundo, stats.broker, stats.pf, stats.nullTaxa,
    )
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def LerArgumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aplica filtros de qualidade (FUNDO/CORRETOR/PF) em NegociosProcessados por dtLiquidacao."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="Processa uma única dtLiquidacao.",
    )
    group.add_argument(
        "--start",
        metavar="YYYY-MM-DD",
        help="Início do intervalo de dtLiquidacao (usar com --end).",
    )
    parser.add_argument(
        "--end",
        metavar="YYYY-MM-DD",
        help="Fim do intervalo de dtLiquidacao (obrigatório com --start).",
    )
    parser.add_argument(
        "--fundo-max",
        type=float,
        dest="fundoMax",
        default=None,
        metavar="N",
        help="Threshold máximo R$/milhão de notional para passagem de fundo (padrão: config.toml filtro.fundoMaxReaisPorMilhao).",
    )
    parser.add_argument(
        "--corretor-bps",
        type=float,
        dest="corretorBps",
        default=None,
        metavar="N",
        help="Tolerância máxima em bps para par corretor não-%%CDI (padrão: config.toml filtro.corretorMaxBps).",
    )
    parser.add_argument(
        "--corretor-pctcdi",
        type=float,
        dest="corretorPctCdi",
        default=None,
        metavar="N",
        help="Tolerância máxima em unidades %%CDI para par corretor (padrão: config.toml filtro.corretorMaxPctCdi).",
    )
    parser.add_argument(
        "--pf-min-bps",
        type=float,
        dest="pfMinBps",
        default=None,
        metavar="N",
        help="Diferença mínima em bps para par PF não-%%CDI (padrão: config.toml filtro.pfMinBps).",
    )
    parser.add_argument(
        "--pf-min-pctcdi",
        type=float,
        dest="pfMinPctCdi",
        default=None,
        metavar="N",
        help="Diferença mínima em unidades %%CDI para par PF (padrão: config.toml filtro.pfMinPctCdi).",
    )

    args = parser.parse_args()

    if args.start and not args.end:
        parser.error("--end é obrigatório quando --start é informado.")
    if args.end and not args.start:
        parser.error("--start é obrigatório quando --end é informado.")

    return args


def MontarIntervaloDatas(args: argparse.Namespace) -> list[str]:
    """Retorna lista de datas YYYY-MM-DD (dtLiquidacao) a processar."""
    if args.date:
        return [args.date]

    startDate = date.fromisoformat(args.start)
    endDate   = date.fromisoformat(args.end)

    if endDate < startDate:
        raise ValueError(f"--end ({args.end}) anterior a --start ({args.start})")

    datas: list[str] = []
    current = startDate
    while current <= endDate:
        datas.append(current.isoformat())
        current += timedelta(days=1)
    return datas


def MontarResumo(statsList: list[EstatisticasData]) -> str:
    """Formata tabela de resumo por dtLiquidacao para o email e log."""
    lines = ["Resultado por dtLiquidacao:", ""]
    header = (
        f"{'Data':<12}  {'Total':>6}  {'VALIDO':>7}  "
        f"{'FUNDO':>6}  {'BROKER':>7}  {'PF':>5}  {'NullTaxa':>9}"
    )
    sep = "-" * len(header)
    lines.extend([header, sep])

    totais = EstatisticasData(dtLiquidacao="TOTAL")
    for s in statsList:
        lines.append(
            f"{s.dtLiquidacao:<12}  {s.total:>6}  {s.valido:>7}  "
            f"{s.fundo:>6}  {s.broker:>7}  {s.pf:>5}  {s.nullTaxa:>9}"
        )
        totais.total    += s.total
        totais.valido   += s.valido
        totais.fundo    += s.fundo
        totais.broker   += s.broker
        totais.pf       += s.pf
        totais.nullTaxa += s.nullTaxa

    lines.append(sep)
    lines.append(
        f"{'TOTAL':<12}  {totais.total:>6}  {totais.valido:>7}  "
        f"{totais.fundo:>6}  {totais.broker:>7}  {totais.pf:>5}  {totais.nullTaxa:>9}"
    )

    if totais.nullTaxa > 0:
        lines.append(
            f"\nATENCAO: {totais.nullTaxa} trade(s) com vrTaxaCalculada = NULL — "
            "ficaram VALIDO isolados (sem classificação possível)."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Principal() -> None:
    log     = ObterLogger("filtrar_trades")
    args    = LerArgumentos()
    conn    = ObterBanco()
    summary = ""
    success = True

    try:
        # CLI tem prioridade sobre config.toml
        fundoMaxReaisPorMilhao: float = (
            args.fundoMax if args.fundoMax is not None
            else float(cfg["filtro"]["fundoMaxReaisPorMilhao"])
        )
        corretorMaxBps: float = (
            args.corretorBps if args.corretorBps is not None
            else float(cfg["filtro"]["corretorMaxBps"])
        )
        corretorMaxPctCdi: float = (
            args.corretorPctCdi if args.corretorPctCdi is not None
            else float(cfg["filtro"]["corretorMaxPctCdi"])
        )
        pfMinBps: float = (
            args.pfMinBps if args.pfMinBps is not None
            else float(cfg["filtro"]["pfMinBps"])
        )
        pfMinPctCdi: float = (
            args.pfMinPctCdi if args.pfMinPctCdi is not None
            else float(cfg["filtro"]["pfMinPctCdi"])
        )

        datas = MontarIntervaloDatas(args)
        log.info(
            "filtrar_trades: %d data(s): %s ... %s | "
            "fundoMax=%.1f corretorBps=%.2f corretorPctCdi=%.3f pfMinBps=%.1f pfMinPctCdi=%.2f",
            len(datas), datas[0], datas[-1],
            fundoMaxReaisPorMilhao, corretorMaxBps, corretorMaxPctCdi, pfMinBps, pfMinPctCdi,
        )

        statsList: list[EstatisticasData] = []
        for dtLiquidacao in datas:
            s = ProcessarData(
                conn, dtLiquidacao,
                fundoMaxReaisPorMilhao,
                corretorMaxBps, corretorMaxPctCdi,
                pfMinBps, pfMinPctCdi,
                log,
            )
            statsList.append(s)

        summary = MontarResumo(statsList)
        log.info("filtrar_trades: concluido.\n%s", summary)

    except Exception:
        success = False
        summary = traceback.format_exc()
        log.exception("filtrar_trades: erro inesperado")

    finally:
        conn.close()
        EnviarEmailConclusao(
            "filtrar_trades",
            success,
            summary,
            logger=log,
        )


if __name__ == "__main__":
    Principal()
