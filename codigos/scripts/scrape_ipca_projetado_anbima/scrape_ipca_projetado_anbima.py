"""
scrape_ipca_projetado_anbima.py
===============================
Projeção de IPCA da Anbima → a tabela `IPCAProjetado`.

É insumo da calculadora de renda fixa: entre a divulgação de um IPCA e a do
seguinte, o VNA dos papéis IPCA+ é atualizado pela **projeção** — sem ela, não
se precifica IPCA+ no mês corrente. Ver `codigos/helpers/calc.py`.
Migrado do `atualizar_projecao.py` do projeto `calculadora-renda-fixa` (12/07/2026).

Metodologia (Data de Validade)
------------------------------
A Anbima publica, para cada projeção, uma **Data de Validade** — o dia a partir do
qual ela passa a valer para atualização do Valor Nominal. Cada projeção vale da sua
validade até a véspera da validade seguinte (carry-forward: "repete-se a última
estimativa até que uma nova seja calculada").

O script raspa a aba IPCA (`#profile`), extrai os pares (Data de Validade → Projeção)
do mês corrente e do histórico de 12 meses, e os expande em registros **diários** até
D+1 (próximo dia útil Anbima) — assim dá para precificar em D+1 por carry-forward.
A tabela "MÊS POSTERIOR" é ignorada de propósito: é um preview sem Data de Validade,
ainda não vigora.

Reprocessa a janela inteira a cada execução (UPSERT), então revisão retroativa da
Anbima é corrigida sozinha. Dias fora da janela nunca são apagados.

ATENÇÃO — horário: rodar **antes das 17h30** (Brasília). A Anbima republica as
projeções por volta desse horário nos dias de divulgação de IPCA/IPCA-15.

CLI:
    python codigos/scripts/scrape_ipca_projetado_anbima.py
"""

import re
import shutil
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers"))

import pandas as pd

import dados as D
from calc import ImportarCalc
from config import cfg, ObterProxyPlaywright
from email_outlook import EnviarEmailConclusao
from relatorio_execucao import RelatorioExecucao
from logger import ObterLogger

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

NOME_SCRIPT = "scrape_ipca_projetado_anbima"

URL_ANBIMA = ("https://www.anbima.com.br/pt_br/informar/estatisticas/"
              "precos-e-indices/projecao-de-inflacao-gp-m.htm")

RE_DATA = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2})$")
RE_NUM = re.compile(r"^-?\d{1,2},\d+$")

DIR_BACKUP = Path(cfg["paths"]["backupsDir"])
MAX_BACKUPS = 10

# ---------------------------------------------------------------------------
# Coleta do HTML
# ---------------------------------------------------------------------------

def ObterHtml(log) -> str:
    """HTML renderizado da página da Anbima (a aba IPCA é montada por JS)."""
    with sync_playwright() as p:
        # proxy explícito: o Chromium NÃO lê HTTP_PROXY/HTTPS_PROXY do ambiente.
        # Sem isso o script funciona no PC pessoal e volta vazio no banco.
        navegador = p.chromium.launch(headless=True, proxy=ObterProxyPlaywright())
        try:
            pagina = navegador.new_page(ignore_https_errors=True)
            log.info("projecao: abrindo %s", URL_ANBIMA)
            pagina.goto(URL_ANBIMA, wait_until="networkidle", timeout=60_000)
            # #profile é uma aba Bootstrap oculta — 'attached', não 'visible'.
            pagina.wait_for_selector("#profile table", state="attached", timeout=30_000)
            return pagina.content()
        finally:
            navegador.close()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def ConverterData(texto: str) -> date:
    """'dd/mm/aa' → date."""
    d, m, a = RE_DATA.match(texto).groups()
    return date(2000 + int(a), int(m), int(d))


def ExtrairProjecoes(html: str, log) -> dict[date, float]:
    """{dataValidade: projecao} da aba IPCA (#profile).

    Aceita só linhas no formato [data, número, data] = (coleta, projeção, validade).
    Esse formato já exclui cabeçalhos, linhas com projeção '-', a tabela "MÊS
    POSTERIOR" (só tem 1 data) e a coluna "IPCA Efetivo" do histórico."""
    soup = BeautifulSoup(html, "html.parser")
    aba = soup.find("div", id="profile")
    if aba is None:
        raise RuntimeError("Aba IPCA (#profile) não encontrada no HTML — a página mudou.")

    projecoes: dict[date, float] = {}

    for linha in aba.find_all("tr"):
        celulas = [c.get_text(strip=True) for c in linha.find_all("td")]

        iColeta = next((i for i, c in enumerate(celulas) if RE_DATA.match(c)), None)
        if iColeta is None:
            continue
        iProjecao = next((i for i in range(iColeta + 1, len(celulas)) if RE_NUM.match(celulas[i])), None)
        if iProjecao is None:
            continue
        iValidade = next((i for i in range(iProjecao + 1, len(celulas)) if RE_DATA.match(celulas[i])), None)
        if iValidade is None:
            continue

        projecoes[ConverterData(celulas[iValidade])] = float(celulas[iProjecao].replace(",", "."))

    if not projecoes:
        raise RuntimeError("Nenhuma projeção com Data de Validade em #profile — a página mudou.")

    log.info("projecao: %d projeções extraídas; validade mais recente: %s = %s",
             len(projecoes), max(projecoes), projecoes[max(projecoes)])
    return projecoes


