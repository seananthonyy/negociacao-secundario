# Estrutura de Pastas

> Ver também: [[00 - Inicio]] | [[04 - Banco de Dados]] | [[02 - Como Rodar]]

Todo o projeto Python fica dentro de `code/`. Os paths internos dos scripts são sempre relativos ao cwd (pasta `code/`), para facilitar a migração entre máquinas.

```
negociacao-secundario/
├── code/
│   ├── scripts/                     Scripts executáveis — cada um é independente e tem CLI próprio
│   │   ├── scrape_b3_boletim.py         Baixa e carrega o Boletim Diário B3 → NegociosBrutos
│   │   ├── scrape_fianalytics_planilha.py  Baixa planilha FI Analytics → InfoAtivos
│   │   ├── scrape_anbima_debentures.py  Taxas indicativas Anbima debêntures → AnbimaIndicativos
│   │   ├── scrape_anbima_cri_cra.py     Taxas indicativas Anbima CRI/CRA → AnbimaIndicativos
│   │   ├── calc_taxa_negocios.py        Cascata de calculadoras (FI Analytics → B3) → NegociosProcessados
│   │   ├── match_referencias.py         Associa cdReferencia (NTN-B / DI Futuro) por duration-match
│   │   ├── calc_spread_over.py          Calcula vrSpreadOver em bps → NegociosProcessados
│   │   ├── filtrar_trades.py            Três filtros (FUNDO/CORRETOR/PF): marca cdStatus → NegociosProcessados
│   │   └── gerar_relatorio_html.py      Renderiza HTML final (prévia ou definitivo)
│   │
│   ├── lib/                         Módulos compartilhados — importados pelos scripts
│   │   ├── db.py                        Conexão SQLite + auto-bootstrap do schema DDL
│   │   ├── config.py                    Lê config.toml e variáveis de ambiente (.env)
│   │   ├── email_outlook.py             Envio de email via Outlook COM (pywin32)
│   │   ├── logger.py                    Configuração de logging padronizado
│   │   ├── fianalytics_api.py           Cliente da API FI Analytics (calculadora + bondbuilder)
│   │   └── b3_calc_api.py               Cliente da API B3 Calculator (Bearer token + calcYield)
│   │
│   ├── data/                        Dados gerados — não versionar
│   │   ├── trades.db                    Banco SQLite único com todas as tabelas
│   │   ├── feriados_anbima.csv          Calendário de feriados Anbima (2001–2099), 1264 datas
│   │   ├── logs/                        Arquivos de log rotacionados por data
│   │   └── relatorios/
│   │       └── YYYY-MM-DD/
│   │           ├── previa_HHMM.html     Prévia do dia (pode haver múltiplas por dia)
│   │           └── definitivo.html      Definitivo do dia anterior
│   │
│   ├── templates/
│   │   └── relatorio.html.j2            Template Jinja2 do relatório HTML
│   │
│   ├── config.toml                  Configurações de parâmetros (URLs, janelas, tolerâncias)
│   ├── .env                         Credenciais (não versionar — ver .env.example)
│   ├── .env.example                 Template do .env sem valores reais
│   └── requirements.txt             Dependências Python
│
├── vault/                           Obsidian — documentação viva do projeto
│   ├── 00 - Inicio.md
│   ├── 01 - O Que Faz.md
│   ├── 02 - Como Rodar.md
│   ├── 03 - Estrutura de Pastas.md
│   ├── 04 - Banco de Dados.md
│   ├── 05 - Fontes/
│   │   ├── B3 Boletim.md
│   │   ├── FI Analytics.md
│   │   └── Anbima.md
│   ├── 06 - Calculadoras/
│   │   ├── FI Analytics API.md
│   │   └── B3 Calculator API.md
│   ├── 07 - Filtro de Duplicados.md
│   ├── 08 - Match de Referencia.md
│   ├── 09 - Progresso.md
│   └── 99 - Credenciais e Links.md
│
├── .claude/
│   └── agents/                      Definições dos subagentes customizados
│       ├── coder.md
│       └── documenter.md
│
├── CLAUDE.md                        Instruções do projeto para os agentes Claude
└── PLANEJAMENTO_v5.md               Documento mestre — fonte da verdade do projeto
```

---

## Notas importantes

- **Nenhum script usa path absoluto** — todos partem do cwd (`code/`).
- **`data/`** não é versionado: contém o banco SQLite, logs e HTMLs gerados.
- **`config.toml`** versiona parâmetros não-secretos (URLs, janelas, tolerâncias).
- **`.env`** nunca versionar — contém credenciais. Use `.env.example` como modelo.
- **`lib/db.py`** faz o bootstrap automático do schema na primeira execução — não é preciso criar o banco manualmente. Ver [[04 - Banco de Dados]].
