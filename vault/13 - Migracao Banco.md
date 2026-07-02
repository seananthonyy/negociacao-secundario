# Migração para o ambiente do Banco (Itaú BBA)

> Nota mestra da migração do projeto do PC pessoal para o PC do trabalho (rede Itaú BBA).
> **O Claude do banco deve ler esta nota + [[11 - Pipeline de Execucao]] + [[04 - Banco de Dados]] + [[99 - Credenciais e Links]] antes de qualquer ajuste.**
> Ver também [[12 - Auditoria pre-migracao]] e [[09 - Progresso]].

Início do planejamento: 01/07/2026.

---

## 1. Objetivo e restrições (dadas pelo usuário)

1. **Transferência via GitHub público**, arquivos baixados **um a um** (não dá `git pull`, não dá baixar `.zip`, só acesso ao site do GitHub). → o nº de arquivos importa; o repo pode conter tudo, o download inicial é seletivo (ver manifesto).
2. **Repo é público** → **nenhum segredo ou dado sensível** pode ir. Trava automática: `code/scripts/check_no_secrets.py` (ver §4).
3. **Raiz no banco:** `Z:\AntonioOliveira\NegociacaoSecundario`. Os paths do código são **relativos à pasta `code/`** (via `Path(__file__).parent.parent` em `lib/config.py`), então **nenhum path precisa ser editado** — basta a estrutura `Z:\AntonioOliveira\NegociacaoSecundario\code\...`.
4. **Rodagem diária:** toda manhã, para os **últimos 5 dias úteis** (pega alterações retroativas das fontes). Scripts são idempotentes (UPSERT) → reprocessar sobrescreve.
5. **Relatório sempre com TODA a base** — `gerar_relatorio_credito` já carrega todos os pregões; nada a mudar.
6. **Tudo documentado** (esta nota + vault) para o Claude do banco fazer pequenos ajustes com contexto completo.
7. **Futuro incremental:** depois da base montada no banco, transferir **só arquivos pontuais** de código para pequenas alterações, **sem resetar a base**. Como `trades.db` vive só no banco e é isolado do código, troca de código nunca reseta dados.
8. **Setup inicial** que puxa todo o histórico disponível de cada fonte (o scraping da Anbima Data inteira é o mais importante).

**Restrições de formato de arquivo:**
- **Nenhum `.bat`/`.ps1`/`.sh`** no projeto — o banco bloqueia download desses. Tudo roda como `python scripts\...`. Agendamento via Task Scheduler apontando direto pro `python.exe` (ou `pythonw.exe`) com o script como argumento.
- **`.ipynb` é permitido** (o banco não bloqueia).
- **Dotfiles** (`.env.example`, `.gitignore`) podem complicar no download 1-a-1 → o Claude do banco recria a partir desta nota, sem baixar.

---

## 2. O que viaja × o que é regenerado no banco

| Categoria | Vai pro repo? | Observação |
|---|---|---|
| `code/lib/` (7 módulos) | ✅ | |
| `code/scripts/` | ✅ | |
| `code/templates/` (2) | ✅ | |
| `code/config.toml`, `requirements.txt`, `.env.example`, `.gitignore` | ✅ | |
| `code/destinatarios.example.py` | ✅ | placeholders (sem emails reais) |
| `code/data/feriados_anbima.csv` | ✅ | **obrigatório em runtime** |
| `code/data/anbima_skip_tickers.csv` | ✅ | opcional |
| Docs (CLAUDE.md, PLANEJAMENTO_v5.md, vault curado) | ✅ | ver manifesto |
| **`code/.env`** | ❌ nunca | segredos; no banco vêm de variáveis da conta |
| **`code/destinatarios.py`** | ❌ nunca | emails internos; recriar no banco |
| **`code/data/trades.db`** (+ backups) | ❌ | 190 MB, > limite GitHub; **reconstruído pelo `--setup`** |
| `data/logs/`, `relatorios/`, `anbima_data_raw/`, `api_samples/`, `debug/` | ❌ | regeneráveis; no `.gitignore` |

**Manifesto do download inicial (núcleo curado):** todo o `code/` publicável + `CLAUDE.md` + `PLANEJAMENTO_v5.md` + notas do vault: [[01 - O Que Faz]], [[02 - Como Rodar]], [[04 - Banco de Dados]], [[11 - Pipeline de Execucao]], [[98 - Backlog]], [[99 - Credenciais e Links]] e esta nota. Outras notas só se precisar.

