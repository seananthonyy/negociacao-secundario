"""
scrape_b3_curva_di.py
=====================
Baixa a curva pré × DI da B3 via API (sem browser). Um download, dois destinos:

  1. `trades.db/MtmAnbima` — as taxas nos vértices dos contratos DI Futuro
     (DI1F27–DI1F34), que é o que o relatório usa como referência de spread.
  2. `data/di.db/CurvaDi`  — a **curva inteira** (todos os vértices, du a du),
     insumo da calculadora de renda fixa para projetar/descontar fluxos DI
     (ver `lib/calc.py`). Metade do antigo `atualizar_di.py` da calculadora;
     a outra metade (DI realizado do BCB) virou `scrape_di_bcb.py`.

O arquivamento da curva importa porque a **B3 não guarda histórico**: a API só
expõe ~20 pregões. Rodando todo dia, acumulamos os snapshots que ela descarta —
é o que permite reprecificar uma data passada.

API: sistemaswebb3-derivativos.b3.com.br/referenceRatesProxy/
  - Search/GetDate/{base64(json)}   → datas disponíveis (~20 últimos pregões)
  - Search/GetDownloadFile/{base64} → CSV completo do dia (base64-encoded)

CSV: Descrição da Taxa;Dias Úteis;Dias Corridos;Preço/Taxa  (sep=';', dec=',')

CLI:
    python scripts/scrape_b3_curva_di.py --date 2026-06-05
"""

import argparse
import base64
import csv
import io
import json
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

import httpx
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Helpers"))

import dados as D
from calc import ObterBancoDi
from config import cfg
from logger import ObterLogger
from email_outlook import EnviarEmailConclusao
from relatorio_execucao import RelatorioExecucao

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

NOME_SCRIPT = "scrape_b3_curva_di"

API_BASE = "https://sistemaswebb3-derivativos.b3.com.br/referenceRatesProxy/"
PRODUTO  = "PRE"

CONTRATOS = [
    "DI1F27", "DI1F28", "DI1F29", "DI1F30",
    "DI1F31", "DI1F32", "DI1F33", "DI1F34",
]

FERIADOS_PATH = Path(cfg["paths"]["feriadosCsv"])

# O que era `ON CONFLICT(cdTicker, dtReferencia) DO UPDATE SET vrTaxa = excluded.vrTaxa,
# vrDuration = excluded.vrDuration`. Aqui as duas SOBRESCREVEM (diferente do
# scrape_anbima_ntnb, que preserva a duration existente): o DI1 nao depende de API
# externa — a duration sai do proprio vertice, e sempre vem.
POLITICA_MTM = {"vrTaxa": D.SOBRESCREVER, "vrDuration": D.SOBRESCREVER}

# Curva completa (di.db) — schema é contrato com a calculadora, ver lib/calc.py.
SQL_UPSERT_CURVA = """
INSERT OR REPLACE INTO CurvaDi (dtReferencia, du, diasCorridos, vrTaxa)
VALUES (?, ?, ?, ?)
"""

HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}

# ---------------------------------------------------------------------------
# Feriados
# ---------------------------------------------------------------------------

def CarregarFeriados() -> frozenset:
    if not FERIADOS_PATH.exists():
        raise FileNotFoundError(f"Feriados nao encontrado: {FERIADOS_PATH.resolve()}")
    feriados = set()
    with open(FERIADOS_PATH, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("data") or "").strip()
            if raw:
                try:
                    feriados.add(date.fromisoformat(raw))
                except ValueError:
                    pass
    return frozenset(feriados)


# ---------------------------------------------------------------------------
# Cálculo de vencimento e du
# ---------------------------------------------------------------------------

def VencimentoDi(ano: int, feriados: frozenset) -> date:
    d = date(ano, 1, 1)
    while d.weekday() >= 5 or d in feriados:
        d += timedelta(days=1)
    return d


