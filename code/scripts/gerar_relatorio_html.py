"""
gerar_relatorio_html.py
=======================
Gera relatório HTML de negócios de crédito privado por dtLiquidacao.

Lê NegociosProcessados (VALIDO + BROKER) + NegociosBrutos (cdInstrumento, cdSituacao) +
InfoAtivos (cdIndexador, vrDuration, dtVencimento) + AnbimaIndicativos
(taxa/spread indicativo mais recente <= dtLiquidacao). Agrega por ticker,
agrupa por cdInstrumento (DEB/CRI/CRA), renderiza via Jinja2 e salva HTML.

Grupos BROKER: agregados por idGrupoNegocio → taxa=(MAX+MIN)/2, volume=SUM/2,
spread calculado em Python contra MtmAnbima[cdReferencia][dtNegocio].

CLI:
    python scripts/gerar_relatorio_html.py --date 2026-06-03 --mode previa
    python scripts/gerar_relatorio_html.py --date 2026-06-03 --mode definitivo
"""

import argparse
import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Formatação pt-BR (usada como filtros Jinja2)
# ---------------------------------------------------------------------------

MESES_PT = ["jan","fev","mar","abr","mai","jun","jul","ago","set","out","nov","dez"]


def FmtBr(value, dec: int = 2) -> str | None:
    """Número em pt-BR: ponto como milhar, vírgula como decimal."""
    if value is None:
        return None
    s = f"{float(value):,.{dec}f}"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def FmtIntBr(value) -> str | None:
    """Inteiro com ponto como separador de milhar, sem decimais."""
    if value is None:
        return None
    s = f"{int(value):,}"
    return s.replace(",", ".")


def FmtMesAno(s) -> str | None:
    """'YYYY-MM-DD' → 'mmm/aa'. Ex: '2026-12-01' → 'dez/26'."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s)[:10])
        return f"{MESES_PT[dt.month - 1]}/{dt.strftime('%y')}"
    except Exception:
        return str(s)


def TipoExibicao(cdInstrumento: str | None, cdIndexador: str | None) -> str | None:
    """
    Tipo usado apenas para exibição/filtro no relatório. Debênture indexada a
    IPCA ou PREFIXADO é classificada como 'DEB 12.431' (incentivada). Demais
    instrumentos permanecem inalterados.
    """
    if cdInstrumento == "DEB" and cdIndexador in ("IPCA", "PREFIXADO"):
        return "DEB 12.431"
    return cdInstrumento

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.config import cfg
from lib.db import ObterBanco
from lib.logger import ObterLogger


def CalcularDMenos1(dtLiquidacao: str) -> str:
    """Retorna o dia útil anterior a dtLiquidacao excluindo fins de semana e feriados Anbima."""
    import csv
    from datetime import date, timedelta

    feriadosPath = Path(cfg["paths"]["dbFile"]).parent / "feriados_anbima.csv"
    feriados: set[date] = set()
    if feriadosPath.exists():
        with feriadosPath.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    feriados.add(date.fromisoformat(row["data"].strip()))
                except ValueError:
                    pass

    d = date.fromisoformat(dtLiquidacao) - timedelta(days=1)
    while d.weekday() >= 5 or d in feriados:
        d -= timedelta(days=1)
    return d.isoformat()


# ---------------------------------------------------------------------------
# Tipos internos
# ---------------------------------------------------------------------------

@dataclass
class LinhaNegocio:
    """Uma linha individual (VALIDO) ou grupo virtual (BROKER) antes da agregação."""
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
    nrTrades: int = 1  # 1 para VALIDO; tamanho do grupo para BROKER virtual


@dataclass
class TickerAgregado:
    cdTicker: str
    cdEmissor: str
    cdInstrumento: str
    cdIndexador: str | None
    cdReferencia: str | None
    vrDuration: float | None
    dtVencimento: str | None
    nrTrades: int
    vrQuantidadeTotal: int
    vrVolumeTotal: float
    vrTaxaMedia: float | None
    vrSpreadOverMedio: float | None
    vrTaxaAnbima: float | None
    vrSpreadAnbima: float | None


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

SQL_BUSCAR = """
WITH AnbimaLatest AS (
    SELECT cdTicker,
           vrTaxaAnbima,
           vrSpreadAnbima,
           ROW_NUMBER() OVER (PARTITION BY cdTicker ORDER BY dtReferencia DESC) AS rn
    FROM AnbimaIndicativos
    WHERE dtReferencia <= ?
)
SELECT
    tp.cdTicker,
    ia.cdEmissor,
    tr.cdInstrumento,
    tp.vrQuantidade,
    tp.vrVolume,
    tp.vrTaxaCalculada,
    tp.vrSpreadOver,
    ia.cdIndexador,
    ia.cdReferencia,
    ia.vrDuration,
    ia.dtVencimento,
    al.vrTaxaAnbima,
    al.vrSpreadAnbima
