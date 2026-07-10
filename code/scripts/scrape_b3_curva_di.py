"""
scrape_b3_curva_di.py
=====================
Baixa a curva pré × DI da B3 via API (sem browser) e extrai as taxas nos
vértices dos contratos DI Futuro (DI1F27–DI1F34), populando MtmAnbima.

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

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.db import ObterBanco
from lib.logger import ObterLogger
from lib.email_outlook import EnviarEmailConclusao

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

FERIADOS_PATH = Path("data/feriados_anbima.csv")

SQL_UPSERT = """
INSERT INTO MtmAnbima (cdTicker, dtReferencia, vrTaxa, vrDuration)
VALUES (?, ?, ?, ?)
ON CONFLICT(cdTicker, dtReferencia) DO UPDATE SET
    vrTaxa     = excluded.vrTaxa,
    vrDuration = excluded.vrDuration
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


def CsvDuMap(client: httpx.Client, dtStr: str, log) -> dict[int, float] | None:
    raw = ChamarApi(client, "Search/GetDownloadFile", {"language": "pt-br", "id": PRODUTO, "date": dtStr})
    csvBytes = base64.b64decode(raw)
    try:
        csvText = csvBytes.decode("utf-8")
    except UnicodeDecodeError:
        csvText = csvBytes.decode("latin-1")

    duMap: dict[int, float] = {}
    reader = csv.reader(io.StringIO(csvText), delimiter=";")
    next(reader, None)  # skip header
    for row in reader:
        if len(row) < 4:
            continue
        try:
            du   = int(row[1].strip())
            taxa = float(row[3].strip().replace(",", "."))
            if du > 0 and taxa > 0:
                duMap[du] = taxa
        except (ValueError, IndexError):
            continue

    log.debug("curva_di: %s — %d vertices no CSV", dtStr, len(duMap))
    return duMap if duMap else None


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
        upsertRows.append((ticker, dtStr, vrTaxa, vrDuration))
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


def MontarResumo(dtStr: str, upserted: int, semMatch: list[str], taxas: dict[str, float]) -> str:
    lines = ["Resultado:", f"Data: {dtStr}", f"Contratos salvos: {upserted}"]
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
    summary = ""
    success = True

    try:
        dtRef  = date.fromisoformat(args.date)
        dtStr  = dtRef.isoformat()

        feriados = CarregarFeriados()
        log.info("curva_di: %d feriados carregados", len(feriados))

        with httpx.Client() as client:
            # Verifica disponibilidade
            datasDisponiveis = DatasDisponiveis(client)
            log.info("curva_di: %d datas disponiveis, mais recente: %s", len(datasDisponiveis), datasDisponiveis[0] if datasDisponiveis else "?")

            if dtRef not in datasDisponiveis:
                datas = [d.isoformat() for d in datasDisponiveis]
                raise ValueError(
                    f"Data {dtStr} nao disponivel na API B3. "
                    f"Disponiveis: {datas}"
                )

            duMap = CsvDuMap(client, dtStr, log)
            if duMap is None:
                raise ValueError(f"CSV vazio para {dtStr}")

        upsertRows, semMatch = ProcessarDuMap(duMap, dtRef, feriados, dtStr, log)

        conn = ObterBanco()
        try:
            if upsertRows:
                conn.executemany(SQL_UPSERT, upsertRows)
                conn.commit()
            log.info("curva_di: %s — %d contratos salvos em MtmAnbima", dtStr, len(upsertRows))
        finally:
            conn.close()

        taxas   = {row[0]: row[2] for row in upsertRows}
        summary = MontarResumo(dtStr, len(upsertRows), semMatch, taxas)
        log.info("curva_di: concluido.\n%s", summary)

    except Exception:
        success = False
        summary = traceback.format_exc()
        log.exception("curva_di: erro inesperado")

    finally:
        EnviarEmailConclusao(NOME_SCRIPT, success, summary, logger=log)


if __name__ == "__main__":
    Principal()
