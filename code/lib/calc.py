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
