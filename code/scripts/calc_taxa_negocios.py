"""
calc_taxa_negocios.py
=====================
Para cada trade em NegociosBrutos com dtLiquidacao na janela informada:
  - Se vrTaxaNegocio NOT NULL: copia para vrTaxaCalculada (cdFonteTaxa = NULL)
  - Se vrTaxaNegocio IS NULL:  cascata **Calc local** -> FI Analytics -> B3 Calculator

A CALCULADORA LOCAL esta implementada como 1o degrau da cascata, para ativo com
`stFluxoValidado = 1` — mas vem DESLIGADA ([calc].usarCalcTaxa = false).

Por que desligada (13/07/2026): a calc reproduz o PU PAR das fontes (86% dos ativos
batem a 1e-6 — ver conferir_pu), mas NAO reproduz a taxa implicita num PU fora do par.
Triangulando 4 negocios de 16/06/2026, FI Analytics e B3 concordam entre si (0 a 1,7
bps) e a calc discorda das duas (+2,2 a +13,7 bps). Quando duas fontes independentes
batem e a nossa diverge, o erro e nosso. Como os fluxos e o VNA estao certos (o PU par
fecha), a suspeita e a convencao de DESCONTO.

Licao: o gate de PU PAR nao basta. Ele valida o fluxo, nao o desconto. O teste que
falta e o round-trip da taxa: dado o PU que a fonte devolve para uma taxa FORA do par,
a calc tem que reproduzir aquela taxa.

Performance (medido em 16/06/2026, o pregao mais cheio da base): 8.065 negocios
validados, mas so 598 pares (cdTicker, vrPU) DISTINTOS — o cache corta 93% do trabalho.
A ~0,7s por par, da ~7 min/dia num core, contra os ~25 min/dia da cascata de API no
banco (onde cada chamada paga proxy). A calc e CPU-bound, entao o ThreadPoolExecutor
nao a paraleliza (GIL) — quem faz o servico e o cache.

Grava resultado em NegociosProcessados via UPSERT.
Trades com cdSituacao = 'Cancelado' sao ignorados.

CLI:
    python scripts/calc_taxa_negocios.py --date 2026-05-29
    python scripts/calc_taxa_negocios.py --start 2026-05-01 --end 2026-05-29
    python scripts/calc_taxa_negocios.py --date 2026-05-29 --limit 40   # smoke test
    python scripts/calc_taxa_negocios.py --date 2026-05-29 --sem-calc   # so API (A/B)
"""

import argparse
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# Garante que code/ esteja no sys.path ao rodar como script
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.calc import CalcularTaxa as CalcularTaxaLocal, CarregarAtivo
from lib.config import cfg
from lib.db import ObterBanco
from lib.logger import ObterLogger
from lib.email_outlook import EnviarEmailConclusao
from lib.fianalytics_api import CalcularTaxa
from lib.b3_calc_api import CalcularYield
from lib.relatorio_execucao import RelatorioExecucao


# ---------------------------------------------------------------------------
# Calculadora local — primeiro degrau da cascata
# ---------------------------------------------------------------------------

# Ativos com fluxo validado, prontos para a calc. Carregado uma vez por rodada, na
# thread principal: a conexao do SQLite nao e thread-safe, e os workers so leem daqui.
ativosValidados: dict[str, dict] = {}

# (cdTicker, dtLiquidacao, vrPU) -> taxa. E ele que viabiliza a troca: num pregao cheio,
# 8.065 negocios colapsam em 598 calculos.
cacheCalc: dict[tuple, Optional[float]] = {}
travaCache = threading.Lock()


# Indexadores em que a calc é confiável para a TAXA de um trade (verificado 15/07/2026
# em trades reais contra a B3: CDI+/IPCA/PREFIXADO batem a mediana 0,00 bps, máx <0,4 bps).
# %CDI fica de FORA: o PU no par é perfeito, mas a taxa implícita num PU de DESCONTO
# PROFUNDO diverge da B3 em até ~15 bps (FI e B3 concordam entre si, a calc não) — é o
# problema aberto do desconto de %CDI. Enquanto não fecha, %CDI cai na cascata FI->B3.
INDEXADORES_CALC = frozenset(cfg["calc"].get("indexadores", ["CDI+", "IPCA", "PREFIXADO"]))


