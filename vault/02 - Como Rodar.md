# Como Rodar

> Ver também: [[00 - Inicio]] | [[03 - Estrutura de Pastas]] | [[99 - Credenciais e Links]]

## Pré-requisitos

| Requisito | Versão mínima | Observação |
|---|---|---|
| Python | 3.11+ | Sem venv obrigatório |
| Playwright (Chromium) | qualquer | Instalar após o pip |
| Microsoft Outlook | qualquer | Instalado e configurado com conta ativa |
| pywin32 | via requirements | Necessário para envio de email pelo Outlook |

### Instalação das dependências

Execute a partir da pasta `code/`:

```powershell
pip install -r requirements.txt
playwright install chromium
```

### Configurar credenciais

Segredos são resolvidos por `lib.config.ObterSegredo()` via bloco `[env]` do `config.toml` (ver [[99 - Credenciais e Links]] e [[13 - Migracao Banco]] §3):

- **PC pessoal:** crie `code/.env` (nunca versionar) a partir de `code/.env.example` com os nomes canônicos:
  ```
  FIANALYTICS_USER=<email FI Analytics>
  FIANALYTICS_PASS=<senha FI Analytics>
  FIANALYTICS_API_KEY=<API key FI Analytics>
  B3_CALC_TOKEN=<token B3 Calculator>
  OUTLOOK_TO=<email destino das notificações>
  ```
- **Banco:** os segredos vêm de variáveis de ambiente da conta (`token_calc_B3`, `token_fianalytics`, `user_fianalytics`, `password_fianalytics`, `proxy_http/https`) — sem `.env`.

**Emails de destinatários:** crie `code/destinatarios.py` (não versionado) a partir de `code/destinatarios.example.py`.

**Antes de publicar no GitHub:** rode `python scripts/check_no_secrets.py` (trava anti-vazamento).

---

## Rotina de fechamento diário

Todos os comandos são executados a partir da pasta `code/`. Substitua as datas conforme o dia de trabalho.

### Exemplo: fechamento do dia 03/06/2026

```powershell
# 1. Boletim B3 — baixar D-1 (trades D+1 que liquidam hoje) e o dia atual
python scripts/scrape_b3_boletim.py --date 2026-06-02
python scripts/scrape_b3_boletim.py --date 2026-06-03

# 2. Infos estáticas dos ativos (FI Analytics)
python scripts/scrape_fianalytics_planilha.py

# 3. Taxas indicativas Anbima
python scripts/scrape_anbima_debentures.py --date 2026-06-03
python scripts/scrape_anbima_cri_cra.py    --date 2026-06-03

# 4. Calcular taxas — DEVE rodar antes de filtrar_trades
#    (popula NegociosProcessados; filtrar_trades só atualiza cdStatus nos registros existentes)
python scripts/calc_taxa_negocios.py --date 2026-06-03

# 5. Filtrar duplicados
python scripts/filtrar_trades.py --date 2026-06-03

# 6. Taxas de referência — X-1 E X (spread usa dtNegocio, não dtLiquidacao)
#    Trades negociados em X-1 precisam da curva de X-1 para ter spread calculado.
python scripts/scrape_anbima_ntnb.py  --date 2026-06-02
python scripts/scrape_b3_curva_di.py  --date 2026-06-02
python scripts/scrape_anbima_ntnb.py  --date 2026-06-03
python scripts/scrape_b3_curva_di.py  --date 2026-06-03
python scripts/calc_spread_anbima.py  --date 2026-06-03

# 7. Match de referência (atribui cdReferencia em InfoAtivos via duration-match)
python scripts/match_referencias.py

# 8. Spread over (calcula vrSpreadOver em NegociosProcessados)
python scripts/calc_spread_over.py --date 2026-06-03

# 9. Gerar relatório
python scripts/gerar_relatorio_html.py --date 2026-06-03 --mode definitivo
```

**Ordem obrigatória — calc_taxa ANTES de filtrar:** `calc_taxa_negocios.py` insere os registros em `NegociosProcessados`. `filtrar_trades.py` apenas atualiza o campo `cdStatus` nesses registros. Se `filtrar_trades` rodar antes de `calc_taxa`, encontra `NegociosProcessados` vazio e não filtra nada.

**match_referencias ANTES de calc_spread_over:** `calc_spread_over.py` lê `InfoAtivos.cdReferencia` para calcular o spread. Se `match_referencias` não tiver rodado, ativos IPCA/PREFIXADO sem ref da Anbima ficam com `vrSpreadOver = NULL`.

**Regra do boletim B3:** para o relatório do dia X (dtLiquidacao), sempre baixar boletim de **X-1 e X**. Trades negociados em X-1 com liquidação D+1 (= X) só aparecem no boletim de X-1. Se X-1 não tiver pregão (feriado/fim de semana), o scraper retorna zero registros — sem problema. Nunca pular X-1 por julgamento próprio.

---

## Reprocessar uma janela histórica

Para recalcular taxas, spreads e filtros de duplicados em um período passado:

```powershell
python scripts/calc_taxa_negocios.py  --start 2026-05-01 --end 2026-05-27
python scripts/filtrar_trades.py      --start 2026-05-01 --end 2026-05-27
python scripts/calc_spread_anbima.py  --start 2026-05-01 --end 2026-05-27 --force
python scripts/match_referencias.py
python scripts/calc_spread_over.py    --start 2026-05-01 --end 2026-05-27
```

Para forçar recálculo dos spreads, use `--force` em `calc_spread_anbima.py`. `match_referencias.py` não aceita `--start/--end` — processa todos os ativos elegíveis de uma vez.

Os scripts de scraping também aceitam `--start --end` se precisar rebaixar o boletim B3 ou Anbima para um período mais longo.

---

## Onde ficam os relatórios gerados

```
code/data/relatorios/
└── YYYY-MM-DD/
    ├── previa_HHMM.html
    └── definitivo.html
```

Cada rodada de prévia cria um arquivo com timestamp no nome (para manter histórico intradiário). O definitivo é sempre sobrescrito.

---

## Dicas de troubleshooting

- **Playwright não abre o browser**: verifique se o Chromium foi instalado com `playwright install chromium`.
- **Email não envia**: confirme que o Outlook está aberto e logado. O script usa COM via `pywin32`.
- **Taxa NULL no relatório**: significa que nem FI Analytics nem B3 Calculator conseguiram calcular. Ver [[06 - Calculadoras/FI Analytics API]] e [[06 - Calculadoras/B3 Calculator API]].
- **`cdReferencia` vazio para um ticker**: rode `match_referencias.py --force` para tentar o match automático. Se falhar, preencha manualmente em `InfoAtivos`.
