"""
lib/calc.py
===========
Ponte para a **calculadora de renda fixa** (projeto `calculadora-renda-fixa`).

Divisão de responsabilidades (handoff de 12/07/2026, `docs/MIGRACAO.md` de lá):
  - a calc é a **biblioteca de cálculo** (precifica; não se mexe nela);
  - **este** projeto coleta e mantém os dados que ela consome.

Os insumos vivem em `code/data/`, junto do `trades.db`:
  - `ipca.db` → `IPCA` (realizado, IBGE) e `IPCAProjetado` (projeção Anbima)
  - `di.db`   → `DiHistorico` (DI realizado, BCB) e `CurvaDi` (curva DI×pré, B3)
  - `feriados_anbima.csv` (já existia aqui; idêntico ao da calc)

A calc resolve esses três por `DIR_ARQUIVOS`, que lê a env var `CALCRF_FILES_DIR`
— setada por este módulo. Sem ela, a calc cai no `files/` dela (é o que o add-in
do Excel faz), então nada quebra do lado de lá.

Este é o **único** módulo que sabe onde a calc está instalada
(`config.toml [paths] calculadoraDir`, sobrescrevível por `CALCULADORA_DIR`).

Schemas das 4 tabelas são **contrato com a calc** — nomes de coluna preservados
como ela os lê (por isso não seguem o prefixo `vr/cd/dt` do `trades.db`).

Uso:
    from lib.calc import ObterBancoIpca, ObterBancoDi, ImportarCalc

    conn = ObterBancoIpca()          # cria/abre data/ipca.db já com as tabelas
    C = ImportarCalc()               # módulo calculadora_rf, apontado para data/
    du = C.ProximoDu(d, C.FERIADOS_ANBIMA)
"""

import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

from lib.config import cfg, ObterSegredo

# Raiz do projeto Python: code/  (lib/../)
RAIZ = Path(__file__).parent.parent

DDL_IPCA = """
CREATE TABLE IF NOT EXISTS IPCA (
    dtIPCA           TEXT PRIMARY KEY,   -- 'YYYY-MM'
    vrIndiceIPCA     REAL,               -- número-índice (base dez/1993 = 100)
    dtDivulgacaoIPCA TEXT                -- 'YYYY-MM-DD'; NULL antes de dez/2016
);

CREATE TABLE IF NOT EXISTS IPCAProjetado (
    dtIPCAProjetado TEXT PRIMARY KEY,    -- 'YYYY-MM-DD' (um registro por dia corrido)
    vrProjecaoIPCA  REAL                 -- projeção em % (ex.: 0.34)
);
"""

DDL_DI = """
CREATE TABLE IF NOT EXISTS DiHistorico (
    dtReferencia   TEXT PRIMARY KEY,     -- 'YYYY-MM-DD' (só dias úteis)
    vrTaxaDiAnual  REAL,                 -- SGS 4389, % a.a. base 252 (ex.: 14.40)
    vrTaxaDiDiaria REAL                  -- SGS 12, % ao dia, 6 casas (ex.: 0.053400)
);

CREATE TABLE IF NOT EXISTS CurvaDi (
    dtReferencia TEXT,                   -- pregão da curva
    du           INTEGER,                -- dias úteis do vértice
    diasCorridos INTEGER,
    vrTaxa       REAL,                   -- taxa pré % a.a. base 252
    PRIMARY KEY (dtReferencia, du)
);
"""

calcImportada = None  # cache do módulo calculadora_rf


def DirArquivos() -> Path:
    """Pasta dos insumos da calc = a mesma do trades.db (code/data/). Absoluta."""
    return (RAIZ / Path(cfg["paths"]["dbFile"])).resolve().parent


def DirCalculadora() -> Path:
    """Onde a calculadora está instalada. Env var (banco) > config.toml."""
    bruto = ObterSegredo("calculadoraDir") or cfg["paths"]["calculadoraDir"]
    caminho = Path(bruto)
    return caminho if caminho.is_absolute() else (RAIZ / caminho).resolve()


def CaminhoBancoIpca() -> Path:
    return (RAIZ / Path(cfg["paths"]["ipcaDb"])).resolve()


def CaminhoBancoDi() -> Path:
    return (RAIZ / Path(cfg["paths"]["diDb"])).resolve()


def ObterBancoIpca() -> sqlite3.Connection:
    """Conexão com data/ipca.db, com IPCA e IPCAProjetado garantidas."""
    return AbrirBanco(CaminhoBancoIpca(), DDL_IPCA)


def ObterBancoDi() -> sqlite3.Connection:
    """Conexão com data/di.db, com DiHistorico e CurvaDi garantidas."""
    return AbrirBanco(CaminhoBancoDi(), DDL_DI)


