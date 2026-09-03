# migrar_para_parquet.py

**Ferramenta.** Carga única, para levar uma base SQLite para o Parquet do projeto.

---

## Overview

Leva o conteúdo dos `.db` de uma pasta para o armazenamento Parquet. Serve à migração da
base de produção do banco, que ainda roda em SQLite.

**Não assume layout de origem.** Descobre as tabelas lendo o `sqlite_master` de cada `.db`
e mapeia **por nome de tabela**, nunca por qual arquivo ela está — então funciona tanto
com um `trades.db` único quanto com o par `ativos.db` + `trades.db`, sem configuração.

---

## Regras de negócio

**Três conferências prévias, e ele aborta em qualquer uma.**

**1. Coluna a coluna, nos dois sentidos.** `Conformar()` descarta coluna fora do esquema e
cria como NULL a que falta — sem erro e sem log. É o comportamento certo no uso diário e
veneno numa migração: se a base for anterior ao rename de 29/06/2026 (`vrRate` →
`vrTaxaCalculada`), a conversão gravaria a coluna de taxa **inteira vazia** e o relatório
sairia em branco sem nada quebrar. Falta de coluna do esquema **aborta**; coluna a mais na
origem só avisa (é descartada de propósito).

**2. Chave natural.** `cdIdentificadorNegocio` presente e **único** em `NegociosBrutos`. O
`idTrade` morreu com o SQLite, e fora dele ninguém gera esse número. Base anterior a
31/08/2026 pode ter negócio com `"-"` nesse campo — e chave nula se anula em silêncio no
primeiro `Mesclar`.

**3. Destino vazio.** A gravação substitui arquivo/partição inteiros. Se a pasta Parquet
já tem dado, aborta e exige `--sobrescrever` — senão uma segunda tentativa apagaria o que
a primeira trouxe.

**Traduz o `idTrade`.** Em `NegociosProcessados` o vínculo era o `idTrade`; o script o
converte para `cdIdentificadorNegocio` e aborta se sobrar linha órfã.

**Confere no fim:** contagem SQLite × Parquet tabela a tabela, e releitura pelo DuckDB.

---

## CLI

```powershell
python codigos\scripts\migrar_para_parquet\migrar_para_parquet.py --origem D:\dbs
python codigos\scripts\migrar_para_parquet\migrar_para_parquet.py --origem D:\dbs --executar
```

| argumento | efeito |
|---|---|
| `--origem PASTA` | **Obrigatório.** Pasta com os `.db`; todos são varridos |
| *(sem `--executar`)* | Dry-run: descobre, confere e não escreve nada |
| `--executar` | Grava de verdade |
| `--tabelas A,B` | Restringe a essas tabelas |
| `--sobrescrever` | Autoriza gravar por cima de destino que já tem dados |

**Sempre rode o dry-run primeiro.** É ele que faz as três conferências.

---

## Interação com a base

**Lê:** os `.db` da pasta de origem.

**Grava:** todas as tabelas reconhecidas, por `GravarDia` (série) ou `GravarTudo`
(estado) — substituição, não mesclagem.

---

## Detalhes técnicos

Só `sqlite3`, `pandas` e o próprio `dados.py`. Tabela que o `ESQUEMA` não conhece é
reportada e ignorada — nada entra na base sem esquema declarado.

---

## Armadilhas

**Substituição, não mesclagem.** Rodar com `--sobrescrever` sobre uma base viva apaga o
que estiver lá. A trava existe exatamente para isso, e **já pegou o caso**: numa máquina
com a base Parquet cheia, ele teria apagado tudo.

**Converter ANTES de ligar o pipeline.** Se o pipeline rodar primeiro, ele não acha
histórico e tenta re-raspar a janela inteira — e as fontes têm janela curta, então parte
não volta.

**Depois da migração, rode o `calc_pu_par --tudo`.** A tabela `PuPar` é de 02/09/2026 e
nasce vazia numa base convertida.

**`sqlite_stat1` e afins são ignorados.** Tabela interna do SQLite aparece no aviso de
"não reconhecidas" — é esperado.
