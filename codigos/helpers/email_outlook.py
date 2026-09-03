import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import cfg, ObterListaEmails
from relatorio_execucao import RelatorioExecucao

EMAIL_TIMEOUT = 20  # segundos antes de desistir e logar warning

# Com NEGSEC_SEM_EMAIL setado, nada vai para o Outlook: o corpo do email e gravado em
# cache/emails/ para conferencia. Existe porque o COM do Outlook trava (dialog de
# permissao) e derruba qualquer rodada em lote — e nao da para depurar o template
# esperando 20s de timeout a cada script.
ENV_SEM_EMAIL = "NEGSEC_SEM_EMAIL"
DIR_EMAILS = Path(cfg["paths"]["cacheDir"]) / "emails"


def EmailDesligado() -> bool:
    return bool(os.environ.get(ENV_SEM_EMAIL)) or not cfg["email"]["ativo"]


def GravarEmailEmArquivo(subject: str, corpoHtml: str, log: logging.Logger) -> None:
    """Despeja o email em disco em vez de mandar. Serve para conferir o template."""
    try:
        DIR_EMAILS.mkdir(parents=True, exist_ok=True)
        seguro = "".join(c if c.isalnum() or c in "-_" else "_" for c in subject)[:80]
        destino = DIR_EMAILS / f"{datetime.now():%Y-%m-%d_%H%M%S}_{seguro}.html"
        destino.write_text(corpoHtml, encoding="utf-8")
        log.info("Email nao enviado (%s). Corpo gravado em %s", ENV_SEM_EMAIL, destino)
    except Exception as exc:
        log.warning("Falha ao gravar o email em arquivo: %s", exc)


def ResolverDestinatarios(log: logging.Logger) -> list[str]:
    """Destinatários dos emails [OK]/[ERROR]. Resolvidos via lib.config.ObterListaEmails
    (destinatarios.py → variável OUTLOOK_TO). Retorna [] se nada configurado."""
    return ObterListaEmails("outlook")


def DespacharEmail(subject: str, body: str, destinatarios: list[str], log: logging.Logger) -> None:
    """
    Cria e envia itens de email via Outlook COM.
    Deve rodar em thread separada com CoInitialize já chamado.
    """
    import pythoncom  # type: ignore[import]
    import win32com.client  # type: ignore[import]

    pythoncom.CoInitialize()
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        ns = outlook.GetNamespace("MAPI")
        for dest in destinatarios:
            mail = outlook.CreateItem(0)
            mail.To = dest
            mail.Subject = subject
            mail.Body = body
            mail.Send()
            log.info("Email enfileirado para %s: %s", dest, subject)
        ns.SendAndReceive(False)
        log.info("SendAndReceive concluido.")
    finally:
        pythoncom.CoUninitialize()


def EnviarEmailConclusao(
    nomeScript: str,
    success: bool,
    resumo: "RelatorioExecucao | str",
    tracebackErro: str | None = None,
    logger: Optional[logging.Logger] = None,
) -> None:
    """
    Email de fim de script, em HTML (paleta Itau).

    `resumo` aceita um RelatorioExecucao (o formato bom: contadores, exemplos, datas,
    avisos) ou uma string — os scripts antigos passavam texto solto, e nao vale a pena
    quebra-los; a string vira um <pre> dentro do mesmo template.

    Nunca propaga excecao: um email que falha nao pode derrubar uma rodada que deu certo.
    """
    log = logger or logging.getLogger(__name__)
    try:
        if isinstance(resumo, RelatorioExecucao):
            corpoHtml = resumo.Html(success, tracebackErro)
            log.info("Resumo da rodada:\n%s", resumo.Texto())
        else:
            rel = RelatorioExecucao(nomeScript)
            rel.Secao("Resumo", ["saida"], [[l] for l in str(resumo).splitlines() if l.strip()])
            corpoHtml = rel.Html(success, tracebackErro)

        status = "OK" if success else "ERRO"
        subject = f"[{status}] {nomeScript}"

        if EmailDesligado():
            GravarEmailEmArquivo(subject, corpoHtml, log)
            return

        destinatarios = ResolverDestinatarios(log)
        if not destinatarios:
            log.warning("OUTLOOK_TO nao configurado — email nao enviado.")
            return

        t = threading.Thread(
            target=DespacharEmailHtml,
            args=(subject, corpoHtml, destinatarios, [], log, False),
            daemon=True,
        )
        t.start()
        t.join(timeout=EMAIL_TIMEOUT)
        if t.is_alive():
            log.warning("Email nao enviado em %ds — verificar dialog de permissao no Outlook.", EMAIL_TIMEOUT)

    except Exception as e:
        log.error("Falha ao enviar email via Outlook: %s", e)