# ---------------------------------------------------------------------------
# Expansão diária
# ---------------------------------------------------------------------------

def ExpandirDiario(projecoes: dict[date, float], ate: date) -> list[tuple[str, float]]:
    """Expande {validade: projeção} em (dtIso, projeção) dia a dia.

    Cada projeção vale da sua validade até a véspera da seguinte (ou até `ate`,
    se for a última). Validades futuras (> ate) ficam de fora."""
    eventos = sorted(projecoes.items())
    registros: list[tuple[str, float]] = []

    for i, (validade, projecao) in enumerate(eventos):
        if validade > ate:
            break
        proxima = eventos[i + 1][0] if i + 1 < len(eventos) else None
        fim = min(ate, proxima - timedelta(days=1)) if proxima else ate

        dia = validade
        while dia <= fim:
            registros.append((dia.isoformat(), projecao))
            dia += timedelta(days=1)

    return registros


# ---------------------------------------------------------------------------
# Persistência
# ---------------------------------------------------------------------------

def Backup(log) -> Path | None:
    """Cópia do parquet de IPCAProjetado antes de gravar.

    O histórico desta tabela **não é reconstruível**: a página da Anbima só mostra a
    projeção corrente e ~13 meses para trás, então um dia perdido é perdido para
    sempre. É a única tabela do projeto com essa propriedade — por isso é a única com
    rede de proteção própria."""
    origem = Path(D.CaminhoTabela("IPCAProjetado")) / "dados.parquet"
    if not origem.exists():
        return None

    DIR_BACKUP.mkdir(parents=True, exist_ok=True)
    destino = DIR_BACKUP / f"ipca_projetado_{datetime.now():%Y%m%d_%H%M%S}.parquet"
    shutil.copy2(origem, destino)

    antigos = sorted(DIR_BACKUP.glob("ipca_projetado_*.parquet"))[:-MAX_BACKUPS]
    for velho in antigos:
        velho.unlink()

    log.info("projecao: backup em %s (mantidos os %d mais recentes)", destino, MAX_BACKUPS)
    return destino


def Gravar(registros: list[tuple[str, float]], log) -> int:
    antes = D.Escalar('SELECT COUNT(*) FROM "IPCAProjetado"') or 0
    D.Upsert("IPCAProjetado", pd.DataFrame(
        [{"dtIPCAProjetado": d, "vrProjecaoIPCA": v} for d, v in registros]))
    depois = D.Escalar('SELECT COUNT(*) FROM "IPCAProjetado"') or 0

    log.info("projecao: %d dias gravados (%d novos)", len(registros), depois - antes)
    return depois - antes


def MontarResumo(projecoes: dict[date, float], registros: list[tuple[str, float]],
                 novos: int, dMais1: date, aviso: str | None) -> str:
    linhas = [
        "Resultado:",
        f"Projeções (Data de Validade) : {len(projecoes)}",
        f"Dias expandidos              : {len(registros)}  ({registros[0][0]} .. {registros[-1][0]})",
        f"Dias novos na base           : {novos}",
        f"D+1 (próximo DU Anbima)      : {dMais1}",
        "",
        "Projeções vigentes (últimas 5):",
    ]
    for validade in sorted(projecoes)[-5:]:
        linhas.append(f"  válida a partir de {validade}: {projecoes[validade]}%")
    if aviso:
        linhas += ["", aviso]
    return "\n".join(linhas)


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
        calc = ImportarCalc()  # ProximoDu + feriados Anbima (mesma régua de DU da calc)

        html = ObterHtml(log)
        projecoes = ExtrairProjecoes(html, log)

        dMais1 = calc.ProximoDu(date.today() + timedelta(days=1), calc.FERIADOS_ANBIMA)
        registros = ExpandirDiario(projecoes, ate=dMais1)
        if not registros:
            raise RuntimeError("Nenhum dia expandido — projeções todas no futuro? Fonte suspeita.")

        # Se D+1 já caiu no período projetado de um aniversário cuja projeção ainda
        # não foi publicada, o valor de D+1 é carry-forward (defasado) — avisar.
        aviso = None
        virada = calc.ProximoDu(date(dMais1.year, dMais1.month, 15), calc.FERIADOS_ANBIMA)
        if dMais1 >= virada and max(projecoes) < virada:
            aviso = (f"AVISO: D+1 ({dMais1}) está no período projetado do aniversário {virada}, "
                     f"mas a projeção mais recente vale a partir de {max(projecoes)}. "
                     f"A nova projeção pode não ter saído ainda — D+1 ficou por carry-forward "
                     f"(revalidar após a publicação da Anbima).")
            log.warning("projecao: %s", aviso)

        Backup(log)
        novos = Gravar(registros, log)

        summary = MontarResumo(projecoes, registros, novos, dMais1, aviso)
        log.info("projecao: concluído.\n%s", summary)

    except Exception:
        success = False
        erro = traceback.format_exc()
        rel.Erro("A rodada abortou — ver traceback.")
        log.exception("projecao: erro inesperado")

    finally:
        if summary:
            rel.Secao("Resumo", ["saida"], [[l] for l in summary.splitlines() if l.strip()])
        EnviarEmailConclusao(NOME_SCRIPT, success, rel, tracebackErro=erro, logger=log)

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    Principal()