def CalcularDu(dtRef: date, dtVenc: date, feriados: frozenset) -> int:
    du = 0
    cur = dtRef + timedelta(days=1)
    while cur <= dtVenc:
        if cur.weekday() < 5 and cur not in feriados:
            du += 1
        cur += timedelta(days=1)
    return du


def AnoDoTicker(ticker: str) -> int:
    return 2000 + int(ticker[-2:])


# ---------------------------------------------------------------------------
# API B3
# ---------------------------------------------------------------------------

def ChamarApi(client: httpx.Client, path: str, params: dict) -> bytes:
    encoded = base64.b64encode(json.dumps(params).encode()).decode()
    url = API_BASE + path + "/" + encoded
    resp = client.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.content


def DatasDisponiveis(client: httpx.Client) -> list[date]:
    raw = ChamarApi(client, "Search/GetDate", {"language": "pt-br", "id": PRODUTO})
    data = json.loads(raw)
    if isinstance(data, str):
        data = json.loads(data)
    return [date.fromisoformat(d[:10]) for d in data]


def CsvVertices(client: httpx.Client, dtStr: str, log) -> list[tuple[int, int, float]] | None:
    """Curva completa do pregão: [(du, diasCorridos, taxa)]. None se o CSV vier vazio."""
    raw = ChamarApi(client, "Search/GetDownloadFile", {"language": "pt-br", "id": PRODUTO, "date": dtStr})
    csvBytes = base64.b64decode(raw)
    try:
        csvText = csvBytes.decode("utf-8")
    except UnicodeDecodeError:
        csvText = csvBytes.decode("latin-1")

    vertices: list[tuple[int, int, float]] = []
    reader = csv.reader(io.StringIO(csvText), delimiter=";")
    next(reader, None)  # skip header
    for row in reader:
        if len(row) < 4:
            continue
        try:
            du   = int(row[1].strip())
            dc   = int(row[2].strip())
            taxa = float(row[3].strip().replace(",", "."))
            if du > 0 and taxa > 0:
                vertices.append((du, dc, taxa))
        except (ValueError, IndexError):
            continue

    log.debug("curva_di: %s — %d vertices no CSV", dtStr, len(vertices))
    return vertices if vertices else None


def GravarCurvaDi(vertices: list[tuple[int, int, float]], dtStr: str, log) -> int:
    """Arquiva a curva inteira em di.db/CurvaDi (insumo da calculadora)."""
    conn = ObterBancoDi()
    try:
        conn.executemany(SQL_UPSERT_CURVA, [(dtStr, du, dc, taxa) for du, dc, taxa in vertices])
        conn.commit()
    finally:
        conn.close()
    log.info("curva_di: %s — %d vertices arquivados em di.db/CurvaDi", dtStr, len(vertices))
    return len(vertices)


# ---------------------------------------------------------------------------
# Processamento dos contratos
# ---------------------------------------------------------------------------

def ProcessarDuMap(
    duMap: dict[int, float],
    dtRef: date,
    feriados: frozenset,
    dtStr: str,
    log,
) -> tuple[list[tuple], list[str]]:
    upsertRows: list[tuple] = []
    semMatch:   list[str]   = []

    for ticker in CONTRATOS:
        ano    = AnoDoTicker(ticker)
        dtVenc = VencimentoDi(ano, feriados)
        du     = CalcularDu(dtRef, dtVenc, feriados)

        if du not in duMap:
            log.warning("curva_di: %s — %s: du=%d nao encontrado (venc=%s)", dtStr, ticker, du, dtVenc)
            semMatch.append(ticker)
            continue

        vrTaxa     = duMap[du]
        vrDuration = round(du / 252, 6)
        upsertRows.append({"cdTicker": ticker, "dtReferencia": dtStr,
                           "vrTaxa": vrTaxa, "vrDuration": vrDuration})
        log.debug("curva_di: %s — %s: du=%d taxa=%.4f%% duration=%.4f", dtStr, ticker, du, vrTaxa, vrDuration)

    return upsertRows, semMatch


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def LerArgumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Baixa a curva DI x pre da B3 e popula MtmAnbima."
    )
    parser.add_argument("--date", required=True, metavar="YYYY-MM-DD")
    return parser.parse_args()