def DespacharEmailHtml(
    subject: str,
    corpoHtml: str,
    destinatarios: list[str],
    attachments: list[str],
    log: logging.Logger,
    draft: bool = False,
) -> None:
    """
    Cria email HTML (com anexos) via Outlook COM e, conforme `draft`:
      - draft=False → envia para cada destinatario (Send + SendAndReceive);
      - draft=True  → salva UM rascunho enderecado a todos na pasta Rascunhos.
    Deve rodar em thread separada com CoInitialize ja chamado.
    """
    import pythoncom  # type: ignore[import]
    import win32com.client  # type: ignore[import]

    def Anexar(mail) -> None:
        for att in attachments:
            if Path(att).exists():
                mail.Attachments.Add(att)
            else:
                log.warning("Anexo nao encontrado, ignorado: %s", att)

    pythoncom.CoInitialize()
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        ns = outlook.GetNamespace("MAPI")

        if draft:
            mail = outlook.CreateItem(0)
            mail.To = "; ".join(destinatarios)
            mail.Subject = subject
            mail.HTMLBody = corpoHtml
            Anexar(mail)
            mail.Save()  # vai para a pasta Rascunhos; nao dispara dialog de envio
            log.info("Rascunho salvo (Rascunhos) para %s: %s", "; ".join(destinatarios), subject)
            return

        for dest in destinatarios:
            mail = outlook.CreateItem(0)
            mail.To = dest
            mail.Subject = subject
            mail.HTMLBody = corpoHtml
            Anexar(mail)
            mail.Send()
            log.info("Email (HTML) enfileirado para %s: %s", dest, subject)
        ns.SendAndReceive(False)
        log.info("SendAndReceive concluido.")
    finally:
        pythoncom.CoUninitialize()


def EnviarEmailHtml(
    subject: str,
    corpoHtml: str,
    attachments: Optional[list[str]] = None,
    logger: Optional[logging.Logger] = None,
    to: Optional[list[str]] = None,
    draft: bool = False,
) -> None:
    """
    Cria email com corpo HTML e anexos via Outlook (win32com) em thread separada
    com timeout de 20s. Anexos resolvidos para caminho absoluto.
      - `to`: lista de destinatarios; se None, usa OUTLOOK_TO do .env.
      - `draft=True`: salva como rascunho (nao envia).
    Se cfg["email"]["ativo"] for false, loga e nao faz nada.
    Falha silenciosa: loga o erro, nao propaga excecao.
    """
    log = logger or logging.getLogger(__name__)
    try:
        if EmailDesligado():
            GravarEmailEmArquivo(subject, corpoHtml, log)
            return

        destinatarios = to if to else ResolverDestinatarios(log)
        if not destinatarios:
            log.warning("Sem destinatarios (OUTLOOK_TO vazio) — email nao gerado.")
            return

        absAttachments = [str(Path(a).resolve()) for a in (attachments or [])]

        t = threading.Thread(
            target=DespacharEmailHtml,
            args=(subject, corpoHtml, destinatarios, absAttachments, log, draft),
            daemon=True,
        )
        t.start()
        t.join(timeout=EMAIL_TIMEOUT)
        if t.is_alive():
            acao = "rascunho nao salvo" if draft else "email nao enviado"
            log.warning("%s em %ds — verificar Outlook.", acao, EMAIL_TIMEOUT)

    except Exception as e:
        log.error("Falha ao gerar email HTML via Outlook: %s", e)
