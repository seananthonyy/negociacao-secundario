"""
datas.py
========
Dias uteis pelo calendario da Anbima. Fonte unica do que era copiado em quatro
lugares.

Por que existe
--------------
Ate 01/09/2026 a leitura do `feriados_anbima.csv` estava escrita QUATRO vezes:
`pipeline_core.Feriados`, `gerar_relatorio_credito.CarregarFeriados`,
`scrape_b3_curva_di.CarregarFeriados` e um bloco solto dentro de uma funcao do
o relatorio diario (ja removido). Todas liam o mesmo arquivo e faziam a mesma coisa -- menos
numa diferenca que importa: tres devolviam conjunto VAZIO quando o arquivo nao
existia, e so a do `scrape_b3_curva_di` levantava.

Aqui vale a versao que LEVANTA. Feriado vazio nao da erro: faz todo sabado, domingo
e feriado virarem dia util, e o efeito e um relatorio com pregao que nao existiu e
uma janela de datas maior do que a real -- errado em silencio, que e o modo de falha
que este projeto ja pagou caro uma vez (o `paths` ancorado no cwd criando banco vazio
noutra pasta, e o script terminando "com sucesso" sobre nada).

O calendario nao muda durante uma rodada, entao o arquivo e lido UMA vez por
processo. Quem precisa reler depois de o CSV mudar chama `Recarregar()`.

Nota: a `calculadora_rf` tem o proprio `FERIADOS_ANBIMA`, carregado do MESMO arquivo
(ela le os tres de CALCRF_FILES_DIR pelo nome). Sao dois caches do mesmo conteudo,
nao duas verdades -- e a dela e contrato da calc, entao nao se mexe.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

from config import cfg

CAMINHO_CSV = Path(cfg["paths"]["feriadosCsv"])

feriadosCache: frozenset[date] | None = None


def Recarregar() -> frozenset[date]:
    """Le o CSV de novo e substitui o cache. Devolve o conjunto lido."""
    global feriadosCache
    if not CAMINHO_CSV.exists():
        raise FileNotFoundError(
            f"feriados_anbima.csv nao encontrado em {CAMINHO_CSV.resolve()}. "
            "Sem ele todo fim de semana e feriado viraria dia util, em silencio. "
            "Ele mora em database/arquivos/ e e lido tambem pela calculadora, "
            "por CALCRF_FILES_DIR.")
    lidos: set[date] = set()
    with CAMINHO_CSV.open(encoding="utf-8", newline="") as fh:
        for linha in csv.DictReader(fh):
            bruto = (linha.get("data") or "").strip()
            if not bruto:
                continue
            try:
                lidos.add(date.fromisoformat(bruto))
            except ValueError:
                pass          # linha suja no CSV nao derruba o calendario inteiro
    feriadosCache = frozenset(lidos)
    return feriadosCache


def Feriados() -> frozenset[date]:
    """Os feriados Anbima. Le o arquivo na primeira chamada do processo."""
    return feriadosCache if feriadosCache is not None else Recarregar()


def EmData(d: date | str) -> date:
    """Aceita `date` ou ISO 'YYYY-MM-DD' e devolve sempre `date`."""
    return date.fromisoformat(d) if isinstance(d, str) else d


def EhDiaUtil(d: date | str, feriados: frozenset[date] | set[date] | None = None) -> bool:
    d = EmData(d)
    feriados = Feriados() if feriados is None else feriados
    return d.weekday() < 5 and d not in feriados


def DiaUtilAnteriorOuIgual(d: date | str,
                           feriados: frozenset[date] | set[date] | None = None) -> date:
    """O proprio `d` se for dia util; senao o dia util imediatamente anterior."""
    d = EmData(d)
    feriados = Feriados() if feriados is None else feriados
    while not EhDiaUtil(d, feriados):
        d -= timedelta(days=1)
    return d


def DiaUtilAnterior(d: date | str,
                    feriados: frozenset[date] | set[date] | None = None) -> date:
    """Dia util imediatamente ANTERIOR a `d` (nunca o proprio `d`)."""
    return DiaUtilAnteriorOuIgual(EmData(d) - timedelta(days=1), feriados)


def UltimosNDiasUteis(n: int, ref: date | str | None = None) -> list[date]:
    """Os `n` dias uteis mais recentes ate `ref` (inclusive, se `ref` for dia util),
    em ordem cronologica (mais antigo -> mais recente)."""
    feriados = Feriados()
    d = date.today() if ref is None else EmData(ref)
    dias: list[date] = []
    while len(dias) < n:
        if EhDiaUtil(d, feriados):
            dias.append(d)
        d -= timedelta(days=1)
    return list(reversed(dias))


def DiasUteisEntre(inicio: date | str, fim: date | str) -> list[date]:
    """Dias uteis no intervalo [inicio, fim], inclusive nas duas pontas."""
    feriados = Feriados()
    atual, ultimo = EmData(inicio), EmData(fim)
    dias: list[date] = []
    while atual <= ultimo:
        if EhDiaUtil(atual, feriados):
            dias.append(atual)
        atual += timedelta(days=1)
    return dias
