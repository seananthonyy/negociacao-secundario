"""
Relatorio de execucao — o que cada script acumula durante a rodada e manda por email.

Antes, cada script montava a mao um bloco de texto solto ("Resultado:\n linhas..."), o
que dava um email diferente por script e sem o essencial: nao dava para saber QUAIS
dias rodaram, o que foi inserido contra o que foi atualizado, nem ver exemplo nenhum
do que entrou na base.

Uso:

    rel = RelatorioExecucao("scrape_b3_boletim", args=vars(args))
    rel.Datas(["2026-07-10", "2026-07-11"])
    rel.Contar("inseridos", 1234)
    rel.Exemplo("inseridos", {"cdTicker": "AESLA5", "vrPU": 1155.67})
    rel.Metrica("Pregoes sem dado na fonte", 2)
    rel.Aviso("2026-07-11 fora da janela da B3.")
    ...
    EnviarEmailConclusao("scrape_b3_boletim", True, rel, logger=log)

Os exemplos sao LIMITADOS (LIMITE_EXEMPLOS): listar tudo trava o Outlook quando a
rodada insere dezenas de milhares de linhas.
"""

from __future__ import annotations

import html
from datetime import datetime

# Paleta Itau.
COR_LARANJA = "#EC7000"
COR_AZUL = "#003B7D"
COR_TEXTO = "#2B2B2B"
COR_CINZA = "#6E6E6E"
COR_BORDA = "#E3E3E3"
COR_FUNDO_CAB = "#FFF3E8"   # laranja bem claro, para cabecalho de tabela
COR_ERRO = "#C4161C"
COR_AVISO = "#B26B00"
COR_OK = "#00875A"

LIMITE_EXEMPLOS = 10

# As acoes que os scripts contam. A ordem aqui e a ordem no email.
ACOES = ("inseridos", "atualizados", "deletados", "ignorados", "falhas")


def Escapar(valor) -> str:
    return html.escape("" if valor is None else str(valor))


def FormatarNumero(n) -> str:
    """1234567 -> '1.234.567' (separador de milhar PT-BR)."""
    try:
        return f"{int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return Escapar(n)


