"""
match_referencias.py
====================
Atribui cdReferencia em InfoAtivos para ativos IPCA e PREFIXADO, por duration-match
contra MtmAnbima na data em que a duration foi gravada (dtAtualizacaoDuration).
Match de data e exato — sem fallback.

  IPCA       → NTN-B de duration mais proxima (cdTicker LIKE 'NTN-B%')
  PREFIXADO  → DI1 de duration mais proxima   (cdTicker LIKE 'DI1%')

QUEM ELE PODE TOCAR (mudou: a Anbima deixou de ser intocavel para sempre).
A precedencia da Anbima virou um PRAZO, ancorado em dtAtualizacaoReferencia — a data
em que alguma fonte reafirmou a referencia pela ultima vez. Os scrapers da Anbima
renovam essa data TODO dia em que publicam o papel, mesmo repetindo o mesmo valor.
Entao:
  - Anbima publicando  -> data sempre fresca -> este script nao encosta (como antes).
  - Anbima parou       -> data congela; passados DIAS_REVALIDAR_REFERENCIA dias
                          (--revalidar-dias, default 30) este script ASSUME o papel.
  - referencia ORFA    -> aponta para benchmark que sumiu da curva viva (venceu):
                          assume na hora, sem esperar o prazo, porque o spread ja
                          esta quebrado. Foi o caso dos papeis presos na NTN-B 26.
  - sem referencia     -> sempre elegivel.
Sem esse prazo, papel que a Anbima deixou de cobrir ficava preso a uma referencia
vencida indefinidamente: ela nunca mais corrige, e o match nao podia mexer.

PRE-PASSO (duration): antes do match, calcula a vrDuration de quem NEGOCIOU e vai ser
rematchado — sem duration, ou com a referencia vencida/orfa acima. A duration e o que
posiciona o ativo na curva, e ela e recalculada as-of a curva MAIS RECENTE; e isso que
faz o match enxergar a curva viva em vez de uma foto antiga.
Cascata de confianca, a mesma do calc_taxa:
  - ativo VALIDADO (stFluxoValidado=1) → calc local (CalcularDuration)
  - ativo NAO-validado                 → FI (maculayDuration) e, se nao cobrir, B3
                                         (CalcularPuGov devolve duration junto do PU)
Descontada na vrTaxaEmissao. Grava vrDuration (anos) + dtAtualizacaoDuration.

CLI (sem argumentos — roda sobre toda a base):
    python scripts/match_referencias.py
    python scripts/match_referencias.py --revalidar-dias 15  # prazo mais curto
    python scripts/match_referencias.py --force              # ignora prazo: TODAS as refs
"""

import argparse
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Helpers"))

import pandas as pd

import dados as D
from logger import ObterLogger
from email_outlook import EnviarEmailConclusao
from relatorio_execucao import RelatorioExecucao
from calc import CarregarAtivo, CalcularDuration
from b3_calc_api import CalcularPuGov
from fianalytics_api import ObterDuration

NOME_SCRIPT = "match_referencias"

# Idade maxima (dias corridos) de uma cdReferencia antes deste script assumi-la.
# O timer e da REFERENCIA, nao da duration: enquanto a Anbima publicar o papel ela
# renova dtAtualizacaoReferencia todo dia e este script nao encosta. Quando ela para
# de cobrir, a data congela, o prazo vence e o match assume — calcula duration nova
# (as-of a curva de hoje) e busca referencia nova. E o que destrava o papel preso numa
# NTN-B ja vencida, que a Anbima nunca mais vai corrigir.
DIAS_REVALIDAR_REFERENCIA = 30

# Elegiveis ao match. A precedencia da Anbima continua valendo ENQUANTO ela mantiver a
# referencia viva (ver dtAtualizacaoReferencia): so quando ela para de reafirmar por mais
# de DIAS_REVALIDAR_REFERENCIA dias e que este script assume o papel.
SQL_BUSCAR_ATIVOS = """
    SELECT cdTicker, cdIndexador, vrDuration, dtAtualizacaoDuration,
           cdReferencia, cdFonteReferencia, dtAtualizacaoReferencia
    FROM   InfoAtivos
    WHERE  cdIndexador IN ('IPCA', 'PREFIXADO')
      AND  vrDuration IS NOT NULL
      AND  dtAtualizacaoDuration IS NOT NULL
      AND  (cdReferencia IS NULL
            OR cdFonteReferencia = 'MatchRef'
            OR dtAtualizacaoReferencia IS NULL
            OR dtAtualizacaoReferencia < CASE cdIndexador
                                           WHEN 'IPCA' THEN $corteIpca
                                           ELSE $cortePre END)
"""

