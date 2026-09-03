# config.py

`codigos/helpers/config.py` — **configuração e segredos**.

---

## Overview

Lê o `config/config.toml`, resolve segredos a partir de variáveis de ambiente, e — o mais
importante — **ancora todo caminho na raiz do projeto**, nunca no diretório de onde o
script foi chamado.

Sem a âncora, um script rodado de outra pasta trabalha silenciosamente sobre outro lugar:
a base nasce numa raiz errada e o script termina "com sucesso", sobre nada. Já aconteceu.

---

## API pública

| símbolo | o que é |
|---|---|
| `RAIZ` | A raiz do projeto — três níveis acima deste arquivo |
| `DIR_CONFIG` | `RAIZ / "config"` — onde moram o `config.toml` e o `.env` |
| `cfg` | Proxy preguiçoso: age como dict, carrega o TOML na primeira leitura |
| `ObterCfg()` | O dict do config, já com os caminhos ancorados |
| `ObterEnv(chave, default)` | Uma variável de ambiente, garantindo o `.env` carregado |
| `ObterSegredo(chaveLogica, default)` | **O resolvedor.** Ver abaixo |
| `ObterListaEmails(qual)` | Lista de destinatários, de variável ou do módulo |
| `AplicarProxyEnv()` | Exporta `HTTP_PROXY`/`HTTPS_PROXY` para o processo |
| `ObterProxyPlaywright()` | Dict de proxy no formato do Playwright, ou `None` |
| `AncorarPaths(conf)` | Torna cada `[paths]` absoluto |
| `PATHS_NAO_ANCORADOS` | As chaves que escapam da âncora (hoje só `calculadoraDir`) |

### Como `ObterSegredo` funciona

O bloco `[env]` do `config.toml` mapeia cada segredo para uma **lista de nomes** de
variável de ambiente. O resolvedor tenta cada candidato na ordem e usa o primeiro
preenchido.

```toml
[env]
b3CalcToken = ["token_calc_B3", "B3_CALC_TOKEN"]
```

**Por que uma lista:** no PC pessoal os segredos vêm de um `.env` com nomes próprios; no
banco vêm das variáveis da conta, com outros nomes. Os dois convivem sem editar arquivo
nenhum ao trocar de máquina.

**Apenas NOMES entram no config** — nunca valores. É o que permite o repositório ser
público.

---

## Invariantes

**1. Todo `[paths]` é ancorado na raiz.** Caminho já absoluto no config é respeitado
(`RAIZ / absoluto` devolve o absoluto). A exceção declarada é `calculadoraDir`, que tem
resolução própria no `calc.py` — a variável de ambiente do banco tem precedência.

**2. `[dados] raiz` segue a mesma regra, com uma exceção:** se for um bucket (`s3://...`)
não é caminho de disco e passa intacto.

**3. `load_dotenv` não sobrescreve.** Variável já definida no ambiente vence o `.env`
(`override=False`). É o que faz o banco ignorar um `.env` que porventura exista.

**4. O carregamento é preguiçoso.** `cfg` só lê o TOML na primeira leitura de chave —
importar o módulo não toca em disco.

---

## Quem consome

**Praticamente tudo.** Todos os scripts e quase todos os helpers.

---

## Armadilhas

**`RAIZ` depende da profundidade do arquivo.** É `parent.parent.parent` porque este módulo
mora em `codigos/helpers/`. Mover o arquivo de nível sem ajustar a contagem faz a base
inteira nascer no lugar errado — e sem erro.

**Caminho relativo no config é relativo à RAIZ, não ao cwd.** É o ponto do módulo. Quem
escrever um caminho novo no `config.toml` deve escrevê-lo a partir da raiz do projeto.

**Segredo ausente devolve `None`, não levanta.** Quem depende dele precisa tratar — o
`b3_calc_api`, por exemplo, falha na primeira chamada em vez de na importação, o que é
mais difícil de diagnosticar. Se um script novo depende de um segredo obrigatório, confira
na entrada.
