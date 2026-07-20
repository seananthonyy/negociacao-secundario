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

> **Não se roda script por script à mão.** O pipeline tem **19 passos** com dependências entre si — ver [[11 - Pipeline de Execucao]]. Use uma das duas portas:

### Notebook (o jeito normal)

Abra **`code/run_secundario.ipynb`** a partir de `code/` e dê **Run All**. Um bloco por fluxo, processando as liquidações **D-3 .. D-1**, com conferência no `.db` a cada passo e o relatório no fim.

### Linha de comando (para agendar)

```powershell
cd code
python scriptsun_diario.py --last 3            # últimos 3 dias úteis (padrão)
python scriptsun_diario.py --start 2026-07-01 --end 2026-07-10   # intervalo
```

É o que vai no Task Scheduler (o Agendador não roda `.ipynb`).

### Rodar sem tocar no Outlook

```powershell
$env:NEGSEC_SEM_EMAIL = "1"
```

O corpo de cada email é gravado em `data/emails/*.html` em vez de enviado. Útil ao depurar, e obrigatório se o COM do Outlook travar (ele derruba rodadas em lote).

## Depois de mexer em cadastro, fluxo ou na calculadora

**Rode o portão de aceitação:**

```powershell
python scripts\conferir_pu.py --date 2026-07-10
```

Ele compara o PU da nossa calc com o da fonte, **no par e fora do par**, e escreve `data/pu_divergencias.csv`. Ver [[10 - Scripts/conferir_pu]].

## Setup de uma máquina nova

1. **`code/setup_teste.ipynb`** — smoke: cria o `.db` e roda **cada fluxo no menor período possível**, conferindo no banco que gravou o que devia. Objetivo: provar que tudo funciona, rápido, **antes** de puxar histórico.
2. **`code/setup_inicial.ipynb`** — carga histórica (defina `INICIO`/`FIM` na 1ª célula).

No banco, siga o runbook **`INSTALACAO_BANCO.md`** na raiz.

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
- **Email não envia / trava**: o COM do Outlook pendura (diálogo de permissão) e derruba rodadas em lote. Rode com `NEGSEC_SEM_EMAIL=1` — o corpo vai para `data/emails/*.html`.
- **Taxa NULL no relatório**: nem FI Analytics nem B3 conseguiram calcular. Ver [[06 - Calculadoras/FI Analytics API]] e [[06 - Calculadoras/B3 Calculator API]].
- **`cdReferencia` vazio para um ticker**: o `match_referencias.py` roda **sem argumentos**, idempotente sobre a base toda, a cada ciclo do pipeline. Se ficar vazio, o ativo provavelmente não tem `vrDuration` — ver [[98 - Backlog]] ("duration de corporates").
- **PU errado num ativo**: rode `conferir_pu.py --tickers XXXX`. Se ele bate **no par** e erra **fora do par**, o problema é o **desconto**, não o fluxo. Ver [[14 - Rotinas da Calculadora]].
- **A calc ignora os eventos do fluxo de um IPCA**: `vrAniversario` errado ou NULL. A calc só aplica evento que caia **exatamente** no aniversário — dia 15 é convenção de NTN-B, não de debênture. Ver [[15 - Cadastro dos Ativos]].
- **`scrape_fianalytics_planilha` grava 0 tickers**: ⚠️ **está quebrado** (seletor Tailwind morto desde 08/07). Ver [[98 - Backlog]].
