# destinatarios.py

`codigos/helpers/destinatarios.py` — **os emails internos**. ⚠️ **Não versionado.**

---

## Overview

Um módulo de uma variável, que existe só para manter endereços de email fora do
repositório público.

```python
EMAIL_DESTINATARIOS = ["fulano@empresa.com.br", "beltrano@empresa.com.br"]
```

O `destinatarios.example.py` — esse sim versionado — mostra o formato.

---

## API pública

| símbolo | o que é |
|---|---|
| `EMAIL_DESTINATARIOS` | Lista de endereços do rascunho do relatório do dia |

---

## Invariantes

**1. Nunca versionar.** Está no `.gitignore` e na lista `PROTEGIDOS` do extrator do
bundle, que se recusa a sobrescrevê-lo ao importar uma versão nova no banco.

**2. É o fallback, não a fonte primária.** `config.ObterListaEmails("destinatarios")`
tenta primeiro a variável de ambiente `EMAIL_DESTINATARIOS` e só cai neste módulo se ela
não existir. No banco, a variável da conta vence.

---

## Quem consome

`config.ObterListaEmails`, chamado pelo `gerar_relatorio_credito --email-dia`.

---

## Armadilhas

**Ausência é silenciosa por design.** Sem o módulo e sem a variável, o
`gerar_relatorio_credito --email-dia` registra um aviso e **não envia** — em vez de
quebrar. É o certo: a falta de destinatário não deve derrubar a geração do relatório.
Mas significa que um email não enviado pode passar despercebido; confira o log.