SQL_BUSCAR_REFS = """
    SELECT cdTicker, vrDuration
    FROM   MtmAnbima
    WHERE  cdTicker LIKE ?
      AND  dtReferencia = ?
      AND  vrDuration IS NOT NULL
"""

# O que eram os dois UPDATE de InfoAtivos deste script:
#
#   SET cdReferencia = ?, cdFonteReferencia = 'MatchRef',
#       dtAtualizacaoReferencia = ?, dtAtualizacao = CURRENT_TIMESTAMP
#   SET vrDuration = ?, dtAtualizacaoDuration = ?, dtAtualizacao = CURRENT_TIMESTAMP
#
# Um UPDATE sobrescreve, entao a politica e SOBRESCREVER em tudo o que ele nomeava. Cada
# lote so leva as colunas do seu UPDATE, e Mesclar nao toca em coluna ausente — o passo
# da duration nao mexe na referencia, e vice-versa.
POLITICA_UPDATE = {c: D.SOBRESCREVER for c in (
    "cdReferencia", "cdFonteReferencia", "dtAtualizacaoReferencia",
    "vrDuration", "dtAtualizacaoDuration", "dtAtualizacao")}


def LerArgumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Atribui cdReferencia em InfoAtivos via duration-match em MtmAnbima."
    )
    parser.add_argument(
        "--revalidar-dias", dest="revalidarDias", type=int,
        default=DIAS_REVALIDAR_REFERENCIA, metavar="N",
        help=f"Assume a cdReferencia que nenhuma fonte reafirma ha mais de N dias "
             f"(default {DIAS_REVALIDAR_REFERENCIA}).",
    )
    parser.add_argument(
        "--force", dest="force", action="store_true",
        help="Ignora o prazo: recalcula a duration e rebusca a referencia de TODOS os "
             "IPCA/PREFIXADO elegiveis, inclusive os que a Anbima acabou de reafirmar.",
    )
    return parser.parse_args()


def Agora() -> str:
    """O que o CURRENT_TIMESTAMP dos UPDATE gravava em dtAtualizacao."""
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def PrefixoDe(cdIndexador: str) -> str:
    return "NTN-B%" if cdIndexador == "IPCA" else "DI1%"


def MelhorMatch(durAtivo: float, candidatos: list) -> str | None:
    if not candidatos:
        return None
    return min(candidatos, key=lambda c: abs(c["vrDuration"] - durAtivo))["cdTicker"]


SQL_DURATION_FALTANTE = f"""
    SELECT DISTINCT ia.cdTicker, ia.cdIndexador, ia.stFluxoValidado, ia.vrTaxaEmissao,
           ia.vrDuration, ia.dtAtualizacaoDuration, ia.cdReferencia, ia.cdFonteReferencia
    FROM   InfoAtivos ia
    JOIN   NegociosBrutos nb ON nb.cdTicker = ia.cdTicker AND {D.NaoCancelado('nb.')}
    WHERE  ia.cdIndexador IN ('IPCA', 'PREFIXADO')
      AND  ia.vrTaxaEmissao IS NOT NULL
      AND  (ia.vrDuration IS NULL
            -- (a) referencia VENCIDA: nenhuma fonte a reafirmou ha mais de N dias.
            -- Vale para qualquer fonte, Anbima inclusive: enquanto ela publicar, o
            -- upsert dela renova a data e este ramo nunca dispara.
            OR ia.dtAtualizacaoReferencia IS NULL
            OR ia.dtAtualizacaoReferencia < CASE ia.cdIndexador
                                              WHEN 'IPCA' THEN $corteIpca
                                              ELSE $cortePre END
            -- (b) referencia ORFA: aponta para papel que sumiu da curva viva (venceu).
            -- Atalho do (a) — nao espera o prazo, porque o spread ja esta quebrado.
            OR (ia.cdReferencia IS NOT NULL
                AND ia.cdReferencia NOT IN (
                    SELECT cdTicker FROM MtmAnbima
                    WHERE dtReferencia = CASE ia.cdIndexador
                                           WHEN 'IPCA' THEN $dtCurvaIpca
                                           ELSE $dtCurvaPre END)))
"""