---

## 3. Modelo de segredos (config `[env]` + `get_secret`)

Segredos **nunca** ficam no código. O `config.toml` tem um bloco `[env]` que mapeia cada segredo para uma **lista de NOMES** de variáveis de ambiente candidatas (**só nomes, nunca valores** → seguro no repo público). O resolver `lib.config.get_secret(chave)` tenta cada candidato na ordem e usa o primeiro preenchido.

```toml
[env]
b3CalcToken        = ["token_calc_B3",        "B3_CALC_TOKEN"]
fianalyticsApiKey  = ["token_fianalytics",    "FIANALYTICS_API_KEY"]
fianalyticsUser    = ["user_fianalytics",     "FIANALYTICS_USER"]
fianalyticsPass    = ["password_fianalytics", "FIANALYTICS_PASS"]
httpProxy          = ["proxy_http",  "HTTP_PROXY",  "http_proxy"]
httpsProxy         = ["proxy_https", "HTTPS_PROXY", "https_proxy"]
outlookTo          = ["OUTLOOK_TO"]
emailDestinatarios = ["EMAIL_DESTINATARIOS"]
```

- **PC pessoal:** o `code/.env` (nomes canônicos: `B3_CALC_TOKEN`, `FIANALYTICS_*`, `OUTLOOK_TO`) resolve tudo. **Nenhuma variável de ambiente precisa ser setada.**
- **Banco:** os segredos vêm de **variáveis de ambiente da conta**, já existentes/a criar: `token_calc_B3`, `token_fianalytics`, `user_fianalytics`, `password_fianalytics`, `proxy_http`, `proxy_https`. Sem `.env`.
- Os **dois convivem** porque ambos os nomes estão na lista de candidatos.
- **Proxy:** `lib.config` copia o proxy resolvido (`proxy_https`) para `HTTPS_PROXY`/`HTTP_PROXY` padrão (que httpx e Playwright leem nativamente), no primeiro carregamento de config.

**Emails (não são variáveis de conta):** ficam em `code/destinatarios.py` (não versionado):
```python
EMAIL_DESTINATARIOS = ["...", "..."]  # rascunho do relatório (--email-dia)
OUTLOOK_TO = []                        # vazio → usa a var OUTLOOK_TO
```
Resolvidos por `lib.config.get_email_list("destinatarios"|"outlook")`: tenta `destinatarios.py` → variável de ambiente → `[]`. No banco, criar `destinatarios.py` a partir de `destinatarios.example.py`.

**Onde cada segredo é usado:** `get_secret("b3CalcToken")` → `lib/b3_calc_api.py`; `get_secret("fianalyticsApiKey")` → `lib/fianalytics_api.py`, `scrape_anbima_ntnb.py`; `get_secret("fianalyticsUser"/"fianalyticsPass")` → `scrape_fianalytics_planilha.py`, `lib/fianalytics_api.py`.

---

## 4. Blindagem do repo público

- **`.gitignore`** (em `code/`) ignora: `.env`, `destinatarios.py`, `*.db` / `data/*.db`, `data/logs/`, `data/relatorios/`, `data/anbima_data_raw/`, `data/api_samples/`, `data/debug/`, `__pycache__/`, `.venv/`, `.playwright/`.
- **`code/scripts/check_no_secrets.py`** — rodar **antes de publicar** (`python scripts/check_no_secrets.py`). Lê os valores reais de segredo do ambiente/`.env` (não contém segredo próprio) e falha (exit 1) se algum aparecer em arquivo publicável; também tem heurística de credencial hardcoded em código (pulando slugs tipo `api_key = 'debentures'`). Já pegou, na 1ª rodada, as 4 credenciais em texto plano no `PLANEJAMENTO_v5.md` e um email interno no `Progresso.md` (ambos scrubbed).

---

## 5. Setup inicial (`--setup`, roda 1 vez)

Enche a base com todo o histórico que cada fonte ainda entrega. **Janelas de arquivamento por fonte** (limitam o quão fundo dá pra ir):

