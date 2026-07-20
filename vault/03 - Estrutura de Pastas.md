# Estrutura de Pastas

> Ver também: [[00 - Inicio]] | [[04 - Banco de Dados]] | [[02 - Como Rodar]]

Todo o projeto Python fica dentro de `code/`. Os paths internos dos scripts são sempre relativos ao cwd (pasta `code/`), para facilitar a migração entre máquinas.

## A calculadora fica FORA daqui

`calculadora_rf.py` vive em **outro repositório** (`calculadora-renda-fixa`) e **não é copiado para dentro deste**. O padrão é ela ser **irmã** da raiz:

```
<pasta-qualquer>├── negociacao-secundario\code\...     ← este projeto
└── calculadora-renda-fixa\             ← a calc
```

Só o **`lib/calc.py`** sabe onde ela está (`config.toml [paths] calculadoraDir`, sobrescrevível pela variável de ambiente **`CALCULADORA_DIR`**). Os insumos dela (`ipca.db`, `di.db`, `feriados_anbima.csv`) vivem em **`code/data/`** — deste projeto. Ver [[14 - Rotinas da Calculadora]] e `INSTALACAO_BANCO.md`.

```
negociacao-secundario/
├── code/
│   ├── scripts/                     Executáveis independentes, com CLI próprio. 24 no total.
│   │   │
│   │   │   ─── COLETA ───
│   │   ├── scrape_b3_boletim.py             Boletim Diário B3 → NegociosBrutos
│   │   ├── scrape_b3_bond_details.py        ★ FONTE PRIMÁRIA do cadastro/fluxo → InfoAtivos + FluxoAtivos
│   │   ├── scrape_anbima_data_ativos.py     Fallback do cadastro (só o que a B3 não cobriu)
│   │   ├── scrape_fianalytics_planilha.py   Planilha FI Analytics → InfoAtivos  ⚠️ QUEBRADO (backlog)
│   │   ├── scrape_anbima_debentures.py      Taxa indicativa deb → AnbimaIndicativos
│   │   ├── scrape_anbima_cri_cra.py         Taxa indicativa CRI/CRA → AnbimaIndicativos
│   │   ├── scrape_anbima_ntnb.py            MtM NTN-B → MtmAnbima
│   │   ├── scrape_b3_curva_di.py            MtM curva DI → MtmAnbima + di.db/CurvaDi
│   │   ├── scrape_outstanding_bloomberg.py  Outstanding (xbbg — só roda no banco)
│   │   │
│   │   │   ─── INSUMOS DA CALCULADORA ───
│   │   ├── scrape_ipca_ibge.py              IPCA realizado → ipca.db/IPCA
│   │   ├── scrape_ipca_projetado_anbima.py  Projeção de IPCA → ipca.db/IPCAProjetado
│   │   ├── scrape_di_bcb.py                 DI realizado → di.db/DiHistorico
│   │   │
│   │   │   ─── VALIDAÇÃO E CÁLCULO ───
│   │   ├── validar_fluxos.py                Quais ativos a calc pode precificar + tripwire de saldo
│   │   ├── conferir_pu.py                   ★ Portão de aceitação da precificação local
│   │   ├── calc_taxa_negocios.py            Taxa por trade (Calc local → FI → B3)
│   │   ├── filtrar_trades.py                VALIDO / FUNDO / BROKER / PF → cdStatus
│   │   ├── calc_spread_anbima.py            Spread das indicativas Anbima
│   │   ├── match_referencias.py             cdReferencia (NTN-B / DI1) por duration-match
│   │   ├── calc_spread_over.py              vrSpreadOver em bps
│   │   │
│   │   │   ─── RELATÓRIO E ORQUESTRAÇÃO ───
│   │   ├── gerar_relatorio_html.py          Relatório diário
│   │   ├── gerar_relatorio_credito.py       Relatório histórico (6 abas)
│   │   ├── pipeline_core.py                 A cadeia dos 19 passos (importado, não executado)
│   │   ├── run_diario.py                    Entrypoint da rotina (Task Scheduler)
│   │   └── check_no_secrets.py              Gate pré-publicação no GitHub
│   │
│   ├── lib/                         Módulos compartilhados
│   │   ├── db.py                        Conexão SQLite + bootstrap do schema + triggers
│   │   ├── config.py                    config.toml + segredos via [env] (ObterSegredo)
│   │   ├── calc.py                      ★ Ponte com a calculadora de renda fixa
│   │   ├── relatorio_execucao.py        ★ Email HTML de fim de script (paleta Itaú)
│   │   ├── email_outlook.py             Envio via Outlook COM (pywin32)
│   │   ├── logger.py                    Logging padronizado
│   │   ├── fianalytics_api.py           Cliente FI Analytics
│   │   └── b3_calc_api.py               Cliente B3 Calculator
│   │
│   ├── data/                        Dados gerados — NÃO versionar
│   │   ├── trades.db                    Banco principal
│   │   ├── ipca.db, di.db               Insumos da calculadora (schema é CONTRATO — não renomear)
│   │   ├── feriados_anbima.csv          Calendário Anbima — fonte ÚNICA (a calc lê esta)
│   │   ├── emails/                      Corpo dos emails quando NEGSEC_SEM_EMAIL=1
│   │   ├── logs/                        Logs por script e data
│   │   └── relatorios/                  HTML gerado
│   │
│   ├── templates/                   Jinja2 do relatório
│   ├── setup_teste.ipynb            Smoke test: 1 bloco por fluxo, 1 pregão
│   ├── setup_inicial.ipynb          Carga histórica
│   ├── run_secundario.ipynb         Rotina diária
│   ├── config.toml                  Configurações
│   ├── destinatarios.py             Emails (NÃO versionar — .example viaja)
│   ├── .env                         Segredos no PC pessoal (NÃO versionar)
│   └── requirements.txt
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
