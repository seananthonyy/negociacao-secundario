# Pipeline de execução

**19 passos.** A ordem não é arbitrária: cada dependência abaixo já custou um bug.

O CLI de cada script vive num lugar só — as funções de `codigos/helpers/pipeline_core.py`.
Os notebooks e qualquer rotina chamam essas funções, nunca remontam o comando. Cada passo
roda como **subprocesso**, com log e email próprios.

```python
import pipeline_core as pc

pc.RodarUltimosN(5)              # a rotina diária: últimos 5 pregões
pc.RodarIntervalo(ini, fim)      # reprocessar um período
pc.RodarDia(X)                   # uma liquidação só
pc.Boletim(Xant, X)              # testar um fluxo isolado
```

---

## A lógica das datas

O relatório agrupa por **`dtLiquidacao`**, não por `dtNegocio`. Uma liquidação `X` recebe
duas pernas: os negócios do pregão de `X` (D+0) e os do pregão anterior `X-1u` (D+1).

Por isso vários passos rodam em **duas datas**: `X` e `X-1u`. Onde só aparece `X`, é
porque aquele passo trabalha sobre a liquidação já montada.

---

## Os 19 passos

| # | script | argumentos | data | rede | o que faz |
|---|---|---|---|---|---|
| 1 | `scrape_b3_boletim` | `--start X-1u --end X` | X-1u, X | ✅ | Os negócios, um a um. **Única fonte de negócio do projeto** |
| 2 | `scrape_b3_bond_details` | `--start X-1u --end X` | X-1u, X | ✅ | **Fonte primária do cadastro.** Cadastro + fluxo de quem negociou. Não valida |
| 3 | `scrape_anbima_debentures` | `--date d` | X-1u, X | ✅ | Taxa indicativa das debêntures |
| 4 | `scrape_anbima_cri_cra` | `--date d` | X-1u, X | ✅ | Taxa indicativa de CRI/CRA |
| 5 | `scrape_fianalytics_planilha` | *(sem data)* | — | ✅ | Ficha resumida do papel; só preenche buraco |
| 6 | `scrape_anbima_data_ativos` | `--start X-1u --end X` | X-1u, X | ✅ | Cadastro + fluxo do que a B3 não cobriu. **O passo mais lento** |
| 7 | `scrape_anbima_ntnb` | `--start X-1u --end X` | X-1u, X | ✅ | Curva NTN-B → `MtmAnbima` |
| 8 | `scrape_b3_curva_di` | `--date d` | X-1u, X | ✅ | Curva DI → `MtmAnbima` (DI1) **e** `CurvaDi` |
| 9 | `scrape_ipca_ibge` | *(sem data)* | — | ✅ | IPCA realizado |
| 10 | `scrape_ipca_projetado_anbima` | *(sem data)* | — | ✅ | Projeção de IPCA. **Rodar antes das 17h30** |
| 11 | `scrape_di_bcb` | *(sem args = incremental)* | — | ✅ | DI realizado |
| 12 | `scrape_outstanding_bloomberg` | `--start --end` | X-1u, X | terminal | Saldo em circulação. **Só no banco** |
| 13 | `validar_calc_b3` | *(sem args)* | — | ✅ | **O gate.** Decide em quais ativos a calc local pode precificar |
| 14 | `calc_taxa_negocios` | `--date X` | X | ✅ | Taxa por negócio. Cascata: direta → calc → FI → B3 |
| 15 | `filtrar_trades` | `--date X` | X | ❌ | Classifica `VALIDO` / `FUNDO` / `BROKER` / `PF` |
| 16 | `calc_pu_par` | `--date X` | X | ✅ | PU par por (ativo, data) → `PuPar` |
| 17 | `calc_spread_anbima` | `--date X-1u` | X-1u | ❌ | Spread das indicativas |
| 18 | `match_referencias` | *(sem data)* | — | ❌ | Preenche `cdReferencia` por duration-match |
| 19 | `calc_spread_over` | `--date X` | X | ❌ | Spread dos negócios contra a curva |
| — | `gerar_relatorio_credito` | *(sem args)* | — | ❌ | Regenera o HTML sobre toda a base |

---

