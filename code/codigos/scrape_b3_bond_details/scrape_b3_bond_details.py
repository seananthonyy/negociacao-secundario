"""
scrape_b3_bond_details — cadastro e fluxo de caixa dos ativos, pela B3.

Fonte PRIMARIA do cadastro. Roda logo depois do boletim: pega os tickers que
NEGOCIARAM no dia (de NegociosBrutos — o filtrar_trades so roda bem depois) e aos quais falta alguma informacao que este endpoint preenche, e so entao
bate na B3 (GET /getBondDetails/{ticker}). Sem esse gate seriam ~2.900 chamadas por
rodada — foi quanto negociou nos ultimos 30 dias.

Por que a B3 e primaria (medido em 12/07/2026, 802 ativos IPCA validados):
  - modelo Anbima (VNE cru + evento de incorporacao) : 719 batem o PU (89,7%)
  - modelo B3     (VNE ja capitalizado)              : 741 batem o PU (92,4%)
  - 35 papeis SO batem pelo modelo B3, e sao os graves (CRA020001US errava 50,8%,
    SSRU11 errava 58%). Apenas 12 so batem pelo Anbima, e neles o dado da B3 e lixo.

DUAS ARMADILHAS, ambas silenciosas:

1. `vrVNE` + `dtInicioRentabilidade` + `FluxoAtivos` sao um PACOTE INDIVISIVEL. A B3
   pre-capitaliza a carencia dentro do VNE e nao emite evento de incorporacao; a Anbima
   traz o VNE cru e a incorporacao como evento. Misturar conta a capitalizacao DUAS
   VEZES. Por isso `cdFonteCadastro`: ou o pacote e todo da B3, ou e todo da Anbima.
   (SSRU11: B3 diz VNE 10.561,83 / inicio 28/11/2018; Anbima diz 10.000 / 29/06/2018
   mais 100% de incorporacao. As duas descrevem o mesmo papel.)

2. As datas de CUPOM ('J') tem que entrar no fluxo como eventos de amortizacao ZERO.
   A calc ancora o juros de cada periodo no evento anterior; sem as datas de cupom ela
   acha que o papel acumula juros por anos sem pagar. Guardar so os 'A' derruba a
   aderencia do modelo B3 de 92% para 25%.

Fluxo vindo da B3 nasce VALIDADO (stFluxoValidado = 1): ele E a fonte, nao ha contra o
que conferir. Quem confere se essa agenda continua valendo e o ConferirSaldo do
validar_calc_b3, contra a calcPU da B3/FI — fonte tambem apodrece (o EMIV11 foi
aditado em fev/26 e a agenda velha continuou de pe).
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from traceback import format_exc

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Helpers"))

from b3_calc_api import ObterDetalhesAtivo
from calc import ImportarCalc
from db import ObterBanco
from email_outlook import EnviarEmailConclusao
from logger import ObterLogger
from relatorio_execucao import RelatorioExecucao

NOME_SCRIPT = "scrape_b3_bond_details"

WORKERS = 8

# 'method' da B3 -> nosso cdIndexador.
MAPA_INDEXADOR = {
    "IPCA-I": "IPCA", "IPCA": "IPCA",
    "DI-PERC": "%CDI", "DI-SPREAD": "CDI+",
    "PRE": "PREFIXADO",
}

# Campos escalares que a B3 preenche e que NAO fazem parte do pacote indivisivel.
# Preenchidos so quando estao NULL (a Anbima continua dona do que ja gravou).
CAMPOS_ESCALARES = ("cdEmissor", "dtVencimento", "dtEmissao", "vrTaxaEmissao",
                    "cdIndexador", "cdInstrumento")


def MapearIndexador(method) -> str | None:
    if not method:
        return None
    if str(method).upper().startswith("IPCA"):
        return "IPCA"
    return MAPA_INDEXADOR.get(method)


def LerArgumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cadastro e fluxo dos ativos negociados, via B3 getBondDetails.")
    parser.add_argument("--date", dest="date", default=None,
                        help="data do pregao (YYYY-MM-DD). Default: ultimo dia com negocio.")
    parser.add_argument("--start", dest="start", default=None, help="inicio do intervalo")
    parser.add_argument("--end", dest="end", default=None, help="fim do intervalo")
    parser.add_argument("--tickers", dest="tickers", default=None,
                        help="lista separada por virgula; ignora o gate")
    parser.add_argument("--todos", dest="todos", action="store_true",
                        help="varre TODO o InfoAtivos (backfill), nao so o que negociou")
    parser.add_argument("--forcar", dest="forcar", action="store_true",
                        help="ignora o gate de info faltante — para reprocessar depois de "
                             "corrigir o parser do fluxo")
    parser.add_argument("--limite", dest="limite", type=int, default=None,
                        help="processa so os N primeiros da fila")
    return parser.parse_args()


def MontarFila(args: argparse.Namespace, conn, log) -> tuple[list[str], list[str]]:
    """(fila, datas). A fila ja vem filtrada pelo gate de informacao faltante."""
    if args.tickers:
        return [t.strip().upper() for t in args.tickers.split(",") if t.strip()], []

    # Gate: so vale bater na B3 se falta algo que ela preenche.
    #   - nunca passou pela B3 (cdFonteCadastro NULL)   -> tenta
    #   - algum escalar NULL                            -> tenta
    #   - IPCA sem aniversario                          -> tenta (a calc precisa)
    #   - sem fluxo na base                             -> tenta
    faltando = "1=1" if args.forcar else (
        "(i.cdFonteCadastro IS NULL"
        f"  OR {' OR '.join(f'i.{c} IS NULL' for c in CAMPOS_ESCALARES)}"
        "  OR (i.cdIndexador = 'IPCA' AND i.vrAniversario IS NULL)"
        "  OR i.stTemFluxo = 0)"
    )

    if args.todos:
        fila = [r["cdTicker"] for r in conn.execute(
            f"SELECT i.cdTicker FROM InfoAtivos i WHERE {faltando} ORDER BY i.cdTicker")]
        log.info("%s: modo --todos, %d ativo(s) com informacao faltante", NOME_SCRIPT, len(fila))
        return fila, []

    if args.start and args.end:
        onde, params = "n.dtNegocio BETWEEN ? AND ?", (args.start, args.end)
    else:
        dtRef = args.date or conn.execute(
            "SELECT MAX(dtNegocio) FROM NegociosBrutos").fetchone()[0]
        onde, params = "n.dtNegocio = ?", (dtRef,)

    datas = [r[0] for r in conn.execute(
        f"SELECT DISTINCT n.dtNegocio FROM NegociosBrutos n WHERE {onde} ORDER BY 1", params)]

    # LEFT JOIN: ticker que negociou e nem existe no InfoAtivos tambem entra na fila —
    # sao 701 dos 1.085 que negociaram em 90 dias e nao tem fluxo nenhum na base.
    fila = [r["cdTicker"] for r in conn.execute(
        f"""SELECT DISTINCT n.cdTicker
              FROM NegociosBrutos n
              LEFT JOIN InfoAtivos i ON i.cdTicker = n.cdTicker
             WHERE {onde} AND (i.cdTicker IS NULL OR {faltando})
             ORDER BY n.cdTicker""", params)]

    negociaram = conn.execute(
        f"SELECT COUNT(DISTINCT n.cdTicker) FROM NegociosBrutos n WHERE {onde}", params).fetchone()[0]
    log.info("%s: %d ticker(s) negociaram; %d com informacao faltante (gate)",
             NOME_SCRIPT, negociaram, len(fila))
    return fila, datas


def FluxoDaB3(det: dict) -> list[tuple[str, float | None, float | None]]:
    """Eventos da B3 -> [(dtEvento, vrPctAmortizacao, vrPctIncorporacao)].

    'A' = amortizacao (% do principal ORIGINAL — a B3 manda saldo_original).
    'J' = cupom. So no estilo 'IPCA-I' o yield do 'J' e a %incorporacao; nos demais e a
    propria taxa do cupom, e le-la como incorporacao transformaria todo IPCA simples em
    falso-divergente.

    TRES normalizacoes, e sem as tres o PU sai errado (medido: 122 dos 2.853 ativos com
    erro acima de 1%, e os 86 piores eram todos IPCA-I):

    1. DATAS AJUSTADAS PARA DIA UTIL. A B3 manda a data CRUA (o TAEE17 incorpora em
       15/03/2025, um sabado). A calc casa evento com aniversario, e o aniversario e
       ajustado por ProximoDu — entao evento passado que caia em fim de semana NAO CASA
       e e silenciosamente descartado. Guardar ja ajustado alinha as duas pontas.

    2. AMORTIZACAO FINAL COMPLETADA. Nos IPCA-I a B3 nao emite o 'A' do vencimento: os
       'A' somam menos de 100 (TAEE17 soma 96,8) e o vencimento vem so com um 'J' de
       yield 0. Isso e fatal porque o InferirTipoAmort da calc decide a convencao pela
       SOMA: != 100 vira 'saldo_restante', e o papel inteiro passa a ser amortizado na
       convencao errada. O resto vai para o vencimento.

    3. DATAS DE CUPOM ('J') ENTRAM como eventos de amortizacao zero: a calc ancora o
       juros de cada periodo no evento anterior. Guardar so os 'A' derruba a aderencia
       do modelo B3 de 92% para 25%.
    """
    C = ImportarCalc()
    leIncorporacao = det.get("method") == "IPCA-I"

    def Normalizar(bruta: str) -> date | None:
        try:
            d = date.fromisoformat(str(bruta)[:10])
        except ValueError:
            return None
        return C.ProximoDu(d, C.FERIADOS_ANBIMA)   # (1)

    eventos: dict[date, list] = {}
    for e in det.get("events") or []:
        dtEvento = Normalizar(e.get("date"))
        if dtEvento is None:
            continue
        pct = float(e.get("yield") or 0)
        tipo = e.get("eventType")
        linha = eventos.setdefault(dtEvento, [None, None])   # (3)
        if tipo == "A" and pct > 0:
            linha[0] = pct
        elif tipo == "J" and leIncorporacao and pct > 0:
            linha[1] = pct

    if not eventos:
        return []

    # (2) — tolerancia de 0,001 p.p.: soma exata nao precisa de nada.
    resto = 100.0 - sum(v[0] or 0.0 for v in eventos.values())
    if 0.001 < resto <= 100.0:
        dtVencimento = Normalizar(det.get("expiredate")) or max(eventos)
        linha = eventos.setdefault(dtVencimento, [None, None])
        linha[0] = (linha[0] or 0.0) + resto

    return sorted((d.isoformat(), v[0], v[1]) for d, v in eventos.items())


def GravarAtivo(conn, cdTicker: str, det: dict, agora: str) -> str:
    """Grava o que a B3 sabe. Devolve 'inseridos' | 'atualizados' | 'ignorados'.

    Sem eventos, a B3 nao serve de fonte de fluxo: preenche so os escalares que estao
    NULL e NAO toca em vrVNE / dtInicioRentabilidade / FluxoAtivos (o pacote).
    """
    antes = conn.execute(
        "SELECT cdTicker, stTemFluxo, cdFonteCadastro FROM InfoAtivos WHERE cdTicker = ?",
        (cdTicker,)).fetchone()
    novo = antes is None

    cdIndexador = MapearIndexador(det.get("method"))
    tipoIf = det.get("tipoIF") or {}
    valores = {
        "cdEmissor": det.get("issuer"),
        "dtVencimento": (det.get("expiredate") or "")[:10] or None,
        "dtEmissao": (det.get("issuedate") or "")[:10] or None,
        "vrTaxaEmissao": det.get("yield"),
        "cdIndexador": cdIndexador,
        "cdInstrumento": tipoIf.get("codigoAsString") if isinstance(tipoIf, dict) else None,
    }
    # Aniversario so faz sentido em IPCA: nos demais indexadores o VNA nao sofre
    # correcao monetaria e a calc ignora o parametro.
    aniversario = det.get("anniversaryday")
    vrAniversario = int(aniversario) if (cdIndexador == "IPCA" and aniversario is not None) else None

    if novo:
        conn.execute("INSERT INTO InfoAtivos (cdTicker, dtAtualizacao) VALUES (?, ?)",
                     (cdTicker, agora))

    # COALESCE nos escalares: a B3 preenche buraco, nao sobrescreve o que a Anbima ja
    # gravou. Excecao: vrAniversario, para o qual a B3 e a unica fonte explicita.
    sets = ", ".join(f"{c} = COALESCE({c}, ?)" for c in CAMPOS_ESCALARES)
    conn.execute(
        f"UPDATE InfoAtivos SET {sets}, "
        "  vrAniversario = COALESCE(?, vrAniversario), dtAtualizacao = ? WHERE cdTicker = ?",
        tuple(valores[c] for c in CAMPOS_ESCALARES) + (vrAniversario, agora, cdTicker))

    fluxo = FluxoDaB3(det)
    if not fluxo:
        return "inseridos" if novo else "ignorados"

    # O PACOTE. Escrever vrVNE/dtInicioRentabilidade dispara trgInfoAtivosInvalidaFluxo,
    # que zera stFluxoValidado — por isso a validacao e reafirmada DEPOIS, mais abaixo.
    conn.execute(
        "UPDATE InfoAtivos SET vrVNE = ?, dtInicioRentabilidade = ?, cdFonteCadastro = 'B3', "
        "  stTemFluxo = 1, dtAtualizacao = ? WHERE cdTicker = ?",
        (det.get("vne"), (det.get("startingdate") or "")[:10] or None, agora, cdTicker))

    conn.execute("DELETE FROM FluxoAtivos WHERE cdTicker = ?", (cdTicker,))
    conn.executemany(
        "INSERT INTO FluxoAtivos (cdTicker, dtEvento, vrPctAmortizacao, vrPctIncorporacao, "
        "dtAtualizacao) VALUES (?, ?, ?, ?, ?)",
        [(cdTicker, d, a, i, agora) for d, a, i in fluxo])

    # Este script NAO valida (24/08/2026). "Fluxo veio da B3" nao e o mesmo que "a calc
    # precifica este ativo certo": marcar validado aqui liberava para a calc local ativo
    # que nunca passou pelo gate, com erro de PU de ate 70%. Quem marca stFluxoValidado=1
    # e SO o validar_calc_b3, e so depois de a nossa calc reproduzir a B3 (ou a FI).
    # Ate la o ativo cai na cascata de API no calc_taxa, que e o comportamento seguro.

    return "inseridos" if (novo or not antes["stTemFluxo"]) else "atualizados"


def Principal() -> None:
    log = ObterLogger(NOME_SCRIPT)
    args = LerArgumentos()
    rel = RelatorioExecucao(NOME_SCRIPT, args=vars(args))
    success = True

    try:
        conn = ObterBanco()
        try:
            fila, datas = MontarFila(args, conn, log)
            rel.Datas(datas)
            if args.limite:
                fila = fila[:args.limite]
            rel.Metrica("Ativos na fila (gate de info faltante)", len(fila))

            if not fila:
                rel.Aviso("Nada a fazer: todos os tickers negociados ja tem o cadastro completo.")
                EnviarEmailConclusao(NOME_SCRIPT, True, rel, logger=log)
                return

            # O getBondDetails e I/O puro e o httpx.Client e thread-safe: paraleliza.
            # A escrita no SQLite fica na thread principal (uma conexao, sem contencao).
            log.info("%s: buscando %d ativo(s) na B3...", NOME_SCRIPT, len(fila))
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                detalhes = list(pool.map(ObterDetalhesAtivo, fila))

            agora = datetime.now().isoformat(timespec="seconds")
            semCobertura = []
            for cdTicker, det in zip(fila, detalhes):
                if not det:
                    semCobertura.append(cdTicker)
                    rel.Contar("ignorados")
                    continue
                try:
                    acao = GravarAtivo(conn, cdTicker, det, agora)
                except Exception as exc:
                    log.warning("%s: falha ao gravar %s: %s", NOME_SCRIPT, cdTicker, exc)
                    rel.Contar("falhas")
                    rel.Exemplo("falhas", {"cdTicker": cdTicker, "erro": str(exc)[:120]})
                    continue

                rel.Contar(acao)
                if acao in ("inseridos", "atualizados"):
                    rel.Exemplo(acao, {
                        "cdTicker": cdTicker,
                        "indexador": MapearIndexador(det.get("method")),
                        "vrVNE": det.get("vne"),
                        "inicio": (det.get("startingdate") or "")[:10],
                        "aniversario": det.get("anniversaryday"),
                        "eventos": len(FluxoDaB3(det)),
                    })
            conn.commit()

            rel.Metrica("Sem cobertura da B3 (caem para a Anbima)", len(semCobertura))
            if semCobertura:
                rel.Aviso(f"{len(semCobertura)} ticker(s) a B3 nao cobre — o "
                          f"scrape_anbima_data_ativos e quem vai preencher.")
            log.info("%s: concluido. %s", NOME_SCRIPT, rel.contadores)

        finally:
            conn.close()

    except Exception:
        success = False
        rel.Erro("A rodada abortou — ver traceback.")
        log.error("%s: falhou\n%s", NOME_SCRIPT, format_exc())
        EnviarEmailConclusao(NOME_SCRIPT, False, rel, tracebackErro=format_exc(), logger=log)
        sys.exit(1)

    EnviarEmailConclusao(NOME_SCRIPT, success, rel, logger=log)


if __name__ == "__main__":
    Principal()
