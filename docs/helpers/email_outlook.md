# email_outlook.py

`codigos/helpers/email_outlook.py` — **envio de email pelo Outlook local**.

---

## Overview

Todo script manda email no fim, sucesso ou erro. É a convenção que torna a rotina agendada
observável: ninguém abre log de madrugada, mas todo mundo lê email.

O envio é por **COM do Outlook** (`pywin32`), não SMTP — o ambiente corporativo não abre
porta de SMTP, mas o Outlook já está autenticado na máquina.

---

## API pública

| função | para quê |
|---|---|
| `EnviarEmailConclusao(nomeScript, sucesso, rel, tracebackErro=None, logger=None)` | O email de fim de rodada, a partir de um `RelatorioExecucao` |
| `EnviarEmailHtml(subject, html, attachments, to, draft, logger)` | Um email de conteúdo próprio — usado pelo rascunho do relatório do dia |
| `EmailDesligado()` | Se `NEGSEC_SEM_EMAIL` está setado |
| `GravarEmailEmArquivo(subject, corpoHtml, log)` | Grava o corpo em `cache/emails/` |
| `ResolverDestinatarios(log)` | A lista de destinatários do email de conclusão |

---

## Invariantes

**1. `NEGSEC_SEM_EMAIL=1` desliga tudo.** Em vez de abrir o Outlook, grava o corpo HTML em
`cache/emails/<timestamp>__<OK|ERRO>__<script>.html`. Cobre também o caminho de erro.
**Usar sempre em teste e em rodada de lote.**

**2. Falha de email nunca derruba o script.** O envio é embrulhado e, no máximo, loga um
aviso. Um problema de Outlook não pode invalidar uma rodada de dados que já deu certo.

**3. Timeout de 20 segundos.** O COM do Outlook trava indefinidamente quando há um diálogo
modal aberto na máquina; o timeout evita que a rodada fique pendurada.

---

## Quem consome

**Todos os 24 scripts**, no bloco `finally`.

---

## Armadilhas

**A instância COM órfã do Outlook.** Uma chamada interrompida deixa um processo do Outlook
sem janela — respondendo, mas pendurado — que trava a próxima execução. Foi o que motivou
a convenção do `NEGSEC_SEM_EMAIL` em lote. Sintoma: o script para no envio e não volta.
Solução: matar o processo `OUTLOOK.EXE` sem janela.

**`draft=True` salva rascunho em vez de enviar.** É o modo do rascunho do relatório do
dia, para o usuário revisar antes de mandar. Fácil de confundir com "não funcionou" —
confira a pasta de rascunhos.

**Sem destinatário, não envia — e avisa.** Não é erro. Ver `destinatarios.md`.
