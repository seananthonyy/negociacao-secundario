# destinatarios.example.py

`codigos/helpers/destinatarios.example.py` — **o modelo do arquivo de emails**.

---

## Overview

Existe para uma razão só: mostrar o formato do `destinatarios.py`, que **não é
versionado**. Numa máquina nova, copia-se este arquivo, tira-se o `.example` e preenche-se.

---

## API pública

Nenhuma. É um modelo, não um módulo importado por ninguém.

```python
EMAIL_DESTINATARIOS = ["nome@empresa.com.br"]
```

---

## Invariantes

**1. Nunca conter endereço real.** Se contiver, vaza para o repositório público — é
exatamente o que ele existe para evitar.

**2. O `check_no_secrets` varre este arquivo.** Um email real aqui derruba a checagem
antes do push.

---

## Quem consome

Ninguém, em runtime. É documentação executável.

---

## Armadilhas

**Copiar sem renomear não funciona.** O `config.ObterListaEmails` procura
`destinatarios.py`, não `destinatarios.example.py`. O sintoma é o relatório do dia não
enviar e registrar aviso — silencioso o bastante para passar despercebido.
