"""
calc_pu_par.py
==============
Grava o **PU PAR** de cada ativo que negociou, por data, na tabela `PuPar`.

PU par e o PU que o papel valeria precificado na PROPRIA taxa de emissao. E o
denominador do %par do negocio:

    %par = vrPU / vrPuPar * 100

100 e no par; acima e agio, abaixo e desagio. E como a mesa le "caro ou barato" sem
depender de spread nem de match de curva -- e, num papel que saiu do cadastro das
calculadoras, e o UNICO numero que sobra (taxa e spread somem junto com o cadastro).

Este script NAO calcula o %par. Ele grava so o puPar; o %par sai na LEITURA, no
relatorio. Ver "Por que o %par nao e gravado", abaixo.

A chave e (cdTicker, dtReferencia)
----------------------------------
PuPar nao e propriedade do ativo, e do par (ativo, data): o PU par ACRETA todo dia
util na taxa de emissao. Guardar um valor por ativo e reusa-lo por alguns dias injeta
erro SISTEMATICO, sempre para cima (puPar velho e menor, entao o %par le alto).

Medido em 249 ativos validados, 7 dias uteis (17/07 -> 28/07 de 2026):

    CDI+      109 ativos   mediana 0,411%   p90 0,462%   max 4,73%
    %CDI       18 ativos   mediana 0,372%   p90 0,387%   max 0,405%
    PREFIXADO   7 ativos   mediana 0,364%   p90 0,382%   max 0,382%
    IPCA      115 ativos   mediana 0,250%   p90 0,292%   max 0,382%
    TODOS     249 ativos   mediana 0,368%   p90 0,437%   max 4,73%

Isso entra direto no %par, e dói mais onde o numero e mais util: CDI+ e %CDI vivem
entre 99 e 101, entao a faixa inteira do sinal sao ~2 pontos e 0,4 come 20% dela.

E ha o degrau, pior que a deriva: EVENTO de fluxo dentro da janela. Nos mesmos 7 dias,
128 dos 2.583 ativos negociados (5%) tiveram evento -- o MATD23 amortizou 100%, o
MOVI34 33%, o JHSFA1 11%. Um puPar de tres dias antes daria um %par sem sentido.

Indexar por data ainda sai mais BARATO: sao ~50 mil pares distintos na base inteira, e
cada um se calcula UMA vez na vida (ver Idempotencia). Com janela de N dias a mesma
chamada de API e re-paga para sempre, e devolve um numero errado.

Cascata
-------
    1. calc local  -> cdFontePuPar = 'Calc'   (so stFluxoValidado = 1)
    2. B3          -> 'B3'    CalcularPuGov(ticker, data, vrTaxaEmissao)
    3. FI Analytics-> 'FI'    ChamarCompleto(ticker, data, vrTaxaEmissao), campo m2m
    4. nada        -> nao grava linha

A B3 vem antes da FI porque e a fonte primaria do cadastro e a regua do proprio gate
`validar_calc_b3`: usar a FI primeiro daria um %par de uma fonte com validacao de
outra. E o degrau 2 e o que faz este script valer a pena -- o ativo que o gate rebaixou
e justamente aquele em que a B3 RESPONDEU e a nossa calc nao reproduziu, entao o puPar
da B3 esta disponivel e e autoritativo.

Idempotencia
------------
Par (cdTicker, dtReferencia) que ja existe NAO e recalculado nem consultado em API. O
valor de um par nao muda nunca -- e o PU de um papel numa data passada. Reprocessar um
pregao inteiro custa zero chamada.

Quem invalida e o `dados.DescartarPuPar`, chamado de dentro do `Mesclar`/`Sincronizar*`
quando o cadastro ou o fluxo do ativo muda de verdade: ai o historico inteiro daquele
ticker sai, e a proxima rodada refaz so as datas que tiveram negocio.

Papel que sai do cadastro (distress)
------------------------------------
Quando o emissor se aproxima do default, as calculadoras REMOVEM o papel. O mercado
passa a usar o ultimo puPar disponivel como referencia e negocia em cents on the
dollar. Este script nao faz nada de especial: simplesmente para de conseguir gravar
linha nova. Quem resolve e a LEITURA, que pega o ultimo puPar com dtReferencia <=
dtLiquidacao e mostra a IDADE dele. Nao existe status "congelado" gravado -- a idade E
o status, e a tabela fica so com valor real.

Custo aceito: sem linha gravada, este script re-tenta B3 e FI todo pregao para esses
papeis, e falha sempre. Sao ~2 chamadas por pregao por nome em default -- punhado de
ativos.

Como o relatorio deve ser LIDO nesse caso: o selo de referencia congelada tem DUAS
leituras, e a diferenca esta na contagem. POUCOS papeis com datas de congelamento
distintas entre si e o caso real (sairam do cadastro um a um, conforme cada emissor
entrou em dificuldade). MUITOS papeis compartilhando a MESMA data nao sao defaults -- e
este script que nao rodou para o dia. Foi exatamente o que apareceu ao testar o relatorio
no meio do backfill: 960 de 1.207 tickers marcados, todos apontando para o mesmo pregao.

Por que o %par nao e gravado
----------------------------
Se o %par ficasse em NegociosProcessados, corrigir o fluxo de um ativo (que apaga o
puPar dele) deixaria o %par VELHO de pe, calculado com um denominador em que ninguem
mais acredita -- e seria preciso lembrar de limpar a coluna em toda particao afetada.
E o mesmo problema que fez o trigger de invalidacao virar codigo dentro do Mesclar.
Com so o puPar gravado, a correcao se propaga sozinha: sumiu o puPar, o relatorio
mostra travessao em vez de um numero errado.

CLI:
    python codigos/calc_pu_par/calc_pu_par.py --date 2026-07-28
    python codigos/calc_pu_par/calc_pu_par.py --start 2026-06-09 --end 2026-07-28
    python codigos/calc_pu_par/calc_pu_par.py --tudo             # a base inteira
    python codigos/calc_pu_par/calc_pu_par.py --tudo --sem-api   # so a calc local
    python codigos/calc_pu_par/calc_pu_par.py --tudo --limite 500
"""

