"""
validar_calc_b3.py
==================
Gate de CONFIANÇA da precificação local: um ativo é `stFluxoValidado = 1` se a **nossa
calc reproduz uma calculadora de mercado** — a **B3** (fonte primária do cadastro) OU,
se a B3 não confirmar, a **FI Analytics** — em PU/taxa e em VÁRIAS datas. Basta UM
oráculo reproduzir a nossa calc (decisão do usuário 15/07: "se bater com a FI, também
é válido"). O gate é BIDIRECIONAL: promove quem passa e rebaixa quem falha.

UNICO VALIDADOR (24/08/2026). O antigo `validar_fluxos` foi removido: ele comparava a
agenda evento a evento contra a FI, teste que o PU ja cobre (PU no par valida
fluxo+VNA; fora do par valida o desconto), e marcava o fluxo da B3 como "nasce
validado" sem conferir se a calc precifica certo — ativos ficavam validados com a calc
errando o PU 1-70%. Aqui, validado significa exatamente "a calc bate a B3 ou a FI".

Testado em 3 datas: [8 pregões atrás, mais recente com curva, D+1]. O D+1 é uma data
futura, sem curva própria — precificada por CARRY-FORWARD (a curva mais recente projeta
o D+1, que é o que a B3/FI também fazem); ver SemearCurvaCarryForward.

O que cada oráculo testa:
  - B3 (primário): PU no par (taxa=emissão) valida FLUXO+VNA; PU a +100 bps valida o
    DESCONTO — CalcularPuGov. Régua: erro RELATIVO de PU ≤ 1e-5 (= 0,001% = R$0,01 num
    PU de R$1.000) em toda data confirmada. Como é PU (não taxa), %CDI usa a mesma régua.
  - FI (secundário, só se a B3 não bater): round-trip de TAXA. A FI não devolve um PU
    limpo dado a taxa (o m2m é mark-a-mercado noutra base), mas devolve a taxa dado um
    PU (ChamarPrimaria). Então: pego o PU que a NOSSA calc gera na taxa T (par e
    T+100bps) e pergunto à FI a taxa — tem que voltar T. Régua: ≤ 5 bps. É o que
    RESGATA os ativos que a B3 não cobre.

Nenhum oráculo respondeu (B3 e FI vazias) ⇒ não-confirmável ⇒ INVÁLIDO (stFluxoValidado=0):
não dá para dar fé, cai na cascata de API no calc_taxa (decisão do usuário 19/07).

Revalidação: um ativo já validado é PULADO enquanto a dtValidacaoFluxo tiver menos de
`--revalidar-dias` (default 15) — só revalida quando vence. Fluxo que muda de verdade zera
stFluxoValidado pelo trigger e é re-testado na hora, sem esperar.

CLI:
    python codigos/scripts/validar_calc_b3.py                 # audita e (des)valida; revalida vencidos
    python codigos/scripts/validar_calc_b3.py --dry-run       # só reporta, não grava
    python codigos/scripts/validar_calc_b3.py --tickers A,B   # testa esses, ignora a janela de revalidação
    python codigos/scripts/validar_calc_b3.py --datas 2026-07-10,2026-07-08
    python codigos/scripts/validar_calc_b3.py --revalidar-dias 0   # re-testa tudo, ignora a data
    python codigos/scripts/validar_calc_b3.py --limite 100    # smoke
"""
import argparse
import csv
import sys
import traceback
import concurrent.futures as cf
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers"))

from calc import CarregarAtivo, CalcularPu, CalcularTaxa, ImportarCalc
from b3_calc_api import CalcularPuGov, CalcularYield, ObterDetalhesAtivo
from fianalytics_api import ChamarCompleto, ChamarPrimaria
from cadastro_b3 import GravarLote, PrepararAtivo
from config import cfg
import pandas as pd

import dados as D
from email_outlook import EnviarEmailConclusao
from logger import ObterLogger
from relatorio_execucao import RelatorioExecucao

NOME_SCRIPT = "validar_calc_b3"


