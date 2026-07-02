# Instalação no ambiente do Banco (Itaú BBA)

> **Claude: se o usuário pedir "me diga o que fazer" / "como instalo isto", siga este runbook e conduza-o passo a passo.**
> Contexto completo da migração: `vault/13 - Migracao Banco.md`. Pipeline: `vault/11 - Pipeline de Execucao.md`. Segredos: `vault/99 - Credenciais e Links.md`. Visão geral: `CLAUDE.md` e `PLANEJAMENTO_v5.md`.

Este projeto gera o relatório diário de negociação secundária de crédito privado (Deb/CRI/CRA). Falta **transferir os arquivos, instalar e montar a base** neste PC.

---

## Passo 0 — Transferir os arquivos (rápido, 1 download)

No banco não dá `git pull` nem baixar `.zip`. Use o **bundle auto-extraível**:

1. No GitHub (web), abra **`bundle_banco.py`** na raiz do repo → botão **"Download raw file"** (é 1 arquivo, ~1 MB).
2. Salve na pasta que será a raiz do projeto (ex.: `Z:\AntonioOliveira\NegociacaoSecundario\`).
3. Abra um terminal nessa pasta e rode:
   ```powershell
   python bundle_banco.py
   ```
   Ele recria a árvore inteira (`code/`, `vault/`, docs) — os ~79 arquivos de uma vez. **Não** traz `.env`, `destinatarios.py` nem `trades.db` (esses você configura/monta nos passos seguintes).

*(Se `bundle_banco.py` estiver desatualizado após mudanças de código, regenere no PC pessoal com `python make_bundle.py` e suba de novo.)*

Alternativa (lenta): baixar cada arquivo 1-a-1 espelhando os caminhos do repo.

---

## Estrutura de pastas (obrigatória dentro de `code/`)

O código resolve caminhos relativos à pasta `code/` — a **raiz pode ter qualquer nome/lugar**, mas a estrutura **dentro de `code/`** deve espelhar o repositório:

```
<raiz>\code\
├── config.toml, requirements.txt, pipeline.ipynb, destinatarios.example.py
├── lib\        (__init__.py, config.py, db.py, logger.py, email_outlook.py, b3_calc_api.py, fianalytics_api.py)
├── scripts\    (scrape_*.py, calc_*.py, filtrar_trades.py, gerar_*.py, match_referencias.py,
│                pipeline_core.py, run_diario.py, check_no_secrets.py)
├── templates\  (relatorio.html.j2, relatorio_secundario.html)
└── data\       (feriados_anbima.csv  ← OBRIGATÓRIO; as subpastas logs/ relatorios/ etc. são criadas sozinhas)
```

`trades.db` **não** vem do repo — é montado aqui pelo setup (passo 5).

---

## Passo 1 — Conferir a estrutura
Verifique que os arquivos acima estão nos caminhos certos e que `code/data/feriados_anbima.csv` existe.

## Passo 2 — Python e dependências
A partir da pasta `code\`:
```powershell
pip install -r requirements.txt
pip install jupyter          # se for abrir o pipeline.ipynb fora do VSCode
playwright install chromium
```

## Passo 3 — Segredos (variáveis de ambiente da conta)
Os segredos são lidos por `lib.config.get_secret()` via o bloco `[env]` do `config.toml` (mapeia nomes de variáveis; ver `vault/99`). Necessárias:

| Segredo | Variável de ambiente | Situação no banco |
|---|---|---|
| Token B3 Calculator | `token_calc_B3` | já existe |
| API key FI Analytics | `token_fianalytics` | já existe |
| Login FI Analytics | `user_fianalytics` | **criar** |
| Senha FI Analytics | `password_fianalytics` | **criar** |
| Proxy | `proxy_http`, `proxy_https` | já existem |

Criar as duas do FI (cmd): `setx user_fianalytics "..."` e `setx password_fianalytics "..."`.
**Depois feche e reabra o terminal/Jupyter** (variáveis novas só aparecem em processos novos).

## Passo 4 — Emails
Copie `destinatarios.example.py` para **`destinatarios.py`** e preencha `EMAIL_DESTINATARIOS` (rascunho do relatório) e `OUTLOOK_TO` (status `[OK]/[ERROR]`; deixe `[]` para usar a variável `OUTLOOK_TO`).

**Verificação rápida** (rodar de `code\`):
```powershell
python -c "import sys; sys.path.insert(0,'.'); from lib.config import get_secret, get_email_list; \
print('B3:', bool(get_secret('b3CalcToken')), '| FIkey:', bool(get_secret('fianalyticsApiKey')), \
'| FIuser:', bool(get_secret('fianalyticsUser')), '| FIpass:', bool(get_secret('fianalyticsPass')), \
'| emails:', get_email_list('destinatarios'))"
```
Tudo `True` = segredos resolvendo.

## Passo 5 — Montar a base (`setup_inicial.ipynb`, bloco a bloco)
Abra **`setup_inicial.ipynb`** a partir de `code\`. Rode a célula **Config** (calcula as janelas máximas de cada fonte automaticamente) e depois **um bloco por vez**:
- **Blocos 1–8 (scraping):** Anbima Data (completa) · FI Analytics · deb · NTN-B · curva DI · CRI/CRA · boletim · Outstanding. Cada bloco já vem na **janela máxima** que a fonte entrega. Se um falhar (proxy, Playwright, login, Bloomberg), **o erro fica só nele** — corrija e **re-rode só esse bloco**.
- **Blocos 9–14 (cálculo):** taxa → filtrar → spread Anbima → match → spread over → relatório. Idempotentes (re-rodar pula o já feito).

Ajuste `INICIO_BOLETIM`/`OUTSTANDING` na célula Config se quiser (no banco, deixe `OUTSTANDING` implícito rodando o bloco 8).

## Passo 6 — (Opcional) Agendar a rotina diária
Use o **`pipeline.ipynb`** (Seção B) no dia a dia, ou agende no Task Scheduler:
`python.exe <raiz>\code\scripts\run_diario.py` — reprocessa os últimos 5 dias úteis e regenera o relatório.

---

## Notas para o Claude do banco
- **Sempre rode o notebook/scripts a partir da pasta `code\`** (os caminhos de `data/` são relativos ao cwd; o `pipeline_core` já força isso nos subprocessos).
- **Janelas das fontes** (limitam o histórico): Anbima Data = tudo; deb/NTN-B ~4 meses; curva DI ~20 pregões; CRI/CRA ~5 pregões. Ver `vault/13` §5.
- **Ordem obrigatória** do pipeline e o "por que" de cada data: `vault/11 - Pipeline de Execucao.md`.
- **Pequenos ajustes de código:** o CLI de cada script está centralizado nas funções de fluxo do `scripts/pipeline_core.py`; a orquestração (ordem/datas) também. Convenções em `CLAUDE.md`.
- **Antes de qualquer `git push`** (se for republicar): `python scripts\check_no_secrets.py`.
