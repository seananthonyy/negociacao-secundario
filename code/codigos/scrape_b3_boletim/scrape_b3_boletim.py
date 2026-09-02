"""
scrape_b3_boletim.py
====================
Baixa o Boletim Diario da B3 -- tabela "Negocio a negocio" de credito privado
(DEB/CRI/CRA) -- e faz UPSERT em NegociosBrutos.

Nao e scraping. E um POST
-------------------------
O dado sai de um endpoint publico do BDI, que responde CSV direto:

    POST https://arquivos.b3.com.br/bdi/table/export/csv?lang=pt-BR
    Content-Type: application/json

    {"Name": "Trade", "Date": "2026-07-28", "FinalDate": "2026-07-28",
     "ClientId": "", "Filters": {}}

`Name: "Trade"` e o que seleciona a tabela NEGOCIO A NEGOCIO -- uma linha por operacao,
em vez do agregado por ativo. `Date` e `FinalDate` iguais pedem um pregao.

Ate 02/09/2026 este script subia um Chromium via Playwright para chegar nesse POST:
abria o iframe `arquivos.b3.com.br/bdi/tabelas`, clicava na aba "Renda fixa", setava a
data no `duet-date-picker` por tres estrategias diferentes, selecionava a tabela e
interceptava o download. Eram ~750 linhas de interacao com a pagina.

Nada disso era necessario. O navegador entrou porque foi COMO O ENDPOINT FOI DESCOBERTO
(interceptando o clique no botao CSV, em 30/05/2026) -- e, uma vez que o corpo do POST
ficou conhecido, ninguem voltou para conferir se ele ainda fazia falta. Andaime de
investigacao que virou producao.

Medido em 02/09/2026, com httpx puro, sem cookie e sem header nenhum:

    2026-07-28   HTTP 200   5,4 MB   20.448 negocios DEB/CRI/CRA
    2026-07-23   HTTP 200   3,6 MB   16.541
    2026-06-11   HTTP 200   4,1 MB   19.405

O que se ganha: nao sobe navegador (muito mais rapido), nao quebra quando a B3 mexe no
layout (so se a API mudar), e no banco o httpx le HTTP_PROXY/HTTPS_PROXY do ambiente
sozinho (`trust_env`), como o b3_calc_api e o fianalytics_api ja fazem -- uma dependencia
a menos no ambiente corporativo, sem Chromium para instalar.

O CSV
-----
Delimitador `;`, com linhas de preambulo descritivo antes do header real. O parser NAO
conta linhas fixas: procura a primeira que contenha alguma coluna conhecida, o que
sobrevive a B3 reescrever o texto do preambulo.

CLI:
    python codigos/scrape_b3_boletim/scrape_b3_boletim.py --date 2026-05-27
    python codigos/scrape_b3_boletim/scrape_b3_boletim.py --start 2026-05-25 --end 2026-05-27
    python codigos/scrape_b3_boletim/scrape_b3_boletim.py --date 2026-05-27 --salvar-csv
"""

import argparse
import csv
import io
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

# Garante que code/ esteja no sys.path ao rodar como script
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Helpers"))

import httpx
import pandas as pd

import dados as D
from config import cfg
from logger import ObterLogger
from email_outlook import EnviarEmailConclusao
from relatorio_execucao import RelatorioExecucao

NOME_SCRIPT = "scrape_b3_boletim"

# ---------------------------------------------------------------------------
# O endpoint
# ---------------------------------------------------------------------------

URL_EXPORT = "https://arquivos.b3.com.br/bdi/table/export/csv?lang=pt-BR"

# Corpos candidatos, tentados em ordem. O primeiro e o formato real, confirmado por
# inspecao em 30/05/2026 e re-testado em 02/09/2026. Os outros dois sao variacoes que ja
# funcionaram em algum momento -- ficam como rede se a B3 apertar a validacao do corpo.
def CorposCandidatos(dataStr: str) -> list[dict]:
    return [
        {"Name": "Trade", "Date": dataStr, "FinalDate": dataStr, "ClientId": "", "Filters": {}},
        {"Name": "Trade", "Date": dataStr, "FinalDate": dataStr, "ClientId": ""},
        {"Name": "Trade@true", "Date": dataStr, "FinalDate": dataStr, "ClientId": "", "Filters": {}},
    ]

TIMEOUT_SEGUNDOS = 90       # o CSV de um pregao cheio passa de 5 MB
TAMANHO_MINIMO = 2_000      # abaixo disso nao e boletim: e erro ou pagina vazia