def RefrescarCadastroB3(cdTicker: str) -> dict | None:
    """Re-busca o getBondDetails e regrava o pacote (VNE + início + FLUXO) pela B3.
    Cura a deriva do fluxo (o scrape de rotina só re-scrapeia info faltando, então
    fluxo já existente envelhece). Devolve o ativo recarregado, ou None.

    É o único ponto do projeto que grava um ativo por vez, e de propósito: o resultado
    da gravação é reavaliado na hora, então não há lote a formar. Custa uma reescrita de
    InfoAtivos e FluxoAtivos por ativo — aceitável porque só os REPROVADOS chegam aqui,
    e `--sem-refresh` desliga o passo."""
    det = ObterDetalhesAtivo(cdTicker)
    if not det:
        return None
    try:
        antes = D.Linha("SELECT cdTicker, stTemFluxo, cdFonteCadastro "
                        "FROM InfoAtivos WHERE cdTicker = ?", (cdTicker,))
        escalares, pacote, linhasFluxo, _ = PrepararAtivo(
            cdTicker, det, date.today().isoformat(), antes)
        GravarLote([escalares], [pacote] if pacote else [],
                   {cdTicker: linhasFluxo} if pacote else {})
    except Exception:
        return None
    return CarregarAtivo(cdTicker)

# Régua de PU: erro RELATIVO |puNosso/puB3 - 1|, então independe da escala do PU (papel
# de emissão 1, 1.000 ou 10.000 usa a mesma régua). 1e-5 = 0,001% = R$0,01 num PU de
# R$1.000 (decisão do usuário). É apertado: barra ~160 CDI+ cujo erro é ruído de
# interpolação da curva DI esparsa (não erro da calc) — esses caem na cascata FI/B3.
TOL_PU = 1e-5
TOL_TAXA_BPS = 5.0     # round-trip; folga p/ o ruído numérico do %CDI (2-4 bps, PU perfeito)
TOL_FI_BPS = 5.0       # round-trip de taxa contra a FI (2o oráculo, papel que a B3 não cobre)
DELTA = 1.0            # +100 bps para o teste fora do par

# No %CDI a "taxa" é um MULTIPLICADOR do CDI (p.ex. 98,5 = 98,5% do CDI), não uma taxa
# a.a. — 1 ponto de %CDI ≈ 10 bps de yield. Converter a diferença de taxa com o mesmo
# fator do filtrar_trades (corretorMaxBps 2,5 ↔ corretorMaxPctCdi 0,25; pfMinBps 20 ↔
# pfMinPctCdi 2,0 → razão 10). Sem isso, `diff*100` (p.p.→bps) inflaria o erro do %CDI
# ~10× e reprovaria por engano quem está dentro da tolerância.
BPS_POR_PONTO_PCTCDI = 10.0


def DiffTaxaEmBps(cdIndexador: str | None, delta: float) -> float:
    """Converte uma diferença de taxa (na unidade nativa do indexador) para bps.
    %CDI: pontos de %CDI × 10. Demais: pontos percentuais × 100."""
    if cdIndexador == "%CDI":
        return delta * BPS_POR_PONTO_PCTCDI
    return delta * 100.0


WORKERS = 10
CSV_SAIDA = Path(cfg["paths"]["cacheDir"]) / "validar_calc_b3" / "validar_calc_b3.csv"

C = ImportarCalc()


def UltimoDuUtil() -> date:
    d = date.today()
    while not C.EhDu(d, C.FERIADOS_ANBIMA):
        d -= timedelta(days=1)
    return d


def ProximoDu(d: date) -> date:
    d += timedelta(days=1)
    while not C.EhDu(d, C.FERIADOS_ANBIMA):
        d += timedelta(days=1)
    return d


# Motivos de falha da nossa calc, contados por mensagem. Sem isto, "a calc levantou"
# e "a calc divergiu" chegam ao CSV com o mesmo 9.9 e ninguem sabe qual foi.
falhasCalc: dict[str, int] = {}


def ContarFalhaCalc(motivo: str) -> None:
    falhasCalc[motivo] = falhasCalc.get(motivo, 0) + 1


def SemearCurvaCarryForward(dHoje: str, dMais1: str) -> bool:
    """Deixa a calc precificar o D+1 (data futura, sem curva própria) projetando com a
    curva mais recente — é o que a B3/FI também fazem. A calc lê a curva de projeção por
    `MERCADO.accProj(dataCalc)` (chave = data exata); então semeamos o cache dela com a
    curva de `dHoje` sob a chave de `dMais1` via `MERCADO.SemearAccProj`. Em memória, sem
    escrever na tabela CurvaDi, sem tocar na calculadora_rf.py. Devolve True se semeou.

    **Duas condições, não uma.** A curva projetada sozinha não basta: para chegar a
    `dMais1` a calc capitaliza o DI **realizado** dia a dia até `dHoje`, e essa série vem
    da `DiHistorico` (BCB), não da `CurvaDi` (B3). As duas fontes andam em ritmos
    diferentes — o BCB publica com um dia de atraso, então é NORMAL a `CurvaDi` estar um
    pregão à frente. Quando está, `dMais1` é impossível e o passo é pulado.

    Foi exatamente esse buraco que, em 03/09/2026, reprovou 1.494 papéis CDI+ de uma vez:
    a curva de 02/09 existia, o DI realizado de 02/09 não, e a exceção da calc virava
    divergência em vez de ausência."""
    vertices = [(r["du"], r["vrTaxa"]) for r in D.Linhas(
        'SELECT du, vrTaxa FROM "CurvaDi" WHERE dtReferencia = ? ORDER BY du', (dHoje,))]
    if not vertices:
        return False
    if not D.Escalar('SELECT COUNT(*) FROM "DiHistorico" WHERE dtReferencia = ?', (dHoje,)):
        return False
    C.MERCADO.SemearAccProj(dMais1, vertices)
    return True


