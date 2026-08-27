"""
cadastro_b3.py
==============
A leitura do `getBondDetails` da B3 traduzida para o nosso cadastro: mapa de
indexador, montagem da agenda de fluxo e a gravacao do pacote em InfoAtivos +
FluxoAtivos.

Vive aqui porque tem DOIS donos: o scrape_b3_bond_details (carga de rotina) e o
validar_calc_b3 (refresh do cadastro quando o gate reprova por deriva de fluxo).
Antes o validador carregava o arquivo do scraper por caminho, o que amarrava as
duas pastas uma na outra.

vrVNE + dtInicioRentabilidade + FluxoAtivos sao um PACOTE INDIVISIVEL por ativo:
a B3 pre-capitaliza a carencia dentro do VNE e nao emite evento de incorporacao,
enquanto a Anbima traz o VNE cru e a incorporacao como evento. Misturar as duas
fontes conta a capitalizacao DUAS VEZES, sem erro nenhum, so um PU errado.
"""

from datetime import date

from calc import ImportarCalc


# 'method' da B3 -> nosso cdIndexador.
MAPA_INDEXADOR = {
    "IPCA-I": "IPCA", "IPCA": "IPCA",
    "DI-PERC": "%CDI", "DI-SPREAD": "CDI+",
    "PRE": "PREFIXADO",
}

# Campos escalares que a B3 preenche e que NAO fazem parte do pacote indivisivel.
# Preenchidos so quando estao NULL (a Anbima continua dona do que ja gravou).
CAMPOS_ESCALARES = ("cdEmissor", "dtVencimento", "dtEmissao", "vrTaxaEmissao",
                    "cdIndexador", "cdInstrumento")


def MapearIndexador(method) -> str | None:
    if not method:
        return None
    if str(method).upper().startswith("IPCA"):
        return "IPCA"
    return MAPA_INDEXADOR.get(method)


def FluxoDaB3(det: dict) -> list[tuple[str, float | None, float | None]]:
    """Eventos da B3 -> [(dtEvento, vrPctAmortizacao, vrPctIncorporacao)].

    'A' = amortizacao (% do principal ORIGINAL — a B3 manda saldo_original).
    'J' = cupom. So no estilo 'IPCA-I' o yield do 'J' e a %incorporacao; nos demais e a
    propria taxa do cupom, e le-la como incorporacao transformaria todo IPCA simples em
    falso-divergente.

    TRES normalizacoes, e sem as tres o PU sai errado (medido: 122 dos 2.853 ativos com
    erro acima de 1%, e os 86 piores eram todos IPCA-I):

    1. DATAS AJUSTADAS PARA DIA UTIL. A B3 manda a data CRUA (o TAEE17 incorpora em
       15/03/2025, um sabado). A calc casa evento com aniversario, e o aniversario e
       ajustado por ProximoDu — entao evento passado que caia em fim de semana NAO CASA
       e e silenciosamente descartado. Guardar ja ajustado alinha as duas pontas.

    2. AMORTIZACAO FINAL COMPLETADA. Nos IPCA-I a B3 nao emite o 'A' do vencimento: os
       'A' somam menos de 100 (TAEE17 soma 96,8) e o vencimento vem so com um 'J' de
       yield 0. Isso e fatal porque o InferirTipoAmort da calc decide a convencao pela
       SOMA: != 100 vira 'saldo_restante', e o papel inteiro passa a ser amortizado na
       convencao errada. O resto vai para o vencimento.

    3. DATAS DE CUPOM ('J') ENTRAM como eventos de amortizacao zero: a calc ancora o
       juros de cada periodo no evento anterior. Guardar so os 'A' derruba a aderencia
       do modelo B3 de 92% para 25%.
    """
    C = ImportarCalc()
    leIncorporacao = det.get("method") == "IPCA-I"

    def Normalizar(bruta: str) -> date | None:
        try:
            d = date.fromisoformat(str(bruta)[:10])
        except ValueError:
            return None
        return C.ProximoDu(d, C.FERIADOS_ANBIMA)   # (1)

    eventos: dict[date, list] = {}
    for e in det.get("events") or []:
        dtEvento = Normalizar(e.get("date"))
        if dtEvento is None:
            continue
        pct = float(e.get("yield") or 0)
        tipo = e.get("eventType")
        linha = eventos.setdefault(dtEvento, [None, None])   # (3)
        if tipo == "A" and pct > 0:
            linha[0] = pct
        elif tipo == "J" and leIncorporacao and pct > 0:
            linha[1] = pct

    if not eventos:
        return []

    # (2) — tolerancia de 0,001 p.p.: soma exata nao precisa de nada.
    resto = 100.0 - sum(v[0] or 0.0 for v in eventos.values())
    if 0.001 < resto <= 100.0:
        dtVencimento = Normalizar(det.get("expiredate")) or max(eventos)
        linha = eventos.setdefault(dtVencimento, [None, None])
        linha[0] = (linha[0] or 0.0) + resto

    return sorted((d.isoformat(), v[0], v[1]) for d, v in eventos.items())


