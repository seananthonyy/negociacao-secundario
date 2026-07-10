import logging
import threading
from pathlib import Path
from typing import Optional

from lib.config import cfg, ObterListaEmails

EMAIL_TIMEOUT = 20  # segundos antes de desistir e logar warning


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
    textoResumo: str,
    tracebackErro: str | None = None,
    logger: Optional[logging.Logger] = None,
) -> None:
    """
    Envia email via Outlook (win32com) em thread separada com timeout de 20s.
    OUTLOOK_TO aceita lista separada por ; .
    Se cfg["email"]["ativo"] for false, loga e nao envia.
    Falha silenciosa: loga o erro, nao propaga excecao.
    """
    log = logger or logging.getLogger(__name__)
    try:
        if not cfg["email"]["ativo"]:
            log.info("Email desativado por config.toml — nao enviado.")
            return

        status = "OK" if success else "ERROR"
        subject = f"[{status}] {nomeScript}"
        body = f"Script: {nomeScript}\nStatus: {status}\n\nResumo:\n{textoResumo}"
        if tracebackErro:
            body += f"\n\nTraceback:\n{tracebackErro}"

        destinatarios = ResolverDestinatarios(log)
        if not destinatarios:
            log.warning("OUTLOOK_TO nao configurado — email nao enviado.")
            return

        t = threading.Thread(
            target=DespacharEmail,
            args=(subject, body, destinatarios, log),
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
        if not cfg["email"]["ativo"]:
            log.info("Email desativado por config.toml — nao gerado.")
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