| Fonte | Janela disponível | Como |
|---|---|---|
| **Anbima Data** (características + fluxo) | **tudo** (sem limite) | `scrape_anbima_data_ativos --mode full` (~4.178 deb + CRIs) — o mais pesado e prioritário |
| Anbima debêntures (indicativas) | **~4 meses móveis** | `scrape_anbima_debentures --start <hoje−4m> --end <hoje>` |
| Anbima NTN-B (`MtmAnbima`) | **~4 meses móveis** (sondado 01/07: mais antigo ≈ 25/02) | `scrape_anbima_ntnb --start <hoje−4m> --end <hoje>` — taxa vem do XLS público `merc-sec/arqs/m{yy}{mmm}{dd}.xls`; só a duration usa API |
| Curva DI B3 (`MtmAnbima`) | **~20 pregões** | `scrape_b3_curva_di --date` por pregão (loop) |
| Anbima CRI/CRA (indicativas) | **~5 pregões** (portal) | `scrape_anbima_cri_cra --date` por pregão (loop) |
| Boletim B3 | janela que você quiser | `scrape_b3_boletim --start --end` |
| FI Analytics planilha | snapshot atual | `scrape_fianalytics_planilha` (sem data) |
| Outstanding (Bloomberg) | **só no banco** | `scrape_outstanding_bloomberg --start --end` (terminal Bloomberg) — peso da aba Visão Anbima |

Depois do scraping largo, rodar a **cadeia de cálculo** (passos 8–13 do §6) para cada data de liquidação da janela + gerar o relatório.

---

## 6. Pipeline diário (últimos 5 dias úteis)

Para cada liquidação **X** nos últimos 5 dias úteis (X-1u = dia útil anterior; ver [[11 - Pipeline de Execucao]] para o "por que" de cada data):

| # | Script | Parâmetro |
|---|---|---|
| 1 | `scrape_b3_boletim` | `--start X-1u --end X` |
| 2 | `scrape_anbima_debentures` | `--date X` **e** `--date X-1u` (ver §7 — Anbima por dtNegocio) |
| 3 | `scrape_anbima_cri_cra` | idem passo 2 |
| 4 | `scrape_fianalytics_planilha` | *(sem data)* |
| 5 | `scrape_anbima_data_ativos` | `--start X-1u --end X` (incremental) |
| 6 | `scrape_anbima_ntnb` | `--start X-1u --end X` (2 datas) |
| 7 | `scrape_b3_curva_di` | `--date X-1u` **e** `--date X` |
| 8 | `calc_taxa_negocios` | `--date X` |
| 9 | `filtrar_trades` | `--date X` |
| 10 | `calc_spread_anbima` | `--date X` (ver §7) |
| 11 | `match_referencias` | *(sem args — global)* |
| 12 | `calc_spread_over` | `--date X` |
| 13 | `gerar_relatorio_credito` | *(sem args, 1× no fim)* |

Como se roda os 5 dias todo dia, a sobreposição cobre as pontas e os UPSERTs sobrescrevem alterações retroativas.

**Regras que não podem ser esquecidas** (de [[11 - Pipeline de Execucao]]): NTN-B e curva DI precisam de X-1u **E** X; `calc_taxa`(8) antes de `filtrar`(9); `match_referencias`(11) antes de `calc_spread_over`(12).

---

## 7. Anbima casado por `dtNegocio` (✅ APLICADA — 02/07/2026)

A coluna Taxa/Spread Anbima do relatório **deixou de usar** a data única D-1 (`AnbimaLatest`/`_CalcDMenos1`, removidos) e passou a casar **por `dtNegocio` de cada trade**: CTE `AnbimaMatch` pega a indicativa mais recente com `dtReferencia <= tp.dtNegocio` (via `ROW_NUMBER` por `idTrade`); no ticker, `_AggregateTicker` pondera por volume (trades do mesmo ticker/liquidação podem ter dtNegocio diferentes). Simétrico ao MtM de `calc_spread_over`. Só `gerar_relatorio_credito` mudou (`gerar_relatorio_html` segue no D-1, caminho secundário).

Validado (02/07): relatório 09→29/06, 15 pregões, 2.193 ativos, R$ 14.889,02 MM — volumes/tickers idênticos ao baseline (a mudança só afeta a coluna Anbima). Depende de ter as indicativas **até X inclusive** — o `pipeline_core` já raspa X e X-1u. Ver [[10 - Scripts/gerar_relatorio_credito]].

