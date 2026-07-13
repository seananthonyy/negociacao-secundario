"""
conferir_pu — bate o PU da nossa calculadora contra o PU das APIs.

E o PORTAO DE ACEITACAO da precificacao local: enquanto os PUs nao baterem, nao se
troca a fonte de taxa do calc_taxa. Margem de erro aqui e baixa por construcao.

Metodo: para cada ativo com fluxo validado, calcula o PU de operacao na data de
referencia usando a TAXA DE EMISSAO como taxa de desconto (PU par, essencialmente) e
compara com o PU que a fonte devolve para a mesma taxa e a mesma data:
  - B3 : GET /calcPU/{ticker}/{data}/{taxa}  -> campo "PU"
  - FI : m2m da resposta completa

CRITERIO — relativo, nao absoluto. Um erro de 1e-3 num PU de 1.000 e um erro relativo
de 1e-6; o mesmo 1e-3 num PU de 10.000 seria um sarrafo 10x mais apertado, e num PU de
400 seria 2,5x mais frouxo. Entao:
  - <= 1e-6 (0,0001%)  bate exato    — o que uma implementacao correta atinge
  - >  1e-3 (0,1%)     investigar    — a fila de trabalho

Saida: data/pu_divergencias.csv, ordenado pelo erro relativo.
"""

from __future__ import annotations

import argparse
import csv
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from traceback import format_exc

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.b3_calc_api import CalcularPuGov
from lib.calc import CalcularPu, CarregarAtivo, ImportarCalc
from lib.db import ObterBanco
from lib.email_outlook import EnviarEmailConclusao
from lib.fianalytics_api import ChamarCompleto
from lib.logger import ObterLogger
from lib.relatorio_execucao import RelatorioExecucao

NOME_SCRIPT = "conferir_pu"

TOL_EXATO = 1e-6      # relativo — "bate exato"
TOL_TRIAGEM = 1e-3    # relativo — acima disso, investigar
WORKERS = 8

CSV_SAIDA = Path("data/pu_divergencias.csv")


def LerArgumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Confere o PU da calc contra as APIs.")
    parser.add_argument("--date", dest="date", default=None,
                        help="data de referencia (YYYY-MM-DD). Default: ultimo dia util.")
    parser.add_argument("--tickers", dest="tickers", default=None, help="lista separada por virgula")
    parser.add_argument("--limite", dest="limite", type=int, default=None)
    parser.add_argument("--desvalidar", dest="desvalidar", action="store_true",
                        help="tira a validacao (stFluxoValidado = 0) de quem diverge acima "
                             "de --tol-desvalida. E o que torna 'calc so no que esta validado' "
                             "seguro por construcao: quem nao reproduz o PU da fonte nao e "
                             "precificado pela calc, cai na cascata de API.")
    parser.add_argument("--tol-desvalida", dest="tolDesvalida", type=float, default=TOL_TRIAGEM,
                        help=f"erro relativo acima do qual desvalida (default {TOL_TRIAGEM})")
    return parser.parse_args()


def UltimoDuUtil(C) -> date:
    d = date.today()
    while not C.EhDu(d, C.FERIADOS_ANBIMA):
        d -= timedelta(days=1)
    return d


def PuDaFonte(cdTicker: str, fonte: str, dtIso: str, vrTaxa: float) -> float | None:
    """PU que a fonte devolve para essa taxa nessa data. A fonte segue a que validou o
    fluxo: comparar contra quem nao e dono do cadastro so mede a diferenca entre elas."""
    try:
        if fonte == "B3":
            pu, _ = CalcularPuGov(cdTicker, dtIso, vrTaxa)
            return pu
        dados = ChamarCompleto(cdTicker, dtIso, vrTaxa)
        if isinstance(dados, dict) and dados.get("m2m") is not None:
            return float(dados["m2m"])
    except Exception:
        return None
    return None