def CarregarAtivosValidados(conn, log) -> None:
    global ativosValidados
    tickers = [r["cdTicker"] for r in conn.execute(
        "SELECT cdTicker FROM InfoAtivos WHERE stFluxoValidado = 1")]
    ativosValidados = {}
    pulados = 0
    for cdTicker in tickers:
        ativo = CarregarAtivo(conn, cdTicker)
        if not ativo:
            continue
        if ativo["cdIndexador"] not in INDEXADORES_CALC:
            pulados += 1
            continue
        ativosValidados[cdTicker] = ativo
    log.info("calc_taxa: %d ativo(s) prontos para a calc local (%s); %d validado(s) de "
             "outro indexador ficam na cascata de API",
             len(ativosValidados), ",".join(sorted(INDEXADORES_CALC)), pulados)


def TaxaPelaCalc(trade: "NegocioBruto", log) -> Optional[float]:
    """Taxa pela calculadora local, ou None se o ativo nao esta validado / a calc falhou."""
    ativo = ativosValidados.get(trade.cdTicker)
    if ativo is None:
        return None

    chave = (trade.cdTicker, trade.dtLiquidacao, trade.vrPU)
    with travaCache:
        if chave in cacheCalc:
            return cacheCalc[chave]
    try:
        taxa = CalcularTaxaLocal(ativo, date.fromisoformat(trade.dtLiquidacao), trade.vrPU)
    except Exception as exc:
        log.debug("calc_taxa: calc local falhou para %s: %s", trade.cdTicker, exc)
        taxa = None
    with travaCache:
        cacheCalc[chave] = taxa
    return taxa


# ---------------------------------------------------------------------------
# Tipos internos
# ---------------------------------------------------------------------------

@dataclass
class NegocioBruto:
    idTrade: int
    cdTicker: str
    cdEmissor: str
    cdInstrumento: str
    dtNegocio: str
    dtLiquidacao: str
    vrQuantidade: int
    vrPU: float
    vrVolume: float
    vrTaxaNegocio: Optional[float]


@dataclass
class AlertaNegocio:
    cdTicker:  str
    cdEmissor: str
    vrVolume:  float


@dataclass
class EstatisticasData:
    dtLiquidacao: str
    total: int = 0
    direta: int = 0       # vrTaxaNegocio copiado direto (sem chamada de API)
    calc: int = 0         # calculada localmente (ativo com fluxo validado)
    fianalytics: int = 0  # calculada via FI Analytics
    b3: int = 0           # calculada via B3 Calculator
    semTaxa: int = 0      # vrTaxaCalculada = NULL (todas as calculadoras falharam)
    alertas: list[AlertaNegocio] = field(default_factory=list)  # sem taxa + volume >= threshold


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

SQL_BUSCAR_NEGOCIOS = """
SELECT idTrade, cdTicker, cdEmissor, cdInstrumento,
       dtNegocio, dtLiquidacao, vrQuantidade, vrPU, vrVolume, vrTaxaNegocio
FROM NegociosBrutos
WHERE dtLiquidacao = ?
  AND cdSituacao != 'Cancelado'
"""

SQL_BUSCAR_EXISTENTES = """
SELECT idTrade, vrTaxaCalculada, cdFonteTaxa
FROM NegociosProcessados
WHERE dtLiquidacao = ?
  AND vrTaxaCalculada IS NOT NULL
"""

SQL_UPSERT = """
INSERT INTO NegociosProcessados (
    idTrade, cdTicker, cdEmissor, dtNegocio, dtLiquidacao,
    vrQuantidade, vrPU, vrVolume, vrTaxaCalculada, cdFonteTaxa,
    vrDuration, vrSpreadOver, idGrupoNegocio, cdStatus, dtProcessamento
) VALUES (
    :idTrade, :cdTicker, :cdEmissor, :dtNegocio, :dtLiquidacao,
    :vrQuantidade, :vrPU, :vrVolume, :vrTaxaCalculada, :cdFonteTaxa,
    NULL, NULL, NULL, 'VALIDO', CURRENT_TIMESTAMP
)
ON CONFLICT(idTrade) DO UPDATE SET
    vrTaxaCalculada = excluded.vrTaxaCalculada,
    cdFonteTaxa    = excluded.cdFonteTaxa,
    dtProcessamento   = excluded.dtProcessamento
"""


# ---------------------------------------------------------------------------
# Logica de cascata
# ---------------------------------------------------------------------------

