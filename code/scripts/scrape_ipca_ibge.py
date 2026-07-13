"""
scrape_ipca_ibge.py
===================
IPCA realizado (número-índice) do IBGE → `data/ipca.db`, tabela `IPCA`.

É um dos insumos da calculadora de renda fixa (VNA de IPCA+) — ver `lib/calc.py`.
Migrado do `atualizar_ipca.py` do projeto `calculadora-renda-fixa` (handoff 12/07/2026).

Fontes:
  - Número-índice   : SIDRA tabela 1737, variável 2266 (base dez/1993 = 100).
                      Devolve a série INTEIRA a cada chamada (desde 1979).
  - Data divulgação : API Calendário do IBGE. Só cobre de dez/2016 em diante;
                      meses anteriores ficam com dtDivulgacaoIPCA NULL (a calc
                      trata isso como "mês fechado" para datas no passado).

Idempotente: UPSERT com COALESCE — nunca apaga o que já está gravado. Sem
argumentos; roda a série toda a cada execução (os meses novos entram sozinhos).

CLI:
    python scripts/scrape_ipca_ibge.py
"""

import gzip
import json
import sys
import traceback
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.calc import ObterBancoIpca
from lib.email_outlook import EnviarEmailConclusao
from lib.relatorio_execucao import RelatorioExecucao
from lib.logger import ObterLogger

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

NOME_SCRIPT = "scrape_ipca_ibge"

URL_SIDRA = "https://apisidra.ibge.gov.br/values/t/1737/n1/all/v/2266/p/all/f/n"
URL_CALENDARIO = "https://servicodados.ibge.gov.br/api/v3/calendario/?qtd=500&de={ini}&ate={fim}"
ALIAS_IPCA = "indice-nacional-de-precos-ao-consumidor-amplo"

ANO_INICIAL = 2014   # início da varredura do calendário (a API só cobre dez/2016+)
ANO_FINAL = 2030

TIMEOUT = 30
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"}

MESES_PT = {
    "janeiro": "01", "fevereiro": "02", "marco": "03", "abril": "04",
    "maio": "05", "junho": "06", "julho": "07", "agosto": "08",
    "setembro": "09", "outubro": "10", "novembro": "11", "dezembro": "12",
}

# COALESCE(excluded, atual): a fonte que não trouxe o campo não apaga o que já
# existe (o calendário não cobre pré-2016; o SIDRA não traz data de divulgação).
SQL_UPSERT = """
INSERT INTO IPCA (dtIPCA, vrIndiceIPCA, dtDivulgacaoIPCA)
VALUES (?, ?, ?)
ON CONFLICT(dtIPCA) DO UPDATE SET
    vrIndiceIPCA     = COALESCE(excluded.vrIndiceIPCA, vrIndiceIPCA),
    dtDivulgacaoIPCA = COALESCE(excluded.dtDivulgacaoIPCA, dtDivulgacaoIPCA)
"""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def Buscar(client: httpx.Client, url: str) -> list | dict:
    """GET + parse JSON. O SIDRA às vezes devolve gzip sem o header correspondente."""
    resp = client.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    raw = resp.content
    try:
        raw = gzip.decompress(raw)
    except Exception:
        pass
    return json.loads(raw.decode("utf-8"))


def SemAcento(texto: str) -> str:
    for de, para in (("ç", "c"), ("ã", "a"), ("á", "a"), ("ê", "e")):
        texto = texto.replace(de, para)
    return texto


# ---------------------------------------------------------------------------
# Fonte 1 — número-índice (SIDRA)
# ---------------------------------------------------------------------------

def BuscarIndices(client: httpx.Client, log) -> dict[str, float]:
    """{'2024-11': 7043.63, ...} — a série inteira publicada pelo SIDRA."""
    dados = Buscar(client, URL_SIDRA)
    indices: dict[str, float] = {}

    for item in dados[1:]:  # a linha 0 é o cabeçalho descritivo
        periodo = SemAcento((item.get("D3N") or "").strip().lower())  # 'novembro 2024'
        valor = (item.get("V") or "").strip()
        if not periodo or valor in ("", "...", "-"):
            continue
        partes = periodo.split()
        if len(partes) != 2:
            continue
        mes = MESES_PT.get(partes[0])
        if not mes:
            continue
        try:
            indices[f"{partes[1]}-{mes}"] = float(valor.replace(",", "."))
        except ValueError:
            continue

    log.info("ipca: %d números-índice no SIDRA", len(indices))
    return indices