import argparse
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers"))

import pandas as pd

import dados as D
from b3_calc_api import CalcularPuGov
from calc import CalcularPu, CarregarAtivo
from email_outlook import EnviarEmailConclusao
from fianalytics_api import ChamarCompleto
from logger import ObterLogger
from relatorio_execucao import RelatorioExecucao

NOME_SCRIPT = "calc_pu_par"

# Banda de ORDEM DE GRANDEZA sobre o %par implicito, so para o resumo -- nada e
# filtrado por causa dela.
#
# Calibrada na distribuicao real de 28/07/2026 (16.411 negocios com %par):
#     min 39,5 | p05 79,6 | p25 89,8 | MEDIANA 94,7 | p75 98,9 | p95 101,0 | max 122,8
#
# Uma primeira tentativa com 50-150 marcava 8 negocios -- e os 8 eram Braskem, CSN e
# Light a ~48% do par. Isso e DISTRESS DE CREDITO, nao erro de cadastro: papel de
# emissor em dificuldade negocia a fracao do par, e e exatamente o que a mesa quer ver.
# Alarme que dispara no caso legitimo mais interessante do dia ensina a ignorar o
# alarme. O que a banda tem de pegar e erro de UNIDADE (VNE em 1 no lugar de 1000,
# indexador trocado, fluxo vazio), que erra por 10x ou 100x, nunca por 2x.
PCT_PAR_MIN, PCT_PAR_MAX = 20.0, 200.0