---

## 8. Orquestração (Fase 1 — ✅ CONSTRUÍDA)

Sem duplicação de lógica: notebook e `run_diario` chamam as mesmas funções do `pipeline_core`. Cada passo é um **subprocesso** do CLI que já existe (mantém os scripts independentes, com log/email próprios). Prints em ASCII (console cp1252 do Windows).

- **`code/scripts/pipeline_core.py`** — lógica compartilhada:
  - `ultimos_n_dias_uteis(n)`, `dia_util_anterior(d)`, `dias_uteis_entre(ini,fim)` — dias úteis via `feriados_anbima.csv` (1263 feriados).
  - `run_step(script, *args)` — roda 1 CLI, retorna ok/falha, acumula resumo.
  - `run_dia(X)` — cadeia dos 13 passos para a liquidação X (raspa X **e** X-1u → forward-compatible com Anbima por dtNegocio).
  - `run_ultimos_n(n=5)` — rotina diária: passos globais 1×, per-date em laço, relatório no fim.
  - `run_setup(inicio_boletim, dias_indicativas=130, dias_curva_di=20, dias_cricra=5, rodar_outstanding=False)` — bootstrap (§5).
- **`code/pipeline.ipynb`** — 1 bloco por fluxo (rotina diária / dia único / setup / blocos individuais). **Rodar a partir da pasta `code/`.**
- **`code/scripts/run_diario.py`** — entrypoint p/ Task Scheduler (`pythonw.exe .../run_diario.py`):
  - `run_diario.py` → últimos 5 dias úteis; `--last N` ajusta.
  - `run_diario.py --setup --inicio-boletim YYYY-MM-DD [--outstanding]` → bootstrap.

Validado (02/07): datas corretas (pula fim de semana/feriado), notebook e scripts compilam, `check_no_secrets` verde.

---

## 9. Passos no banco (checklist de instalação)

1. Baixar os arquivos do manifesto (§2) 1-a-1 do GitHub.
2. Recriar `.gitignore` e `.env.example` (dotfiles — o Claude do banco cria a partir desta nota).
3. `pip install -r requirements.txt` ; `playwright install chromium`.
4. Criar as **variáveis de ambiente da conta** (§3) se ainda não existirem; conferir `proxy_http`/`proxy_https`.
5. Criar `code/destinatarios.py` a partir do `.example` e preencher.
6. `python scripts\scrape_outstanding_bloomberg.py ...` disponível (terminal Bloomberg logado).
7. `python scripts\run_diario.py --setup` (ou rodar a seção SETUP do notebook) → base montada.
8. Agendar `run_diario.py` no Task Scheduler (manhã) → rotina diária.

---

## 10. Status das fases

- **Fase 0 — limpeza + infra de segredos: ✅ CONCLUÍDA (01/07/2026).** Ver [[09 - Progresso]] e [[12 - Auditoria pre-migracao]]. Resumo: `dump_api_samples.py` deletado; 8 imports mortos + 2 renames + docstring corrigida; `[env]`/`get_secret`/`get_email_list`/proxy em `lib/config.py`; call sites migrados; `destinatarios.py`/`.example`; `.gitignore` blindado; `check_no_secrets.py`; segredos scrubbed de `PLANEJAMENTO_v5.md` e `Progresso.md`. Local roda sem env vars (validado). Tudo compila, 0 imports mortos, verificador verde.
- **Fase 1 — ✅ COMPLETA (02/07/2026):** orquestração (`pipeline_core.py`, `pipeline.ipynb`, `run_diario.py` — §8) + Anbima por `dtNegocio` (§7). Tudo validado.
- **Fase 2 — criar o repo público + transferir: a fazer.** Docs/manifesto prontos (esta nota).

## 11. Pendências abertas

- Criar o repositório GitHub público, rodar `check_no_secrets`, subir os arquivos (Fase 2).
- Definir a janela do boletim B3 no `--setup` (parâmetro `inicio_boletim`).
- Testar modo incremental do `scrape_anbima_data_ativos` com Playwright **no banco** (pendência antiga — ver [[98 - Backlog]]).
- `scrape_outstanding_bloomberg` só testável no banco.