def AbrirBanco(caminho: Path, ddl: str) -> sqlite3.Connection:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(caminho)
    conn.row_factory = sqlite3.Row
    conn.executescript(ddl)
    conn.commit()
    return conn


def ImportarCalc():
    """Importa `calculadora_rf` apontada para os nossos dados. Devolve o módulo.

    A calc carrega feriados, IPCA e IPCA projetado **no import** — se as tabelas
    ainda não existirem (máquina nova, antes da primeira coleta), o import
    quebraria. Por isso os dois bancos são criados vazios antes."""
    global calcImportada
    if calcImportada is not None:
        return calcImportada

    os.environ["CALCRF_FILES_DIR"] = str(DirArquivos())

    ObterBancoIpca().close()
    ObterBancoDi().close()

    dirCalc = DirCalculadora()
    if not (dirCalc / "calculadora_rf.py").exists():
        raise FileNotFoundError(
            f"calculadora_rf.py não encontrado em {dirCalc}. "
            "Ajuste [paths].calculadoraDir no config.toml (ou a env var CALCULADORA_DIR)."
        )
    if str(dirCalc) not in sys.path:
        sys.path.insert(0, str(dirCalc))

    import calculadora_rf  # noqa: E402  (import tardio: depende do sys.path acima)

    calcImportada = calculadora_rf
    return calcImportada


def RecarregarMercado() -> None:
    """Descarta o cache de dados de mercado da calc (feriados, IPCA, projecao, DI).
    Chame apos rodar os scrapers de IPCA/DI no MESMO processo, para a proxima
    precificacao ler os dados frescos em vez do snapshot carregado antes."""
    ImportarCalc().MERCADO.Recarregar()


# ---------------------------------------------------------------------------
# Precificação — a ponte entre o trades.db e a calculadora
# ---------------------------------------------------------------------------
#
# Um lugar só monta os argumentos da calc a partir da nossa base. Sem isso, cada script
# (calc_taxa, validar_calc_b3, match_referencias) remontaria o mesmo dicionário, e a chance de
# um deles esquecer o `vrAniversario` — e a calc então IGNORAR silenciosamente todos os
# eventos do fluxo — é alta demais. Ver [[14 - Rotinas da Calculadora]].

INDEXADORES_SUPORTADOS = ("IPCA", "PREFIXADO", "CDI+", "%CDI")


def CarregarAtivo(conn, cdTicker: str) -> dict | None:
    """Cadastro + fluxo no formato que a calculadora consome, ou None se não dá para
    precificar (falta taxa de emissão, início de rentabilidade, fluxo ou indexador)."""
    info = conn.execute(
        "SELECT cdIndexador, vrTaxaEmissao, vrVNE, dtInicioRentabilidade, dtVencimento, "
        "       vrAniversario, cdTipoAmortizacao, stFluxoValidado "
        "FROM InfoAtivos WHERE cdTicker = ?", (cdTicker,)).fetchone()
    if info is None:
        return None

    cdIndexador = info["cdIndexador"]
    if (cdIndexador not in INDEXADORES_SUPORTADOS
            or info["vrTaxaEmissao"] is None
            or not info["dtInicioRentabilidade"]):
        return None

    fluxo = [(date.fromisoformat(e["dtEvento"]),
              e["vrPctAmortizacao"] or 0.0,
              e["vrPctIncorporacao"] or 0.0)
             for e in conn.execute(
                 "SELECT dtEvento, vrPctAmortizacao, vrPctIncorporacao FROM FluxoAtivos "
                 "WHERE cdTicker = ? ORDER BY dtEvento", (cdTicker,))]
    if not fluxo:
        return None

    # Aniversário é conceito de IPCA. Nos demais a calc ignora o parâmetro — passar o
    # default (15) é inócuo, e evita um `if` em cada chamador.
    aniversario = info["vrAniversario"] if cdIndexador == "IPCA" else None

    return {
        "cdTicker": cdTicker,
        "cdIndexador": cdIndexador,
        "vrTaxaEmissao": float(info["vrTaxaEmissao"]),
        "vrVNE": float(info["vrVNE"]) if info["vrVNE"] else 1000.0,
        "dtInicioRentabilidade": date.fromisoformat(info["dtInicioRentabilidade"]),
        "dtVencimento": info["dtVencimento"],
        "vrAniversario": int(aniversario) if aniversario is not None else None,
        "cdTipoAmortizacao": info["cdTipoAmortizacao"],
        "stFluxoValidado": info["stFluxoValidado"],
        "fluxo": fluxo,
    }


