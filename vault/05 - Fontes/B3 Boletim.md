# B3 Boletim Diário — scrape_b3_boletim.py

> Ver também: [[../04 - Banco de Dados]] | [[../06 - Calculadoras/FI Analytics API]] | [[../07 - Filtro de Duplicados]]

---

## O que faz

Baixa o boletim diário de negócios da B3 para Debêntures, CRIs e CRAs via Playwright, filtra por instrumento e faz UPSERT em `NegociosBrutos`. Aceita uma data única ou um intervalo. Ao final (sucesso ou erro) envia email via Outlook.

---

## Como rodar

```powershell
# Um único dia (abre janela do browser por padrão)
python scripts/scrape_b3_boletim.py --date 2026-05-27

# Intervalo de dias
python scripts/scrape_b3_boletim.py --start 2026-05-25 --end 2026-05-27

# Modo headless (sem janela visível)
python scripts/scrape_b3_boletim.py --date 2026-05-27 --headless

# Só salva HTML/PNG de debug, não grava nada no banco
python scripts/scrape_b3_boletim.py --date 2026-05-27 --debug-only
```

`--start` e `--end` são obrigatórios juntos; `--date` e `--start` são mutuamente exclusivos.

---

## Como a página funciona (descoberto em 2026-05-30)

- A URL pública (`b3.com.br/...boletim-diario/...`) redireciona para um iframe hospedado em `arquivos.b3.com.br/bdi/tabelas?lang=pt-BR`
- O download real é um POST para `https://arquivos.b3.com.br/bdi/table/export/csv?lang=pt-BR` com body JSON
- Body padrão confirmado:
  ```json
  {"Name": "Trade", "Date": "YYYY-MM-DD", "FinalDate": "YYYY-MM-DD", "ClientId": "", "Filters": {}}
  ```
- Para ranges, o script tenta primeiro um único POST com `Date != FinalDate`; se o servidor retornar HTML (ou resposta inválida), cai para loop data por data
- CSV: delimitador `";"`, encoding `utf-8-sig` (com BOM), cerca de 7 linhas de preamble descritivo antes do header real
- A coluna "Origem negócio" está sempre preenchida com "Pré-registro - Voice" para DEB/CRI/CRA — ignorada no mapeamento

---

## Fluxo de interação Playwright

1. Navega diretamente para o iframe `arquivos.b3.com.br/bdi/tabelas?lang=pt-BR` (mais estável que partir da página principal da B3)
2. Clica na aba "Renda fixa" (via `get_by_text`, com fallback por `wai-aria`)
3. Seta a data via API JS do `duet-date-picker` (`dp.setValue(date_str)` + evento `duetChange`); fallback: digita no input visível (`.duet-date__input`) com `triple_click` + `type`
4. Seleciona "Negócio a negócio" (`Trade@true`) no `select#selectTabelas`; tenta os values `Trade@true`, `Trade`, `trade`, `TRADE` e o label `"Negócio a negócio"` em sequência
5. Clica no botão CSV (`span.b3__ico--csv`) e captura o download via `page.expect_download()` — tenta 10 seletores candidatos em cascata
6. Fallback: POST direto no endpoint `/bdi/table/export/csv` com body JSON (usa body interceptado durante o click se disponível, senão usa bodies candidatos padrão)
7. Fallback 2: usa CSV interceptado nas responses de rede (listener `page.on("response")` captura qualquer response com `content-type: csv` ou `octet-stream` vinda de `arquivos.b3.com.br`)

Após cada passo importante, salva screenshot e HTML em `data/debug/`.

---

## Mapeamento de colunas (confirmado em produção)

| Coluna no CSV da B3 | Campo em `NegociosBrutos` |
|---|---|
| `Instrumento financeiro` | `cdInstrumento` |
| `Emissor` | `cdEmissor` |
| `Código IF` | `cdTicker` |
| `Quantidade negociada` | `vrQuantidade` (INTEGER) |
| `Preço negócio` | `vrPU` (REAL) |
| `Volume financeiro (R$)` | `vrVolume` (REAL) |
| `Taxa negócio` | `vrTaxaNegocio` (REAL, nullable) |
| `Horário negócio` | `dtHorarioNegocio` (TEXT `HH:MM:SS`) |
| `Data negócio` | `dtNegocio` (TEXT `YYYY-MM-DD`) |
| `Cód. identificador do negócio` | `cdIdentificadorNegocio` (PK) |
| `Código ISIN` | `cdISIN` (TEXT, nullable) |
| `Data liquidação` | `dtLiquidacao` (TEXT `YYYY-MM-DD`) |
| `Situação negócio` | `cdSituacao` (TEXT) |

O header do CSV é localizado dinamicamente buscando qualquer coluna do mapeamento — não depende de número fixo de linhas de preamble.

---

## Logs e debug

- Logs em: `data/logs/scrape_b3_boletim/{YYYY-MM-DD_HHMMSS}.log`
- Debug HTML/PNG em: `data/debug/` — prefixos `01_bdi_inicial_`, `02_apos_renda_fixa_`, `03_apos_data_`, `04_apos_select_tabela_`, `05_apos_download_`, `ERRO_`
- CSV capturado salvo em: `data/debug/boletim_click_{date}.csv` (via click) ou `boletim_post_{date}.csv` (via POST) ou `boletim_range_{start}_{end}.csv` (via range)
- Requests de rede para `arquivos.b3.com.br` salvas em: `data/debug/network_requests_{date}.txt`

---

## Observações técnicas

- **Instrumentos aceitos:** `DEB`, `CRI`, `CRA` — definidos em `config.toml` em `scrape.b3.instrumentosAceitos`
- **`vrTaxaNegocio`:** fica `NULL` se a célula for vazia, `"-"`, `"N/A"`, `"n/a"` ou `"0"`
- **Datas:** convertidas de `DD/MM/YYYY` para `YYYY-MM-DD` na função `_normalize_date`; se já estiver em `YYYY-MM-DD` passa direto
- **Horário:** padronizado para `HH:MM:SS` — adiciona `":00"` se vier em `HH:MM`
- **Valores numéricos BR:** `vrPU` e `vrVolume` têm ponto como separador de milhar e vírgula como decimal (ex: `1.234.567,89`) — o parse remove pontos e troca vírgula por ponto
- **Idempotente via UPSERT:** em conflito de `cdIdentificadorNegocio`, só atualiza `vrTaxaNegocio`, `cdSituacao` e `dtAtualizacao`
- **Browser:** Chromium via Playwright, `headless=False` por padrão para facilitar debug visual; `slow_mo=200ms`
- **Email:** `send_completion_email("scrape_b3_boletim", ok, summary, traceback)` sempre dispara ao fim — falha no email não propaga

---

**Decisao (30/05/2026):** Navegar direto para o iframe em `arquivos.b3.com.br` (em vez da pagina principal da B3) foi escolhido por ser mais estavel — a pagina principal carrega varios scripts de analytics e pode bloquear o Playwright.

**Decisao (30/05/2026):** Para ranges, POST unico com `Date != FinalDate` e tentado primeiro. Se a API nao suportar (retorno HTML), o script itera data por data abrindo um contexto de browser por data para garantir isolamento de sessao.

**Decisao (30/05/2026):** O fallback em cascata (click → POST → interceptacao de response) garante que o script nao falhe silenciosamente: se a UI mudar e o botao CSV nao for mais encontrado, o POST direto ainda funciona independentemente.
