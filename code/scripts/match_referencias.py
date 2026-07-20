"""
match_referencias.py
====================
Atribui cdReferencia em InfoAtivos para ativos IPCA e PREFIXADO sem referencia
ou cuja referencia foi atribuida por este proprio script (cdFonteReferencia = 'MatchRef'),
fazendo duration-match contra MtmAnbima na data em que a duration foi inserida
no banco (dtAtualizacaoDuration). Match de data e exato — sem fallback.

Nao sobrescreve refs oriundas da Anbima (cdFonteReferencia = 'Anbima').

  IPCA       → NTN-B de duration mais proxima (cdTicker LIKE 'NTN-B%')
  PREFIXADO  → DI1 de duration mais proxima   (cdTicker LIKE 'DI1%')

PRE-PASSO (fecha o buraco da duration): antes do match, calcula a vrDuration dos
IPCA/PREFIXADO que NEGOCIARAM mas estao sem duration (a Anbima nao os cobre no
indicativo, entao nunca teriam ref -> nunca teriam spread). A duration vem da mesma
cascata de confianca do calc_taxa:
  - ativo VALIDADO (stFluxoValidado=1) → calc local (CalcularDuration)
  - ativo NAO-validado                 → FI (maculayDuration) e, se nao cobrir, B3
                                         (CalcularPuGov devolve duration junto do PU)
Descontada na vrTaxaEmissao, as-of a data da curva de benchmark mais recente (para o
match casar). Grava vrDuration (anos) + dtAtualizacaoDuration; o match logo abaixo os pega.

CLI (sem argumentos — roda sobre toda a base):
    python scripts/match_referencias.py
"""

import argparse
import sys
import traceback
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.db import ObterBanco
from lib.logger import ObterLogger
from lib.email_outlook import EnviarEmailConclusao
from lib.relatorio_execucao import RelatorioExecucao
from lib.calc import CarregarAtivo, CalcularDuration
from lib.b3_calc_api import CalcularPuGov
from lib.fianalytics_api import ObterDuration

NOME_SCRIPT = "match_referencias"

SQL_BUSCAR_ATIVOS = """
    SELECT cdTicker, cdIndexador, vrDuration, dtAtualizacaoDuration
    FROM   InfoAtivos
    WHERE  (cdReferencia IS NULL OR cdFonteReferencia = 'MatchRef')
      AND  cdIndexador IN ('IPCA', 'PREFIXADO')
      AND  vrDuration IS NOT NULL
      AND  dtAtualizacaoDuration IS NOT NULL
"""

SQL_BUSCAR_REFS = """
    SELECT cdTicker, vrDuration
    FROM   MtmAnbima
    WHERE  cdTicker LIKE ?
      AND  dtReferencia = ?
      AND  vrDuration IS NOT NULL
"""

SQL_ATUALIZAR_REF = """
    UPDATE InfoAtivos
    SET    cdReferencia = ?, cdFonteReferencia = 'MatchRef', dtAtualizacao = CURRENT_TIMESTAMP
    WHERE  cdTicker = ?
"""


def LerArgumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Atribui cdReferencia em InfoAtivos via duration-match em MtmAnbima."
    )
    return parser.parse_args()


def PrefixoDe(cdIndexador: str) -> str:
    return "NTN-B%" if cdIndexador == "IPCA" else "DI1%"


def MelhorMatch(durAtivo: float, candidatos: list) -> str | None:
    if not candidatos:
        return None
    return min(candidatos, key=lambda c: abs(c["vrDuration"] - durAtivo))["cdTicker"]


SQL_DURATION_FALTANTE = """
    SELECT DISTINCT ia.cdTicker, ia.cdIndexador, ia.stFluxoValidado, ia.vrTaxaEmissao
    FROM   InfoAtivos ia
    JOIN   NegociosBrutos nb ON nb.cdTicker = ia.cdTicker AND nb.cdSituacao != 'Cancelado'
    WHERE  ia.cdIndexador IN ('IPCA', 'PREFIXADO')
      AND  ia.vrDuration IS NULL
      AND  ia.vrTaxaEmissao IS NOT NULL
"""

SQL_MAX_DATA_BENCHMARK = """
    SELECT MAX(dtReferencia) FROM MtmAnbima WHERE cdTicker LIKE ? AND vrDuration IS NOT NULL
"""

SQL_GRAVAR_DURATION = """
    UPDATE InfoAtivos
    SET    vrDuration = ?, dtAtualizacaoDuration = ?, dtAtualizacao = CURRENT_TIMESTAMP
    WHERE  cdTicker = ?
"""


