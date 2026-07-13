# scrape_b3_bond_details.py

> **Fonte PRIMÁRIA do cadastro e do fluxo dos ativos.** Passo **2** do pipeline (logo depois do boletim, antes do `scrape_anbima_data_ativos`). Criado em 13/07/2026. Contexto e números em [[15 - Cadastro dos Ativos]].

## O que faz

Pega os tickers que **negociaram** na data e aos quais falta alguma informação que a B3 preenche, e chama `GET /getBondDetails/{ticker}`. Grava cadastro + agenda em `InfoAtivos` + `FluxoAtivos`, e marca `stFluxoValidado = 1` — o fluxo da B3 **é** a fonte, não há contra o que conferir.

## O gate (sem ele o script é inviável)

Só bate na API se o ativo tem campo faltante:

- `cdFonteCadastro IS NULL` (nunca passou pela B3), **ou**
- algum escalar NULL (`cdEmissor`, `dtVencimento`, `dtEmissao`, `vrTaxaEmissao`, `cdIndexador`, `cdInstrumento`), **ou**
- IPCA sem `vrAniversario` (a calc precisa), **ou**
- `stTemFluxo = 0`

Sem o gate seriam **~2.900 chamadas por rodada** — foi quanto negociou nos últimos 30 dias.

Lê de **`NegociosBrutos`**, não de `NegociosProcessados`: o `filtrar_trades` só roda muito depois no pipeline.

O `LEFT JOIN` faz ticker que negociou e **nem existe** no `InfoAtivos` entrar na fila — eram **701** dos 1.085 que negociaram em 90 dias sem fluxo na base.

## As três normalizações do fluxo (`FluxoDaB3`)

**Sem as três o PU sai errado.** Custaram 122 ativos com erro acima de 1%.

**1. Datas ajustadas para dia útil.** A B3 manda a data **crua** — o TAEE17 incorpora em **15/03/2025, um sábado**. A calc casa evento com aniversário, e o aniversário passa por `ProximoDu`. Evento **passado** que caia em fim de semana **não casa e é descartado, em silêncio**. Gravamos já ajustado.

**2. Amortização final completada.** Nos `IPCA-I` a B3 **não emite** o `'A'` do vencimento: os `'A'` somam menos de 100 (TAEE17: **96,8**) e o vencimento vem só com um `'J'` de yield 0. É fatal porque o `InferirTipoAmort` da calc decide a convenção **pela soma** — ≠ 100 vira `saldo_restante`, e o papel inteiro passa a amortizar errado. O resto vai para o vencimento.

**3. Datas de cupom (`'J'`) entram como amortização zero.** A calc ancora o juros de cada período no evento anterior; sem elas ela acha que o papel acumula juros por 11 anos sem pagar. Guardar só os `'A'` derruba a aderência do modelo B3 de **92% para 25%**.

## O pacote indivisível

`vrVNE` + `dtInicioRentabilidade` + `FluxoAtivos` são **um pacote**. A B3 pré-capitaliza a carência dentro do VNE e não emite incorporação; a Anbima traz o VNE cru e a incorporação como evento. **Misturar conta a capitalização duas vezes, sem erro nenhum.**

Por isso o script grava `cdFonteCadastro = 'B3'`, e o `lib.db.SincronizarFluxoAtivos` **se recusa a escrever** no fluxo de um ativo B3.

Os campos **escalares** vão com `COALESCE` (a B3 preenche buraco, não sobrescreve o que a Anbima já gravou). Exceção: `vrAniversario`, para o qual ela é a única fonte explícita.

## Ordem de escrita (importa)

Gravar `vrVNE`/`dtInicioRentabilidade` dispara o `trgInfoAtivosInvalidaFluxo`, que **zera** `stFluxoValidado`. Por isso a validação é reafirmada **depois**, no fim da função.

## CLI

```bash
python scripts/scrape_b3_bond_details.py                      # última data com negócio
python scripts/scrape_b3_bond_details.py --date 2026-07-10
python scripts/scrape_b3_bond_details.py --start X --end Y
python scripts/scrape_b3_bond_details.py --tickers SSRU11     # ignora o gate
python scripts/scrape_b3_bond_details.py --todos              # backfill sobre o InfoAtivos inteiro
python scripts/scrape_b3_bond_details.py --todos --forcar     # ignora o gate (reprocessar após corrigir o parser)
```

`--forcar` existe porque o gate impede reprocessar um ativo que já tem tudo preenchido — e foi exatamente o que precisei quando descobri as três armadilhas acima.

## Cobertura

A B3 cobre **2.355** dos 4.197 ativos com fluxo (**58% do volume negociado**). O que ela não cobre cai para a Anbima. Ver [[15 - Cadastro dos Ativos]] para o quadro completo.