# ---------------------------------------------------------------------------
# Fonte 2 — datas de divulgação (Calendário IBGE)
# ---------------------------------------------------------------------------

def BuscarDivulgacoes(client: httpx.Client, log) -> dict[str, str]:
    """{'2024-11': '2024-12-10', ...} — data em que o IBGE divulgou cada mês."""
    divulgacoes: dict[str, str] = {}

    for ano in range(ANO_INICIAL, ANO_FINAL + 1):
        url = URL_CALENDARIO.format(ini=f"{ano}-01-01", fim=f"{ano}-12-31")
        try:
            dados = Buscar(client, url)
        except Exception as exc:
            # Ano sem calendário publicado não é motivo para derrubar a rotina —
            # o número-índice (que é o que a calc usa de fato) já veio do SIDRA.
            log.warning("ipca: calendário de %d indisponível: %s", ano, exc)
            continue

        for evento in dados.get("items", []):
            if evento.get("alias_produto") != ALIAS_IPCA:
                continue
            mes = evento.get("mes_referencia_inicio")
            anoRef = evento.get("ano_referencia_inicio")
            dtDivulgacao = (evento.get("data_divulgacao") or "")[:10]
            if not mes or not anoRef or not dtDivulgacao:
                continue
            if "/" in dtDivulgacao:  # 'DD/MM/AAAA' → ISO
                d, m, a = dtDivulgacao.split("/")
                dtDivulgacao = f"{a}-{m}-{d}"
            divulgacoes[f"{anoRef}-{str(mes).zfill(2)}"] = dtDivulgacao

    log.info("ipca: %d datas de divulgação no calendário", len(divulgacoes))
    return divulgacoes


# ---------------------------------------------------------------------------
# Persistência
# ---------------------------------------------------------------------------

def Gravar(indices: dict[str, float], divulgacoes: dict[str, str], log) -> int:
    chaves = sorted(set(indices) | set(divulgacoes))
    linhas = [(k, indices.get(k), divulgacoes.get(k)) for k in chaves]

    conn = ObterBancoIpca()
    try:
        antes = conn.execute("SELECT COUNT(*) FROM IPCA").fetchone()[0]
        conn.executemany(SQL_UPSERT, linhas)
        conn.commit()
        depois = conn.execute("SELECT COUNT(*) FROM IPCA").fetchone()[0]
    finally:
        conn.close()

    log.info("ipca: %d meses gravados (%d novos)", len(linhas), depois - antes)
    return depois - antes


def MontarResumo(indices: dict[str, float], divulgacoes: dict[str, str], novos: int) -> str:
    ultimo = max(indices) if indices else "?"
    return "\n".join([
        "Resultado:",
        f"Números-índice (SIDRA)      : {len(indices)}",
        f"Datas divulgação (Calendário): {len(divulgacoes)}",
        f"Meses novos na base          : {novos}",
        "",
        f"Último mês: {ultimo} = {indices.get(ultimo)}"
        + (f" (divulgado em {divulgacoes[ultimo]})" if ultimo in divulgacoes else ""),
    ])


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Principal() -> None:
    log = ObterLogger(NOME_SCRIPT)
    rel     = RelatorioExecucao(NOME_SCRIPT)
    erro    = None
    summary = ""
    success = True

    try:
        with httpx.Client(trust_env=True) as client:
            indices = BuscarIndices(client, log)
            divulgacoes = BuscarDivulgacoes(client, log)

        # Zero número-índice = fonte quebrada (o SIDRA sempre devolve a série
        # inteira). Falha alto em vez de sair [OK] mudo — regra do [[98 - Backlog]].
        if not indices:
            raise RuntimeError("SIDRA não devolveu nenhum número-índice — fonte quebrada.")

        novos = Gravar(indices, divulgacoes, log)
        summary = MontarResumo(indices, divulgacoes, novos)
        log.info("ipca: concluído.\n%s", summary)

    except Exception:
        success = False
        erro = traceback.format_exc()
        rel.Erro("A rodada abortou — ver traceback.")
        log.exception("ipca: erro inesperado")

    finally:
        if summary:
            rel.Secao("Resumo", ["saida"], [[l] for l in summary.splitlines() if l.strip()])
        EnviarEmailConclusao(NOME_SCRIPT, success, rel, tracebackErro=erro, logger=log)

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    Principal()