SQL_MAX_DATA_BENCHMARK = """
    SELECT MAX(dtReferencia) FROM MtmAnbima WHERE cdTicker LIKE ? AND vrDuration IS NOT NULL
"""

def DatasBenchmark() -> dict:
    """Pregao mais recente com curva de benchmark na base, por indexador. E a data as-of
    da qual a duration e calculada E onde o match procura candidatos — por isso os dois
    passos tem de usar exatamente a mesma."""
    return {
        "IPCA": D.Escalar(SQL_MAX_DATA_BENCHMARK, ("NTN-B%",)),
        "PREFIXADO": D.Escalar(SQL_MAX_DATA_BENCHMARK, ("DI1%",)),
    }


def CalcularDurationAtivo(a, dtRef: str, log):
    """Duration (anos) de um ativo pela cascata de confiança. Validado -> calc local.
    Nao-validado -> FI (maculayDuration) e, se a FI nao cobrir, B3 (CalcularPuGov).
    Desconta na vrTaxaEmissao (as-of dtRef). Devolve (vrDuration, fonte) ou (None, None)."""
    cdTicker = a["cdTicker"]
    taxa = a["vrTaxaEmissao"]

    if a["stFluxoValidado"] == 1:
        ativoCalc = CarregarAtivo(cdTicker)   # precisa de fluxo/cadastro completo
        if ativoCalc:
            try:
                return CalcularDuration(ativoCalc, date.fromisoformat(dtRef), taxa), "calc"
            except Exception as exc:
                log.warning("match_ref: calc de duration falhou p/ %s: %s", cdTicker, exc)
        return None, None

    # nao-validado: FI primeiro (cobre mais corporates), B3 como fallback.
    try:
        durFi = ObterDuration(cdTicker, dtRef, taxa)
    except Exception as exc:
        log.warning("match_ref: FI duration falhou p/ %s: %s", cdTicker, exc)
        durFi = None
    if durFi and durFi > 0:
        return durFi, "FI"
    try:
        _pu, durB3 = CalcularPuGov(cdTicker, dtRef, taxa)   # B3 devolve (pu, duration)
    except Exception as exc:
        log.warning("match_ref: B3 duration falhou p/ %s: %s", cdTicker, exc)
        durB3 = None
    if durB3 and durB3 > 0:
        return durB3, "B3"
    return None, None


# Corte sentinela do --force: maior que qualquer data real, entao toda comparacao
# `dtAtualizacaoReferencia < corte` da True e a fila vira "todos os elegiveis".
# Evita um segundo par de SQLs so para o modo forcado.
CORTE_FORCE = "9999-12-31"


def CorteRevalidacao(dtBenchmark: str | None, revalidarDias: int, force: bool = False) -> str:
    """Data-limite: referencia reafirmada pela ultima vez ANTES desta e reprocessada.
    Ancorada na curva de benchmark (nao em date.today()) para a regra ser a mesma quer a
    base esteja em dia, quer esteja atrasada — sem churn quando a curva nao andou.
    force -> CORTE_FORCE (pega todo mundo). Sem curva na base, devolve data minima
    (so pega quem nao tem duration/referencia)."""
    if force:
        return CORTE_FORCE
    if not dtBenchmark:
        return "0001-01-01"
    return (date.fromisoformat(dtBenchmark) - timedelta(days=revalidarDias)).isoformat()