def ArgumentosCalc(ativo: dict) -> tuple:
    """Os argumentos posicionais comuns a CalcularPuOperacao / CalcularTaxaNegociacao,
    depois de (dataCalc, dataInicioRent, taxaEmissao)."""
    C = ImportarCalc()
    aniv = ativo["vrAniversario"]
    return (ativo["fluxo"], ativo["vrVNE"], ativo["cdIndexador"],
            aniv if aniv is not None else C.DIA_ANIV, ativo["cdTipoAmortizacao"])


def CalcularPu(ativo: dict, dtCalc: date, vrTaxa: float) -> float:
    """PU de operação do ativo em dtCalc, descontado por vrTaxa (% a.a. base 252)."""
    C = ImportarCalc()
    return C.CalcularPuOperacao(
        dtCalc, ativo["dtInicioRentabilidade"], ativo["vrTaxaEmissao"], vrTaxa,
        *ArgumentosCalc(ativo))


def CalcularTaxa(ativo: dict, dtCalc: date, vrPU: float) -> float:
    """Taxa de negociação (% a.a. base 252) implícita num PU. Newton-Raphson."""
    C = ImportarCalc()
    return C.CalcularTaxaNegociacao(
        dtCalc, ativo["dtInicioRentabilidade"], ativo["vrTaxaEmissao"], vrPU,
        *ArgumentosCalc(ativo))


def CalcularVnaAtivo(ativo: dict, dtCalc: date) -> float:
    """VNA (saldo devedor atualizado) do ativo em dtCalc."""
    C = ImportarCalc()
    aniv = ativo["vrAniversario"]
    # No VNA a calc só distingue IPCA (corrigido) de PREFIXADO (nominal). CDI é nominal
    # também — o acúmulo do DI vive no PU par, não no VNA.
    cdIndexador = "IPCA" if ativo["cdIndexador"] == "IPCA" else "PREFIXADO"
    return C.CalcularVna(
        dtCalc, ativo["dtInicioRentabilidade"], ativo["vrVNE"], ativo["fluxo"],
        cdIndexador, ativo["vrTaxaEmissao"], aniv if aniv is not None else C.DIA_ANIV,
        ativo["cdTipoAmortizacao"])


def CalcularDuration(ativo: dict, dtCalc: date, vrTaxaNegociacao: float) -> float:
    """Duration de Macaulay do ativo em dtCalc (em ANOS, base 252), descontada por
    vrTaxaNegociacao — mesma unidade da `vrDuration` na base (Anbima/MtmAnbima).
    Desde a FASE 2 a calc ja devolve em anos; nao dividimos mais por 252.

    Desde o Passo 3 da FASE 3 a duration sai do gerador unificado e usa o VNA real
    (antes usava VNE cru como face). Por isso agora depende do `diaAniversario`:
    com o aniversario errado, os eventos do fluxo nao casam e sao ignorados no VNA."""
    C = ImportarCalc()
    aniv = ativo["vrAniversario"]
    return C.CalcularDuration(
        dtCalc, ativo["dtInicioRentabilidade"], ativo["vrTaxaEmissao"], vrTaxaNegociacao,
        ativo["fluxo"], ativo["vrVNE"], ativo["cdIndexador"], ativo["cdTipoAmortizacao"],
        aniv if aniv is not None else C.DIA_ANIV)


def CalcularDurationModificada(ativo: dict, dtCalc: date, vrTaxaNegociacao: float) -> float:
    """Duration modificada (anos) do ativo em dtCalc = D_macaulay / (1 + y)."""
    C = ImportarCalc()
    aniv = ativo["vrAniversario"]
    return C.CalcularDurationModificada(
        dtCalc, ativo["dtInicioRentabilidade"], ativo["vrTaxaEmissao"], vrTaxaNegociacao,
        ativo["fluxo"], ativo["vrVNE"], ativo["cdIndexador"], ativo["cdTipoAmortizacao"],
        aniv if aniv is not None else C.DIA_ANIV)


def CalcularDv01(ativo: dict, dtCalc: date, vrTaxaNegociacao: float) -> float:
    """DV01 (R$/face por +1 bp) do ativo em dtCalc, por bump-and-reprice."""
    C = ImportarCalc()
    aniv = ativo["vrAniversario"]
    return C.CalcularDv01(
        dtCalc, ativo["dtInicioRentabilidade"], ativo["vrTaxaEmissao"], vrTaxaNegociacao,
        ativo["fluxo"], ativo["vrVNE"], ativo["cdIndexador"],
        aniv if aniv is not None else C.DIA_ANIV, ativo["cdTipoAmortizacao"])