INSTRUMENTOS: list[str] = cfg["scrape"]["b3"]["instrumentosAceitos"]
DIR_CSV = Path(cfg["paths"]["dadosDir"]) / "debug"

# ---------------------------------------------------------------------------
# Mapeamento de colunas CSV -> colunas internas
#
# Nomes reais confirmados no CSV de "Negocio a negocio" da B3 (descobertos em 2026-05-30).
# Os nomes a direita (valores) sao as colunas em NegociosBrutos — NAO altere.
# ---------------------------------------------------------------------------
COLUMN_MAP: dict[str, str] = {
    # Nome exato no CSV (strip aplicado)           →  coluna interna em NegociosBrutos
    "Instrumento financeiro"                       : "cdInstrumento",
    "Emissor"                                      : "cdEmissor",
    "Código IF"                                    : "cdTicker",
    "Quantidade negociada"                         : "vrQuantidade",
    "Preço negócio"                                : "vrPU",
    "Volume financeiro (R$)"                       : "vrVolume",
    "Taxa negócio"                                 : "vrTaxaNegocio",
    "Horário negócio"                              : "dtHorarioNegocio",
    "Data negócio"                                 : "dtNegocio",
    "Cód. identificador do negócio"                : "cdIdentificadorNegocio",
    "Código ISIN"                                  : "cdISIN",
    "Data liquidação"                              : "dtLiquidacao",
    "Situação negócio"                             : "cdSituacao",
    # Coluna ignorada: "Origem negócio" (sempre "Pre-registro - Voice")
}

# Colunas obrigatorias (nao podem estar ausentes no CSV apos mapeamento)
REQUIRED_COLS = {
    "cdIdentificadorNegocio", "cdInstrumento", "cdEmissor", "cdTicker",
    "vrQuantidade", "vrPU", "vrVolume", "dtHorarioNegocio",
    "dtNegocio", "dtLiquidacao", "cdSituacao",
}

COLS_NEGOCIO = ("cdIdentificadorNegocio", "cdInstrumento", "cdEmissor", "cdTicker",
                "vrQuantidade", "vrPU", "vrVolume", "vrTaxaNegocio",
                "dtHorarioNegocio", "dtNegocio", "cdISIN", "dtLiquidacao", "cdSituacao")

# O `ON CONFLICT(cdIdentificadorNegocio) DO UPDATE` daqui reescrevia so tres colunas:
# vrTaxaNegocio, cdSituacao e dtAtualizacao. Um negocio ja gravado nao muda de ticker,
# de PU nem de volume — o que a B3 revisa depois e a taxa e a situacao.
#
# Como Mesclar aplica a politica a toda coluna PRESENTE no DataFrame, o lote e partido em
# dois, como no calc_taxa_negocios: linha nova entra inteira, linha existente leva so as
# tres do DO UPDATE.
COLS_ATUALIZAVEIS = ("vrTaxaNegocio", "cdSituacao", "dtAtualizacao")
POLITICA_UPSERT = {c: D.SOBRESCREVER for c in COLS_ATUALIZAVEIS}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def LerArgumentos() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Baixa o Boletim Diario da B3 (DEB/CRI/CRA) e grava em NegociosBrutos."
    )
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--date",  metavar="YYYY-MM-DD", help="Data unica")
    grp.add_argument("--start", metavar="YYYY-MM-DD", help="Inicio do intervalo")
    p.add_argument("--end",     metavar="YYYY-MM-DD", help="Fim do intervalo (requer --start)")
    p.add_argument("--salvar-csv", dest="salvarCsv", action="store_true",
                   help="Grava o CSV cru em files/debug/ antes de parsear (diagnostico).")
    p.add_argument("--sem-gravar", dest="semGravar", action="store_true",
                   help="Baixa e parseia, mas nao escreve na base. Serve para conferir o "
                        "endpoint num ambiente novo (o banco, atras do proxy).")
    args = p.parse_args()
    if args.start and not args.end:
        p.error("--end e obrigatorio com --start.")
    if args.end and not args.start:
        p.error("--start e obrigatorio com --end.")
    return args