## As ordens obrigatórias, e por quê

**`scrape_b3_bond_details` (2) antes de `scrape_anbima_data_ativos` (6).** A B3 é a fonte
primária do cadastro; a Anbima só deve preencher o que ela não cobriu. Invertido, a Anbima
escreveria por cima e o pacote VNE+início+fluxo ficaria misturado.

**`scrape_anbima_data_ativos` (6) antes de `validar_calc_b3` (13).** É o passo 6 que
atualiza `FluxoAtivos`/`InfoAtivos` e, quando o fluxo muda de verdade, **zera a
validação**. Validar antes seria validar um fluxo prestes a mudar.

**`validar_calc_b3` (13) antes de `calc_taxa_negocios` (14).** O gate decide quais ativos
a calc local pode precificar. Sem ele, o passo 14 usaria a calc em ativo não confirmado.

**`calc_taxa_negocios` (14) antes de `filtrar_trades` (15).** O filtro só atualiza
`cdStatus` em linhas que já existem em `NegociosProcessados` — é o passo 14 que as cria.

**`match_referencias` (18) antes de `calc_spread_over` (19).** O spread depende de
`cdReferencia` estar preenchido.

**`calc_pu_par` (16) depois de 13 e 14.** Precisa do gate (para saber onde a calc vale) e
das linhas de `NegociosProcessados` (de onde sai a fila). Não depende de curva nem de
match — é preço, não spread.

---

## Duas armadilhas de data

**MtM precisa de X-1u E X.** O `calc_spread_over` busca a referência em
`MtmAnbima WHERE dtReferencia = dtNegocio` — pela data do **negócio**, não da liquidação.
Como os negócios de `X` liquidam tanto pela perna `X-1u` quanto pela `X`, as duas datas de
MtM têm de existir. **Sintoma:** `nullSemMtm > 0` no resumo do passo 19.

**As indicativas são de X-1u.** O `calc_spread_anbima` roda em `X-1u` porque é a data das
indicativas que o relatório exibe. Por isso os passos 3 e 4 também raspam as duas pontas.

---

## Uma rodada típica

```powershell
# a diária: últimos 5 pregões, pega alteração retroativa
python -c "import sys; sys.path.insert(0,'codigos/helpers'); import pipeline_core as pc; pc.RodarUltimosN(5)"

# reprocessar um período
python -c "import sys; sys.path.insert(0,'codigos/helpers'); import pipeline_core as pc; pc.RodarIntervalo('2026-08-01','2026-08-31')"
```

Ou pelo notebook `rotinas/run-pipeline-diario.ipynb`, que é o mesmo com um botão.

**Em qualquer rodada de lote, use `NEGSEC_SEM_EMAIL=1`.** Sem isso são ~20 emails por
pregão, e o COM do Outlook deixa processo órfão.

---

## Janelas das fontes

Limitam o quanto de histórico dá para reconstruir. Medido em 01–02/09/2026:

| fonte | alcance |
|---|---|
| DI realizado (BCB) | desde 03/01/2000 |
| IPCA (IBGE) | desde 12/1979 |
| Anbima deb / NTN-B | **desde 21/02/2026** — janela deslizante de ~6 meses |
| Anbima CRI/CRA | ~5 pregões |
| Curva DI (B3) | ~20 pregões |
| Anbima Data (cadastro) | universo completo (não é série) |

Detalhe por fonte em `fontes.md`.

---

## Armadilhas

**Pregão sem nenhum negócio quase sempre é falha silenciosa.** A B3 sempre tem negócio em
dia útil. Se o passo 1 sair com zero, é falha de download — foi assim que um bug de proxy
passou batido. O script avisa no resumo.

**Um passo que falha não derruba os outros.** O `RodarPasso` registra e segue. Isso é
proposital numa rodada de lote, mas exige **ler o resumo do fim**: `N/M passos OK`.

**Reprocessar uma data é trocar a partição.** Rodar o passo 14 de novo para uma data
reescreve o dia inteiro. É idempotente, mas não é acumulativo.

**A rotina diária processa os últimos 5 pregões, não só o último.** É de propósito: a B3
revisa o boletim depois do pregão, e a re-raspagem pega essas revisões.