def MontarResumo(dtStr: str, upserted: int, semMatch: list[str], taxas: dict[str, float],
                 nVertices: int) -> str:
    lines = ["Resultado:", f"Data: {dtStr}",
             f"Contratos salvos (MtmAnbima): {upserted}",
             f"Vertices arquivados (CurvaDi): {nVertices}"]
    if semMatch:
        lines.append(f"Sem match du: {', '.join(semMatch)}")
    lines.append("")
    for ticker, taxa in sorted(taxas.items()):
        lines.append(f"  {ticker}: {taxa:.4f}%")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Principal() -> None:
    log     = ObterLogger(NOME_SCRIPT)
    args    = LerArgumentos()
    rel     = RelatorioExecucao(NOME_SCRIPT, args=vars(args))
    erro    = None
    summary = ""
    success = True

    try:
        dtRef  = date.fromisoformat(args.date)
        dtStr  = dtRef.isoformat()

        feriados = CarregarFeriados()
        log.info("curva_di: %d feriados carregados", len(feriados))

        with httpx.Client(verify=False) as client:
            # Verifica disponibilidade
            datasDisponiveis = DatasDisponiveis(client)
            log.info("curva_di: %d datas disponiveis, mais recente: %s", len(datasDisponiveis), datasDisponiveis[0] if datasDisponiveis else "?")

            if not datasDisponiveis:
                raise RuntimeError("API B3 nao devolveu nenhuma data disponivel — fonte quebrada.")

            if dtRef not in datasDisponiveis:
                # A B3 só publica ~20 pregões. Data fora dessa janela é "a fonte
                # não tem esse dado", não "o scraper quebrou": WARNING e exit 0
                # (senão a carga histórica derruba o pipeline inteiro à toa).
                datas = ", ".join(d.isoformat() for d in datasDisponiveis)
                log.warning("curva_di: %s fora da janela da API B3 — nada gravado. Disponiveis: %s", dtStr, datas)
                summary = (f"Data {dtStr} nao esta na janela publicada pela B3 (~20 pregoes) — nada gravado.\n\n"
                           f"Datas disponiveis: {datas}")
                rel.Aviso(summary.splitlines()[0])
                EnviarEmailConclusao(NOME_SCRIPT, True, rel, logger=log)
                return

            vertices = CsvVertices(client, dtStr, log)
            if vertices is None:
                raise ValueError(f"CSV vazio para {dtStr}")

        # Destino 1: a curva inteira (calculadora). Destino 2: os vértices dos
        # contratos DI1 (relatório). Mesmo CSV, uma requisição só.
        nVertices = GravarCurvaDi(vertices, dtStr, log)

        duMap = {du: taxa for du, _dc, taxa in vertices}
        upsertRows, semMatch = ProcessarDuMap(duMap, dtRef, feriados, dtStr, log)

        if upsertRows:
            D.Mesclar("MtmAnbima", pd.DataFrame(upsertRows), politica=POLITICA_MTM)
        log.info("curva_di: %s — %d contratos salvos em MtmAnbima", dtStr, len(upsertRows))

        taxas   = {row["cdTicker"]: row["vrTaxa"] for row in upsertRows}
        summary = MontarResumo(dtStr, len(upsertRows), semMatch, taxas, nVertices)
        log.info("curva_di: concluido.\n%s", summary)

    except Exception:
        success = False
        erro = traceback.format_exc()
        rel.Erro("A rodada abortou — ver traceback.")
        log.exception("curva_di: erro inesperado")

    finally:
        if summary:
            rel.Secao("Resumo", ["saida"], [[l] for l in summary.splitlines() if l.strip()])
        EnviarEmailConclusao(NOME_SCRIPT, success, rel, tracebackErro=erro, logger=log)

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    Principal()
