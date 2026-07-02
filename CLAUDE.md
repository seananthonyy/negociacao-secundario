# Projeto: Relatório Diário de Negociação Secundária de Crédito Privado

Este projeto gera relatórios HTML diários de negócios de crédito privado brasileiro (Debêntures, CRIs, CRAs) consolidando dados da B3, FI Analytics e Anbima.

## Documento mestre

**Toda decisão de projeto está em `PLANEJAMENTO_v5.md` na raiz.** Leia esse arquivo antes de qualquer ação. É a fonte da verdade.

## Estrutura

```
negociacao-secundario/
├── code/                    Projeto Python (todos os paths internos relativos a esta pasta)
│   ├── scripts/             Scripts executáveis independentes (CLI)
│   ├── lib/                 Módulos compartilhados
│   ├── data/                trades.db, logs/, relatorios/
│   ├── templates/           Jinja2 do relatório HTML
│   ├── config.toml          Configurações
│   ├── .env                 Secrets (não versionar)
│   └── requirements.txt
├── vault/                   Obsidian — fonte da verdade viva do projeto
└── .claude/agents/          Subagents customizados (coder, documenter)
```

## Agentes customizados

- **coder** — implementa scripts em `code/scripts/` e módulos em `code/lib/`. Lê o vault para contexto.
- **documenter** — mantém o vault Obsidian sincronizado com o código atual. Atualiza notas em `vault/` conforme o coder progride.

Use `/agents` no Claude Code pra ver/invocar.

## Convenções fechadas (não revisitar sem motivo forte)

- **Python 3.11+**, sem venv, sem tests automatizados
- **SQLite único** em `code/data/trades.db`
- **Paths relativos ao cwd** (projeto vai ser migrado entre PC pessoal e PC trabalho)
- **Tabelas SQLite**: PascalCase (`TradesRaw`, `InfoAtivos`, etc)
- **Colunas SQLite**: camelCase com prefixos `vr` (valor), `cd` (código/categoria), `dt` (data/hora), `id` (identificador)
- **Scripts**: snake_case, sem prefixo numérico (`scrape_b3_boletim.py`, não `01_scrape_...`)
- **Python variáveis**: camelCase (`dtRef`, `cdTicker`, `anbimaRows`, `totalAnbima`)
- **Python funções**: PascalCase (`_ParseArgs`, `_ProcessDate`, `_BuildUrl`, `Main`)
- **Sem CHECK constraints** no schema
- **Cada script é independente**, idempotente, com CLI próprio (`--date` ou `--start --end`)
- **Email no fim de cada script** via Outlook (`pywin32`), sucesso ou erro
- **Filtro de duplicados**: union-find, status só `PRIMARY` ou `DUPLICATE`
- **Cascata calculadoras**: FI Analytics → B3 → NULL (só Deb/CRI/CRA)
- **Match de referência** (`IPCA→NTN-B`, `PREFIXADO→DI Futuro`): script separado, só roda com `--force`
- **Relatório agrupa por `dtLiquidacao`**, não `dtNegocio`

## Estado atual

Veja `vault/09 - Progresso.md` para saber em que fase está a implementação e o que falta.

Veja `vault/98 - Backlog.md` para itens pendentes de decisão. **Leia e mencione ao usuário no início de cada sessão se houver itens relevantes ao trabalho em curso.**

## Workflow de implementação

O projeto é dividido em **fases** (uma por script principal). Em cada fase:

1. Coder lê PLANEJAMENTO_v5.md + notas relevantes do vault
2. Coder implementa o script + dependências em `lib/`
3. Documenter atualiza as notas do vault e o Progresso.md
4. Usuário revisa, aprova, próxima fase

## Como começar

Diga: **"Leia PLANEJAMENTO_v5.md e vault/09 - Progresso.md, depois proponha a próxima fase de implementação"**.

Se for a primeira vez (vault ainda vazio), diga: **"Despache o documenter para popular o vault baseado em PLANEJAMENTO_v5.md, depois comece a Fase 1 com o coder"**.
