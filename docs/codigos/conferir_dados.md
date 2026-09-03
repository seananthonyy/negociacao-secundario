# conferir_dados.py

**Ferramenta**, não passo de pipeline.

---

## Overview

Confere o **contrato de `codigos/helpers/dados.py`**: a mesclagem coluna a coluna, as três
políticas, a invalidação de fluxo (o que era o trigger no banco), o descarte de PU par e o
round-trip de tipos.

Existe porque **achou três bugs que teriam passado em silêncio**:

- NULL voltava como NaN, e `NaN != NaN` — então agenda idêntica parecia ter mudado e
  invalidava a validação **todo dia**;
- `groupby.nth` deixou de agregar no pandas 2 e quebrava o `SOBRESCREVER`;
- coluna inteira com NULL estourava no pyarrow.

Rode-o depois de qualquer mudança em `dados.py`.

---

## Regras de negócio

Nove seções, cada uma sobre uma garantia da camada de dados:

| # | confere |
|---|---|
| 1 | `Mesclar` em tabela vazia (linha nova) |
| 2 | Escritor parcial não apaga o que o outro escreveu |
| 3 | As três políticas por coluna |
| 4 | `Dobrar`: duas linhas da mesma chave no mesmo lote |
| 5 | O trigger: mudança em coluna de fluxo zera a validação |
| 6 | Tabela particionada: `Mesclar` por dia |
| 7 | `SincronizarFluxoAtivos` — e a recusa de escrever sobre fonte B3 |
| 8 | Tipos: inteiro nulável sobrevive ao round-trip |
| 9 | `PuPar`: mudança de cadastro ou fluxo descarta o PU par do ativo |

---

## CLI

```powershell
python codigos\scripts\conferir_dados\conferir_dados.py
python codigos\scripts\conferir_dados\conferir_dados.py D:\uma\pasta
```

| argumento | efeito |
|---|---|
| *(nenhum)* | Base de teste numa pasta temporária do sistema, apagada no fim se tudo passou |
| `<caminho>` | Usa a pasta indicada e **não** a apaga — para inspecionar depois |

Sai `0` se tudo passa, `1` se algo falha. Quando falha, a base fica de pé para
diagnóstico, e o caminho aparece na primeira linha da saída.

---

## Interação com a base

**Nenhuma — não encosta na base real.** Aponta `[dados] raiz` para uma pasta temporária
antes de importar o `dados.py`.

---

## Detalhes técnicos

Sem dependência externa além de `pandas` e `pyarrow`, que a camada de dados já exige.

---

## Armadilhas

**O default já escreveu dentro do repositório.** Até 03/09/2026 a pasta padrão era
`./tmp_parquet`, relativa ao cwd — e os 4 parquets que ela gerou foram parar no git.
Agora usa a pasta temporária do sistema. Se você passar um caminho relativo no argumento,
o `.gitignore` cobre `tmp_parquet/` como rede.

**Os tickers de teste colidem se você reusar nomes.** A seção 9 usa `PPPP11`/`QQQQ11`
justamente porque `DDDD11` já é da seção 4 — e reusar fez um teste falhar por motivo
errado (preencher NULL **é** mudança de cadastro, então o descarte estava certo).
