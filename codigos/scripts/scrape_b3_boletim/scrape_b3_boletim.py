"""
scrape_b3_boletim.py
====================
Baixa o Boletim Diario da B3 -- tabela "Negocio a negocio" de credito privado
(DEB/CRI/CRA) -- e grava em NegociosBrutos.

Nao e scraping: e um POST
-------------------------
    POST https://arquivos.b3.com.br/bdi/table/export/csv?lang=pt-BR
    Content-Type: application/json

    {"Name": "Trade", "Date": "2026-07-28", "FinalDate": "2026-07-28",
     "ClientId": "", "Filters": {}}

Endpoint publico: nao pede login, cookie, token nem header especial. Responde o
CSV direto.

  Name        "Trade" seleciona a tabela NEGOCIO A NEGOCIO -- uma linha por
              operacao, em vez do agregado por ativo.
  Date        pregao pedido. FinalDate igual = um dia so (ver BaixarCsv).
  ClientId    sempre vazio.
  Filters     sempre vazio; o filtro de DEB/CRI/CRA e nosso, no parse.

O CSV vem com delimitador `;`, BOM UTF-8 e algumas linhas de preambulo
descritivo antes do header real.

CLI:
    python codigos/scrape_b3_boletim/scrape_b3_boletim.py --date 2026-05-27
    python codigos/scrape_b3_boletim/scrape_b3_boletim.py --start 2026-05-25 --end 2026-05-27
    python codigos/scrape_b3_boletim/scrape_b3_boletim.py --date 2026-05-27 --sem-gravar
"""

import argparse
import csv
import io
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

# Garante que code/ esteja no sys.path ao rodar como script
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers"))

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
TIMEOUT_SEGUNDOS = 90       # o CSV de um pregao cheio passa de 5 MB
TAMANHO_MINIMO = 2_000      # abaixo disso nao e boletim: e erro ou pagina vazia
DELIMITADOR = ";"           # o boletim negocio-a-negocio da B3 sempre usa ponto-e-virgula

INSTRUMENTOS: list[str] = cfg["scrape"]["b3"]["instrumentosAceitos"]
DIR_CSV = Path(cfg["paths"]["cacheDir"]) / "scrape_b3_boletim"

# ---------------------------------------------------------------------------
# Mapeamento de colunas CSV -> colunas internas
#
# Nomes reais confirmados no CSV de "Negocio a negocio" da B3 (2026-05-30).
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

# Colunas obrigatorias: linha sem alguma delas e rodape ou lixo, e e pulada.
REQUIRED_COLS = {
    "cdIdentificadorNegocio", "cdInstrumento", "cdEmissor", "cdTicker",
    "vrQuantidade", "vrPU", "vrVolume", "dtHorarioNegocio",
    "dtNegocio", "dtLiquidacao", "cdSituacao",
}

COLS_NEGOCIO = ("cdIdentificadorNegocio", "cdInstrumento", "cdEmissor", "cdTicker",
                "vrQuantidade", "vrPU", "vrVolume", "vrTaxaNegocio",
                "dtHorarioNegocio", "dtNegocio", "cdISIN", "dtLiquidacao", "cdSituacao")

# Negocio ja gravado nao muda de ticker, de PU nem de volume: o que a B3 revisa
# depois do pregao e a taxa e a situacao. Sao as unicas colunas reescritas.
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
                   help="Grava o CSV cru em cache/scrape_b3_boletim/ antes de parsear (diagnostico).")
    p.add_argument("--sem-gravar", dest="semGravar", action="store_true",
                   help="Baixa e parseia, mas nao escreve na base. Serve para validar o "
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

def BaixarCsv(cliente: httpx.Client, dataAlvo: date, log) -> str:
    """O CSV do pregao. Levanta se a B3 nao entregou.

    Uma requisicao por pregao, de proposito. Ja houve um atalho que pedia o intervalo
    inteiro num POST so (`Date` != `FinalDate`), e a API devolvia apenas as duas PONTAS
    do intervalo — sem os pregoes do meio, e com status 200. Os dias sumiam em silencio.
    """
    dataStr = dataAlvo.isoformat()
    corpo = {"Name": "Trade", "Date": dataStr, "FinalDate": dataStr,
             "ClientId": "", "Filters": {}}

    resp = cliente.post(URL_EXPORT, json=corpo, timeout=TIMEOUT_SEGUNDOS)
    resp.raise_for_status()

    # A B3 responde 200 com HTML quando nao gosta do corpo — o content-type e o unico
    # jeito de distinguir isso de um CSV legitimo.
    tipo = resp.headers.get("content-type", "").lower()
    if "csv" not in tipo:
        raise RuntimeError(f"{dataStr}: resposta nao e CSV (content-type={tipo or '?'})")
    if len(resp.content) < TAMANHO_MINIMO:
        raise RuntimeError(f"{dataStr}: CSV de {len(resp.content)} bytes — "
                           "pequeno demais para ser boletim")

    log.info("%s: %d KB baixados", dataStr, len(resp.content) // 1024)
    # O CSV vem com BOM UTF-8; `errors="replace"` evita derrubar o pregao inteiro por
    # causa de um acento estranho num nome de emissor.
    return resp.content.decode("utf-8-sig", errors="replace")


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
    # Localiza o header real: a primeira linha que contenha uma coluna conhecida.
    # Procurar pelo NOME, em vez de contar linhas de preambulo, sobrevive a B3
    # reescrever o texto descritivo do topo.
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
                            delimiter=DELIMITADOR)

    nomesColunas = leitor.fieldnames or []
    log.info("Colunas no CSV: %s", nomesColunas)
    semMapa = [f for f in nomesColunas if f.strip() not in COLUMN_MAP]
    if semMapa:
        log.warning("Colunas no CSV sem mapeamento (ignoradas): %s", semMapa)

    aceitos = {x.upper() for x in INSTRUMENTOS}
    linhasSaida: list[dict] = []
    linhasPuladas = linhasInstrumentoErrado = linhasSemId = 0

    for i, row in enumerate(leitor, start=2):   # linha 1 = header
        mapped: dict = {}
        for colCsv, colInterna in COLUMN_MAP.items():
            val = row.get(colCsv)
            if val is not None:
                mapped[colInterna] = val.strip() if isinstance(val, str) else val

        faltando = REQUIRED_COLS - mapped.keys()
        if faltando:
            # Linha pode ser rodape vazio — so loga em DEBUG
            log.debug("Linha %d: colunas obrigatorias ausentes %s — pulando", i, faltando)
            linhasPuladas += 1
            continue

        if mapped.get("cdInstrumento", "").upper().strip() not in aceitos:
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