def DatasPadrao() -> dict[str, str | None]:
    """As tres datas do gate, cada uma com um papel proprio:

        passada  ~8 pregoes atras  — curva REALIZADA
        hoje     mais recente com curva na base
        futura   o DU seguinte a "hoje" — curva PROJETADA (carry-forward)

    So datas com curva DI na base: sem curva, todo papel indexado a CDI levanta na
    calc e seria reprovado por um buraco NOSSO. `futura` volta None quando nao da
    para semear — e ai o teste dela e PULADO, nao reprovado."""
    datas = [r["dtReferencia"] for r in D.Linhas(
        'SELECT DISTINCT dtReferencia FROM "CurvaDi" ORDER BY dtReferencia DESC LIMIT 10')]
    if not datas:
        hoje = UltimoDuUtil().isoformat()
        return {"passada": hoje, "hoje": hoje, "futura": None}

    dHoje = datas[0]
    dPassada = datas[min(8, len(datas) - 1)]
    dFutura = ProximoDu(date.fromisoformat(dHoje)).isoformat()
    if not SemearCurvaCarryForward(dHoje, dFutura):
        dFutura = None
    return {"passada": dPassada, "hoje": dHoje, "futura": dFutura}


def PuDoOraculo(tk: str, instr: str | None, dt: str, taxa: float) -> tuple:
    """PU do papel na `taxa`, naquela data, pedido a um oraculo externo.

    Tenta a B3 e, se ela nao responder, a FI Analytics. Devolve (pu, fonte) ou
    (None, None) quando NENHUM dos dois respondeu -- o que e diferente de responder
    um numero divergente: oraculo mudo nao reprova ninguem.

    A B3 vem primeiro porque e a fonte primaria do cadastro e a regua do proprio
    gate; usar a FI antes daria um PU de uma fonte com validacao de outra.

    Ate 03/09/2026 a FI respondia OUTRA pergunta aqui (round-trip taxa->PU), o que
    obrigava o gate a ter dois caminhos de avaliacao. Ela tambem faz PU dado a taxa
    (`ChamarCompleto` em modo `rate`, campo `m2m`), entao passou a responder a mesma
    coisa que a B3 e o gate ficou com um caminho so."""
    try:
        pu, _ = CalcularPuGov(tk, dt, taxa)
        if pu:
            return pu, "B3"
    except Exception:
        pass
    try:
        resp = ChamarCompleto(tk, dt, taxa)
    except Exception:
        resp = None
    if resp:
        try:
            pu = float(resp.get("m2m"))
        except (TypeError, ValueError):
            pu = None
        if pu and pu > 0:
            return pu, "FiAnalytics"
    return None, None


def TaxaDoOraculo(tk: str, instr: str | None, dt: str, vrPU: float) -> tuple:
    """A taxa que um oraculo enxerga naquele PU. Mesma cascata: B3, depois FI."""
    try:
        taxa = CalcularYield(tk, dt, vrPU)
        if taxa is not None:
            return taxa, "B3"
    except Exception:
        pass
    for cand in ([instr] if instr else ["DEB", "CRA"]):
        try:
            taxa = ChamarPrimaria(tk, cand, dt, vrPU)
        except Exception:
            taxa = None
        if taxa is not None:
            return taxa, "FiAnalytics"
    return None, None