class RelatorioExecucao:
    """Acumula o que aconteceu na rodada e renderiza em HTML (email) ou texto (log)."""

    def __init__(self, nomeScript: str, args: dict | None = None):
        self.nomeScript = nomeScript
        self.args = {k: v for k, v in (args or {}).items() if v not in (None, False)}
        self.dtInicio = datetime.now()
        self.datas: list[str] = []
        self.contadores: dict[str, int] = {}
        self.exemplos: dict[str, list[dict]] = {}
        self.metricas: list[tuple[str, str]] = []
        self.secoes: list[tuple[str, list[str], list[list]]] = []   # (titulo, cabecalho, linhas)
        self.avisos: list[str] = []
        self.erros: list[str] = []

    # -- coleta ---------------------------------------------------------------

    def Datas(self, datas) -> None:
        """Dias que a rodada processou. Aceita date, str ou lista."""
        if not isinstance(datas, (list, tuple, set)):
            datas = [datas]
        self.datas = sorted({str(d) for d in datas if d})

    def Contar(self, acao: str, n: int = 1) -> None:
        self.contadores[acao] = self.contadores.get(acao, 0) + int(n)

    def Exemplo(self, acao: str, linha: dict) -> None:
        """Guarda um exemplo da acao. Silenciosamente ignora depois do limite —
        o objetivo e ilustrar, nao listar (listar trava o Outlook)."""
        fila = self.exemplos.setdefault(acao, [])
        if len(fila) < LIMITE_EXEMPLOS:
            fila.append(linha)

    def Metrica(self, nome: str, valor) -> None:
        self.metricas.append((nome, FormatarNumero(valor) if isinstance(valor, int) else str(valor)))

    def Secao(self, titulo: str, cabecalho: list[str], linhas: list[list]) -> None:
        """Tabela livre (ex.: resultado por dia, divergencias). Truncada no limite."""
        self.secoes.append((titulo, cabecalho, list(linhas)[:LIMITE_EXEMPLOS * 3]))

    def Aviso(self, texto: str) -> None:
        self.avisos.append(texto)

    def Erro(self, texto: str) -> None:
        self.erros.append(texto)

    # -- render ---------------------------------------------------------------

    def Duracao(self) -> str:
        seg = int((datetime.now() - self.dtInicio).total_seconds())
        if seg < 60:
            return f"{seg}s"
        return f"{seg // 60}min {seg % 60}s"

    def Texto(self) -> str:
        """Versao plana — vai para o log e para o fallback do email."""
        linhas = [f"Script   : {self.nomeScript}",
                  f"Duracao  : {self.Duracao()}"]
        if self.datas:
            linhas.append(f"Datas    : {', '.join(self.datas)}")
        if self.args:
            linhas.append(f"Args     : {self.args}")
        linhas.append("")
        for acao in ACOES:
            if acao in self.contadores:
                linhas.append(f"  {acao:<12} {FormatarNumero(self.contadores[acao])}")
        for nome, valor in self.metricas:
            linhas.append(f"  {nome}: {valor}")
        for a in self.avisos:
            linhas.append(f"  [AVISO] {a}")
        for e in self.erros:
            linhas.append(f"  [ERRO] {e}")
        return "\n".join(linhas)

    def Tabela(self, cabecalho: list[str], linhas: list[list]) -> str:
        th = "".join(
            f'<th style="padding:6px 10px;text-align:left;font-size:12px;'
            f'color:{COR_AZUL};border-bottom:2px solid {COR_LARANJA};">{Escapar(c)}</th>'
            for c in cabecalho)
        trs = []
        for i, linha in enumerate(linhas):
            fundo = "#FFFFFF" if i % 2 == 0 else "#FAFAFA"
            tds = "".join(
                f'<td style="padding:6px 10px;font-size:12px;color:{COR_TEXTO};'
                f'border-bottom:1px solid {COR_BORDA};">{Escapar(c)}</td>' for c in linha)
            trs.append(f'<tr style="background:{fundo};">{tds}</tr>')
        return (f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;'
                f'width:100%;margin:6px 0 16px 0;background:{COR_FUNDO_CAB};">'
                f"<thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>")

    def Titulo(self, texto: str) -> str:
        return (f'<div style="margin:20px 0 4px 0;font-size:13px;font-weight:bold;'
                f'color:{COR_AZUL};text-transform:uppercase;letter-spacing:0.5px;">'
                f"{Escapar(texto)}</div>")

    def Cartoes(self) -> str:
        """Os contadores, lado a lado, no topo — e o que se olha primeiro."""
        itens = [(a, self.contadores[a]) for a in ACOES if a in self.contadores]
        itens += [(a, n) for a, n in self.contadores.items() if a not in ACOES]
        if not itens:
            return ""
        tds = []
        for acao, n in itens:
            cor = COR_ERRO if acao == "falhas" and n else COR_LARANJA
            tds.append(
                f'<td style="padding:12px 18px;border:1px solid {COR_BORDA};'
                f'background:#FFFFFF;text-align:center;">'
                f'<div style="font-size:22px;font-weight:bold;color:{cor};">{FormatarNumero(n)}</div>'
                f'<div style="font-size:11px;color:{COR_CINZA};text-transform:uppercase;'
                f'letter-spacing:0.5px;">{Escapar(acao)}</div></td>')
        return (f'<table cellpadding="0" cellspacing="6" style="border-collapse:separate;'
                f'margin:8px 0 4px 0;"><tr>{"".join(tds)}</tr></table>')

    def Html(self, success: bool, tracebackErro: str | None = None) -> str:
        status = "CONCLUIDO" if success else "FALHOU"
        corStatus = COR_OK if success else COR_ERRO

        p = [f'<div style="font-family:Segoe UI,Arial,sans-serif;color:{COR_TEXTO};'
             f'max-width:900px;">']

        # Cabecalho
        p.append(
            f'<div style="background:{COR_AZUL};padding:14px 18px;">'
            f'<div style="color:#FFFFFF;font-size:17px;font-weight:bold;">{Escapar(self.nomeScript)}</div>'
            f'<div style="color:{COR_LARANJA};font-size:12px;margin-top:2px;'
            f'letter-spacing:0.5px;">NEGOCIACAO SECUNDARIA &middot; CREDITO PRIVADO</div></div>')
        p.append(
            f'<div style="border-left:4px solid {corStatus};background:#FAFAFA;'
            f'padding:8px 14px;margin-bottom:4px;">'
            f'<span style="color:{corStatus};font-weight:bold;font-size:13px;">{status}</span>'
            f'<span style="color:{COR_CINZA};font-size:12px;">'
            f' &nbsp;|&nbsp; {self.dtInicio.strftime("%d/%m/%Y %H:%M:%S")}'
            f' &nbsp;|&nbsp; duracao {self.Duracao()}</span></div>')

        # Erro primeiro: e o que importa quando falha.
        if self.erros or tracebackErro:
            p.append(self.Titulo("Erro"))
            for e in self.erros:
                p.append(f'<div style="color:{COR_ERRO};font-size:12px;margin:2px 0;">{Escapar(e)}</div>')
            if tracebackErro:
                p.append(
                    f'<pre style="background:#FFF5F5;border:1px solid {COR_ERRO};padding:10px;'
                    f'font-size:11px;color:{COR_ERRO};overflow:auto;white-space:pre-wrap;">'
                    f"{Escapar(tracebackErro)}</pre>")

        # Parametros da rodada
        param = []
        if self.datas:
            resumo = (f"{self.datas[0]} a {self.datas[-1]} ({len(self.datas)} dias)"
                      if len(self.datas) > 3 else ", ".join(self.datas))
            param.append(["Datas processadas", resumo])
        for k, v in self.args.items():
            param.append([k, v])
        if param:
            p.append(self.Titulo("Parametros da rodada"))
            p.append(self.Tabela(["Parametro", "Valor"], param))

        p.append(self.Cartoes())

        if self.metricas:
            p.append(self.Titulo("Metricas"))
            p.append(self.Tabela(["Metrica", "Valor"], [[n, v] for n, v in self.metricas]))

        for titulo, cabecalho, linhas in self.secoes:
            if linhas:
                p.append(self.Titulo(titulo))
                p.append(self.Tabela(cabecalho, linhas))

        # Exemplos: ilustram o que entrou, sem listar tudo.
        for acao in list(ACOES) + [a for a in self.exemplos if a not in ACOES]:
            fila = self.exemplos.get(acao)
            if not fila:
                continue
            total = self.contadores.get(acao, len(fila))
            titulo = f"Exemplos — {acao} ({len(fila)} de {FormatarNumero(total)})"
            cols = list(fila[0].keys())
            p.append(self.Titulo(titulo))
            p.append(self.Tabela(cols, [[linha.get(c) for c in cols] for linha in fila]))

        if self.avisos:
            p.append(self.Titulo("Avisos"))
            for a in self.avisos:
                p.append(
                    f'<div style="border-left:3px solid {COR_AVISO};background:#FFFBF3;'
                    f'padding:6px 10px;margin:3px 0;font-size:12px;color:{COR_AVISO};">'
                    f"{Escapar(a)}</div>")

        p.append(
            f'<div style="margin-top:22px;padding-top:8px;border-top:1px solid {COR_BORDA};'
            f'font-size:11px;color:{COR_CINZA};">Gerado automaticamente pelo pipeline de '
            f"negociacao secundaria.</div></div>")
        return "".join(p)
