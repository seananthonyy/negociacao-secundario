# Runbook — migração segura do `trades.db` no banco

> Como levar o código novo para cima do `trades.db` histórico do banco **sem perder
> dado e sem erro de coluna faltando**. Escrito em 23/07/2026 · schema **v3**.

---

## O que muda no banco quando você roda um `.py`

A migração é **autônoma**: não há passo manual, não há `.zip`. Todo script chama
`ObterBanco()`, que chama `Bootstrap()` (em `code/lib/db.py`). No instante em que você
roda qualquer script sobre um `trades.db` de versão anterior, o `Bootstrap`:

1. **Lê a estrutura atual** do banco (`PRAGMA table_info` de cada tabela).
2. **Compara com a DDL** (fonte única da verdade em `lib/db.py`) e descobre as colunas
   que faltam — em **qualquer** tabela, não só `InfoAtivos`.
3. Se há coluna faltando **e** o banco já tem dados: **tira um backup preventivo**
   antes de tocar em qualquer coisa (`data/backups/trades_pre_migracao_AAAAMMDD_HHMMSS.db`,
   via API de backup do SQLite, segura com WAL).
4. **Cria as tabelas que faltam**, depois **`ALTER TABLE ... ADD COLUMN`** para cada
   coluna ausente, depois cria os índices. Nunca `DROP` de tabela, nunca recria — o
   histórico de negócios e cadastros fica 100% intacto.
5. **Valida a sanidade**: conta as linhas de cada tabela migrada antes e depois. Migração
   só adiciona coluna, então a contagem tem de ser idêntica; se cair, **aborta o commit**
   (`ROLLBACK`), preserva o backup e levanta erro — nada fica pela metade.
6. **Grava a versão** em `SchemaVersao` (uma linha por versão aplicada). A versão corrente
   é `MAX(vrVersao)`.

Numa base **já atualizada**, tudo isso é um no-op barato (só o diff de colunas, ~0,07 s):
sem backup, sem ALTER, sem cópia.

> **Nome da tabela de versão:** o pedido citava `_SchemaVersion`. A convenção do projeto
> (`CLAUDE.md`) proíbe `_` no início de qualquer nome e exige tabelas em PascalCase
> português, então ela se chama **`SchemaVersao`**. Mesma função; nome que respeita a regra
> do repositório.

---

## Passo a passo no banco

Tudo roda de dentro de `code/`.

### 1. Confira onde está o banco e faça sua própria cópia (cinto além do suspensório)

O `Bootstrap` já faz backup automático, mas um backup manual seu, fora da pasta do
projeto, é barato e definitivo:

```bash
cd code
copy data\trades.db  D:\backup_manual\trades_ANTES_migracao.db     # Windows (cmd)
#  ou:  cp data/trades.db /caminho/seguro/trades_ANTES_migracao.db   (bash)
```

### 2. Veja em que versão o banco está hoje (opcional, só informativo)

```bash
python -c "import sys; sys.path.insert(0,'.'); from lib.db import ObterConexao, LerVersaoSchema; c=ObterConexao(); print('versao atual:', LerVersaoSchema(c))"
```

`0` significa "banco anterior ao versionamento" (vai migrar para 3). `3` significa "já
está atual".

> Use `ObterConexao` (não `ObterBanco`) para **só olhar** sem disparar a migração.

### 3. Rode a migração — é só abrir o banco

Qualquer script serve; o mais barato é validar o schema explicitamente:

```bash
python -c "import sys; sys.path.insert(0,'.'); from lib.db import ObterBanco, LerVersaoSchema; c=ObterBanco(); print('migrado para versao', LerVersaoSchema(c))"
```

Saída esperada num banco antigo: cria o backup em `data/backups/`, adiciona as colunas
que faltavam e imprime `migrado para versao 3`. Se algo estivesse errado, ele **abortaria
com erro** em vez de deixar o banco pela metade.

### 4. Prove que nada se perdeu

```bash
python -c "import sys; sys.path.insert(0,'.'); from lib.db import ObterConexao; c=ObterConexao(); print({t: c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('NegociosBrutos','NegociosProcessados','InfoAtivos','FluxoAtivos','AnbimaIndicativos','MtmAnbima')})"
```

Compare com a contagem de antes (se tiver anotado). Devem bater exatamente — a migração
não apaga nem altera linha nenhuma.

### 5. Rode o pipeline normalmente

```bash
python run_diario.py --start AAAA-MM-DD --end AAAA-MM-DD
```

A partir daqui é operação normal. O `Bootstrap` roda a cada conexão, mas num banco já
na v3 é no-op.

---

## Se algo der errado

- **A migração levantou erro e abortou.** O banco não foi alterado (houve `ROLLBACK`) e o
  backup preventivo está em `data/backups/trades_pre_migracao_*.db`. Nada foi perdido.
  Mande o traceback para análise antes de tentar de novo.
- **Quero voltar atrás.** Feche todos os processos que usam o banco e restaure a cópia:
  substitua `data/trades.db` pelo backup preventivo (ou pela sua cópia manual do passo 1).
- **Apareceu um `trades.db-wal` / `-shm` ao lado.** Normal (WAL do SQLite). Não apague com
  o banco em uso; some sozinho quando o último processo fecha.

---

## Onde está cada peça

| O quê | Onde |
|---|---|
| Lógica da migração | `code/lib/db.py` → `Bootstrap`, `ColunasFaltantes`, `BackupPreventivo` |
| Fonte da verdade do schema | a `DDL` no topo de `lib/db.py` |
| Versão alvo | constante `SCHEMA_VERSION` em `lib/db.py` (hoje **3**) |
| Backups preventivos | `code/data/backups/` (gitignored) |
| Trilha de versões aplicadas | tabela `SchemaVersao` no próprio `trades.db` |
| Dicionário de dados | `CONTEXTO_PROJETO.md` §4 |

## Para o desenvolvedor: como bumpar o schema no futuro

1. Edite a `DDL` em `lib/db.py` (adicione a coluna/tabela — **só aditivo**, nunca remova).
2. Incremente `SCHEMA_VERSION`.
3. Pronto. O reconciliador genérico detecta a coluna nova e a adiciona sozinho em todo
   banco que abrir — **não existe mais lista paralela de ALTER para manter em dia** (era a
   origem de colunas esquecidas fora de `InfoAtivos`).