def AvaliarAtivo(ativo: dict, datas: dict, comTaxa: bool = True,
                 comFi: bool = True) -> dict:
    """Roda os quatro testes do gate. Devolve o pior erro de cada natureza e a fonte
    que confirmou.

    O ativo e VALIDO quando todos os testes que puderam rodar passaram, e pelo menos
    um oraculo respondeu.

        passo 2  PU na taxa de emissao, data PASSADA  -> fluxo e VNA, contra curva
                                                          realizada
        passo 3  PU a +100 bps, HOJE                  -> o desconto, contra a curva
                                                          do dia
        passo 4  PU a +100 bps, D+1                   -> o desconto, contra a curva
                                                          projetada (carry-forward)
        passo 5  taxa implicita no PU do passo 3      -> o numero que vai para o
                                                          relatorio, em BPS

    Por que o passo 5 existe, se o 3 ja compara a mesma curva: a regua de PU
    (`1e-5` = R$ 0,01 por R$ 1.000) vale bps DIFERENTES conforme a duration -- e
    rigida demais em papel curto e frouxa em papel longo, medida no que a mesa le.
    O passo 5 mede na grandeza publicada, e ai `5 bps` significa a mesma coisa para
    todo indexador (o DiffTaxaEmBps normaliza o %CDI).

    Erro so e FALHA quando o oraculo RESPONDEU e divergiu. Oraculo mudo (HTTP 500,
    timeout, papel que ele nao cobre) e NAO-CONFIRMA: pula, nao penaliza. E o passo
    4 tambem e pulado quando nao houve curva para semear o D+1 -- a falta e da nossa
    base, nao do ativo."""
    tk = ativo["cdTicker"]
    taxa = ativo["vrTaxaEmissao"]
    instr = ativo.get("cdInstrumento")

    piorPU = piorTaxa = 0.0
    taxaMedida = False          # separa "bateu exato" de "nao rodou" no relatorio
    nConf = 0
    fontes: set[str] = set()
    detalhe = []

    def ConferirPu(rotulo: str, dt: str | None, taxaAlvo: float):
        """(erroRelativo, puDoOraculo). (None, None) se ninguem respondeu."""
        nonlocal piorPU, nConf
        if dt is None:
            detalhe.append((rotulo, None, "sem data"))
            return None, None
        puRef, fonte = PuDoOraculo(tk, instr, dt, taxaAlvo)
        if puRef is None:
            detalhe.append((rotulo, None, "oraculo mudo"))
            return None, None
        nConf += 1
        fontes.add(fonte)
        try:
            nosso = CalcularPu(ativo, date.fromisoformat(dt), taxaAlvo)
            # A calc falhar onde o oraculo respondeu e falha real, nao ausencia.
            erro = abs(nosso / puRef - 1) if nosso else 9.9
        except Exception as e:
            # O motivo NAO pode sumir. Engolir a excecao aqui transforma "falta dado na
            # nossa base" em "o ativo diverge" — indistinguiveis no CSV, os dois viram
            # 9.9. Um log por mensagem distinta basta para separar os dois em segundos.
            ContarFalhaCalc(f"{type(e).__name__}: {e}")
            erro = 9.9
        piorPU = max(piorPU, erro)
        detalhe.append((rotulo, erro, fonte))
        return erro, puRef

    ConferirPu("par/passada", datas["passada"], taxa)
    _, puForaHoje = ConferirPu("fora/hoje", datas["hoje"], taxa + DELTA)
    ConferirPu("fora/futura", datas["futura"], taxa + DELTA)

    # Passo 5 — a taxa, em bps, sobre o MESMO PU fora do par que o passo 3 buscou.
    # Reusar em vez de pedir de novo economiza uma chamada por ativo, e elas sao
    # contadas pela B3.
    if comTaxa and puForaHoje:
        taxaRef, fonteTaxa = TaxaDoOraculo(tk, instr, datas["hoje"], puForaHoje)
        if taxaRef is not None:
            fontes.add(fonteTaxa)
            try:
                nossa = CalcularTaxa(ativo, date.fromisoformat(datas["hoje"]), puForaHoje)
            except Exception:
                nossa = None
            # A nossa calc falhar num PU que o oraculo precificou e FALHA, nao
            # ausencia de medida — sem isto o ativo passaria como se tivesse
            # reproduzido a taxa exatamente.
            piorTaxa = (DiffTaxaEmBps(ativo["cdIndexador"], abs(nossa - taxaRef))
                        if nossa is not None else 9_999.0)
            taxaMedida = True
            detalhe.append(("taxa/hoje", piorTaxa, fonteTaxa))

    passou = nConf > 0 and piorPU <= TOL_PU and piorTaxa <= TOL_TAXA_BPS
    fonte = ("B3" if "B3" in fontes else "FiAnalytics") if passou else None

    return {"tk": tk, "idx": ativo["cdIndexador"], "piorPU": piorPU,
            "piorTaxa": piorTaxa, "taxaMedida": taxaMedida, "piorFi": 0.0,
            "nConfB3": nConf, "nConfFi": 0, "fonte": fonte, "detalhe": detalhe}