FROM NegociosProcessados tp
JOIN NegociosBrutos tr ON tr.idTrade = tp.idTrade
LEFT JOIN InfoAtivos ia ON ia.cdTicker = tp.cdTicker
LEFT JOIN AnbimaLatest al ON al.cdTicker = tp.cdTicker AND al.rn = 1
WHERE tp.dtLiquidacao = ?
  AND tp.cdStatus = 'VALIDO'
  AND tr.cdSituacao != 'Cancelado'
"""

SQL_BUSCAR_BROKER = """
WITH AnbimaLatest AS (
    SELECT cdTicker,
           vrTaxaAnbima,
           vrSpreadAnbima,
           ROW_NUMBER() OVER (PARTITION BY cdTicker ORDER BY dtReferencia DESC) AS rn
    FROM AnbimaIndicativos
    WHERE dtReferencia <= ?
)
SELECT
    tp.cdTicker,
    ia.cdEmissor,
    tr.cdInstrumento,
    tp.idGrupoNegocio,
    tp.dtNegocio,
    tp.vrQuantidade,
    tp.vrVolume,
    tp.vrTaxaCalculada,
    ia.cdIndexador,
    ia.cdReferencia,
    ia.vrDuration,
    ia.dtVencimento,
    al.vrTaxaAnbima,
    al.vrSpreadAnbima
FROM NegociosProcessados tp
JOIN NegociosBrutos tr ON tr.idTrade = tp.idTrade
LEFT JOIN InfoAtivos ia ON ia.cdTicker = tp.cdTicker
LEFT JOIN AnbimaLatest al ON al.cdTicker = tp.cdTicker AND al.rn = 1
WHERE tp.dtLiquidacao = ?
  AND tp.cdStatus = 'BROKER'
  AND tp.idGrupoNegocio IS NOT NULL
  AND tr.cdSituacao != 'Cancelado'
