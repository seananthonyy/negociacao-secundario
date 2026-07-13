# Projeto: Relatório Diário de Negociação Secundária de Crédito Privado

Este projeto gera relatórios HTML diários de negócios de crédito privado brasileiro (Debêntures, CRIs, CRAs) consolidando dados da B3, FI Analytics e Anbima.

> **Instalando/migrando para o PC do banco?** Se o usuário pedir "me diga o que fazer" / "como instalo isto", siga o runbook **`INSTALACAO_BANCO.md`** (raiz) — passo a passo de pastas, dependências, segredos, teste de fluxos e montagem da base. Contexto da migração em `vault/13 - Migracao Banco.md`.

## Documento mestre

**Toda decisão de projeto está em `PLANEJAMENTO_v5.md` na raiz.** Leia esse arquivo antes de qualquer ação. É a fonte da verdade.

## Estrutura

```
negociacao-secundario/
├── code/                    Projeto Python (todos os paths internos relativos a esta pasta)
│   ├── scripts/             Scripts executáveis independentes (CLI)
│   ├── lib/                 Módulos compartilhados
│   ├── data/                trades.db + ipca.db/di.db (insumos da calc), logs/, relatorios/
│   ├── templates/           Jinja2 do relatório HTML
│   ├── config.toml          Configurações
│   ├── .env                 Secrets (não versionar)
│   └── requirements.txt
├── vault/                   Obsidian — fonte da verdade viva do projeto
└── .claude/agents/          Subagents customizados (coder, documenter)
```

## Calculadora de renda fixa (projeto vizinho)

`D:\ItauBBA\calculadora-renda-fixa` é a **biblioteca de cálculo** (precifica: VNA, PU Par, PU de operação, duration). **Não modificar `calculadora_rf.py` sem permissão explícita do usuário.**

Desde 12/07/2026, **este** projeto roda as rotinas de dados que ela consome (IPCA, projeção de IPCA, DI, curva DI) e valida o fluxo dos ativos que ela pode precificar. Os bancos `data/ipca.db` e `data/di.db` são nossos; o schema deles é **contrato com a calc** (não segue o prefixo `vr/cd/dt` — não renomear). Import via `lib/calc.py`. Ver **[[14 - Rotinas da Calculadora]]** no vault.

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
- **Nomes de arquivo de script**: snake_case, sem prefixo numérico (`scrape_b3_boletim.py`, não `01_scrape_...`). Nome de arquivo é a **única** coisa em snake_case.
- **Python funções e classes**: PascalCase, **em português** (`LerArgumentos`, `ProcessarData`, `MontarUrl`, `AnalisarCsv`, `Principal`)
- **Python variáveis e parâmetros**: camelCase, em português (`dtRef`, `cdTicker`, `anbimaRows`, `limiteTrades`)
- **Constantes de módulo**: UPPER_SNAKE (`SQL_UPSERT`, `MESES_PT`) — o `_` **interno** é permitido
- **NUNCA `_` no início de nome nenhum** (nem função "privada", nem constante, nem variável). `_ParseArgs`, `_SQL_UPSERT`, `_smoke` são todos proibidos.
- **Exceções que ficam em inglês**: jargão de mercado (`vrSpreadOver`, `vrDuration`, `vrPU`, `cdISIN`, `Mtm*`, `Outstanding`, `Broker`, `Yield`), API de terceiros (`parse_args`, `status_code`, `format_exc`), nomes de módulo/arquivo, e **chaves de contrato** (colunas do banco, variáveis do template Jinja como `data_json`).
- **`dest=` explícito no argparse** sempre que a flag tiver hífen (`--email-dia` → `dest="emailDia"`), senão o argparse gera `email_dia` em snake_case e quebra a convenção — e o mismatch só aparece em runtime.
- **Sem CHECK constraints** no schema
- **Cada script é independente**, idempotente, com CLI próprio (`--date` ou `--start --end`)
- **Email no fim de cada script** via Outlook (`pywin32`), sucesso ou erro
- **Filtro de duplicados**: union-find, status só `PRIMARY` ou `DUPLICATE`
- **Cascata calculadoras**: FI Analytics → B3 → NULL (só Deb/CRI/CRA)
- **Match de referência** (`IPCA→NTN-B`, `PREFIXADO→DI1`): script separado (`match_referencias.py`), sem args — roda idempotente sobre a base toda a cada ciclo do pipeline. Duration-match data-exata contra `MtmAnbima`; não sobrescreve refs da Anbima (`cdFonteReferencia='Anbima'`)
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