def LerArgumentos() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Gate de confiança: desvalida ativo cuja calc não reproduz a B3.")
    p.add_argument("--dry-run", dest="dryRun", action="store_true", help="só reporta, não grava")
    p.add_argument("--tickers", default=None, help="lista separada por vírgula")
    p.add_argument("--datas", default=None, help="datas YYYY-MM-DD separadas por vírgula")
    p.add_argument("--sem-taxa", dest="semTaxa", action="store_true",
                   help="pula o teste de TAXA (passo 5) e valida so por PU. O teste de "
                        "taxa e o unico que mede na grandeza que o relatorio publica — "
                        "desligue apenas para uma rodada rapida de diagnostico.")
    p.add_argument("--sem-fi", dest="semFi", action="store_true",
                   help="não usa a FI como 2o oráculo (só B3); volta ao gate B3-only")
    p.add_argument("--sem-refresh", dest="semRefresh", action="store_true",
                   help="não tenta refrescar o cadastro pela B3 antes de desvalidar")
    p.add_argument("--negociados-dias", dest="negociadosDias", type=int, default=None,
                   help="testa só validados que negociaram nos últimos N dias (rotina)")
    p.add_argument("--revalidar-dias", dest="revalidarDias", type=int, default=15,
                   help="revalida um ativo já validado só se a dtValidacaoFluxo tiver "
                        "mais de N dias (default 15). Validado há menos que isso é pulado. "
                        "0 = re-testa tudo, ignora a data. Não se aplica a --tickers.")
    p.add_argument("--limite", type=int, default=None)
    return p.parse_args()


