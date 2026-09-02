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
from datetime import date, datetime, timedelta
from pathlib import Path
from traceback import format_exc

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Helpers"))

from b3_calc_api import ObterDetalhesAtivo
from cadastro_b3 import (CAMPOS_ESCALARES, FluxoDaB3, GravarLote, MapearIndexador,
                         PrepararAtivo)
import dados as D
from email_outlook import EnviarEmailConclusao
from logger import ObterLogger
from relatorio_execucao import RelatorioExecucao

NOME_SCRIPT = "scrape_b3_bond_details"

WORKERS = 8

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
    parser.add_argument("--refrescar-dias", dest="refrescarDias", type=int, default=30,
                        help="tambem re-raspa ativo cujo fluxo da B3 tem mais de N dias "
                             "(default 30). 0 desliga. Ver REFRESCO em MontarFila.")
    parser.add_argument("--refrescar-max", dest="refrescarMax", type=int, default=150,
                        help="teto de ativos REFRESCADOS por rodada, do mais velho para o "
                             "mais novo (default 150). Nao limita quem entrou na fila por "
                             "informacao faltante. 0 = sem teto.")
    parser.add_argument("--limite", dest="limite", type=int, default=None,
                        help="processa so os N primeiros da fila")
    return parser.parse_args()


def Unir(faltantes: list[str], refrescar: list[str]) -> list[str]:
    """Junta as duas filas sem repetir, com os FALTANTES na frente.

    A ordem importa quando ha `--limite`: quem nao tem cadastro nenhum bloqueia a calc
    hoje, enquanto quem so envelheceu ainda esta precificando. Cortar a fila pelo fim
    tem de sacrificar o refresco, nunca o buraco."""
    vistos = set(faltantes)
    return faltantes + [t for t in refrescar if t not in vistos]


def FilaRefresco(args: argparse.Namespace, log) -> list[str]:
    """Os ativos cujo fluxo da B3 envelheceu, do MAIS VELHO para o mais novo, ate o teto.

    Por que existe (01/09/2026). O gate de `MontarFila` so pergunta "falta alguma
    coisa?". Fluxo que JA EXISTE nunca era re-raspado, entao ele so envelhecia — e a
    fonte tambem apodrece: o EMIV11 foi aditado em fev/26 e a agenda velha continuou de
    pe. Foi essa deriva silenciosa que produziu os 19 fluxos defasados da faxina de
    14/07/2026.

    Por que TETO e nao so idade. Medido na base local em 01/09/2026: sem teto, o corte de
    30 dias leva a fila de 1.917 para 5.009 ativos de uma vez — porque nenhum fluxo tinha
    sido refrescado ainda, entao a populacao inteira vence junta. Numa API que CONTA
    chamada, isso e um pico de ~3.000 requisicoes na primeira rodada e zero nas
    seguintes. Com teto + ordem do mais velho, o refresco vira uma ESTEIRA: um pedaco por
    dia, sempre o mais atrasado, sem pico nenhum, e a populacao converge para "ninguem com
    mais de N dias" em poucas rodadas.

    A ancora e `FluxoAtivos.dtAtualizacao`, nao `InfoAtivos.dtAtualizacao`: o segundo e
    escrito por QUALQUER fonte que toque o ativo (a FI Analytics escreve a cada rodada),
    entao um toque da FI zeraria o relogio sem que a B3 tivesse sido consultada, e o
    ativo nunca mais refrescaria. Restrito a `cdFonteCadastro = 'B3'` porque so ai a
    agenda em disco e da B3 — ativo da Anbima nao e assunto deste script.

    A comparacao de texto funciona apesar de a base misturar os dois separadores de ISO
    ('2026-08-01T10:00:00' do cadastro_b3 e '2026-08-01 10:00:00' dos demais): contra um
    corte que e so a data, os dois ordenam certo.
    """
    if args.forcar or not args.refrescarDias or args.refrescarDias <= 0:
        return []

    corte = (date.today() - timedelta(days=args.refrescarDias)).isoformat()
    limite = f"LIMIT {args.refrescarMax}" if args.refrescarMax and args.refrescarMax > 0 else ""
    linhas = D.Linhas(f"""
        SELECT i.cdTicker,
               COALESCE((SELECT MAX(f.dtAtualizacao) FROM FluxoAtivos f
                          WHERE f.cdTicker = i.cdTicker), '') AS dtFluxo
          FROM InfoAtivos i
         WHERE i.cdFonteCadastro = 'B3'
           AND COALESCE((SELECT MAX(f.dtAtualizacao) FROM FluxoAtivos f
                          WHERE f.cdTicker = i.cdTicker), '') < ?
         ORDER BY dtFluxo ASC, i.cdTicker
         {limite}""", (corte,))

    vencidos = D.Escalar("""
        SELECT COUNT(*) FROM InfoAtivos i
         WHERE i.cdFonteCadastro = 'B3'
           AND COALESCE((SELECT MAX(f.dtAtualizacao) FROM FluxoAtivos f
                          WHERE f.cdTicker = i.cdTicker), '') < ?""", (corte,))

    fila = [r["cdTicker"] for r in linhas]
    if fila:
        log.info("%s: refresco — %d ativo(s) com fluxo da B3 anterior a %s; "
                 "%d nesta rodada (do mais velho, teto %s)",
                 NOME_SCRIPT, vencidos, corte, len(fila),
                 args.refrescarMax or "sem teto")
    return fila


