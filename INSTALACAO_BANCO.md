# Instalação no ambiente do Banco (Itaú BBA)

> **Claude: se o usuário pedir "me diga o que fazer" / "como instalo isto", siga este runbook e conduza-o passo a passo.**
> Contexto completo da migração: `vault/13 - Migracao Banco.md`. Pipeline: `vault/11 - Pipeline de Execucao.md`. Segredos: `vault/99 - Credenciais e Links.md`. Visão geral: `CLAUDE.md` e `PLANEJAMENTO_v5.md`.

Este projeto gera o relatório diário de negociação secundária de crédito privado (Deb/CRI/CRA). Falta **transferir os arquivos, instalar e montar a base** neste PC.

---

## Passo 0 — Transferir os arquivos (2 downloads, 1 por repo)

No banco não dá `git clone`/`git pull` nem baixar `.zip`. Cada repo tem um **bundle
auto-extraível** (`.py` único) que você abre pela **web do GitHub**, baixa e roda.
São **dois repos** (o projeto + a calculadora que ele importa) — monte-os como **irmãos**:

```
<pasta-pai>\
├── negociacao-secundario\    ← extraia o bundle_banco.py AQUI DENTRO
│   └── code\ ...
└── calculadora-renda-fixa\   ← extraia o bundle_calc.py AQUI DENTRO
    └── calculadora_rf.py ...
```