def AplicarCascata(trade: NegocioBruto, log, usarCalc: bool = True) -> tuple[Optional[float], Optional[str]]:
    """
    Retorna (vrTaxaCalculada, cdFonteTaxa).

    Cascata:
      1. vrTaxaNegocio nao nulo -> copia direto (source = None)
      2. Calculadora local      -> source = 'Calc'         (so ativo com fluxo validado)
      3. FI Analytics           -> source = 'FiAnalytics'
      4. B3 Calculator          -> source = 'B3'
      5. Falhou tudo            -> (None, None)
    """
    if trade.vrTaxaNegocio is not None:
        return trade.vrTaxaNegocio, None

    if usarCalc:
        taxa = TaxaPelaCalc(trade, log)
        if taxa is not None:
            return taxa, "Calc"

    log.debug(
        "calc_taxa: vrTaxaNegocio NULL para %s/%s/%s — tentando FI Analytics",
        trade.cdTicker, trade.dtLiquidacao, trade.vrPU,
    )

    taxa = CalcularTaxa(trade.cdTicker, trade.cdInstrumento, trade.dtLiquidacao, trade.vrPU)
    if taxa is not None:
        return taxa, "FiAnalytics"

    log.debug(
        "calc_taxa: FI Analytics falhou para %s — tentando B3 Calculator",
        trade.cdTicker,
    )

    taxa = CalcularYield(trade.cdTicker, trade.dtLiquidacao, trade.vrPU)
    if taxa is not None:
        return taxa, "B3"

    log.warning(
        "calc_taxa: todas as calculadoras falharam para idTrade=%d cdTicker=%s dtLiquidacao=%s",
        trade.idTrade, trade.cdTicker, trade.dtLiquidacao,
    )
    return None, None


# ---------------------------------------------------------------------------
# Processamento por data
# ---------------------------------------------------------------------------