"""

SQL_MTM_TAXA = """
SELECT vrTaxa FROM MtmAnbima WHERE cdTicker = ? AND dtReferencia = ?
"""


# ---------------------------------------------------------------------------
# Lógica de aggregação
# ---------------------------------------------------------------------------

def MediaPonderada(valores: list[tuple[float | None, float]]) -> float | None:
    """
    Calcula média ponderada de (valor, peso). Pares com valor=None são ignorados.
    Retorna None se nenhum par tiver valor.
    """
    numerador = 0.0
    denominador = 0.0
    for valor, peso in valores:
        if valor is not None:
            numerador += valor * peso
            denominador += peso
    if denominador == 0.0:
        return None
    return numerador / denominador


def BuscarNegocios(conn, dtLiquidacao: str, dtAnbima: str) -> list[LinhaNegocio]:
    """Executa o SELECT principal e retorna lista de LinhaNegocio."""
    rows = conn.execute(SQL_BUSCAR, (dtAnbima, dtLiquidacao)).fetchall()
    result: list[LinhaNegocio] = []
    for r in rows:
        result.append(LinhaNegocio(
            cdTicker=r["cdTicker"],
            cdEmissor=r["cdEmissor"],
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
        ))
    return result


def BuscarGruposBroker(conn, dtLiquidacao: str, dtAnbima: str) -> list[LinhaNegocio]:
    """
    Busca grupos BROKER, agrega por idGrupoNegocio e retorna LinhaNegocio virtuais
    (uma por grupo): taxa=(MAX+MIN)/2, volume=SUM/2, spread calculado em Python.
    """
    rows = conn.execute(SQL_BUSCAR_BROKER, (dtAnbima, dtLiquidacao)).fetchall()
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

        taxaMedia = (max(taxas) + min(taxas)) / 2.0
        volumeGrupo = sum(t["vrVolume"] for t in trades) / 2.0
        qtdGrupo = sum(t["vrQuantidade"] for t in trades) // 2

        # Trade mais representativo (maior volume) para metadados e dtNegocio
        rep = max(trades, key=lambda t: t["vrVolume"])
        cdReferencia = rep["cdReferencia"]
        dtNegocio = rep["dtNegocio"]

        vrSpreadOver: float | None = None
        if cdReferencia == "FUNDING":
            vrSpreadOver = taxaMedia
        elif cdReferencia is not None:
            mtm = conn.execute(SQL_MTM_TAXA, (cdReferencia, dtNegocio)).fetchone()
            if mtm:
                vrSpreadOver = ((1 + taxaMedia / 100) / (1 + mtm["vrTaxa"] / 100) - 1) * 100

        result.append(LinhaNegocio(
            cdTicker=rep["cdTicker"],
            cdEmissor=rep["cdEmissor"],
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


def AgregarTicker(cdTicker: str, grupo: list[LinhaNegocio]) -> TickerAgregado:
    """
    Agrega uma lista de LinhaNegocio do mesmo ticker em um único TickerAgregado.
    Atributos do ativo (cdIndexador, vrDuration, dtVencimento, etc.) são
    lidos da primeira linha — são constantes por ticker.
    """
    primeira = grupo[0]
    vrVolumeTotal = sum(t.vrVolume for t in grupo)
    vrQuantidadeTotal = sum(t.vrQuantidade for t in grupo)
    nrTrades = sum(t.nrTrades for t in grupo)
    vrTaxaMedia = MediaPonderada([(t.vrTaxaCalculada, t.vrVolume) for t in grupo])
    vrSpreadOverMedio = MediaPonderada([(t.vrSpreadOver, t.vrVolume) for t in grupo])

    # Anbima e atributos do ativo: pegar da primeira linha (são idênticos por ticker)
    vrTaxaAnbima = primeira.vrTaxaAnbima
    vrSpreadAnbima = primeira.vrSpreadAnbima

    return TickerAgregado(
        cdTicker=cdTicker,
        cdEmissor=primeira.cdEmissor,
        cdInstrumento=primeira.cdInstrumento,
        cdIndexador=primeira.cdIndexador,
        cdReferencia=primeira.cdReferencia,
        vrDuration=primeira.vrDuration,
        dtVencimento=primeira.dtVencimento,
        nrTrades=nrTrades,
        vrQuantidadeTotal=vrQuantidadeTotal,
        vrVolumeTotal=vrVolumeTotal,
        vrTaxaMedia=vrTaxaMedia,
        vrSpreadOverMedio=vrSpreadOverMedio,
        vrTaxaAnbima=vrTaxaAnbima,
        vrSpreadAnbima=vrSpreadAnbima,
    )


def MontarContexto(
    dtLiquidacao: str,
    modo: str,
    tickers: list[TickerAgregado],
) -> dict:
    """
    Monta o dicionário de contexto passado ao template Jinja2.
    Todos os instrumentos em lista única, ordenados DEB→CRI→CRA por volume DESC.
    resumoSecoes: totais por instrumento para o cabeçalho.
    """
    dtObj = datetime.fromisoformat(dtLiquidacao)
    dtLiquidacaoFmt = dtObj.strftime("%d/%m/%Y")
    dtGeracao = datetime.now().strftime("%d/%m/%Y %H:%M")

    ordemInstr = {"DEB": 0, "DEB 12.431": 1, "CRI": 2, "CRA": 3}

    def TipoDe(t: TickerAgregado) -> str:
        return TipoExibicao(t.cdInstrumento, t.cdIndexador) or "OUTRO"

    tickersSorted = sorted(
        tickers,
        key=lambda t: (ordemInstr.get(TipoDe(t), 99), -(t.vrVolumeTotal or 0)),
    )

    # Resumo por tipo de exibição (DEB / DEB 12.431 / CRI / CRA) para chips no cabeçalho
    resumoMap: dict[str, dict] = {}
    for t in tickers:
        tipo = TipoDe(t)
        if tipo not in resumoMap:
            resumoMap[tipo] = {"cdTipo": tipo, "nrTickers": 0, "vrVolume": 0.0, "nrTrades": 0}
        resumoMap[tipo]["nrTickers"] += 1
        resumoMap[tipo]["vrVolume"] += t.vrVolumeTotal or 0.0
        resumoMap[tipo]["nrTrades"] += t.nrTrades

    ordemSecoes = ["DEB", "DEB 12.431", "CRI", "CRA"]
    resumoOrdenado = [resumoMap[i] for i in ordemSecoes if i in resumoMap]
    resumoOrdenado += [v for k, v in resumoMap.items() if k not in ordemSecoes]

    instrumentosDisponiveis = [r["cdTipo"] for r in resumoOrdenado]
    indexadoresDisponiveis = sorted({t.cdIndexador for t in tickers if t.cdIndexador})

    vrVolumeTotalGeral = sum(t.vrVolumeTotal or 0.0 for t in tickers)
    nrTickersGeral = len(tickers)
    nrTradesGeral = sum(t.nrTrades for t in tickers)
    vrQuantidadeTotalGeral = sum(t.vrQuantidadeTotal for t in tickers)

    tickerDicts = [
        {
            "cdTicker": t.cdTicker,
            "cdEmissor": t.cdEmissor,
            "cdInstrumento": t.cdInstrumento or "—",
            "cdTipo": TipoExibicao(t.cdInstrumento, t.cdIndexador) or "—",
            "cdIndexador": t.cdIndexador,
            "cdReferencia": t.cdReferencia,
            "vrDuration": t.vrDuration,
            "dtVencimento": t.dtVencimento,
            "nrTrades": t.nrTrades,
            "vrQuantidadeTotal": t.vrQuantidadeTotal,
            "vrVolumeTotal": t.vrVolumeTotal,
            "vrTaxaMedia": t.vrTaxaMedia,
            "vrSpreadOverMedio": t.vrSpreadOverMedio,
            "vrTaxaAnbima": t.vrTaxaAnbima,
            "vrSpreadAnbima": t.vrSpreadAnbima,
        }
        for t in tickersSorted
    ]

    return {
        "dtLiquidacao": dtLiquidacao,
        "dtLiquidacaoFmt": dtLiquidacaoFmt,
        "modo": modo,
        "dtGeracao": dtGeracao,
        "tickers": tickerDicts,
        "resumoSecoes": resumoOrdenado,
        "instrumentosDisponiveis": instrumentosDisponiveis,
        "indexadoresDisponiveis": indexadoresDisponiveis,
        "vrVolumeTotalGeral": vrVolumeTotalGeral,
        "nrTickersGeral": nrTickersGeral,
        "nrTradesGeral": nrTradesGeral,
        "vrQuantidadeTotalGeral": vrQuantidadeTotalGeral,
    }


def RenderizarHtml(contexto: dict, log) -> str:
    from jinja2 import Environment, FileSystemLoader

    templatesDir = Path(cfg["paths"]["templatesDir"])
    env = Environment(loader=FileSystemLoader(str(templatesDir)), autoescape=True)
    env.filters["br"] = lambda v, dec=2: FmtBr(v, dec)
    env.filters["intbr"] = FmtIntBr
    env.filters["mesbr"] = FmtMesAno
    template = env.get_template("relatorio.html.j2")
    html = template.render(**contexto)
    log.debug("gerar_relatorio_html: HTML renderizado (%d bytes)", len(html))
    return html


def MontarCaminhoSaida(dtLiquidacao: str, modo: str) -> Path:
    """
    Retorna o Path do arquivo HTML de saída.
    Prévia: DD_MM_YYYY_previa_HHMM.html
    Definitivo: DD_MM_YYYY_definitivo.html
    """
    relatoriosDir = Path(cfg["paths"]["relatoriosDir"])
    relatoriosDir.mkdir(parents=True, exist_ok=True)

    dtObj = datetime.fromisoformat(dtLiquidacao)
    dataPart = dtObj.strftime("%d_%m_%Y")

    if modo == "previa":
        agora = datetime.now().strftime("%H%M")
        nomeArquivo = f"{dataPart}_previa_{agora}.html"
    else:
        nomeArquivo = f"{dataPart}_definitivo.html"

    return relatoriosDir / nomeArquivo


def EnviarEmailComAnexo(
    scriptName: str,
    success: bool,
    summaryText: str,
    caminhoHtml: Path | None,
    log,
) -> None:
    """
    Envia email via Outlook com o HTML gerado como anexo.
    Roda em thread separada com CoInitialize + timeout de 60s.
    Falha silenciosa: loga erro, não propaga.
    """
    from lib.config import ObterListaEmails

    if not cfg["email"]["ativo"]:
        log.info("Email desativado por config.toml — nao enviado.")
        return

    status = "OK" if success else "ERROR"
    subject = f"[{status}] {scriptName}"
    body = f"Script: {scriptName}\nStatus: {status}\n\nResumo:\n{summaryText}"

    destinatarios = ObterListaEmails("outlook")
    if not destinatarios:
        log.warning("Sem destinatarios (OUTLOOK_TO / destinatarios.py) — email nao enviado.")
        return

    anexo = str(caminhoHtml.resolve()) if caminhoHtml and caminhoHtml.exists() else None

    def Rodar():
        import pythoncom  # type: ignore[import]
        import win32com.client  # type: ignore[import]
        pythoncom.CoInitialize()
        try:
            outlook = win32com.client.Dispatch("Outlook.Application")
            ns = outlook.GetNamespace("MAPI")
            for dest in destinatarios:
                mail = outlook.CreateItem(0)
                mail.To = dest
                mail.Subject = subject
                mail.Body = body
                if anexo:
                    mail.Attachments.Add(anexo)
                mail.Send()
                log.info("Email enfileirado para %s: %s", dest, subject)
            ns.SendAndReceive(False)
            log.info("SendAndReceive concluido.")
        except Exception as e:
            log.error("Falha ao enviar email via Outlook: %s", e)
        finally:
            pythoncom.CoUninitialize()

    t = threading.Thread(target=Rodar, daemon=True)
    t.start()
    t.join(timeout=60)
    if t.is_alive():
        log.warning("Email nao enviado em 60s — verificar dialog de permissao no Outlook.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def LerArgumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Gera relatório HTML de crédito privado por dtLiquidacao, "
            "agregando trades VALIDO por ticker."
        )
    )
    parser.add_argument(
        "--date",
        required=True,
        metavar="YYYY-MM-DD",
        help="dtLiquidacao a reportar.",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["previa", "definitivo"],
        help="Tipo do relatório: 'previa' ou 'definitivo'.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Principal() -> None:
    log = ObterLogger("gerar_relatorio_html")
    args = LerArgumentos()

    dtLiquidacao: str = args.date
    modo: str = args.mode

    conn = ObterBanco()
    summary = ""
    success = True
    caminhoHtml: Path | None = None

    try:
        log.info(
            "gerar_relatorio_html: iniciando | dtLiquidacao=%s modo=%s",
            dtLiquidacao, modo,
        )

        # 1. Buscar trades VALIDO + grupos BROKER agregados
        # Taxa/spread Anbima sempre de D-1 (dia útil anterior à liquidação)
        dtAnbima = CalcularDMenos1(dtLiquidacao)
        log.info("gerar_relatorio_html: dtAnbima (D-1) = %s", dtAnbima)

        linhas = BuscarNegocios(conn, dtLiquidacao, dtAnbima)
        linhasBroker = BuscarGruposBroker(conn, dtLiquidacao, dtAnbima)
        linhas.extend(linhasBroker)
        log.info(
            "gerar_relatorio_html: %d linha(s) VALIDO + %d grupo(s) BROKER",
            len(linhas) - len(linhasBroker), len(linhasBroker),
        )

        if not linhas:
            log.warning(
                "gerar_relatorio_html: nenhum trade para dtLiquidacao=%s",
                dtLiquidacao,
            )

        # 2. Agregar por ticker
        gruposTicker: dict[str, list[LinhaNegocio]] = {}
        for linha in linhas:
            gruposTicker.setdefault(linha.cdTicker, []).append(linha)

        tickers = [
            AgregarTicker(cdTicker, grupo)
            for cdTicker, grupo in gruposTicker.items()
        ]

        # 3. Montar contexto para o template
        contexto = MontarContexto(dtLiquidacao, modo, tickers)

        # 4. Renderizar HTML
        htmlStr = RenderizarHtml(contexto, log)

        # 5. Salvar arquivo
        caminhoHtml = MontarCaminhoSaida(dtLiquidacao, modo)
        caminhoHtml.write_text(htmlStr, encoding="utf-8")
        log.info("gerar_relatorio_html: HTML salvo em %s", caminhoHtml)

        # 6. Montar summary para email
        linhasPorSecao = "\n".join(
            f"  {r['cdTipo']}: {r['nrTickers']} ticker(s), "
            f"R$ {r['vrVolume'] / 1_000_000:.2f} MM, "
            f"{r['nrTrades']} neg."
            for r in contexto["resumoSecoes"]
        )
        summary = (
            f"dtLiquidacao : {dtLiquidacao}\n"
            f"Modo         : {modo}\n"
            f"Arquivo      : {caminhoHtml}\n\n"
            f"Tickers      : {contexto['nrTickersGeral']}\n"
            f"Trades       : {contexto['nrTradesGeral']}\n"
            f"Volume total : R$ {contexto['vrVolumeTotalGeral'] / 1_000_000:.2f} MM\n\n"
            f"Por seção:\n{linhasPorSecao if linhasPorSecao else '  (sem dados)'}"
        )

        log.info("gerar_relatorio_html: concluido.\n%s", summary)

    except Exception:
        success = False
        summary = traceback.format_exc()
        log.exception("gerar_relatorio_html: erro inesperado")

    finally:
        conn.close()
        EnviarEmailComAnexo(
            "gerar_relatorio_html",
            success,
            summary,
            caminhoHtml,
            log,
        )


if __name__ == "__main__":
    Principal()