def Principal() -> None:
    log = ObterLogger(NOME_SCRIPT)
    args = LerArgumentos()
    rel = RelatorioExecucao(NOME_SCRIPT, args=vars(args))
    success = True

    try:
        if args.datas:
            # Ordem posicional: passada, hoje, futura. Menos que tres, o resto e None.
            partes = (args.datas.split(",") + [None, None, None])[:3]
            datas = dict(zip(("passada", "hoje", "futura"), partes))
        else:
            datas = DatasPadrao()
        rel.Datas([d for d in datas.values() if d])
        log.info("%s: datas — passada=%s hoje=%s futura=%s", NOME_SCRIPT,
                 datas["passada"], datas["hoje"], datas["futura"] or "(sem curva p/ semear)")
        # Gate BIDIRECIONAL: candidatos NÃO se restringem a stFluxoValidado=1 — quem
        # passa é promovido, quem falha é rebaixado. Mas quem já está validado há menos
        # de `--revalidar-dias` é PULADO (confia até vencer). Fluxo que muda de verdade
        # zera stFluxoValidado pelo trigger → cai em não-validado e é re-testado na hora.
        # `--tickers` ignora essa janela (o usuário pediu aqueles explicitamente).
        corteReval = None
        if args.revalidarDias and args.revalidarDias > 0 and not args.tickers:
            corteReval = (date.today() - timedelta(days=args.revalidarDias)).isoformat()
        # cláusula que mantém: não-validado (promover) OU validado-vencido (revalidar)
        recente = ("(i.stFluxoValidado IS NULL OR i.stFluxoValidado = 0 "
                   "OR i.dtValidacaoFluxo IS NULL OR i.dtValidacaoFluxo < ?)")
        if args.tickers:
            alvos = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
            marcas = ",".join("?" * len(alvos))
            tickers = [r["cdTicker"] for r in D.Linhas(
                f"SELECT cdTicker FROM InfoAtivos WHERE cdTicker IN ({marcas})", tuple(alvos))]
        elif args.negociadosDias:
            # os que NEGOCIARAM na janela (validados ou não) — é o que aparece no
            # relatório, e mantém o gate barato na rotina (a base toda leva ~13 min).
            corte = (date.today() - timedelta(days=args.negociadosDias)).isoformat()
            sql = ("SELECT DISTINCT i.cdTicker FROM InfoAtivos i "
                   "JOIN NegociosBrutos nb ON nb.cdTicker = i.cdTicker "
                   f"WHERE nb.dtLiquidacao >= ? AND {D.NaoCancelado('nb.')}")
            params = [corte]
            if corteReval:
                sql += " AND " + recente
                params.append(corteReval)
            sql += " ORDER BY i.cdTicker"
            tickers = [r["cdTicker"] for r in D.Linhas(sql, tuple(params))]
        else:
            sql = "SELECT i.cdTicker FROM InfoAtivos i"
            params = []
            if corteReval:
                sql += " WHERE " + recente
                params.append(corteReval)
            sql += " ORDER BY i.cdTicker"
            tickers = [r["cdTicker"] for r in D.Linhas(sql, tuple(params))]
        if args.limite:
            tickers = tickers[:args.limite]

        # cdInstrumento (DEB/CRI/CRA) por ticker — a FI precisa para escolher o
        # endpoint; e o estado ATUAL de validação, para reportar promoções × rebaixos.
        instrMap = {r["cdTicker"]: r["cdInstrumento"] for r in D.Linhas(
            "SELECT cdTicker, cdInstrumento FROM InfoAtivos")}
        estavaValidado = {r["cdTicker"] for r in D.Linhas(
            "SELECT cdTicker FROM InfoAtivos WHERE stFluxoValidado = 1")}

        # pré-carrega na thread principal; workers só chamam B3/calc/FI
        ativos = {}
        for tk in tickers:
            a = CarregarAtivo(tk)
            if a:
                a["cdInstrumento"] = instrMap.get(tk)
                ativos[tk] = a
        log.info("%s: %d candidato(s), %d carregável(is), datas=%s",
                 NOME_SCRIPT, len(tickers), len(ativos), datas)

        comFi = not args.semFi
        resultados = []
        with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
            fut = {ex.submit(AvaliarAtivo, a, datas, not args.semTaxa, comFi): tk
                   for tk, a in ativos.items()}
            for i, f in enumerate(cf.as_completed(fut), 1):
                resultados.append(f.result())
                if i % 200 == 0:
                    log.info("%s: [%d/%d]", NOME_SCRIPT, i, len(ativos))

        # confiável = algum oráculo (B3 ou FI) reproduziu a calc → r["fonte"] setado.
        # não-confirmável = nenhum oráculo respondeu (B3 e FI vazias) → INVÁLIDO também
        # (decisão do usuário: "se nenhuma retorna, impossível validar → inválido"). Cai
        # na cascata de API no calc_taxa. Distinto de REPROVADO só para o relatório/CSV.
        def NaoConfirmavel(r):
            return r["nConfB3"] == 0 and r["nConfFi"] == 0

        reprovados, confirmados, naoConfLista = [], [], []
        for r in resultados:
            if NaoConfirmavel(r):
                naoConfLista.append(r)
            elif r["fonte"]:
                confirmados.append(r)
            else:
                reprovados.append(r)
        naoConf = len(naoConfLista)

        # PASS 2 — refresh-on-fail: cura a deriva do fluxo antes de rebaixar. O scrape
        # de rotina só re-scrapeia info FALTANDO, então fluxo já existente envelhece;
        # aqui, quem falha ganha um cadastro fresco da B3 e é re-testado (contra os dois
        # oráculos). Só rebaixa quem AINDA falha (falha genuína de metodologia).
        recuperados = 0
        if reprovados and not args.dryRun and not args.semRefresh:
            aindaFalha = []
            log.info("%s: refresh-on-fail em %d reprovado(s)...", NOME_SCRIPT, len(reprovados))
            for r in reprovados:
                a2 = RefrescarCadastroB3(r["tk"])
                if a2:
                    a2["cdInstrumento"] = instrMap.get(r["tk"])
                    r2 = AvaliarAtivo(a2, datas, not args.semTaxa, comFi)
                else:
                    r2 = None
                if r2 and r2["fonte"]:
                    recuperados += 1
                    confirmados.append(r2)
                else:
                    aindaFalha.append(r)
            reprovados = aindaFalha
            log.info("%s: refresh recuperou %d; restam %d reprovado(s)",
                     NOME_SCRIPT, recuperados, len(reprovados))

        reprovados.sort(key=lambda r: -r["piorPU"])
        confPorTk = {r["tk"]: r for r in confirmados}
        CSV_SAIDA.parent.mkdir(parents=True, exist_ok=True)
        with open(CSV_SAIDA, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            # piorTaxaBps so e preenchido com --com-taxa; sem a flag sai vazio. Ate
             # 01/09/2026 este numero era CALCULADO e jogado fora: entrava na decisao
             # (b3Pass exige piorTaxa <= TOL_TAXA_BPS) mas nao saia em relatorio nenhum,
             # entao nao dava para MEDIR a divergencia de taxa que o backlog persegue.
            w.writerow(["cdTicker", "idx", "fonteConfirma", "piorPU", "piorTaxaBps",
                        "piorFiBps", "nConfB3", "nConfFi", "veredito"])
            for r in resultados:
                rr = confPorTk.get(r["tk"], r)   # se recuperado no refresh, usa o r2
                if NaoConfirmavel(rr):
                    v = "nao_confirmavel"
                elif rr["fonte"]:
                    v = "confiavel"
                else:
                    v = "REPROVADO"
                w.writerow([rr["tk"], rr["idx"], rr["fonte"] or "-", f"{rr['piorPU']:.2e}",
                            f"{rr['piorTaxa']:.3f}" if rr.get("taxaMedida") else "",
                            f"{rr['piorFi']:.2f}", rr["nConfB3"], rr["nConfFi"], v])

        # UPDATE bidirecional: promove os confiáveis (validado=1 + fonte + dtValidacaoFluxo
        # de hoje), rebaixa os inválidos = reprovados + não-confirmáveis (validado=0).
        invalidos = reprovados + naoConfLista
        if not args.dryRun:
            hoje = date.today().isoformat()
            # Promoção e rebaixo entram no MESMO lote: são o mesmo UPDATE de três
            # colunas, com valores opostos, e um ticker nunca está nas duas listas.
            # Tudo SOBRESCREVER — inclusive o rebaixo, cujo dtValidacaoFluxo vai a
            # NULL de propósito (o resultado anterior virou lixo).
            marcas = (
                [{"cdTicker": r["tk"], "stFluxoValidado": 1, "dtValidacaoFluxo": hoje,
                  "cdFonteValidacaoFluxo": r["fonte"]} for r in confirmados]
                + [{"cdTicker": r["tk"], "stFluxoValidado": 0, "dtValidacaoFluxo": None,
                    "cdFonteValidacaoFluxo": None} for r in invalidos])
            if marcas:
                D.Mesclar("InfoAtivos", pd.DataFrame(marcas), politica={
                    c: D.SOBRESCREVER for c in
                    ("stFluxoValidado", "dtValidacaoFluxo", "cdFonteValidacaoFluxo")})

        from collections import Counter
        porFonte = Counter(r["fonte"] for r in confirmados)
        promovidos = [r for r in confirmados if r["tk"] not in estavaValidado]
        rebaixados = [r for r in invalidos if r["tk"] in estavaValidado]
        porIdx = Counter(r["idx"] for r in reprovados)

        # ── ACURACIA PU calc vs B3, por indexador (so onde a B3 respondeu) ──
        # Metrica-chave do gate: distribuicao do pior erro RELATIVO de PU por
        # faixa. Expoe o "fora do par" (bate no par, erra o desconto) — que se
        # concentra em IPCA e %CDI — em vez de so um placar de aprovados.
        from collections import defaultdict
        comB3 = [r for r in resultados if r["nConfB3"] > 0]

        def Faixa(e: float) -> str:
            if e <= 1e-6: return "<=1e-6"
            if e <= TOL_PU: return "<=1e-5"
            if e <= 1e-4: return "<=1e-4"
            if e <= 1e-3: return "<=1e-3"
            return ">1e-3"

        FAIXAS = ["<=1e-6", "<=1e-5", "<=1e-4", "<=1e-3", ">1e-3"]
        distr = defaultdict(Counter)
        for r in comB3:
            distr[r["idx"]][Faixa(r["piorPU"])] += 1
        linhasAcc = []
        for idx in sorted(distr):
            c = distr[idx]
            n = sum(c.values())
            dentro = c["<=1e-6"] + c["<=1e-5"]
            linhasAcc.append([idx, n, f"{100 * dentro / n:.1f}%"] + [c[f] for f in FAIXAS])
        if linhasAcc:
            rel.Secao("Acuracia PU calc vs B3 (por indexador; so onde a B3 respondeu)",
                      ["indexador", "n", "%<=1e-5"] + FAIXAS, linhasAcc)
            totN = len(comB3)
            totDentro = sum(1 for r in comB3 if r["piorPU"] <= TOL_PU)
            rel.Metrica("Acuracia PU<=1e-5 (R$0,01/1000) global",
                        f"{100 * totDentro / totN:.1f}% ({totDentro}/{totN})" if totN else "n/a")

        # ── ACURACIA da TAXA (round-trip calcYield) ──
        # E a medicao que separa "o fluxo esta certo" (PU no par) de "o DESCONTO esta
        # certo" (taxa implicita num PU fora do par). Ja em BPS DE YIELD via
        # DiffTaxaEmBps, entao o %CDI aparece na mesma escala dos demais — e nao ~10x
        # inflado como na medicao de 13/07 que tirou o %CDI da calc local.
        comTaxaMedida = [r for r in resultados if r.get("taxaMedida")]
        if comTaxaMedida:
            porIdxTaxa = defaultdict(list)
            for r in comTaxaMedida:
                porIdxTaxa[r["idx"]].append(r["piorTaxa"])
            linhasTaxa = []
            for idx in sorted(porIdxTaxa):
                v = sorted(porIdxTaxa[idx])
                mediana = v[len(v) // 2]
                p90 = v[min(len(v) - 1, int(0.9 * len(v)))]
                linhasTaxa.append([idx, len(v), f"{mediana:.2f}", f"{p90:.2f}",
                                   f"{v[-1]:.2f}",
                                   sum(1 for x in v if x <= TOL_TAXA_BPS)])
            rel.Secao("Acuracia da TAXA calc vs oraculo, em bps de yield (passo 5)",
                      ["indexador", "n", "mediana", "p90", "pior",
                       f"<= {TOL_TAXA_BPS:.0f} bps"], linhasTaxa)

        rel.Metrica("Candidatos testados", len(ativos))
        rel.Metrica("Confiáveis (calc reproduz um oráculo)", len(confirmados))
        rel.Metrica("- por oráculo (B3 / FI)", dict(porFonte))
        rel.Metrica("- resgatados pela FI (B3 não bateu)", porFonte.get("FiAnalytics", 0))
        rel.Metrica("Promovidos (eram não-validados)", len(promovidos))
        rel.Metrica("Recuperados por refresh da B3", recuperados)
        rel.Metrica("INVÁLIDOS (rebaixados p/ 0)" + ("" if not args.dryRun else " (dry-run)"),
                    len(invalidos))
        rel.Metrica("- por divergência (REPROVADOS)", len(reprovados))
        rel.Metrica("- por não-confirmável (B3 e FI mudas)", naoConf)
        rel.Metrica("- eram validados (rebaixados de fato)", len(rebaixados))
        rel.Metrica("Reprovados por indexador", dict(porIdx))
        if falhasCalc:
            # Um motivo que domina a lista quase nunca e o ativo: e a nossa base.
            # "Curva DI x pre indisponivel" ou "Taxa DI realizada ausente" em milhares
            # de ativos significa buraco de dado, e a rodada inteira deve ser refeita
            # depois de fechar o buraco — nao aceitar o rebaixamento.
            rel.Secao("Por que a NOSSA calc nao respondeu (motivos, mais frequentes)",
                      ["ocorrencias", "motivo"],
                      [[n, m] for m, n in sorted(falhasCalc.items(),
                                                 key=lambda kv: -kv[1])[:10]])
        rel.Secao("Piores reprovados",
                  ["ticker", "idx", "piorPU", "piorFiBps", "nConfB3", "nConfFi"],
                  [[r["tk"], r["idx"], f"{r['piorPU']:.1e}", f"{r['piorFi']:.1f}",
                    r["nConfB3"], r["nConfFi"]] for r in reprovados[:30]])
        total = D.Escalar("SELECT COUNT(*) FROM InfoAtivos WHERE stFluxoValidado = 1")
        rel.Metrica("Total validado na base (após a rodada)", total)
        if invalidos and not args.dryRun:
            rel.Aviso(f"{len(invalidos)} ativo(s) INVÁLIDOS ({len(reprovados)} por divergência, "
                      f"{naoConf} sem oráculo): a calc não pôde ser confirmada. Caem na cascata "
                      f"de API no calc_taxa. Ver {CSV_SAIDA}.")
        log.info("%s: confiáveis=%d (B3=%d FI=%d) promovidos=%d reprovados=%d nao_conf=%d",
                 NOME_SCRIPT, len(confirmados), porFonte.get("B3", 0),
                 porFonte.get("FiAnalytics", 0), len(promovidos), len(reprovados), naoConf)
    except Exception:
        success = False
        rel.Erro("A rodada abortou — ver traceback.")
        log.error("%s: falhou\n%s", NOME_SCRIPT, traceback.format_exc())
        EnviarEmailConclusao(NOME_SCRIPT, False, rel, tracebackErro=traceback.format_exc(), logger=log)
        sys.exit(1)

    EnviarEmailConclusao(NOME_SCRIPT, success, rel, logger=log)


if __name__ == "__main__":
    Principal()
