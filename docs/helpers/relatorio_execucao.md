# relatorio_execucao.py

`codigos/helpers/relatorio_execucao.py` — **o resumo estruturado que vai para o email**.

---

## Overview

Cada script acumula o que aconteceu num objeto `RelatorioExecucao` e, no fim, entrega-o ao
`email_outlook`, que o renderiza em HTML com a paleta do Itaú.

**Por que um objeto e não texto solto:** o email de conclusão é o único lugar onde alguém
olha o resultado de uma rodada agendada. Sem estrutura, cada script formataria à sua
maneira e o leitor teria de reaprender o layout a cada mensagem.

---

## API pública

`RelatorioExecucao(nomeScript, args=None)` — os argumentos passados aparecem no cabeçalho,
o que torna o email auto-explicativo sobre o que foi rodado.

| método | para quê |
|---|---|
| `Datas(datas)` | As datas processadas na rodada |
| `Contar(acao, n=1)` | Incrementa um contador: `inseridos`, `atualizados`, `deletados`, `ignorados`, `falhas` |
| `Exemplo(acao, linha)` | Guarda uma amostra daquela ação (até `LIMITE_EXEMPLOS`) |
| `Metrica(nome, valor)` | Um número avulso, com rótulo |
| `Secao(titulo, cabecalho, linhas)` | Uma tabela |
| `PorData(titulo, colunas, linhas)` | Tabela por data — o formato mais comum |
| `Aviso(texto)` | Destaque em laranja |
| `Erro(texto)` | Destaque em vermelho |
| `Texto()` | O resumo em texto puro, para o log |

---

## Invariantes

**1. As cinco ações são fixas:** `inseridos`, `atualizados`, `deletados`, `ignorados`,
`falhas`. Um script que precisa de outra contagem usa `Metrica`.

**2. Exemplos são limitados a 10.** Um email com mil linhas de exemplo não é lido por
ninguém.

**3. O relatório é montado mesmo quando a rodada falha.** O bloco `finally` de cada script
chama `EnviarEmailConclusao` com o que houver — um traceback sem contexto é bem pior que
um resumo parcial com traceback.

---

## Quem consome

**Todos os 24 scripts.**

---

## Armadilhas

**`Erro()` não interrompe nada.** Ele só marca o email. Quem quer abortar levanta exceção
— e o `finally` do script cuida do email.

**Métrica com valor `None` some do email.** Se um número importante não aparece, confira se
ele não é `None` na origem.
