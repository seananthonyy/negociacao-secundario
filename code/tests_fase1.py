"""
tests_fase1.py — testes unitarios das correcoes criticas da FASE 1.

Cobre:
  #1  Extrapolacao flat-forward da curva DI (_MontarAccProjDi) + len>=2.
  #2  ResolverTipoAmort (cadastro > heuristica) + override altera o PU.
  #3  CalcularTaxaNegociacao: round-trip PU->taxa->PU e raise em PU nao-bracketavel.
  #4  MERCADO: cache lazy + Recarregar().
  #2b Integracao: coluna cdTipoAmortizacao no schema + propagacao via lib/calc.

Rodar de code/:
    python tests_fase1.py
Sai 0 se tudo passa, 1 se algo falha. Nao toca o trades.db real (usa temp).
"""
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.calc import ImportarCalc, CarregarAtivo, CalcularPu, CalcularTaxa
from lib.db import ObterBanco

C = ImportarCalc()   # modulo calculadora_rf, com CALCRF_FILES_DIR ja apontado p/ data/

falhas: list[str] = []


def Checar(nome: str, cond: bool, detalhe: str = "") -> None:
    marca = "[OK]  " if cond else "[FALHA]"
    print(f"{marca} {nome}" + (f"  — {detalhe}" if detalhe and not cond else ""))
    if not cond:
        falhas.append(nome)


# ─── #1: extrapolacao flat-forward ───────────────────────────────────────────
def TestAccProj() -> None:
    print("\n== #1 Extrapolacao flat-forward da curva DI ==")
    vertices = [(252, 10.0), (504, 12.0)]   # (du, taxa % a.a.)
    acc = C._MontarAccProjDi(vertices)
    accs = [(1 + t / 100) ** (d / 252) for d, t in vertices]

    # No vertice: reproduz o fator exato.
    Checar("AccProj no ultimo vertice == fator do vertice",
           abs(acc(504) - accs[1]) < 1e-12)

    # ALEM do ultimo vertice: NAO congela (forward > 0). O bug antigo devolvia accs[-1].
    du_ext = 756
    fwd = (accs[1] / accs[0]) ** (1.0 / (504 - 252))
    esperado = accs[1] * fwd ** (du_ext - 504)
    Checar("AccProj alem do vertice mantem o forward (nao congela)",
           acc(du_ext) > accs[1] + 1e-9)
    Checar("AccProj alem do vertice == composicao no ultimo forward",
           abs(acc(du_ext) - esperado) < 1e-9,
           f"{acc(du_ext):.8f} vs {esperado:.8f}")

    # Curva com < 2 vertices deve levantar.
    levantou = False
    try:
        C._MontarAccProjDi([(252, 10.0)])
    except ValueError:
        levantou = True
    Checar("_MontarAccProjDi com 1 vertice levanta ValueError", levantou)


# ─── #2: ResolverTipoAmort ────────────────────────────────────────────────────
def TestResolverTipoAmort() -> None:
    print("\n== #2 ResolverTipoAmort (cadastro > heuristica) ==")
    inicio = date(2024, 1, 15)
    # Fluxo que SOMA 100% (a heuristica inferiria 'saldo_original').
    fluxo100 = [(date(2025, 1, 15), 50.0, 0.0), (date(2026, 1, 15), 50.0, 0.0)]

    Checar("cadastro 'saldo_restante' vence a heuristica",
           C.ResolverTipoAmort(fluxo100, 'saldo_restante') == 'saldo_restante')
    Checar("cadastro 'saldo_original' vence",
           C.ResolverTipoAmort(fluxo100, 'saldo_original') == 'saldo_original')
    Checar("sem cadastro (None) cai na heuristica: soma 100 -> saldo_original",
           C.ResolverTipoAmort(fluxo100, None) == 'saldo_original')
    Checar("sem cadastro: soma != 100 -> saldo_restante",
           C.ResolverTipoAmort([(date(2025, 1, 15), 50.0, 0.0)], None) == 'saldo_restante')

    # O override tem que MUDAR o PU (prova que a convencao propaga ate o desconto).
    dataCalc = date(2024, 6, 17)
    puInfer = C.CalcularPuOperacao(dataCalc, inicio, 10.0, 12.0, fluxo100, 1000.0,
                                   'PREFIXADO')                       # None -> infere saldo_original
    puRest  = C.CalcularPuOperacao(dataCalc, inicio, 10.0, 12.0, fluxo100, 1000.0,
                                   'PREFIXADO', cdTipoAmortizacao='saldo_restante')
    Checar("override 'saldo_restante' altera o PU vs. inferido",
           abs(puInfer - puRest) > 1e-4,
           f"puInfer={puInfer:.6f} puRest={puRest:.6f}")