**0a — Projeto (`negociacao-secundario`, repo público):**
1. No GitHub, repo `seananthonyy/negociacao-secundario` → abra **`bundle_banco.py`** na raiz → **"Download raw file"** (~1,5 MB).
2. Crie a pasta `negociacao-secundario\`, salve o bundle nela, e rode:
   ```powershell
   python bundle_banco.py
   ```
   Recria a árvore (`code/`, `vault/`, docs) — ~98 arquivos. **Não** traz `.env`, `destinatarios.py` nem `*.db`.

**0b — Calculadora (`calculadora-renda-fixa`, repo PRIVADO — precisa estar logado no GitHub):**
1. No GitHub, repo `seananthonyy/calculadora-renda-fixa` → abra **`bundle_calc.py`** na raiz → **"Download raw file"** (~0,3 MB).
2. Crie a pasta `calculadora-renda-fixa\` **irmã** da anterior, salve o bundle nela, e rode:
   ```powershell
   python bundle_calc.py
   ```
   Recria a calculadora (`calculadora_rf.py` + tooling + docs). **Não** traz `*.db` (os insumos vivem no `code/data/` do projeto — ver seção da calc abaixo).

Com o layout irmão acima, o `config.toml` acha a calc sozinho (`calculadoraDir = "../../calculadora-renda-fixa"`). Se puser em outro lugar, use `CALCULADORA_DIR` (seção da calc).

*(Se um bundle ficar desatualizado após mudanças, regenere no PC pessoal com `python make_bundle.py` no repo correspondente e suba de novo.)*

---

## Estrutura de pastas (obrigatória dentro de `code/`)

O código resolve caminhos relativos à pasta `code/` — a **raiz pode ter qualquer nome/lugar**, mas a estrutura **dentro de `code/`** deve espelhar o repositório:

```
<raiz>\code\
├── config.toml, requirements.txt, destinatarios.example.py
│   setup_teste.ipynb, setup_inicial.ipynb, run_secundario.ipynb
├── lib\        (__init__.py, config.py, db.py, logger.py, email_outlook.py, relatorio_execucao.py,
│                b3_calc_api.py, fianalytics_api.py, calc.py)
├── scripts\    (scrape_*.py, calc_*.py, validar_calc_b3.py,
│                filtrar_trades.py, gerar_*.py, match_referencias.py, pipeline_core.py,
│                run_diario.py, check_no_secrets.py)
├── templates\  (relatorio.html.j2, relatorio_secundario.html)
└── data\       (feriados_anbima.csv  ← OBRIGATÓRIO; as subpastas logs/ relatorios/ etc. são criadas sozinhas)
```

`trades.db` **não** vem do repo — é criado no Passo 5 (teste) e populado no Passo 6 (carga).
`ipca.db` e `di.db` também são criados sozinhos (vazios) e populados pelas rotinas da calculadora.

---

## A calculadora de renda fixa (projeto vizinho)

Este projeto **importa** `calculadora_rf.py`, que vive em **outro repositório**
(`calculadora-renda-fixa`) e **não é copiado para dentro daqui**. Só o `lib/calc.py`
sabe onde ela está — nenhum outro módulo precisa saber.

**Onde colocar:** qualquer pasta. O padrão do `config.toml` assume que ela é **irmã** da
raiz deste projeto:

```
<pasta-qualquer>\
├── negociacao-secundario\code\...     ← este projeto
└── calculadora-renda-fixa\            ← a calc  (calculadora_rf.py na raiz dela)
```

**Se ficar em outro lugar**, aponte de um dos dois jeitos — a variável de ambiente vence:

| Como | Onde | Quando usar |
|---|---|---|
| `[paths] calculadoraDir` no `config.toml` | caminho **relativo a `code/`** (default `"../../calculadora-renda-fixa"`) | layout fixo |
| Variável de ambiente **`CALCULADORA_DIR`** | caminho absoluto | quando não dá para mexer no config (é o caso do banco) |

```powershell
# no banco, se a calc estiver em outro drive/pasta:
setx CALCULADORA_DIR "D:\ferramentas\calculadora-renda-fixa"
```

**Não copie os `.db` da calc para dentro dela.** Os insumos (`ipca.db`, `di.db`,
`feriados_anbima.csv`) vivem em **`code/data/`** — deste projeto — e o `lib/calc.py`
aponta a calc para cá via `CALCRF_FILES_DIR`. Sem a env var, a calc volta ao
comportamento antigo (o `files/` dela), então o add-in do Excel continua funcionando.

Conferir que resolveu:
```powershell
python -c "import sys; sys.path.insert(0,'.'); from lib.calc import DirCalculadora, ImportarCalc; print(DirCalculadora()); ImportarCalc(); print('calc OK')"
```

---

## Passo 1 — Conferir a estrutura
Verifique que os arquivos acima estão nos caminhos certos, que `code/data/feriados_anbima.csv`
existe e que a calculadora resolve (comando acima).

## Passo 2 — Python e dependências
A partir da pasta `code\`:
```powershell
pip install -r requirements.txt
pip install jupyter          # se for abrir os .ipynb fora do VSCode
playwright install chromium
```

## Passo 3 — Segredos (variáveis de ambiente da conta)
Os segredos são lidos por `lib.config.ObterSegredo()` via o bloco `[env]` do `config.toml` (mapeia nomes de variáveis; ver `vault/99`). Necessárias:

| Segredo | Variável de ambiente | Situação no banco |
|---|---|---|
| Token B3 Calculator | `token_calc_B3` | já existe |
| API key FI Analytics | `token_fianalytics` | já existe |
| Login FI Analytics | `user_fianalytics` | **criar** |
| Senha FI Analytics | `password_fianalytics` | **criar** |
| Proxy | `proxy_http`, `proxy_https` | já existem |
| Pasta da calculadora | `CALCULADORA_DIR` | **só se ela não for irmã da raiz** (ver seção acima) |

Criar as duas do FI (cmd): `setx user_fianalytics "..."` e `setx password_fianalytics "..."`.
**Depois feche e reabra o terminal/Jupyter** (variáveis novas só aparecem em processos novos).

## Passo 4 — Emails
Copie `destinatarios.example.py` para **`destinatarios.py`** e preencha `EMAIL_DESTINATARIOS` (rascunho do relatório) e `OUTLOOK_TO` (status `[OK]/[ERRO]`; deixe `[]` para usar a variável `OUTLOOK_TO`).

Todo script manda um email HTML de conclusão (paleta Itaú) com: datas processadas, contadores de inserido/atualizado/ignorado, exemplos do que entrou, avisos e — se falhar — o traceback.

Para rodar **sem tocar no Outlook** (útil ao depurar, ou se o COM travar): defina `NEGSEC_SEM_EMAIL=1`. O corpo do email é gravado em `data/emails/*.html` em vez de enviado.

**Verificação rápida** (rodar de `code\`):
```powershell
python -c "import sys; sys.path.insert(0,'.'); from lib.config import ObterSegredo, ObterListaEmails; `
print('B3:', bool(ObterSegredo('b3CalcToken')), '| FIkey:', bool(ObterSegredo('fianalyticsApiKey')), `
'| FIuser:', bool(ObterSegredo('fianalyticsUser')), '| FIpass:', bool(ObterSegredo('fianalyticsPass')), `
'| emails:', ObterListaEmails('destinatarios'))"
```
Tudo `True` = segredos resolvendo.

## Passo 5 — Testar cada fluxo (`setup_teste.ipynb`)
Abra **`setup_teste.ipynb`** a partir de `code\` e dê **`Run All`**. Ele **cria o `.db`** e roda **cada fluxo no menor período possível** (1 pregão; `calc_taxa` e `anbima_data` com `--limit`), conferindo no banco que gravou o que devia: cada bloco mostra `[OK]`/`[VAZIO]`. A célula final resume. Objetivo: **provar que todo fluxo funciona, rápido**, antes de puxar histórico. Se um bloco der `[FALHA]`/`[VAZIO]` (proxy, Playwright, login), corrija e **re-rode só ele**. `outstanding` fica `[VAZIO]` fora do banco — normal.

## Passo 6 — Montar a base (`setup_inicial.ipynb`)
Com os fluxos validados, abra **`setup_inicial.ipynb`** a partir de `code\` e **defina `INICIO`/`FIM` na primeira célula** — é a janela da carga. Dê **`Run All`** (é demorado — dá pra deixar rodando). As fontes de histórico curto (deb/NTN-B ~4 meses, DI ~20 pregões, CRI/CRA ~5 pregões) já têm a janela máxima **hardcoded** na config; você não mexe nelas. **Anbima Data (o mais pesado) roda por último.** Cada bloco é idempotente e confere no `.db` quantos pregões ficaram cobertos: se um falhar, corrija e **re-rode só ele**.

## Passo 7 — Rotina diária (`run_secundario.ipynb`)
No dia a dia abra **`run_secundario.ipynb`** a partir de `code\` e dê **`Run All`**: 1 bloco por fluxo, processando as liquidações **D-3 .. D-1** (dias úteis) e regenerando o relatório no fim.

Para agendar no Task Scheduler (o Agendador não roda `.ipynb`), o equivalente é:
`python.exe <raiz>\code\scripts\run_diario.py --last 3` — mesma cadeia, mesmos fluxos.

---

## Notas para o Claude do banco
- **Sempre rode o notebook/scripts a partir da pasta `code\`** (os caminhos de `data/` são relativos ao cwd; o `pipeline_core` já força isso nos subprocessos).
- **A calc local está LIGADA** (`config.toml [calc] usarCalcTaxa=true`, indexadores CDI+/IPCA/PREFIXADO): o `calc_taxa_negocios` a usa como 1º degrau nos ativos `stFluxoValidado=1`. O gate `validar_calc_b3` (passo 13 do pipeline) garante a confiança. No banco, com o proxy, a calc local **evita ~70% das chamadas de API** do `calc_taxa`. Se algo der errado com a calc, desligar é 1 linha (`usarCalcTaxa=false`) — cai na cascata FI→B3.
- **Pipeline tem 18 passos** (o `validar_calc_b3` é o único validador, logo antes do `calc_taxa`). Ver `vault/11 - Pipeline de Execucao.md`.
- **Janelas das fontes** (limitam o histórico): Anbima Data = tudo; deb/NTN-B ~4 meses; curva DI ~20 pregões; CRI/CRA ~5 pregões. Ver `vault/13` §5.
- **Ordem obrigatória** do pipeline e o "por que" de cada data: `vault/11 - Pipeline de Execucao.md`.
- **Pequenos ajustes de código:** o CLI de cada script está centralizado nas funções de fluxo do `scripts/pipeline_core.py`; a orquestração (ordem/datas) também. Convenções em `CLAUDE.md`.
- **Antes de qualquer `git push`** (se for republicar): `python scripts\check_no_secrets.py`.
