# Insumos da calculadora — `scrape_ipca_ibge`, `scrape_ipca_projetado_anbima`, `scrape_di_bcb`

> Os três scripts que alimentam os bancos que a **calculadora de renda fixa** lê (`code/data/ipca.db` e `code/data/di.db`). Passos **9–11** do pipeline. Migrados em 12/07/2026. Contexto completo em [[../14 - Rotinas da Calculadora]].

Nenhum depende da liquidação X — rodam **uma vez por ciclo**, no bloco global do `pipeline_core`.

---

## `scrape_ipca_ibge` — IPCA realizado

**Destino:** `ipca.db/IPCA`. **Sem argumentos.**

Duas fontes na mesma rodada:
- **SIDRA** (tabela 1737, variável 2266 = número-índice, base dez/1993) — devolve a **série inteira** (desde 1979) a cada chamada.
- **API Calendário do IBGE** — as datas de divulgação. Só cobre **dez/2016 em diante**; antes disso fica NULL, e a calc trata como mês fechado.

O UPSERT usa `COALESCE(excluded.x, x)` — **nunca apaga o que já existe**. Se o SIDRA devolver zero índices, o script **falha (exit 1)**: é quebra, não ausência de dado.

---

## `scrape_ipca_projetado_anbima` — projeção de IPCA

**Destino:** `ipca.db/IPCAProjetado`. **Sem argumentos.** Playwright.

Raspa a aba IPCA (`#profile`) da página da Anbima. Cada projeção tem uma **Data de Validade** e vale até a véspera da validade seguinte (*carry-forward*); o script expande isso em registros **diários** até D+1.

**Sem a projeção não se precifica IPCA+ no mês corrente** — entre uma divulgação e a seguinte, o VNA anda pela projeção.

- ⚠️ **Rodar antes das 17h30.** A Anbima republica por volta desse horário nos dias de divulgação.
- Playwright **com proxy explícito** (`ObterProxyPlaywright()`). O script original não passava proxy: no banco teria voltado vazio, como aconteceu com os 4 scrapers em 03/07.
- **A janela da fonte é curta (~13 meses).** O histórico mais antigo **não é reconstruível** — o `ipca.db` herdado da calc (392 dias de projeção) é **patrimônio**, não se apaga.
- Faz backup em `data/backups/ipca_*.db` (guarda 10).

---

## `scrape_di_bcb` — DI realizado

**Destino:** `di.db/DiHistorico`. `--start`/`--end` opcionais.

API SGS do BCB, **duas séries da mesma taxa**:
- **4389** — anualizada, base 252, 2 casas
- **12** — **fator diário, 6 casas**

O acúmulo do PU Par usa a **série 12**: o fator de 6 casas reproduz a calculadora da B3 a ~1e-5, enquanto a 4389 perde precisão ao virar taxa diária.

Incremental (retoma do último dia gravado); base vazia → **backfill desde 2000**. Janelas de 9 anos por requisição.

---

## `scrape_b3_curva_di` — curva DI (já existia, ganhou 2º destino)

**Destinos:** `MtmAnbima` (os 8 contratos DI1, para o relatório) **e** `di.db/CurvaDi` (a curva inteira, vértice a vértice).

O script **já baixava** o CSV completo do produto `PRE` da B3 e **jogava fora todos os vértices**. Agora arquiva a curva toda — **zero requisição a mais**.

Isso importa porque **a B3 não guarda histórico**: a API só expõe **~20 pregões**. Rodando todo dia, acumulamos os snapshots que ela descarta — é o que permite **reprecificar uma data passada**.

**Semântica de saída:** data **fora da janela** da B3 é WARNING com **exit 0** (é "a fonte não tem", não "o scraper quebrou"); qualquer outra falha é **exit 1**.

---

## Rede — hosts novos (conferir no banco)

`apisidra.ibge.gov.br` · `servicodados.ibge.gov.br` (calendário) · `api.bcb.gov.br` (SGS) · `www.anbima.com.br` (página de projeção).

**Testar esses 4 antes de confiar na rotina no banco.**