# ─── #3: Newton com guardrail + bisseccao ─────────────────────────────────────
def TestNewton() -> None:
    print("\n== #3 CalcularTaxaNegociacao (round-trip + raise) ==")
    inicio = date(2024, 1, 15)
    fluxo  = [(date(2027, 1, 15), 100.0, 0.0)]   # bullet PREFIXADO
    dataCalc = date(2024, 6, 17)

    for taxaAlvo in (8.0, 12.0, 15.5):
        pu = C.CalcularPuOperacao(dataCalc, inicio, 10.0, taxaAlvo, fluxo, 1000.0, 'PREFIXADO')
        taxa = C.CalcularTaxaNegociacao(dataCalc, inicio, 10.0, pu, fluxo, 1000.0, 'PREFIXADO')
        Checar(f"round-trip PU->taxa reproduz {taxaAlvo}%",
               abs(taxa - taxaAlvo) < 0.01, f"achou {taxa}")

    # PU impossivel (absurdamente alto) -> nao bracketavel -> ValueError.
    levantou = False
    try:
        C.CalcularTaxaNegociacao(dataCalc, inicio, 10.0, 1e12, fluxo, 1000.0, 'PREFIXADO')
    except ValueError:
        levantou = True
    Checar("PU impossivel levanta ValueError (em vez de taxa espuria)", levantou)


# ─── #4: MERCADO cache lazy + Recarregar ──────────────────────────────────────
def TestMercado() -> None:
    print("\n== #4 MERCADO (cache lazy + Recarregar) ==")
    C.MERCADO.Recarregar()
    Checar("apos Recarregar, feriados nao carregados (lazy)",
           C.MERCADO._feriados is None)
    _ = C.MERCADO.feriados
    Checar("acesso a .feriados carrega o cache", C.MERCADO._feriados is not None)
    Checar("__getattr__ de compat expoe FERIADOS_ANBIMA",
           len(C.FERIADOS_ANBIMA) > 0)


# ─── #2b: integracao schema + lib/calc ────────────────────────────────────────
def TestIntegracao() -> None:
    print("\n== #2b Integracao: coluna cdTipoAmortizacao + propagacao ==")
    with tempfile.TemporaryDirectory() as tmp:
        dbPath = str(Path(tmp) / "teste.db")
        conn = ObterBanco(dbPath)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(InfoAtivos)")}
        Checar("schema tem cdTipoAmortizacao", "cdTipoAmortizacao" in cols)

        conn.execute(
            "INSERT INTO InfoAtivos (cdTicker, cdIndexador, vrTaxaEmissao, vrVNE, "
            "dtInicioRentabilidade, cdTipoAmortizacao, dtAtualizacao) "
            "VALUES ('TESTE11','PREFIXADO',10.0,1000.0,'2024-01-15','saldo_restante','2024-06-17')")
        conn.executemany(
            "INSERT INTO FluxoAtivos (cdTicker, dtEvento, vrPctAmortizacao, "
            "vrPctIncorporacao, dtAtualizacao) VALUES (?,?,?,?,?)",
            [("TESTE11", "2025-01-15", 50.0, 0.0, "2024-06-17"),
             ("TESTE11", "2026-01-15", 50.0, 0.0, "2024-06-17")])
        conn.commit()

        ativo = CarregarAtivo(conn, "TESTE11")
        Checar("CarregarAtivo devolve cdTipoAmortizacao do cadastro",
               ativo is not None and ativo["cdTipoAmortizacao"] == "saldo_restante")

        # A precificacao via lib/calc deve respeitar o cadastro (saldo_restante),
        # diferindo do que a heuristica (saldo_original, soma 100) daria.
        dataCalc = date(2024, 6, 17)
        puCadastro = CalcularPu(ativo, dataCalc, 12.0)
        puInfer = C.CalcularPuOperacao(dataCalc, date(2024, 1, 15), 10.0, 12.0,
                                       ativo["fluxo"], 1000.0, "PREFIXADO")  # None -> infere
        Checar("PU via lib/calc usa o cadastro (difere do inferido)",
               abs(puCadastro - puInfer) > 1e-4,
               f"cadastro={puCadastro:.6f} inferido={puInfer:.6f}")
        conn.close()


if __name__ == "__main__":
    TestAccProj()
    TestResolverTipoAmort()
    TestNewton()
    TestMercado()
    TestIntegracao()
    print("\n" + "=" * 50)
    if falhas:
        print(f"[X] {len(falhas)} teste(s) FALHARAM: {falhas}")
        sys.exit(1)
    print("[OK] Todos os testes da FASE 1 passaram.")