# So a fase de API e paralelizada. A calc local e CPU-bound e o GIL nao a deixa
# escalar (mesma licao do calc_taxa_negocios: la quem faz o servico e o cache, nao o
# pool). Ja B3 e FI sao I/O puro atras de um proxy, e cada chamada paga handshake --
# ai o pool vale, e e o mesmo numero que o validar_calc_b3 usa.
WORKERS_API = 10


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Quem negociou na data e ainda NAO tem puPar gravado para ela. O LEFT JOIN e o gate de
# idempotencia: par que existe nem aparece na fila, entao nao ha o que pular depois.
SQL_FILA = """
    SELECT DISTINCT tp.cdTicker
    FROM   NegociosProcessados tp
    LEFT   JOIN PuPar pp
           ON pp.cdTicker = tp.cdTicker AND pp.dtReferencia = tp.dtLiquidacao
    WHERE  tp.dtLiquidacao = ?
      AND  tp.vrPU IS NOT NULL
      AND  pp.cdTicker IS NULL
    ORDER  BY tp.cdTicker
"""

# So para o resumo: a distribuicao do %par que a rodada acabou de viabilizar.
SQL_AMOSTRA_PCT_PAR = """
    SELECT tp.vrPU / pp.vrPuPar * 100.0 AS vrPctPar
    FROM   NegociosProcessados tp
    JOIN   PuPar pp
           ON pp.cdTicker = tp.cdTicker AND pp.dtReferencia = tp.dtLiquidacao
    WHERE  tp.dtLiquidacao = ? AND tp.vrPU IS NOT NULL AND pp.vrPuPar > 0
"""


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@dataclass
class EstatisticasData:
    dtLiquidacao: str
    fila: int = 0          # ativos que a rodada de fato TENTOU
    pendente: int = 0      # ficaram de fora por --limite (voltam na proxima rodada)
    calc: int = 0
    b3: int = 0
    fi: int = 0
    semFonte: int = 0      # nenhuma das tres respondeu
    foraDaBanda: int = 0   # %par implicito fora de PCT_PAR_MIN..PCT_PAR_MAX


# ---------------------------------------------------------------------------
# A cascata
# ---------------------------------------------------------------------------

def PuParPelaCalc(cdTicker: str, dtReferencia: str, falhas: dict,
                  log) -> Optional[float]:
    """Degrau 1: a nossa calculadora, so em ativo com fluxo validado contra a B3/FI."""
    ativo = CarregarAtivo(cdTicker)
    if ativo is None or ativo.get("stFluxoValidado") != 1:
        return None
    try:
        puPar = CalcularPu(ativo, date.fromisoformat(dtReferencia),
                           ativo["vrTaxaEmissao"])
    except Exception as exc:
        # WARNING uma vez por ticker: o mesmo papel falha em varios pregoes pelo mesmo
        # motivo (tipicamente curva DI ausente na data), e o log viraria ruido.
        if cdTicker not in falhas:
            log.warning("pu_par: calc local falhou em %s (%s) — caindo p/ B3/FI",
                        cdTicker, exc)
        falhas[cdTicker] = falhas.get(cdTicker, 0) + 1
        return None
    # PU par <= 0 nao e erro da calc, e cadastro impossivel (VNE zerado, fluxo que
    # amortiza 100% antes da data). Dividir por ele daria infinito ou sinal trocado.
    return puPar if puPar and puPar > 0 else None


def PuParPelaB3(cdTicker: str, dtReferencia: str, vrTaxaEmissao: float,
                log) -> Optional[float]:
    """Degrau 2: `/calcPU` da B3 na taxa de emissao — o mesmo caminho que o
    `validar_calc_b3` usa no teste 'PU no par'."""
    try:
        puPar, _ = CalcularPuGov(cdTicker, dtReferencia, vrTaxaEmissao)
    except Exception as exc:
        log.debug("pu_par: B3 nao respondeu para %s/%s: %s", cdTicker, dtReferencia, exc)
        return None
    return puPar if puPar and puPar > 0 else None


