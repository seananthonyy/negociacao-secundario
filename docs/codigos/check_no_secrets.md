# check_no_secrets.py

**Ferramenta.** A trava de segurança pré-publicação.

---

## Overview

Rode **antes de subir qualquer coisa para o GitHub** — o repositório é público. Sai `1` se
encontrar, nos arquivos que iriam para o repo:

1. **Valores de segredo reais**, lidos do ambiente/`.env` pelo resolvedor de config;
2. **Arquivos proibidos soltos** (`.env`, `destinatarios.py`, `*.db`);
3. **Padrões suspeitos** de credencial escrita no código.

O script **não contém segredo nenhum** — ele lê os valores em runtime e procura por eles.

---

## Regras de negócio

**Rode no PC pessoal, com o `.env` preenchido.** É lá que a checagem de valores faz
sentido: sem os segredos carregados, ele não tem o que procurar e passa por engano.

Varre só o que iria para o repositório — respeita o `.gitignore`.

---

## CLI

```powershell
python codigos\scripts\check_no_secrets\check_no_secrets.py
```

Sem argumentos. Imprime quantos arquivos verificou e o veredito.

---

## Interação com a base

Nenhuma.

---

## Detalhes técnicos

Usa `config.ObterSegredo` para obter os valores a procurar, e varre os arquivos por
conteúdo. `ROOT` é a raiz do projeto, três níveis acima do script.

---

## Armadilhas

**Passar sem `.env` é falso negativo.** Se o resolvedor não achar nenhum segredo, não há o
que procurar — e o script passa. Ele avisa quantos segredos carregou; confira que não é
zero.

**Ele não substitui o `.gitignore`.** É a segunda linha de defesa, não a primeira: um
arquivo proibido que já foi commitado antes continua no histórico, e o script só olha o
estado atual.

**O escopo vem do git, e isso não é detalhe.** Até 03/09/2026 a lista de exclusão era
escrita à mão, com o comentário *"espelha o .gitignore"* — e derivou: a trava reprovou um
HTML de documentação com a chave da FI Analytics que o git já ignorava havia meses. Não era
vazamento; era ruído. **Alarme que dispara no caso legítimo ensina a ignorar o alarme**, e
uma trava de segurança ignorada não é trava. Hoje o conjunto vem de `git ls-files` mais
`git ls-files --others --exclude-standard`, que é literalmente "o que seria commitado".

**Os dois repositórios são NOMEADOS, não descobertos.** A pasta irmã tem outros projetos, e
varrê-los faria a trava reprovar por código que não tem nada a ver com esta publicação. O
segundo repo sai de `[paths] calculadoraDir`, e só entra se de fato tiver `.git`.