def ProcessarData(conn, dtLiquidacao: str, log, workers: int, force: bool,
                 limit: Optional[int] = None, usarCalc: bool = True) -> EstatisticasData:
    """Processa todos os trades de uma dtLiquidacao e faz UPSERT em NegociosProcessados.

    `limit` (só para smoke test) processa apenas N trades, priorizando os que
    precisam de chamada de API (vrTaxaNegocio IS NULL) — assim a cascata
    FI Analytics -> B3 e o caminho 'direta' são ambos exercitados.
    """
    stats = EstatisticasData(dtLiquidacao=dtLiquidacao)

    rows = conn.execute(SQL_BUSCAR_NEGOCIOS, (dtLiquidacao,)).fetchall()

    if limit is not None and len(rows) > limit:
        # trades sem taxa (precisam de API) primeiro — o resto completa o lote
        rows = sorted(rows, key=lambda r: r["vrTaxaNegocio"] is not None)[:limit]
        log.warning("calc_taxa: --limit %d ativo — processando apenas %d trade(s) de %s (SMOKE TEST)",
                    limit, len(rows), dtLiquidacao)

    stats.total = len(rows)

    if stats.total == 0:
        log.info("calc_taxa: dtLiquidacao=%s — nenhum trade encontrado em NegociosBrutos", dtLiquidacao)
        return stats

    trades = [
        NegocioBruto(
            idTrade=row["idTrade"],
            cdTicker=row["cdTicker"],
            cdEmissor=row["cdEmissor"],
            cdInstrumento=row["cdInstrumento"],
            dtNegocio=row["dtNegocio"],
            dtLiquidacao=row["dtLiquidacao"],
            vrQuantidade=row["vrQuantidade"],
            vrPU=row["vrPU"],
            vrVolume=row["vrVolume"],
            vrTaxaNegocio=row["vrTaxaNegocio"],
        )
        for row in rows
    ]

    # Carrega taxa já calculada (exceto se --force)
    existing: dict[int, tuple[Optional[float], Optional[str]]] = {}
    if not force:
        for row in conn.execute(SQL_BUSCAR_EXISTENTES, (dtLiquidacao,)).fetchall():
            existing[row["idTrade"]] = (row["vrTaxaCalculada"], row["cdFonteTaxa"])

    # Separa trades que já têm taxa (cached) dos que precisam de API
    cached:     list[tuple[NegocioBruto, Optional[float], Optional[str]]] = []
    aProcessar: list[NegocioBruto] = []

    for trade in trades:
        if trade.idTrade in existing:
            cached.append((trade, *existing[trade.idTrade]))
        else:
            aProcessar.append(trade)

    nCache = len(cached)
    nApi    = len(aProcessar)

    if force:
        log.info(
            "calc_taxa: dtLiquidacao=%s — %d trade(s), --force ativo, recalculando tudo (workers=%d)",
            dtLiquidacao, stats.total, workers,
        )
    else:
        log.info(
            "calc_taxa: dtLiquidacao=%s — %d trade(s): %d via API, %d já calculados (mantidos)",
            dtLiquidacao, stats.total, nApi, nCache,
        )

    # Fase paralela: API calls apenas para to_process
    resultadosApi: list[tuple[NegocioBruto, Optional[float], Optional[str]]] = []

    if aProcessar:
        feitosApi   = 0
        tempoInicio = time.monotonic()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futureParaNegocio = {executor.submit(AplicarCascata, t, log, usarCalc): t for t in aProcessar}
            for future in as_completed(futureParaNegocio):
                trade = futureParaNegocio[future]
                try:
                    taxa, source = future.result()
                except Exception:
                    log.exception(
                        "calc_taxa: erro inesperado para idTrade=%d cdTicker=%s",
                        trade.idTrade, trade.cdTicker,
                    )
                    taxa, source = None, None

                feitosApi += 1
                resultadosApi.append((trade, taxa, source))

                elapsed  = time.monotonic() - tempoInicio
                taxaStr = f"{taxa:.4f}%" if taxa is not None else "sem taxa"
                origem   = source if source else ("direta" if trade.vrTaxaNegocio is not None else "sem taxa")
                if feitosApi >= 2:
                    eta     = elapsed / feitosApi * (nApi - feitosApi)
                    etaStr = f" | ~{eta:.0f}s restante"
                else:
                    etaStr = ""
                log.info(
                    "[%d/%d] %s → %s %s | %.0fs elapsed%s",
                    feitosApi, nApi, trade.cdTicker, origem, taxaStr, elapsed, etaStr,
                )

    # Fase sequencial: contagem de stats + UPSERTs (só para to_process)
    # Trades cached: apenas contagem, sem UPSERT (já estão corretos no banco)
    volumeMin: float = cfg["alerta"]["volumeMinSemTaxa"]

    def ContarEstatisticas(trade: NegocioBruto, vrTaxaCalculada: Optional[float], cdFonteTaxa: Optional[str]) -> None:
        if trade.vrTaxaNegocio is not None:
            stats.direta += 1
        elif cdFonteTaxa == "Calc":
            stats.calc += 1
        elif cdFonteTaxa == "FiAnalytics":
            stats.fianalytics += 1
        elif cdFonteTaxa == "B3":
            stats.b3 += 1
        else:
            stats.semTaxa += 1
            if trade.vrVolume >= volumeMin:
                stats.alertas.append(AlertaNegocio(trade.cdTicker, trade.cdEmissor, trade.vrVolume))

    for trade, vrTaxaCalculada, cdFonteTaxa in cached:
        ContarEstatisticas(trade, vrTaxaCalculada, cdFonteTaxa)

    for trade, vrTaxaCalculada, cdFonteTaxa in resultadosApi:
        ContarEstatisticas(trade, vrTaxaCalculada, cdFonteTaxa)
        conn.execute(SQL_UPSERT, {
            "idTrade":         trade.idTrade,
            "cdTicker":        trade.cdTicker,
            "cdEmissor":       trade.cdEmissor,
            "dtNegocio":       trade.dtNegocio,
            "dtLiquidacao":    trade.dtLiquidacao,
            "vrQuantidade":    trade.vrQuantidade,
            "vrPU":            trade.vrPU,
            "vrVolume":        trade.vrVolume,
            "vrTaxaCalculada": vrTaxaCalculada,
            "cdFonteTaxa":    cdFonteTaxa,
        })

    conn.commit()

    log.info(
        "calc_taxa: dtLiquidacao=%s — direta=%d calc=%d fianalytics=%d b3=%d semTaxa=%d",
        dtLiquidacao, stats.direta, stats.calc, stats.fianalytics, stats.b3, stats.semTaxa,
    )
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def LerArgumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Calcula vrTaxaCalculada para trades de crédito privado (cascata FI Analytics -> B3)."
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
        "--workers",
        type=int,
        default=8,
        metavar="N",
        help="Número de threads paralelas para chamadas de API (padrão: 8).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Recalcula taxa mesmo para trades que já têm vrTaxaCalculada no banco.",
    )
    parser.add_argument(
        "--sem-calc", dest="semCalc", action="store_true",
        help="forca so a cascata de API (ignora [calc].usarCalcTaxa)",
    )
    parser.add_argument(
        "--com-calc", dest="comCalc", action="store_true",
        help="forca a calculadora local (ignora [calc].usarCalcTaxa). Veja o aviso no "
             "config.toml: hoje a calc diverge das APIs em 2-14 bps na taxa.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="SMOKE TEST: processa só N trades por data (prioriza os que precisam de API).",
    )
    args = parser.parse_args()

    if args.start and not args.end:
        parser.error("--end é obrigatório quando --start é informado.")
    if args.end and not args.start:
        parser.error("--start é obrigatório quando --end é informado.")

    return args