def PuParPelaFi(cdTicker: str, dtReferencia: str, vrTaxaEmissao: float,
                log) -> Optional[float]:
    """Degrau 3: calculador da FI no modo `rate`. O PU vem no campo `m2m`."""
    try:
        resposta = ChamarCompleto(cdTicker, dtReferencia, vrTaxaEmissao)
    except Exception as exc:
        log.debug("pu_par: FI nao respondeu para %s/%s: %s", cdTicker, dtReferencia, exc)
        return None
    if not resposta:
        return None
    puPar = resposta.get("m2m")
    try:
        puPar = float(puPar)
    except (TypeError, ValueError):
        return None
    return puPar if puPar > 0 else None


def PuParPelaApi(cdTicker: str, dtReferencia: str, vrTaxaEmissao: Optional[float],
                 log) -> tuple[Optional[float], Optional[str]]:
    """Degraus 2 e 3 (B3 -> FI). E o que roda no pool: I/O puro, sem estado partilhado.

    Sem taxa de emissao nao ha "par" definido — nem a B3 nem a FI teriam o que receber,
    entao nem se tenta."""
    if vrTaxaEmissao is None:
        return None, None
    puPar = PuParPelaB3(cdTicker, dtReferencia, vrTaxaEmissao, log)
    if puPar is not None:
        return puPar, "B3"
    puPar = PuParPelaFi(cdTicker, dtReferencia, vrTaxaEmissao, log)
    if puPar is not None:
        return puPar, "FI"
    return None, None


# ---------------------------------------------------------------------------
# Processamento por data
# ---------------------------------------------------------------------------

def ProcessarData(dtLiquidacao: str, log, taxasEmissao: dict, falhas: dict,
                  usarApi: bool = True, limite: Optional[int] = None) -> EstatisticasData:
    stats = EstatisticasData(dtLiquidacao=dtLiquidacao)

    fila = [r["cdTicker"] for r in D.Linhas(SQL_FILA, (dtLiquidacao,))]
    if not fila:
        log.info("pu_par: %s — nada a fazer (todo ativo negociado ja tem puPar)",
                 dtLiquidacao)
        return stats

    # `fila` conta o que a rodada TENTOU, nao o que faltava: com --limite, dividir pelo
    # total daria um percentual de cobertura enganoso (89/1512 em vez de 89/120).
    if limite is not None and len(fila) > limite:
        stats.pendente = len(fila) - limite
        log.warning("pu_par: %s — fila de %d cortada em %d (--limite); %d ficam para a "
                    "proxima rodada", dtLiquidacao, len(fila), limite, stats.pendente)
        fila = fila[:limite]
    stats.fila = len(fila)

    agora = datetime.now().isoformat(sep=" ", timespec="seconds")
    contador = {"Calc": 0, "B3": 0, "FI": 0}
    linhas: list[dict] = []

    def Anotar(cdTicker: str, puPar: Optional[float], fonte: Optional[str]) -> None:
        if puPar is None:
            stats.semFonte += 1
            return
        contador[fonte] += 1
        linhas.append({"cdTicker": cdTicker, "dtReferencia": dtLiquidacao,
                       "vrPuPar": round(puPar, 8), "cdFontePuPar": fonte,
                       "dtCriacao": agora})

    # FASE 1 — calc local, sequencial (CPU-bound; ver WORKERS_API).
    paraApi: list[str] = []
    for cdTicker in fila:
        puPar = PuParPelaCalc(cdTicker, dtLiquidacao, falhas, log)
        if puPar is not None:
            Anotar(cdTicker, puPar, "Calc")
        elif usarApi:
            paraApi.append(cdTicker)
        else:
            stats.semFonte += 1

    # FASE 2 — B3 e FI, em paralelo, so para quem a calc nao resolveu.
    if paraApi:
        log.info("pu_par: %s — calc resolveu %d; %d vao para B3/FI (%d workers)",
                 dtLiquidacao, contador["Calc"], len(paraApi), WORKERS_API)
        with ThreadPoolExecutor(max_workers=WORKERS_API) as pool:
            resultados = pool.map(
                lambda tk: (tk, *PuParPelaApi(tk, dtLiquidacao,
                                              taxasEmissao.get(tk), log)),
                paraApi)
            for cdTicker, puPar, fonte in resultados:
                Anotar(cdTicker, puPar, fonte)

    if linhas:
        # Mesclar e nao GravarDia: a particao pode ja ter linhas de uma rodada
        # anterior (a fila so traz quem falta, entao o resto tem de ficar intacto).
        D.Mesclar("PuPar", pd.DataFrame(linhas), padrao=D.SOBRESCREVER,
                  data=dtLiquidacao)

    stats.calc, stats.b3, stats.fi = contador["Calc"], contador["B3"], contador["FI"]
    stats.foraDaBanda = ContarForaDaBanda(dtLiquidacao)
    log.info("pu_par: %s — tentou %d: calc=%d b3=%d fi=%d semFonte=%d | %%par fora da "
             "banda: %d", dtLiquidacao, stats.fila, stats.calc, stats.b3, stats.fi,
             stats.semFonte, stats.foraDaBanda)
    return stats