def MontarIntervaloDatas(start: str, end: str) -> list[date]:
    dtInicio, dtFim = date.fromisoformat(start), date.fromisoformat(end)
    if dtFim < dtInicio:
        raise ValueError(f"--end ({end}) anterior a --start ({start})")
    datas, atual = [], dtInicio
    while atual <= dtFim:
        datas.append(atual)
        atual += timedelta(days=1)
    return datas


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def BaixarCsv(cliente: httpx.Client, dataAlvo: date, log) -> str | None:
    """O CSV do pregao, ou None se a B3 nao entregou.

    Uma requisicao por pregao, de proposito. Ja houve um atalho que pedia o intervalo
    inteiro num POST so (`Date` != `FinalDate`), e a API devolvia apenas as duas PONTAS
    do intervalo — sem os pregoes do meio, e com status 200. O fallback nunca disparava e
    os dias sumiam em silencio."""
    dataStr = dataAlvo.isoformat()

    for corpo in CorposCandidatos(dataStr):
        try:
            resp = cliente.post(URL_EXPORT, json=corpo, timeout=TIMEOUT_SEGUNDOS)
        except httpx.HTTPError as exc:
            log.warning("%s: erro de rede no POST (%s) — %s", dataStr, corpo["Name"], exc)
            continue

        if resp.status_code != 200:
            log.warning("%s: HTTP %d com corpo %s", dataStr, resp.status_code, corpo)
            continue

        # A B3 responde 200 com HTML quando nao gosta do corpo. Content-type e o unico
        # jeito de distinguir isso de um CSV legitimo.
        tipo = resp.headers.get("content-type", "").lower()
        if "csv" not in tipo:
            log.warning("%s: resposta nao e CSV (content-type=%s) com corpo %s",
                        dataStr, tipo or "?", corpo)
            continue

        if len(resp.content) < TAMANHO_MINIMO:
            log.warning("%s: CSV de %d bytes — pequeno demais para ser boletim",
                        dataStr, len(resp.content))
            continue

        log.info("%s: %d KB baixados (corpo %s)",
                 dataStr, len(resp.content) // 1024, corpo["Name"])
        return Decodificar(resp.content, log)

    log.error("%s: nenhum corpo de POST foi aceito pela B3.", dataStr)
    return None


def Decodificar(bruto: bytes, log) -> str:
    """O CSV vem com BOM UTF-8, mas ja veio latin-1 em algum momento. Tenta em ordem."""
    for codificacao in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            return bruto.decode(codificacao, errors="strict")
        except UnicodeDecodeError:
            continue
    log.warning("nenhuma codificacao decodificou limpo — caindo para latin-1 tolerante")
    return bruto.decode("latin-1", errors="replace")


def SalvarCsvCru(texto: str, dataAlvo: date, log) -> None:
    DIR_CSV.mkdir(parents=True, exist_ok=True)
    destino = DIR_CSV / f"boletim_{dataAlvo.isoformat()}.csv"
    destino.write_text(texto, encoding="utf-8")
    log.info("CSV cru salvo em %s", destino)


# ---------------------------------------------------------------------------
# Parsing do CSV
# ---------------------------------------------------------------------------

def AnalisarCsv(textoBruto: str, log) -> list[dict]:
    """
    Parseia o CSV do boletim B3, aplica COLUMN_MAP, filtra por cdInstrumento
    e retorna lista de dicts com colunas internas.

    O CSV da B3 tem um preamble de linhas descritivas antes do header real.
    A funcao detecta automaticamente onde o header comeca buscando
    a coluna "Instrumento financeiro" (ou outra coluna do COLUMN_MAP).
    """
    # Detecta delimitador: B3 usa ";" no boletim de negocio-a-negocio
    amostra = textoBruto[:4096]
    delimitador = ";" if amostra.count(";") > amostra.count(",") else ","
    log.debug("CSV delimiter detectado: '%s'", delimitador)

    # Divide em linhas e localiza o header real
    linhas = textoBruto.splitlines()
    idxHeader = None
    for i, linha in enumerate(linhas):
        # O header real contem pelo menos uma coluna conhecida do COLUMN_MAP
        if any(col in linha for col in COLUMN_MAP):
            idxHeader = i
            log.info("Header CSV encontrado na linha %d: %s", i + 1, linha[:120])
            break

    if idxHeader is None:
        log.error("Header CSV nao encontrado. Primeiras 10 linhas:\n%s",
                  "\n".join(linhas[:10]))
        return []

    leitor = csv.DictReader(io.StringIO("\n".join(linhas[idxHeader:])),
                            delimiter=delimitador)

    nomesColunas = leitor.fieldnames or []
    log.info("Colunas no CSV: %s", nomesColunas)
    semMapa = [f for f in nomesColunas if f.strip() not in COLUMN_MAP]
    if semMapa:
        log.warning("Colunas no CSV sem mapeamento (ignoradas): %s", semMapa)

    linhasSaida: list[dict] = []
    linhasPuladas = linhasInstrumentoErrado = linhasSemId = 0

    for i, row in enumerate(leitor, start=2):   # linha 1 = header
        mapped: dict = {}
        for colCsv, colInterna in COLUMN_MAP.items():
            val = row.get(colCsv) or row.get(colCsv.strip())
            if val is not None:
                mapped[colInterna] = val.strip() if isinstance(val, str) else val

        faltando = REQUIRED_COLS - mapped.keys()
        if faltando:
            # Linha pode ser rodape vazio — so loga em DEBUG
            log.debug("Linha %d: colunas obrigatorias ausentes %s — pulando", i, faltando)
            linhasPuladas += 1
            continue

        instrumento = mapped.get("cdInstrumento", "").upper().strip()
        if instrumento not in [x.upper() for x in INSTRUMENTOS]:
            linhasInstrumentoErrado += 1
            continue

        # DESCARTA negocio sem identificador. A B3 manda "-" nesse campo em parte das
        # operacoes (25 no pregao de 28/07/2026), e ele e a CHAVE da base desde que o
        # idTrade AUTOINCREMENT do SQLite foi aposentado. Guardar esses negocios nao e
        # opcao: todos os "-" de um pregao colapsam numa linha so, e — pior — a chave
        # so e unica DENTRO da particao, entao um "-" por pregao se acumula e o JOIN do
        # relatorio (que nao filtra data) passa a contar o mesmo negocio varias vezes.
        # Medido: um negocio de R$ 3,24 MM contado em dobro.
        # Decisao do usuario (31/08/2026): descartar. Perde-se o negocio, mas nao se
        # corrompe o total.
        if not str(mapped.get("cdIdentificadorNegocio", "")).strip(" -"):
            linhasSemId += 1
            continue

        try:
            mapped["vrQuantidade"] = int(
                str(mapped["vrQuantidade"]).replace(".", "").replace(",", ""))
        except (ValueError, KeyError):
            log.warning("Linha %d: vrQuantidade invalido '%s' — pulando",
                        i, mapped.get("vrQuantidade"))
            linhasPuladas += 1
            continue

        # vrPU e vrVolume vem no formato BR: 1.234.567,89
        pularLinha = False
        for col in ("vrPU", "vrVolume"):
            try:
                mapped[col] = float(str(mapped[col]).replace(".", "").replace(",", "."))
            except (ValueError, KeyError):
                log.warning("Linha %d: %s invalido '%s' — pulando", i, col, mapped.get(col))
                linhasPuladas += 1
                pularLinha = True
                break
        if pularLinha:
            continue

        mapped["vrTaxaNegocio"] = AnalisarTaxa(mapped.get("vrTaxaNegocio"))
        mapped["cdISIN"] = mapped.get("cdISIN") or None

        for col in ("dtNegocio", "dtLiquidacao"):
            if col in mapped:
                mapped[col] = NormalizarData(mapped[col])
        if "dtHorarioNegocio" in mapped:
            mapped["dtHorarioNegocio"] = NormalizarHora(mapped["dtHorarioNegocio"])

        linhasSaida.append(mapped)

    log.info("CSV parseado: %d trades DEB/CRI/CRA, %d outros instrumentos ignorados, "
             "%d linhas com erro puladas",
             len(linhasSaida), linhasInstrumentoErrado, linhasPuladas)
    if linhasSemId:
        log.warning(
            "%d negocio(s) DESCARTADOS por virem sem identificador da B3 ('-'). Esse "
            "campo e a chave da base; sem ele o negocio nao tem como ser guardado sem "
            "colidir com os outros do mesmo pregao. Ver [[98 - Backlog]].", linhasSemId)
    return linhasSaida


def AnalisarTaxa(bruto) -> float | None:
    """Taxa e opcional: a B3 manda vazio ou '-' em boa parte dos negocios."""
    texto = str(bruto or "").strip()
    if texto in ("", "-", "N/A", "n/a", "0"):
        return None
    try:
        return float(texto.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def NormalizarData(val: str) -> str:
    """Converte DD/MM/YYYY ou YYYY-MM-DD para YYYY-MM-DD."""
    val = val.strip()
    if len(val) == 10 and val[2] == "/":
        try:
            d, m, y = val.split("/")
            return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
        except ValueError:
            pass
    return val   # assume que ja esta em YYYY-MM-DD


def NormalizarHora(val: str) -> str:
    """Garante HH:MM:SS."""
    val = val.strip()
    return val + ":00" if (len(val) == 5 and val[2] == ":") else val


# ---------------------------------------------------------------------------
# Gravacao
# ---------------------------------------------------------------------------

def UpsertLinhas(rows: list[dict], log) -> tuple[int, int]:
    """Faz UPSERT de todos os rows em NegociosBrutos. Retorna (inseridos, atualizados)."""
    if not rows:
        return 0, 0

    agora = datetime.now().isoformat(sep=" ", timespec="seconds")
    inseridos = atualizados = 0

    # NegociosBrutos e particionada por dtNegocio, e cada Mesclar toca UM dia. Na pratica
    # o CSV de um pregao so traz aquele dtNegocio, mas agrupar aqui torna isso explicito
    # em vez de suposto.
    porDia: dict[str, list[dict]] = {}
    for r in rows:
        porDia.setdefault(r["dtNegocio"], []).append(r)

    for dtNegocio, doDia in porDia.items():
        marcas = ",".join("?" * len(doDia))
        idsExistentes = {r[0] for r in D.Tuplas(
            f"SELECT cdIdentificadorNegocio FROM NegociosBrutos "
            f"WHERE dtNegocio = ? AND cdIdentificadorNegocio IN ({marcas})",
            [dtNegocio] + [r["cdIdentificadorNegocio"] for r in doDia])}

        novos = [{**{c: r.get(c) for c in COLS_NEGOCIO},
                  "dtCriacao": agora, "dtAtualizacao": agora}
                 for r in doDia if r["cdIdentificadorNegocio"] not in idsExistentes]
        revisados = [{"cdIdentificadorNegocio": r["cdIdentificadorNegocio"],
                      "dtNegocio": r["dtNegocio"],
                      "vrTaxaNegocio": r.get("vrTaxaNegocio"),
                      "cdSituacao": r["cdSituacao"], "dtAtualizacao": agora}
                     for r in doDia if r["cdIdentificadorNegocio"] in idsExistentes]

        if novos:
            D.Mesclar("NegociosBrutos", pd.DataFrame(novos),
                      padrao=D.SOBRESCREVER, data=dtNegocio)
        if revisados:
            D.Mesclar("NegociosBrutos", pd.DataFrame(revisados),
                      politica=POLITICA_UPSERT, data=dtNegocio)

        # Conta IDENTIFICADORES, nao linhas do CSV: se a B3 repetir um numero, as duas
        # linhas viram uma so e o contador tem de dizer 1.
        inseridos   += len({r["cdIdentificadorNegocio"] for r in novos})
        atualizados += len({r["cdIdentificadorNegocio"] for r in revisados})

    log.info("UPSERT: %d inseridos, %d atualizados", inseridos, atualizados)
    return inseridos, atualizados


def SoftCancelAusentes(dataStr: str, idsBaixados: set, log) -> int:
    """Negocio que estava na base para dataStr e NAO veio no download de agora vira
    `cdSituacao = 'Cancelado'` (soft delete, preserva o rastro) e sai de
    NegociosProcessados (hard delete).

    Nao e hipotetico: a B3 revisa o boletim depois do pregao. Re-raspar 28/07/2026 um mes
    depois trouxe 926 negocios que eram 'Confirmado' e viraram 'Cancelado B3'."""
    aCancelar = [r[0] for r in D.Tuplas(
        "SELECT cdIdentificadorNegocio FROM NegociosBrutos "
        f"WHERE dtNegocio = ? AND {D.NaoCancelado()}", (dataStr,))
        if r[0] not in idsBaixados]

    if not aCancelar:
        return 0

    agora = datetime.now().isoformat(sep=" ", timespec="seconds")
    D.Apagar("NegociosProcessados", "cdIdentificadorNegocio", aCancelar)
    D.Mesclar("NegociosBrutos", pd.DataFrame(
        [{"cdIdentificadorNegocio": i, "dtNegocio": dataStr,
          "cdSituacao": "Cancelado", "dtAtualizacao": agora} for i in aCancelar]),
        politica={"cdSituacao": D.SOBRESCREVER, "dtAtualizacao": D.SOBRESCREVER},
        data=dataStr)

    log.warning("Soft-cancel %s: %d trade(s) marcados Cancelado e removidos de "
                "NegociosProcessados — reprocessar calc_taxa → filtrar_trades para essa "
                "data. IDs: %s", dataStr, len(aCancelar), aCancelar)
    return len(aCancelar)


# ---------------------------------------------------------------------------
# Processamento por pregao
# ---------------------------------------------------------------------------

def ProcessarData(cliente: httpx.Client, dataAlvo: date, log,
                  salvarCsv: bool, semGravar: bool) -> tuple[int, int, int]:
    """(inseridos, atualizados, cancelados) de um pregao."""
    dataStr = dataAlvo.isoformat()

    texto = BaixarCsv(cliente, dataAlvo, log)
    if texto is None:
        raise RuntimeError(f"B3 nao entregou o CSV de {dataStr}")
    if salvarCsv:
        SalvarCsvCru(texto, dataAlvo, log)

    linhas = AnalisarCsv(texto, log)
    if not linhas:
        log.warning("%s: CSV veio, mas sem nenhum negocio DEB/CRI/CRA.", dataStr)
        return 0, 0, 0

    if semGravar:
        log.info("%s: --sem-gravar — %d negocio(s) parseados, nada foi escrito.",
                 dataStr, len(linhas))
        return 0, 0, 0

    inseridos, atualizados = UpsertLinhas(linhas, log)
    cancelados = SoftCancelAusentes(
        dataStr, {r["cdIdentificadorNegocio"] for r in linhas}, log)
    return inseridos, atualizados, cancelados


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def Principal() -> RelatorioExecucao:
    log  = ObterLogger(NOME_SCRIPT)
    args = LerArgumentos()
    rel  = RelatorioExecucao(NOME_SCRIPT, args=vars(args))

    datas = ([date.fromisoformat(args.date)] if args.date
             else MontarIntervaloDatas(args.start, args.end))
    log.info("%s: %d pregao(oes): %s ... %s", NOME_SCRIPT, len(datas),
             datas[0], datas[-1])

    totalInseridos = totalAtualizados = totalCancelados = 0
    resultados: list[list] = []

    # Um Client so para todos os pregoes: mantem a conexao viva (keep-alive), o que
    # importa atras do proxy do banco, onde cada CONNECT novo e caro. `trust_env` faz o
    # httpx ler HTTP_PROXY/HTTPS_PROXY do ambiente sozinho.
    with httpx.Client(trust_env=True, verify=False, follow_redirects=True,
                      headers={"Content-Type": "application/json"}) as cliente:
        for dataAlvo in datas:
            dataStr = dataAlvo.isoformat()
            try:
                ins, atu, cnl = ProcessarData(cliente, dataAlvo, log,
                                              args.salvarCsv, args.semGravar)
                totalInseridos   += ins
                totalAtualizados += atu
                totalCancelados  += cnl
                resultados.append([dataStr, ins, atu, cnl])
                log.info("%s: %d inseridos, %d atualizados, %d cancelados",
                         dataStr, ins, atu, cnl)
            except Exception as exc:
                # Um pregao que falha nao derruba os outros — o resumo mostra qual foi.
                log.error("%s: ERRO — %s", dataStr, exc)
                log.debug(traceback.format_exc())
                resultados.append([dataStr, "ERRO", str(exc)[:60], ""])

    rel.Datas([r[0] for r in resultados])
    rel.Contar("inseridos", totalInseridos)
    rel.Contar("atualizados", totalAtualizados)
    rel.Contar("cancelados", totalCancelados)
    rel.Secao("Resultado por pregão", ["data", "inseridos", "atualizados", "cancelados"],
              resultados)

    # Pregão que não trouxe negócio nenhum quase sempre é falha silenciosa (a B3 sempre
    # tem negócio em dia útil) — foi assim que o bug do proxy no Playwright passou batido.
    semDado = [r[0] for r in resultados if r[1] == 0 and r[2] == 0]
    if semDado and not args.semGravar:
        rel.Aviso(f"{len(semDado)} pregão(ões) sem nenhum negócio: {', '.join(semDado[:6])}. "
                  f"Em dia útil isso quase sempre é falha de download, não ausência de dado.")

    log.info(rel.Texto())
    return rel


if __name__ == "__main__":
    log = ObterLogger(NOME_SCRIPT)
    rel, ok, tb = RelatorioExecucao(NOME_SCRIPT), True, None
    try:
        rel = Principal()
    except Exception:
        ok = False
        tb = traceback.format_exc()
        rel.Erro("A rodada abortou — ver traceback.")
        print(tb, file=sys.stderr)
        raise
    finally:
        EnviarEmailConclusao(NOME_SCRIPT, ok, rel, tb, logger=log)