def Principal() -> None:
    log = ObterLogger(NOME_SCRIPT)
    args = LerArgumentos()
    rel = RelatorioExecucao(NOME_SCRIPT, args=vars(args))
    success = True

    try:
        C = ImportarCalc()
        dtRef = date.fromisoformat(args.date) if args.date else UltimoDuUtil(C)
        dtIso = dtRef.isoformat()
        rel.Datas([dtIso])

        conn = ObterBanco()
        try:
            onde = "stFluxoValidado = 1"
            params: tuple = ()
            if args.tickers:
                alvos = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
                onde = f"cdTicker IN ({','.join('?' * len(alvos))})"
                params = tuple(alvos)

            fila = [(r["cdTicker"], r["cdFonteValidacaoFluxo"] or "B3") for r in conn.execute(
                f"SELECT cdTicker, cdFonteValidacaoFluxo FROM InfoAtivos "
                f"WHERE {onde} AND (dtVencimento IS NULL OR dtVencimento > ?) ORDER BY cdTicker",
                params + (dtIso,))]
            if args.limite:
                fila = fila[:args.limite]

            ativos = {t: CarregarAtivo(conn, t) for t, _ in fila}
            fila = [(t, f) for t, f in fila if ativos[t]]
            rel.Metrica("Ativos comparados", len(fila))
            log.info("%s: %d ativo(s) em %s", NOME_SCRIPT, len(fila), dtIso)

            def BuscarPu(par):
                cdTicker, fonte = par
                return cdTicker, PuDaFonte(cdTicker, fonte, dtIso, ativos[cdTicker]["vrTaxaEmissao"])

            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                pus = dict(pool.map(BuscarPu, fila))

            linhas: list[list] = []
            exato = semFonte = 0
            for cdTicker, fonte in fila:
                ativo = ativos[cdTicker]
                puApi = pus.get(cdTicker)
                if not puApi:
                    semFonte += 1
                    continue
                try:
                    puCalc = CalcularPu(ativo, dtRef, ativo["vrTaxaEmissao"])
                except Exception as exc:
                    rel.Contar("falhas")
                    rel.Exemplo("falhas", {"cdTicker": cdTicker, "erro": str(exc)[:100]})
                    continue

                relativo = abs(puCalc / puApi - 1)
                if relativo <= TOL_EXATO:
                    exato += 1
                    continue
                linhas.append([cdTicker, ativo["cdIndexador"], fonte, ativo["vrAniversario"],
                               round(puCalc, 6), round(puApi, 6),
                               round(puCalc - puApi, 6), f"{relativo:.3e}"])

            linhas.sort(key=lambda l: -float(l[7]))
            CSV_SAIDA.parent.mkdir(parents=True, exist_ok=True)
            with open(CSV_SAIDA, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["cdTicker", "cdIndexador", "fonte", "vrAniversario",
                            "puCalc", "puApi", "dif", "difRelativa"])
                w.writerows(linhas)

            graves = [l for l in linhas if float(l[7]) > TOL_TRIAGEM]

            if args.desvalidar:
                reprovados = [l[0] for l in linhas if float(l[7]) > args.tolDesvalida]
                conn.executemany(
                    "UPDATE InfoAtivos SET stFluxoValidado = 0, dtValidacaoFluxo = NULL, "
                    "cdFonteValidacaoFluxo = NULL WHERE cdTicker = ?",
                    [(t,) for t in reprovados])
                conn.commit()
                rel.Contar("desvalidados", len(reprovados))
                for t in reprovados[:10]:
                    linha = next(l for l in linhas if l[0] == t)
                    rel.Exemplo("desvalidados", {"cdTicker": t, "indexador": linha[1],
                                                 "puCalc": linha[4], "puApi": linha[5],
                                                 "erro": linha[7]})
                rel.Aviso(f"{len(reprovados)} ativo(s) perderam a validacao: o PU da calc nao "
                          f"reproduz o da fonte (erro > {args.tolDesvalida:.0e}). Eles voltam "
                          f"para a cascata de API no calc_taxa.")
                log.info("%s: %d desvalidados por divergencia de PU", NOME_SCRIPT, len(reprovados))

            rel.Contar("batem", exato)
            rel.Contar("divergem", len(linhas))
            rel.Metrica("Bate exato (<= 1e-6)", f"{exato} ({100*exato/max(1,len(fila)):.1f}%)")
            rel.Metrica("Investigar (> 1e-3)", len(graves))
            rel.Metrica("Sem PU na fonte", semFonte)
            rel.Secao("Divergencias a investigar (> 0,1%)",
                      ["ticker", "indexador", "fonte", "aniv", "PU calc", "PU API", "dif", "rel"],
                      graves)
            if graves:
                rel.Aviso(f"{len(graves)} ativo(s) acima de 0,1% de erro — ver {CSV_SAIDA}.")

            log.info("%s: %d batem, %d divergem, %d graves", NOME_SCRIPT, exato, len(linhas), len(graves))
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