def GravarAtivo(conn, cdTicker: str, det: dict, agora: str) -> str:
    """Grava o que a B3 sabe. Devolve 'inseridos' | 'atualizados' | 'ignorados'.

    Sem eventos, a B3 nao serve de fonte de fluxo: preenche so os escalares que estao
    NULL e NAO toca em vrVNE / dtInicioRentabilidade / FluxoAtivos (o pacote).
    """
    antes = conn.execute(
        "SELECT cdTicker, stTemFluxo, cdFonteCadastro FROM InfoAtivos WHERE cdTicker = ?",
        (cdTicker,)).fetchone()
    novo = antes is None

    cdIndexador = MapearIndexador(det.get("method"))
    tipoIf = det.get("tipoIF") or {}
    valores = {
        "cdEmissor": det.get("issuer"),
        "dtVencimento": (det.get("expiredate") or "")[:10] or None,
        "dtEmissao": (det.get("issuedate") or "")[:10] or None,
        "vrTaxaEmissao": det.get("yield"),
        "cdIndexador": cdIndexador,
        "cdInstrumento": tipoIf.get("codigoAsString") if isinstance(tipoIf, dict) else None,
    }
    # Aniversario so faz sentido em IPCA: nos demais indexadores o VNA nao sofre
    # correcao monetaria e a calc ignora o parametro.
    aniversario = det.get("anniversaryday")
    vrAniversario = int(aniversario) if (cdIndexador == "IPCA" and aniversario is not None) else None

    if novo:
        conn.execute("INSERT INTO InfoAtivos (cdTicker, dtAtualizacao) VALUES (?, ?)",
                     (cdTicker, agora))

    # COALESCE nos escalares: a B3 preenche buraco, nao sobrescreve o que a Anbima ja
    # gravou. Excecao: vrAniversario, para o qual a B3 e a unica fonte explicita.
    sets = ", ".join(f"{c} = COALESCE({c}, ?)" for c in CAMPOS_ESCALARES)
    conn.execute(
        f"UPDATE InfoAtivos SET {sets}, "
        "  vrAniversario = COALESCE(?, vrAniversario), dtAtualizacao = ? WHERE cdTicker = ?",
        tuple(valores[c] for c in CAMPOS_ESCALARES) + (vrAniversario, agora, cdTicker))

    fluxo = FluxoDaB3(det)
    if not fluxo:
        return "inseridos" if novo else "ignorados"

    # O PACOTE. Escrever vrVNE/dtInicioRentabilidade dispara trgInfoAtivosInvalidaFluxo,
    # que zera stFluxoValidado — por isso a validacao e reafirmada DEPOIS, mais abaixo.
    conn.execute(
        "UPDATE InfoAtivos SET vrVNE = ?, dtInicioRentabilidade = ?, cdFonteCadastro = 'B3', "
        "  stTemFluxo = 1, dtAtualizacao = ? WHERE cdTicker = ?",
        (det.get("vne"), (det.get("startingdate") or "")[:10] or None, agora, cdTicker))

    conn.execute("DELETE FROM FluxoAtivos WHERE cdTicker = ?", (cdTicker,))
    conn.executemany(
        "INSERT INTO FluxoAtivos (cdTicker, dtEvento, vrPctAmortizacao, vrPctIncorporacao, "
        "dtAtualizacao) VALUES (?, ?, ?, ?, ?)",
        [(cdTicker, d, a, i, agora) for d, a, i in fluxo])

    # Este script NAO valida (24/08/2026). "Fluxo veio da B3" nao e o mesmo que "a calc
    # precifica este ativo certo": marcar validado aqui liberava para a calc local ativo
    # que nunca passou pelo gate, com erro de PU de ate 70%. Quem marca stFluxoValidado=1
    # e SO o validar_calc_b3, e so depois de a nossa calc reproduzir a B3 (ou a FI).
    # Ate la o ativo cai na cascata de API no calc_taxa, que e o comportamento seguro.

    return "inseridos" if (novo or not antes["stTemFluxo"]) else "atualizados"