def PreencherDurationFaltante(log, revalidarDias: int = DIAS_REVALIDAR_REFERENCIA,
                              force: bool = False) -> dict:
    """Calcula a vrDuration dos IPCA/PREFIXADO que negociaram e estao sem ela OU cuja
    duration esta as-of uma curva com mais de `revalidarDias` dias (a duration envelhece
    e, com ela, a data em que o match do passo 2 procura candidatos).
    Cascata: validado -> calc; nao-validado -> FI -> B3.
    Retorna dict com contagem por fonte + novos/recalculados/semDados/semCurva."""
    # data da curva de benchmark mais recente por indexador — a duration e gravada
    # as-of essa data (dtAtualizacaoDuration), que e onde o match vai procurar candidatos.
    dataBenchmark = DatasBenchmark()
    corteIpca = CorteRevalidacao(dataBenchmark["IPCA"], revalidarDias, force)
    cortePre  = CorteRevalidacao(dataBenchmark["PREFIXADO"], revalidarDias, force)

    ativos = D.Linhas(SQL_DURATION_FALTANTE, {
        "corteIpca": corteIpca, "cortePre": cortePre,
        "dtCurvaIpca": dataBenchmark["IPCA"] or "", "dtCurvaPre": dataBenchmark["PREFIXADO"] or "",
    })
    contagem = {"calc": 0, "FI": 0, "B3": 0, "semDados": 0, "semCurva": 0,
                "novos": 0, "recalculados": 0, "orfaos": 0}
    if not ativos:
        log.info("match_ref: nenhuma duration a calcular (corte IPCA<%s, PREFIXADO<%s)",
                 corteIpca, cortePre)
        return contagem

    vivos = {ix: {r[0] for r in D.Tuplas(SQL_BUSCAR_REFS,
                                         (PrefixoDe(ix), dataBenchmark[ix] or ""))}
             for ix in ("IPCA", "PREFIXADO")}
    contagem["novos"]  = sum(1 for a in ativos if a["vrDuration"] is None)
    contagem["orfaos"] = sum(1 for a in ativos if a["vrDuration"] is not None
                             and a["cdReferencia"] and a["cdReferencia"] not in vivos[a["cdIndexador"]])
    contagem["recalculados"] = len(ativos) - contagem["novos"]
    log.info("match_ref: %d ativo(s) IPCA/PREFIXADO com duration a calcular "
             "(%d sem duration + %d recalculada(s), das quais %d por referencia ORFA); "
             "datas de curva=%s; corte IPCA<%s, PREFIXADO<%s",
             len(ativos), contagem["novos"], contagem["recalculados"], contagem["orfaos"],
             dataBenchmark, corteIpca, cortePre)

    # Os UPDATE viravam um lote so no `commit()` do fim; aqui eles viram um lote so no
    # Mesclar do fim, pelo mesmo motivo — e porque reescrever InfoAtivos por ativo
    # invalidaria o indice do CarregarAtivo a cada volta do laco.
    updates: list[dict] = []

    for a in ativos:
        dtRef = dataBenchmark.get(a["cdIndexador"])
        if not dtRef:
            contagem["semCurva"] += 1
            continue

        vrDuration, fonte = CalcularDurationAtivo(a, dtRef, log)
        if not vrDuration or vrDuration <= 0:
            contagem["semDados"] += 1
            continue

        updates.append({"cdTicker": a["cdTicker"], "vrDuration": vrDuration,
                        "dtAtualizacaoDuration": dtRef, "dtAtualizacao": Agora()})
        contagem[fonte] += 1
        if a["vrDuration"] is None:
            log.info("match_ref: duration %s (%s) = %.4f anos (%s, as-of %s) [nova]",
                     a["cdTicker"], a["cdIndexador"], vrDuration, fonte, dtRef)
        else:
            log.info("match_ref: duration %s (%s) = %.4f anos (%s, as-of %s) "
                     "[recalculada: era %.4f as-of %s]",
                     a["cdTicker"], a["cdIndexador"], vrDuration, fonte, dtRef,
                     a["vrDuration"], a["dtAtualizacaoDuration"])

    if updates:
        D.Mesclar("InfoAtivos", pd.DataFrame(updates), politica=POLITICA_UPDATE)
    log.info("match_ref: duration gravada — calc=%d FI=%d B3=%d semDados=%d semCurva=%d "
             "(fila: %d nova(s) + %d recalculada(s))",
             contagem["calc"], contagem["FI"], contagem["B3"], contagem["semDados"],
             contagem["semCurva"], contagem["novos"], contagem["recalculados"])
    return contagem


