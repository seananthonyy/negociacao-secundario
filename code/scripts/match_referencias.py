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

CLI (sem argumentos — roda sobre toda a base):
    python scripts/match_referencias.py
"""

import argparse
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.db import ObterBanco
from lib.logger import ObterLogger
from lib.email_outlook import EnviarEmailConclusao

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


def Principal() -> None:
    log     = ObterLogger(NOME_SCRIPT)
    args    = LerArgumentos()
    summary = ""
    success = True

    try:
        conn = ObterBanco()
        try:
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
        summary = traceback.format_exc()
        log.exception("match_ref: erro inesperado")

    finally:
        EnviarEmailConclusao(NOME_SCRIPT, success, summary, logger=log)


if __name__ == "__main__":
    Principal()
