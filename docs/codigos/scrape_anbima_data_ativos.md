# scrape_anbima_data_ativos.py

**Passo 6** do pipeline. O passo mais lento.

---

## Overview

Busca **características e agenda de pagamentos** no portal Anbima Data e completa
`InfoAtivos` + `FluxoAtivos`.

É o **fallback** do cadastro: roda depois do `scrape_b3_bond_details` e só cobre o que a
B3 não cobriu.

---

## Regras de negócio

### Fila por demanda, com re-scrape por campo NULL

A fila é a união de quem **negociou** no período com quem a **Anbima divulgou**. Dentro
dela, entra quem tem campo faltando — não se re-raspa ativo completo.

**Skip-list manual.** `anbima_skip_tickers.csv`, ao lado do script, lista papéis que o
portal genuinamente não cobre. É **curada à mão** e versionada: sem ela, os mesmos ativos
seriam tentados em toda rodada.

### O pacote, e a recusa de sobrescrever a B3

`vrVNE` + `dtInicioRentabilidade` + `FluxoAtivos` são um pacote. **O
`dados.SincronizarFluxos` se recusa a escrever agenda em ativo com
`cdFonteCadastro = 'B3'`** — a guarda mora na camada de dados, e não aqui, porque
`FluxoAtivos` tem vários escritores.

Se a Anbima reescrevesse por cima, o VNE capitalizado da B3 ficaria órfão e a carência
passaria a contar **duas vezes**, sem erro nenhum — só um PU errado.

### Campos genuinamente ausentes

`cdISIN` e `vrQuantidadeEmissao` faltam para parte dos papéis **na própria fonte**. Não é
falha de scrape; o email de info faltante os separa dos que valeria re-tentar.

---

## CLI

```powershell
python codigos\scripts\scrape_anbima_data_ativos\scrape_anbima_data_ativos.py --start 2026-09-01 --end 2026-09-02
python codigos\scripts\scrape_anbima_data_ativos\scrape_anbima_data_ativos.py --mode full
```

| argumento | efeito |
|---|---|
| `--date` / `--start` / `--end` | Modo incremental: quem negociou ou foi divulgado |
| `--mode full` | Universo completo — só no bootstrap da base |
| `--limit N` | Só N tickers, para smoke test |
| `--force` | Ignora o cache e re-raspa |

---

## Interação com a base

**Lê:** `NegociosBrutos`, `AnbimaIndicativos` (a fila), `InfoAtivos` (quem está incompleto
e qual é a fonte do cadastro).

**Grava:** `InfoAtivos` (preenchendo buraco) e `FluxoAtivos` (via `SincronizarFluxos`, que
recusa ativo de fonte B3). Em **lote**.

**Escreve fora da base:** um JSON cru por ticker em
`cache/scrape_anbima_data_ativos/` — cache e auditoria, permite reprocessar sem re-raspar.

---

## Detalhes técnicos

Playwright sobre `data.anbima.com.br`, duas páginas por ticker:

```
/{debentures|certificado-de-recebiveis}/{ticker}/caracteristicas
/{debentures|certificado-de-recebiveis}/{ticker}/agenda?page={n}
```

A agenda é paginada. Medido: ~1,74 s por ativo em headless, e paraleliza com 4–6 contextos.

---

## Armadilhas

**É o passo mais lento do pipeline** — milhares de páginas. Numa rodada de bootstrap, conte
horas.

**"características não capturadas" é comum e nem sempre é erro.** O portal não cobre todo
papel, e alguns campos faltam na fonte.

**Nunca deixe este script escrever agenda sobre ativo da B3.** A guarda está no
`dados.py`; se alguém a contornar, o erro é silencioso e contamina a precificação.

**A skip-list é curada à mão.** O extrator do bundle a protege por nome. Sobrescrevê-la
apaga trabalho manual — e o sintoma é o script voltar a tentar papéis que já se sabia não
existirem.