def MontarIntervaloDatas(args: argparse.Namespace) -> list[str]:
    """Retorna lista de datas YYYY-MM-DD a processar."""
    if args.date:
        date.fromisoformat(args.date)   # valida: data inválida aborta em vez de sair mudo (0 linhas, exit 0)
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


def MontarRelatorio(statsList: list[EstatisticasData], args, rel) -> None:
    """Preenche o RelatorioExecucao com o resultado por dtLiquidacao."""
    rel.Datas([s.dtLiquidacao for s in statsList])

    linhas, totais = [], {"total": 0, "direta": 0, "calc": 0, "fi": 0, "b3": 0, "sem": 0}
    for s in statsList:
        linhas.append([s.dtLiquidacao, s.total, s.direta, s.calc, s.fianalytics, s.b3, s.semTaxa])
        totais["total"] += s.total
        totais["direta"] += s.direta
        totais["calc"] += s.calc
        totais["fi"] += s.fianalytics
        totais["b3"] += s.b3
        totais["sem"] += s.semTaxa
    linhas.append(["TOTAL", totais["total"], totais["direta"], totais["calc"],
                   totais["fi"], totais["b3"], totais["sem"]])

    rel.Secao("Fonte da taxa, por liquidacao",
              ["dtLiquidacao", "negocios", "direta (boletim)", "calc local",
               "FI Analytics", "B3", "sem taxa"], linhas)

    rel.Contar("processados", totais["total"])
    rel.Metrica("Taxa direta do boletim", totais["direta"])
    rel.Metrica("Calculadora local", totais["calc"])
    rel.Metrica("FI Analytics (fallback)", totais["fi"])
    rel.Metrica("B3 (fallback)", totais["b3"])
    rel.Metrica("Sem taxa", totais["sem"])
    if totais["calc"]:
        rel.Metrica("Calculos distintos em cache (ticker, data, PU)", len(cacheCalc))
    if args.semCalc:
        rel.Aviso("--sem-calc: a calculadora local foi desligada; so a cascata de API rodou.")
    if totais["sem"]:
        rel.Aviso(f"{totais['sem']} negocio(s) sem taxa calculada (vrTaxaCalculada = NULL).")

    volumeMin: float = cfg["alerta"]["volumeMinSemTaxa"]
    alertas = [[s.dtLiquidacao, a.cdTicker, a.cdEmissor, f"{a.vrVolume:,.0f}"]
               for s in statsList for a in s.alertas]
    if alertas:
        rel.Secao(f"Sem taxa com volume >= R$ {volumeMin:,.0f}",
                  ["data", "ticker", "emissor", "volume (R$)"], alertas)
        rel.Aviso(f"{len(alertas)} negocio(s) de volume relevante ficaram sem taxa.")

def Principal() -> None:
    log = ObterLogger("calc_taxa_negocios")
    args = LerArgumentos()
    conn = ObterBanco()
    rel = RelatorioExecucao("calc_taxa_negocios", args=vars(args))
    success = True
    erro = None

    try:
        datas = MontarIntervaloDatas(args)
        log.info("calc_taxa: processando %d data(s): %s ... %s", len(datas), datas[0], datas[-1])

        # Config manda; --sem-calc e --com-calc sobrescrevem pontualmente.
        usarCalc = bool(cfg["calc"].get("usarCalcTaxa", False))
        if args.semCalc:
            usarCalc = False
        if args.comCalc:
            usarCalc = True
        log.info("calc_taxa: calculadora local %s", "LIGADA" if usarCalc else "desligada")
        if usarCalc:
            CarregarAtivosValidados(conn, log)
            rel.Metrica("Ativos prontos para a calc (fluxo validado)", len(ativosValidados))

        statsList: list[EstatisticasData] = []
        for dtLiquidacao in datas:
            s = ProcessarData(conn, dtLiquidacao, log, args.workers, args.force, args.limit, usarCalc)
            statsList.append(s)

        MontarRelatorio(statsList, args, rel)
        log.info("calc_taxa: concluido.\n%s", rel.Texto())

    except Exception:
        success = False
        erro = traceback.format_exc()
        rel.Erro("A rodada abortou — ver traceback.")
        log.exception("calc_taxa: erro inesperado")

    finally:
        conn.close()
        EnviarEmailConclusao("calc_taxa_negocios", success, rel, tracebackErro=erro, logger=log)

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    Principal()