def ContarForaDaBanda(dtLiquidacao: str) -> int:
    """Negocios do dia cujo %par implicito esta fora da banda de ordem de grandeza."""
    df = D.Consultar(SQL_AMOSTRA_PCT_PAR, (dtLiquidacao,))
    if df.empty:
        return 0
    return int(((df["vrPctPar"] < PCT_PAR_MIN) | (df["vrPctPar"] > PCT_PAR_MAX)).sum())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def LerArgumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Grava o PU par (por ativo e data) em PuPar. Cascata calc -> B3 -> FI."
    )
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--date",  metavar="YYYY-MM-DD", help="dtLiquidacao unica.")
    grupo.add_argument("--start", metavar="YYYY-MM-DD", help="Inicio do intervalo.")
    grupo.add_argument("--tudo",  action="store_true", dest="tudo",
                       help="Todas as dtLiquidacao que a base tem.")
    parser.add_argument("--end", metavar="YYYY-MM-DD",
                        help="Fim do intervalo (requer --start).")
    parser.add_argument("--sem-api", action="store_true", dest="semApi",
                        help="So a calc local; nao consulta B3 nem FI. Util para medir "
                             "a cobertura propria sem gastar chamada.")
    parser.add_argument("--limite", type=int, default=None,
                        help="Teto de ativos por data — fatia um backfill grande.")
    args = parser.parse_args()
    if args.start and not args.end:
        parser.error("--end e obrigatorio com --start.")
    if args.end and not args.start:
        parser.error("--start e obrigatorio com --end.")
    return args


def MontarIntervaloDatas(args: argparse.Namespace) -> list[str]:
    if args.tudo:
        return D.Datas("NegociosProcessados")
    if args.date:
        date.fromisoformat(args.date)   # data invalida aborta em vez de sair muda
        return [args.date]
    dtInicio, dtFim = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if dtFim < dtInicio:
        raise ValueError(f"--end ({args.end}) anterior a --start ({args.start})")
    datas, atual = [], dtInicio
    while atual <= dtFim:
        datas.append(atual.isoformat())
        atual += timedelta(days=1)
    return datas