def MontarFila(args: argparse.Namespace, log) -> tuple[list[str], list[str]]:
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
        fila = [r["cdTicker"] for r in D.Linhas(
            f"SELECT i.cdTicker FROM InfoAtivos i WHERE {faltando} ORDER BY i.cdTicker")]
        log.info("%s: modo --todos, %d ativo(s) com informacao faltante", NOME_SCRIPT, len(fila))
        return Unir(fila, FilaRefresco(args, log)), []

    if args.start and args.end:
        onde, params = "n.dtNegocio BETWEEN ? AND ?", (args.start, args.end)
    else:
        dtRef = args.date or D.Escalar("SELECT MAX(dtNegocio) FROM NegociosBrutos")
        onde, params = "n.dtNegocio = ?", (dtRef,)

    datas = [r[0] for r in D.Tuplas(
        f"SELECT DISTINCT n.dtNegocio FROM NegociosBrutos n WHERE {onde} ORDER BY 1", params)]

    # LEFT JOIN: ticker que negociou e nem existe no InfoAtivos tambem entra na fila —
    # sao 701 dos 1.085 que negociaram em 90 dias e nao tem fluxo nenhum na base.
    fila = [r["cdTicker"] for r in D.Linhas(
        f"""SELECT DISTINCT n.cdTicker
              FROM NegociosBrutos n
              LEFT JOIN InfoAtivos i ON i.cdTicker = n.cdTicker
             WHERE {onde} AND (i.cdTicker IS NULL OR {faltando})
             ORDER BY n.cdTicker""", params)]

    negociaram = D.Escalar(
        f"SELECT COUNT(DISTINCT n.cdTicker) FROM NegociosBrutos n WHERE {onde}", params)
    log.info("%s: %d ticker(s) negociaram; %d com informacao faltante (gate)",
             NOME_SCRIPT, negociaram, len(fila))
    return Unir(fila, FilaRefresco(args, log)), datas


def Principal() -> None:
    log = ObterLogger(NOME_SCRIPT)
    args = LerArgumentos()
    rel = RelatorioExecucao(NOME_SCRIPT, args=vars(args))
    success = True

    try:
        fila, datas = MontarFila(args, log)
        rel.Datas(datas)
        if args.limite:
            fila = fila[:args.limite]
        rel.Metrica("Ativos na fila (gate de info faltante)", len(fila))

        if not fila:
            rel.Aviso("Nada a fazer: todos os tickers negociados ja tem o cadastro completo.")
            EnviarEmailConclusao(NOME_SCRIPT, True, rel, logger=log)
            return

        # O getBondDetails e I/O puro e o httpx.Client e thread-safe: paraleliza.
        log.info("%s: buscando %d ativo(s) na B3...", NOME_SCRIPT, len(fila))
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            detalhes = list(pool.map(ObterDetalhesAtivo, fila))

        agora = datetime.now().isoformat(timespec="seconds")
        semCobertura = []
        # InfoAtivos e FluxoAtivos sao arquivos unicos: monta-se tudo em memoria e
        # grava-se uma vez so, no lugar do commit() unico que o SQLite tinha.
        infoAtual = {r["cdTicker"]: r for r in D.Linhas(
            "SELECT cdTicker, stTemFluxo, cdFonteCadastro FROM InfoAtivos")}
        escalares, pacotes, fluxos = [], [], {}

        for cdTicker, det in zip(fila, detalhes):
            if not det:
                semCobertura.append(cdTicker)
                rel.Contar("ignorados")
                continue
            try:
                linhaEsc, pacote, linhasFluxo, acao = PrepararAtivo(
                    cdTicker, det, agora, infoAtual.get(cdTicker))
                escalares.append(linhaEsc)
                if pacote:
                    pacotes.append(pacote)
                    fluxos[cdTicker] = linhasFluxo
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

        GravarLote(escalares, pacotes, fluxos)

        rel.Metrica("Sem cobertura da B3 (caem para a Anbima)", len(semCobertura))
        if semCobertura:
            rel.Aviso(f"{len(semCobertura)} ticker(s) a B3 nao cobre — o "
                      f"scrape_anbima_data_ativos e quem vai preencher.")
        log.info("%s: concluido. %s", NOME_SCRIPT, rel.contadores)

    except Exception:
        success = False
        rel.Erro("A rodada abortou — ver traceback.")
        log.error("%s: falhou\n%s", NOME_SCRIPT, format_exc())
        EnviarEmailConclusao(NOME_SCRIPT, False, rel, tracebackErro=format_exc(), logger=log)
        sys.exit(1)

    EnviarEmailConclusao(NOME_SCRIPT, success, rel, logger=log)


if __name__ == "__main__":
    Principal()
