# Relatório Secundário de Crédito Privado

Projeto Python local que gera relatórios diários HTML de negociação secundária de Debêntures, CRIs e CRAs.

## Setup inicial (uma vez)

```powershell
# 1. Instalar Python 3.11+ e Outlook (já tem)

# 2. Instalar dependências (rode em code/)
cd code
pip install -r requirements.txt
playwright install chromium

# 3. Configurar secrets
copy .env.example .env
# Edite .env preenchendo as credenciais
```

## Como rodar (depois de implementado)

Veja `vault/02 - Como Rodar.md` quando estiver pronto.

## Como continuar o desenvolvimento

Este projeto usa Claude Code com dois subagents:

1. Abra terminal na raiz do projeto (`D:\ItauBBA\negociacao-secundario\`)
2. Rode: `claude`
3. Diga: **"Leia CLAUDE.md e vault/09 - Progresso.md, depois proponha a próxima fase"**

Se quiser usar um modelo específico:
```
claude --model claude-sonnet-4-6
```
Ou dentro do Claude Code: `/model`

## Por onde começar a entender o projeto

| Você quer… | Abra |
|---|---|
| Entender o sistema visualmente, em 10 minutos | **`guia_projeto.html`** (abra no navegador — funciona offline) |
| Dar contexto completo a um agente de IA | **`CONTEXTO_PROJETO.md`** (arquitetura + dicionário de dados + motor) |
| Saber por que uma decisão foi tomada | `PLANEJAMENTO_v5.md` (documento mestre) |
| Instalar no PC do banco | `INSTALACAO_BANCO.md` |

## Estrutura

- `guia_projeto.html` — guia visual do fluxo de dados (leitura humana)
- `CONTEXTO_PROJETO.md` — contexto técnico completo + dicionário de dados
- `requirements.txt` — dependências (fonte única; `code/requirements.txt` aponta para cá)
- `CLAUDE.md` — contexto auto-carregado pelo Claude Code (não editar à toa)
- `PLANEJAMENTO_v5.md` — documento mestre de planejamento (fonte da verdade)
- `vault/` — Obsidian (abra essa pasta como vault no Obsidian)
- `code/` — projeto Python
- `docs/` — relatórios de fase e backlogs técnicos
- `.claude/agents/` — subagents customizados (coder, documenter)

## Migrar pra outro PC

1. Copie a pasta `D:\ItauBBA\negociacao-secundario\` inteira
2. No PC novo, rode `pip install -r requirements.txt` e `playwright install chromium`
3. Recrie `code/.env` (não vem na cópia, contém secrets)
4. Pronto