def MontarResumo(statsList: list[EstatisticasData], falhas: dict) -> str:
    cabecalho = (f"{'Data':<12}  {'Tentou':>6}  {'Calc':>6}  {'B3':>6}  {'FI':>6}  "
                 f"{'SemFonte':>8}  {'ForaBanda':>9}")
    separador = "-" * len(cabecalho)
    linhas = ["PU par gravado por dtLiquidacao (tabela PuPar):", "",
              cabecalho, separador]

    tot = dict(fila=0, calc=0, b3=0, fi=0, semFonte=0, foraDaBanda=0, pendente=0)
    for s in statsList:
        linhas.append(f"{s.dtLiquidacao:<12}  {s.fila:>6}  {s.calc:>6}  {s.b3:>6}  "
                      f"{s.fi:>6}  {s.semFonte:>8}  {s.foraDaBanda:>9}")
        for k in tot:
            tot[k] += getattr(s, k)

    linhas.append(separador)
    linhas.append(f"{'TOTAL':<12}  {tot['fila']:>6}  {tot['calc']:>6}  {tot['b3']:>6}  "
                  f"{tot['fi']:>6}  {tot['semFonte']:>8}  {tot['foraDaBanda']:>9}")

    gravados = tot["calc"] + tot["b3"] + tot["fi"]
    if tot["fila"]:
        linhas.append("")
        linhas.append(f"Resolvidos: {gravados}/{tot['fila']} "
                      f"({100.0 * gravados / tot['fila']:.1f}%) dos ativos tentados.")
    if tot["pendente"]:
        linhas.append(f"  - {tot['pendente']} ficaram de fora por --limite; voltam na "
                      "proxima rodada (a fila é sempre quem ainda não tem puPar).")
    if tot["semFonte"]:
        linhas.append(f"  - {tot['semFonte']} sem puPar: nem calc, nem B3, nem FI. "
                      "Papel que saiu do cadastro cai aqui — o relatorio passa a usar o "
                      "ultimo puPar dele e mostra a idade.")
    if falhas:
        linhas.append(f"  - calc local falhou em {len(falhas)} ativo(s): "
                      + ", ".join(sorted(falhas)[:10])
                      + (" ..." if len(falhas) > 10 else ""))
    if tot["foraDaBanda"]:
        linhas.append("")
        linhas.append(f"ATENCAO: {tot['foraDaBanda']} negocio(s) com %par fora de "
                      f"{PCT_PAR_MIN:.0f}-{PCT_PAR_MAX:.0f}%. Nesta faixa o desvio ja e "
                      "de ORDEM DE GRANDEZA, entao a suspeita e cadastro (VNE na unidade "
                      "errada, indexador trocado, fluxo vazio) — nao distress de credito, "
                      "que cabe dentro da banda.")
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Principal() -> None:
    log     = ObterLogger(NOME_SCRIPT)
    args    = LerArgumentos()
    rel     = RelatorioExecucao(NOME_SCRIPT, args=vars(args))
    erro    = None
    resumo  = ""
    sucesso = True

    try:
        datas = MontarIntervaloDatas(args)
        if not datas:
            log.warning("pu_par: nenhuma data a processar.")
        else:
            log.info("pu_par: %d data(s): %s ... %s%s", len(datas), datas[0], datas[-1],
                     " (so calc local)" if args.semApi else "")

        # Taxa de emissao de todo mundo, de uma vez: e o argumento que a B3 e a FI
        # recebem, e ler InfoAtivos por ativo dentro do laco reabriria os parquets
        # milhares de vezes.
        taxasEmissao = {r["cdTicker"]: D.SemNaN(r["vrTaxaEmissao"]) for r in D.Linhas(
            'SELECT cdTicker, vrTaxaEmissao FROM "InfoAtivos"')}
        falhas: dict[str, int] = {}

        statsList = [ProcessarData(dt, log, taxasEmissao, falhas, not args.semApi,
                                   args.limite) for dt in datas]

        resumo = MontarResumo(statsList, falhas)
        rel.Metrica("Linhas em PuPar (total na base)",
                    D.Escalar('SELECT COUNT(*) FROM "PuPar"'))
        log.info("pu_par: concluido.\n%s", resumo)

    except Exception:
        sucesso = False
        erro = traceback.format_exc()
        rel.Erro("A rodada abortou — ver traceback.")
        log.exception("pu_par: erro inesperado")

    finally:
        if resumo:
            rel.Secao("Resumo", ["saida"], [[l] for l in resumo.splitlines() if l.strip()])
        EnviarEmailConclusao(NOME_SCRIPT, sucesso, rel, tracebackErro=erro, logger=log)

    if not sucesso:
        sys.exit(1)


if __name__ == "__main__":
    Principal()