def CalcularDurationAtivo(conn, a, dtRef: str, log):
    """Duration (anos) de um ativo pela cascata de confiança. Validado -> calc local.
    Nao-validado -> FI (maculayDuration) e, se a FI nao cobrir, B3 (CalcularPuGov).
    Desconta na vrTaxaEmissao (as-of dtRef). Devolve (vrDuration, fonte) ou (None, None)."""
    cdTicker = a["cdTicker"]
    taxa = a["vrTaxaEmissao"]

    if a["stFluxoValidado"] == 1:
        ativoCalc = CarregarAtivo(conn, cdTicker)   # precisa de fluxo/cadastro completo
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


def PreencherDurationFaltante(conn, log) -> dict:
    """Calcula a vrDuration dos IPCA/PREFIXADO que negociaram mas estao sem ela, para
    o match logo abaixo poder casa-los. Cascata: validado -> calc; nao-validado -> FI -> B3.
    Retorna dict com contagem por fonte + semDados/semCurva."""
    ativos = conn.execute(SQL_DURATION_FALTANTE).fetchall()
    contagem = {"calc": 0, "FI": 0, "B3": 0, "semDados": 0, "semCurva": 0}
    if not ativos:
        return contagem

    # data da curva de benchmark mais recente por indexador — a duration e gravada
    # as-of essa data (dtAtualizacaoDuration), que e onde o match vai procurar candidatos.
    dataBenchmark = {
        "IPCA": conn.execute(SQL_MAX_DATA_BENCHMARK, ("NTN-B%",)).fetchone()[0],
        "PREFIXADO": conn.execute(SQL_MAX_DATA_BENCHMARK, ("DI1%",)).fetchone()[0],
    }
    log.info("match_ref: %d ativo(s) IPCA/PREFIXADO sem duration a calcular; datas de curva=%s",
             len(ativos), dataBenchmark)

    for a in ativos:
        dtRef = dataBenchmark.get(a["cdIndexador"])
        if not dtRef:
            contagem["semCurva"] += 1
            continue

        vrDuration, fonte = CalcularDurationAtivo(conn, a, dtRef, log)
        if not vrDuration or vrDuration <= 0:
            contagem["semDados"] += 1
            continue

        conn.execute(SQL_GRAVAR_DURATION, (vrDuration, dtRef, a["cdTicker"]))
        contagem[fonte] += 1
        log.info("match_ref: duration %s (%s) = %.4f anos (%s, as-of %s)",
                 a["cdTicker"], a["cdIndexador"], vrDuration, fonte, dtRef)

    conn.commit()
    log.info("match_ref: duration preenchida — calc=%d FI=%d B3=%d semDados=%d semCurva=%d",
             contagem["calc"], contagem["FI"], contagem["B3"], contagem["semDados"], contagem["semCurva"])
    return contagem


def Principal() -> None:
    log     = ObterLogger(NOME_SCRIPT)
    args    = LerArgumentos()
    rel     = RelatorioExecucao(NOME_SCRIPT)
    erro    = None
    summary = ""
    success = True

    try:
        conn = ObterBanco()
        try:
            # PRE-PASSO: calcula a duration de quem negociou e esta sem ela, para o
            # match logo abaixo poder casa-los (senao ficariam sem ref -> sem spread).
            durStats = PreencherDurationFaltante(conn, log)

            ativos = conn.execute(SQL_BUSCAR_ATIVOS).fetchall()
            log.info("match_ref: %d ativos IPCA/PREFIXADO sem cdReferencia com duration disponivel", len(ativos))

            nMatch  = 0
            nSemRef = 0
            detalhes: list[str] = []

            for ativo in ativos:
                cdTicker    = ativo["cdTicker"]
                cdIndexador = ativo["cdIndexador"]
                vrDuration  = ativo["vrDuration"]
                dtUpsertDur = ativo["dtAtualizacaoDuration"]

                prefix     = PrefixoDe(cdIndexador)
                candidatos = conn.execute(SQL_BUSCAR_REFS, (prefix, dtUpsertDur)).fetchall()

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
                conn.execute(SQL_ATUALIZAR_REF, (cdReferencia, cdTicker))
                nMatch += 1

                log.info(
                    "match_ref: %s (%s) dur=%.4f → %s",
                    cdTicker, cdIndexador, vrDuration, cdReferencia,
                )
                detalhes.append(
                    f"  MATCH    {cdTicker:<20} ({cdIndexador}) dur={vrDuration:.4f} → {cdReferencia}"
                )

            conn.commit()

        finally:
            conn.close()

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