def Principal() -> None:
    log     = ObterLogger(NOME_SCRIPT)
    args    = LerArgumentos()
    rel     = RelatorioExecucao(NOME_SCRIPT)
    erro    = None
    summary = ""
    success = True

    try:
        # PRE-PASSO: calcula a duration de quem negociou e esta sem ela, para o
        # match logo abaixo poder casa-los (senao ficariam sem ref -> sem spread).
        durStats = PreencherDurationFaltante(log, args.revalidarDias, args.force)

        benchmark = DatasBenchmark()
        ativos = D.Linhas(SQL_BUSCAR_ATIVOS, {
            "corteIpca": CorteRevalidacao(benchmark["IPCA"], args.revalidarDias, args.force),
            "cortePre":  CorteRevalidacao(benchmark["PREFIXADO"], args.revalidarDias, args.force),
        })
        nAssumidos = sum(1 for a in ativos
                         if a["cdFonteReferencia"] not in (None, "MatchRef"))
        log.info("match_ref: %d ativo(s) IPCA/PREFIXADO elegiveis ao match "
                 "(%d assumidos de outra fonte; criterio: %s)",
                 len(ativos), nAssumidos,
                 "--force (ignora prazo)" if args.force
                 else f"referencia parada ha >{args.revalidarDias}d")

        nMatch  = 0
        nSemRef = 0
        detalhes: list[str] = []
        updates: list[dict] = []
        # Milhares de ativos disputam um punhado de curvas: a consulta de candidatos
        # so depende de (prefixo, data), e nao do ativo. Uma vez por curva basta.
        curvas: dict[tuple, list] = {}

        for ativo in ativos:
            cdTicker    = ativo["cdTicker"]
            cdIndexador = ativo["cdIndexador"]
            vrDuration  = ativo["vrDuration"]
            dtUpsertDur = ativo["dtAtualizacaoDuration"]

            prefix = PrefixoDe(cdIndexador)
            if (prefix, dtUpsertDur) not in curvas:
                curvas[(prefix, dtUpsertDur)] = D.Linhas(
                    SQL_BUSCAR_REFS, (prefix, dtUpsertDur))
            candidatos = curvas[(prefix, dtUpsertDur)]

            if not candidatos:
                log.warning(
                    "match_ref: %s (%s) — nenhum candidato em MtmAnbima para data %s",
                    cdTicker, cdIndexador, dtUpsertDur,
                )
                nSemRef += 1
                detalhes.append(
                    f"  SEM_REF  {cdTicker:<20} ({cdIndexador}) dtUpsert={dtUpsertDur}"
                )
                continue

            cdReferencia = MelhorMatch(vrDuration, candidatos)
            # dtAtualizacaoReferencia = a data da curva contra a qual casamos (nao
            # o timestamp de agora): e a mesma unidade que a Anbima grava (data de
            # mercado), entao as duas fontes sao comparaveis no mesmo prazo.
            updates.append({
                "cdTicker": cdTicker, "cdReferencia": cdReferencia,
                "cdFonteReferencia": "MatchRef",
                "dtAtualizacaoReferencia": dtUpsertDur, "dtAtualizacao": Agora(),
            })
            nMatch += 1

            refAnterior = ativo["cdReferencia"]
            fonteAnterior = ativo["cdFonteReferencia"]
            if refAnterior and refAnterior != cdReferencia:
                log.info(
                    "match_ref: %s (%s) dur=%.4f → %s [era %s, fonte %s]",
                    cdTicker, cdIndexador, vrDuration, cdReferencia,
                    refAnterior, fonteAnterior,
                )
                detalhes.append(
                    f"  TROCA    {cdTicker:<20} ({cdIndexador}) dur={vrDuration:.4f} "
                    f"{refAnterior} → {cdReferencia} (era fonte {fonteAnterior})"
                )
            else:
                log.info(
                    "match_ref: %s (%s) dur=%.4f → %s",
                    cdTicker, cdIndexador, vrDuration, cdReferencia,
                )
                detalhes.append(
                    f"  MATCH    {cdTicker:<20} ({cdIndexador}) dur={vrDuration:.4f} → {cdReferencia}"
                )

        if updates:
            D.Mesclar("InfoAtivos", pd.DataFrame(updates), politica=POLITICA_UPDATE)

        linhas = [
            f"Duration calculada : calc={durStats['calc']} FI={durStats['FI']} B3={durStats['B3']} semDados={durStats['semDados']} semCurva={durStats['semCurva']}",
            f"Ativos processados : {len(ativos)}",
            f"Matches atribuidos : {nMatch}",
            f"Sem ref na data    : {nSemRef}",
            "",
            "Detalhes:",
        ] + detalhes

        summary = "\n".join(linhas)
        log.info("match_ref: concluido.\n%s", summary)

    except Exception:
        success = False
        erro = traceback.format_exc()
        rel.Erro("A rodada abortou — ver traceback.")
        log.exception("match_ref: erro inesperado")

    finally:
        if summary:
            rel.Secao("Resumo", ["saida"], [[l] for l in summary.splitlines() if l.strip()])
        EnviarEmailConclusao(NOME_SCRIPT, success, rel, tracebackErro=erro, logger=log)

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    Principal()
