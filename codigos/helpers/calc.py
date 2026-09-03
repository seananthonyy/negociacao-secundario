"""
codigos/helpers/calc.py
===========
Ponte para a **calculadora de renda fixa** (projeto `calculadora-renda-fixa`).

Divisão de responsabilidades (handoff de 12/07/2026, `docs/MIGRACAO.md` de lá):
  - a calc é a **biblioteca de cálculo** (precifica; não se mexe nela);
  - **este** projeto coleta e mantém os dados que ela consome.

Os insumos vivem em `database/parquets/`, junto do `trades.db`:
  - `ipca.db` → `IPCA` (realizado, IBGE) e `IPCAProjetado` (projeção Anbima)
  - `di.db`   → `DiHistorico` (DI realizado, BCB) e `CurvaDi` (curva DI×pré, B3)
  - `feriados_anbima.csv` (já existia aqui; idêntico ao da calc)

A calc resolve esses três por `DIR_ARQUIVOS`, que lê a env var `CALCRF_FILES_DIR`
— setada por este módulo. Sem ela, a calc cai no `files/` dela (é o que o add-in
do Excel faz), então nada quebra do lado de lá.

Este é o **único** módulo que sabe onde a calc está instalada
(`config.toml [paths] calculadoraDir`, sobrescrevível por `CALCULADORA_DIR`).

As 4 tabelas de insumo (IPCA, IPCAProjetado, DiHistorico, CurvaDi) viraram Parquet
em 03/09/2026 — eram `ipca.db` e `di.db`. Os nomes de coluna delas continuam sendo
**contrato com a calc** e por isso não seguem o prefixo `vr/cd/dt` do projeto:
renomear qualquer um quebra a precificação inteira, em silêncio.

Uso:
    from calc import ImportarCalc, CarregarAtivo, CalcularPu

    C = ImportarCalc()               # calculadora_rf, apontada para os nossos dados
    du = C.ProximoDu(d, C.FERIADOS_ANBIMA)
"""

import os
import sys
from datetime import date
from pathlib import Path

import dados
from config import cfg, ObterSegredo

# Raiz do projeto (codigos/helpers/calc.py -> tres niveis acima).
RAIZ = Path(__file__).parent.parent.parent

calcImportada = None  # cache do modulo calculadora_rf


def DirArquivos() -> Path:
    """Pasta do feriados_anbima.csv — o unico insumo da calc que continua arquivo.

    A calculadora_rf a le por CALCRF_FILES_DIR, pelo nome do arquivo."""
    return Path(cfg["paths"]["feriadosCsv"]).parent


def DirParquet() -> Path:
    """Pasta das tabelas Parquet, que a calc le por CALCRF_PARQUET_DIR.

    E a MESMA raiz de dados do projeto ([dados] raiz): IPCA, IPCAProjetado,
    DiHistorico e CurvaDi sao tabelas como as outras desde 03/09/2026 -- antes eram
    dois SQLite a parte (ipca.db e di.db)."""
    return Path(dados.Raiz())


def DirCalculadora() -> Path:
    """Onde a calculadora esta instalada. Env var (banco) > config.toml."""
    bruto = ObterSegredo("calculadoraDir") or cfg["paths"]["calculadoraDir"]
    caminho = Path(bruto)
    return caminho if caminho.is_absolute() else (RAIZ / caminho).resolve()


def ImportarCalc():
    """Importa `calculadora_rf` apontada para os nossos dados. Devolve o módulo.

    Duas variaveis de ambiente: CALCRF_FILES_DIR (o feriados_anbima.csv) e
    CALCRF_PARQUET_DIR (as quatro tabelas de insumo). Sao pastas diferentes desde
    que o ipca.db e o di.db viraram Parquet.

    A calc carrega feriados, IPCA e IPCA projetado **no import**. Se os parquets
    ainda nao existirem (maquina nova, antes da primeira coleta), ela levanta com a
    mensagem de qual insumo falta -- e e o que tem de acontecer: precificar sem
    serie de IPCA daria numero errado em silencio."""
    global calcImportada
    if calcImportada is not None:
        return calcImportada

    os.environ["CALCRF_FILES_DIR"]   = str(DirArquivos())
    os.environ["CALCRF_PARQUET_DIR"] = str(DirParquet())

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


# Índice em memória de InfoAtivos e FluxoAtivos, reconstruído só quando alguém grava.
#
# No SQLite, CarregarAtivo custava dois acertos de índice e era chamado à vontade — o
# validar_calc_b3 e o match_referencias o chamam uma vez por ativo, milhares por rodada.
# No DuckDB cada consulta reabre os parquets, e esse padrão sairia da casa dos
# milissegundos para a de dezenas de minutos. As duas tabelas somam 0,55 MB: cabem
# inteiras na memória.
#
# A guarda contra dado velho é a geração de dados.py: quem grava a incrementa, e o
# índice se reconstrói na leitura seguinte. Sem isso o cache devolveria, por exemplo, o
# stFluxoValidado de antes da última validação.
indiceCache: dict = {}


def IndiceAtivos() -> dict:
    """{cdTicker: {cadastro, fluxo}}, reconstruído quando InfoAtivos ou FluxoAtivos mudam."""
    geracao = (dados.Geracao("InfoAtivos"), dados.Geracao("FluxoAtivos"))
    if indiceCache.get("geracao") == geracao:
        return indiceCache["ativos"]

    info = dados.Consultar(
        "SELECT cdTicker, cdIndexador, vrTaxaEmissao, vrVNE, dtInicioRentabilidade, "
        "       dtVencimento, vrAniversario, cdTipoAmortizacao, stFluxoValidado "
        'FROM "InfoAtivos"')
    ativos = {r["cdTicker"]: {"cadastro": r, "fluxo": []}
              for r in info.to_dict(orient="records")}

    fluxo = dados.Consultar(
        "SELECT cdTicker, dtEvento, vrPctAmortizacao, vrPctIncorporacao "
        'FROM "FluxoAtivos" ORDER BY cdTicker, dtEvento')
    for cdTicker, dtEvento, amort, incorp in fluxo.itertuples(index=False):
        alvo = ativos.get(cdTicker)
        if alvo is not None:
            alvo["fluxo"].append((date.fromisoformat(dtEvento),
                                  dados.SemNaN(amort) or 0.0,
                                  dados.SemNaN(incorp) or 0.0))

    indiceCache["geracao"] = geracao
    indiceCache["ativos"]  = ativos
    return ativos


def CarregarAtivo(cdTicker: str) -> dict | None:
    """Cadastro + fluxo no formato que a calculadora consome, ou None se não dá para
    precificar (falta taxa de emissão, início de rentabilidade, fluxo ou indexador)."""
    entrada = IndiceAtivos().get(cdTicker)
    if entrada is None:
        return None
    info = {k: dados.SemNaN(v) for k, v in entrada["cadastro"].items()}

    cdIndexador = info["cdIndexador"]
    if (cdIndexador not in INDEXADORES_SUPORTADOS
            or info["vrTaxaEmissao"] is None
            or not info["dtInicioRentabilidade"]):
        return None

    fluxo = entrada["fluxo"]
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
