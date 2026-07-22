"""
comparar_calcpu_b3.py — diagnostico termo-a-termo da nossa calc vs. o /calcPU da B3.

Para um ativo (default TRGP13, IPCA-I com divergencia fora-do-par conhecida), compara
o presentValue de CADA evento do fluxo: o que a NOSSA calc desconta vs. o cashFlowList
que a B3 devolve. Roda no par e a +DELTA bps para isolar onde nasce o erro do desconto.

Uso (de code/):
    python scripts/comparar_calcpu_b3.py --ticker TRGP13 --date 2026-07-17 --delta 100
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.calc import ImportarCalc, CarregarAtivo
from lib.b3_calc_api import Requisitar
from lib.config import cfg
from lib.db import ObterBanco

C = ImportarCalc()


def B3CalcPuRaw(tk: str, dt: str, taxa: float) -> dict | None:
    base = cfg["api"]["b3"]["baseUrl"]
    url = f"{base}/calcPU/{tk}/{dt}/{taxa}"
    resp = Requisitar(url, cfg["calc"]["timeoutSeconds"])
    if resp is None or not resp.is_success:
        print(f"B3 calcPU falhou: HTTP {getattr(resp,'status_code','sem resposta')} — {url}")
        return None
    return resp.json()


def PvsNossos(ativo: dict, dCalc: date, taxaNeg: float):
    """[(dataEv, FV, du, PV)] por evento futuro, replicando CalcularPuOperacao (IPCA)."""
    inicio = ativo["dtInicioRentabilidade"]
    taxaEmi = ativo["vrTaxaEmissao"]
    fluxo = ativo["fluxo"]
    vne = ativo["vrVNE"]
    aniv = ativo["vrAniversario"] if ativo["vrAniversario"] is not None else C.DIA_ANIV
    tipo = ativo["cdTipoAmortizacao"]
    fluxosDesc = C._FluxosDescontaveis(dCalc, inicio, taxaEmi, fluxo, vne, "IPCA", aniv, tipo)
    datasFut = [d for d, _, __ in sorted(fluxo, key=lambda x: x[0]) if d > dCalc]
    jNeg = taxaNeg / 100
    saida = []
    for (fv, du), dEv in zip(fluxosDesc, datasFut):
        fatorDesc = round((1 + jNeg) ** (du / 252), 9)
        pv = C.Trunca(fv / fatorDesc, 6)
        saida.append((dEv, fv, du, pv))
    return saida


def DataCalcPadrao() -> str:
    import sqlite3
    di = sqlite3.connect("data/di.db")
    d = di.execute("SELECT MAX(dtReferencia) FROM CurvaDi").fetchone()[0]
    di.close()
    return d


def ErroRelativo(tk: str, dtStr: str, delta: float, conn) -> tuple | None:
    """(puParB3, relPar, puForaB3, relFora) — erro relativo do PU nosso vs B3, ou None."""
    ativo = CarregarAtivo(conn, tk)
    if not ativo or ativo["cdIndexador"] != "IPCA":
        return None
    dCalc = date.fromisoformat(dtStr)
    taxaEmi = ativo["vrTaxaEmissao"]
    out = []
    for taxa in (taxaEmi, taxaEmi + delta / 100):
        raw = B3CalcPuRaw(tk, dtStr, taxa)
        puB3 = float(raw["PU"]) if raw and raw.get("PU") else None
        nosso = sum(pv for _, __, ___, pv in PvsNossos(ativo, dCalc, taxa))
        rel = abs(nosso / puB3 - 1) if puB3 else None
        out += [puB3, rel]
    return tuple(out)


def Tabela(tk: str, dtStr: str, delta: float, conn) -> None:
    ativo = CarregarAtivo(conn, tk)
    dCalc = date.fromisoformat(dtStr)
    taxaEmi = ativo["vrTaxaEmissao"]
    print(f"== {tk} ({ativo['cdIndexador']}) em {dtStr} | inicio={ativo['dtInicioRentabilidade']} "
          f"taxaEmi={taxaEmi} VNE={ativo['vrVNE']:.6f} aniv={ativo['vrAniversario']} "
          f"tipoAmort={ativo['cdTipoAmortizacao']} nEventosNossos={len(ativo['fluxo'])} ==")
    for rotulo, taxa in (("PAR", taxaEmi), (f"+{delta:.0f}bps", taxaEmi + delta / 100)):
        raw = B3CalcPuRaw(tk, dtStr, taxa)
        if raw is None:
            continue
        cfl = raw.get("cashFlowList") or []
        puB3 = float(raw["PU"]) if raw.get("PU") else None
        # A B3 itemiza cupom (J) e amortizacao (A) como DUAS linhas na mesma data;
        # a nossa calc combina num evento so. Somamos por data para alinhar 1:1.
        b3PvPorData = {}
        for e in cfl:
            k = e["date"][:10]
            pv, fv, ev = b3PvPorData.get(k, (0.0, 0.0, ""))
            evNovo = (ev + "/" + (e.get("eventType") or "")).strip("/")
            b3PvPorData[k] = (pv + (e.get("presentValue") or 0.0),
                              fv + (e.get("finalValue") or 0.0), evNovo)
        nossos = {d.isoformat(): (fv, pv) for d, fv, du, pv in PvsNossos(ativo, dCalc, taxa)}
        somaNossa = sum(pv for _, pv in nossos.values())
        somaB3Fut = sum(v[0] for k, v in b3PvPorData.items() if k > dtStr and v[0])
        print(f"\n---- {rotulo} (desconto {taxa:.4f}) | PU B3={puB3}  somaPVnossa={somaNossa:.6f}  "
              f"rel={abs(somaNossa/puB3-1):.2e} ----")
        datas = sorted(set(nossos) | {k for k in b3PvPorData if k > dtStr})
        print(f"{'data':<12}{'evB3':<6}{'PV_nosso':>14}{'PV_B3(evento)':>16}{'FV_nosso':>14}")
        for d in datas:
            pvN = nossos.get(d, (None, None))[1]
            fvN = nossos.get(d, (None, None))[0]
            pvB, fvB, evB = b3PvPorData.get(d, (None, None, ""))
            print(f"{d:<12}{(evB or ''):<6}"
                  f"{(f'{pvN:.4f}' if pvN is not None else '-'):>14}"
                  f"{(f'{pvB:.4f}' if pvB is not None else '-'):>16}"
                  f"{(f'{fvN:.2f}' if fvN is not None else '-'):>14}")
        print(f"  (B3 itemiza {len([k for k in b3PvPorData if k>dtStr])} eventos futuros; "
              f"nos agregamos em {len(nossos)}. Soma B3 futura={somaB3Fut:.4f})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="TRGP13")
    ap.add_argument("--batch", default=None, help="lista de tickers separada por virgula (so resumo)")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD; default = ultima curva DI")
    ap.add_argument("--delta", type=float, default=100.0, help="bps fora do par (default 100)")
    args = ap.parse_args()
    dtStr = args.date or DataCalcPadrao()
    conn = ObterBanco()
    try:
        if args.batch:
            print(f"Resumo (data={dtStr}, +{args.delta:.0f}bps) — rel = |PUnosso/PUb3 - 1|\n")
            print(f"{'ticker':<12}{'PU_par_B3':>13}{'rel_par':>11}{'PU_fora_B3':>13}{'rel_fora':>11}")
            for tk in [t.strip().upper() for t in args.batch.split(",") if t.strip()]:
                r = ErroRelativo(tk, dtStr, args.delta, conn)
                if r is None:
                    print(f"{tk:<12}{'nao IPCA/nao carregavel':>48}")
                    continue
                pP, rP, pF, rF = r
                print(f"{tk:<12}{(f'{pP:.4f}' if pP else '-'):>13}{(f'{rP:.2e}' if rP is not None else '-'):>11}"
                      f"{(f'{pF:.4f}' if pF else '-'):>13}{(f'{rF:.2e}' if rF is not None else '-'):>11}")
        else:
            ativo = CarregarAtivo(conn, args.ticker)
            if not ativo:
                print(f"{args.ticker}: nao carregavel.")
                sys.exit(1)
            Tabela(args.ticker, dtStr, args.delta, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
