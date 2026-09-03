# logger.py

`codigos/helpers/logger.py` — **o log de cada script**.

---

## Overview

Devolve um `logging.Logger` que escreve ao mesmo tempo no console e num arquivo por
execução, dentro da pasta do próprio script.

Um arquivo por **sessão**, não por dia: o timestamp é fixado no import do módulo
(`TS_SESSAO`), então todos os passos de uma mesma rodada compartilham o instante no nome,
e é fácil juntar o que aconteceu numa execução.

---

## API pública

| função | devolve |
|---|---|
| `ObterLogger(name)` | O logger do script, já com handler de arquivo e console |

`name` é o nome do script (`"scrape_b3_boletim"`). O arquivo vai para
`codigos/scripts/<name>/logs/<timestamp>.log`.

Quando a pasta do script não existe — uso avulso, notebook — cai em `cache/logs/<name>/`.

---

## Invariantes

**1. O log mora ao lado do script.** É proposital: quem abre a pasta de um script vê a
história dele. O `.gitignore` ignora `codigos/scripts/*/logs/`, e não a pasta do script,
porque ali também vivem a skip-list e o template, que são versionados.

**2. Um arquivo por sessão, não por chamada.** Chamar `ObterLogger` duas vezes no mesmo
processo devolve o mesmo logger, sem duplicar handler.

---

## Quem consome

**Todos os 24 scripts.** Os helpers não logam por conta própria — recebem o logger de
quem os chama, quando precisam.

---

## Armadilhas

**O console do Windows não é UTF-8 por padrão.** Acento em mensagem de log sai como `?` ou
`�` no terminal, mas o arquivo está correto. Não é bug — é o code page do console. Se
precisar ler acentuado, olhe o arquivo.

**Logger é por nome, e o nome vira pasta.** Passar um nome que não corresponde a um script
cria uma pasta em `cache/logs/`. Use sempre a constante `NOME_SCRIPT` do próprio script.
