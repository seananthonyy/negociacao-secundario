# gerar_relatorio_credito.py

**O último passo.** Gera `relatorios/relatorio_secundario.html`.

---

## Overview

O relatório HTML interativo, com **seis abas**, sobre toda a base:

| aba | o que mostra |
|---|---|
| **Boletim Diário** | o pregão inteiro por ticker, com seletor de data |
| **Visão Mercado** | volume e spread médio ponderado, dia a dia, por indexador |
| **Visão Anbima** | spread das indicativas e as curvas NTN-B / DI por vértice |
| **Por Ativo** | volume e spread de ticker(s) selecionado(s) |
| **Spread × Duration** | bolhas: X = duration, Y = spread, tamanho = volume |
| **Info Ativos** | o cadastro, para conferência |

O relatório **diário** existia como script separado e foi absorvido aqui em 03/09/2026 —
eram duas implementações da mesma agregação, e elas já tinham divergido: a Taxa Anbima do
diário vinha do primeiro negócio, a do geral é média ponderada por volume.

---

## Regras de negócio

### O pregão de hoje entra, marcado como PRÉVIA

Até 31/08/2026 o corte era em **D-1**: o pregão de hoje não fechou, e os negócios já
lançados para a liquidação de hoje são a **perna D+1 do pregão anterior** — meio dia de
dado. Publicá-los sem aviso mostrava volume e spread incompletos como se fossem fechados.

O problema nunca foi o dado; era a ausência do aviso. Agora o corte é o último dia útil
≤ hoje, e o selo aparece em quatro lugares: banner no topo (vale para todas as abas),
sufixo no seletor do Boletim, nota dentro do Boletim, e `[PRÉVIA]` no assunto do email.

### `%par`, calculado na leitura

`vrPU / vrPuPar × 100`. **Não é coluna da base.** A CTE `PuParVigente` resolve o puPar de
cada ticker com `dtReferencia <= dtLiquidacao`, particionando por `cdTicker` — todos os
negócios do mesmo ticker no mesmo dia dividem o denominador, então resolver por ticker
devolve uma linha em vez de uma por negócio, e a consulta não cresce com o histórico.

O `<=` é o que atende o **papel que saiu do cadastro**: sem linha nova para a data, vem a
última que existe, e a **idade** dela (em pregões) diz se a referência está viva ou
congelada. Acima de `PREGOES_CONGELADO = 5` a célula ganha selo, e o dia ganha uma pílula
com a contagem.

### Banda de sanidade do spread

**Só nas médias AGREGADAS por indexador** (Visão Mercado). Não altera a base nem as visões
por-ticker, onde o usuário precisa enxergar o caso extremo.

| indexador | faixa |
|---|---|
| CDI+ / IPCA / PREFIXADO | `\|spread\| ≤ 15%` |
| %CDI | 60% a 180% (é multiplicador, bilateral) |

Erros de PU↔taxa a montante produzem spreads impossíveis que detonam a média. A banda
decide se a linha entra **na média**; o volume é sempre somado integralmente.

### Outras regras

- **Agrupa por `dtLiquidacao`**, não `dtNegocio`.
- **`VALIDO` + `BROKER`**, com o grupo BROKER agregado por `idGrupoNegocio` e volume ÷ 2.
- **Anbima em D-1** da liquidação, casada por `dtNegocio` de cada negócio.
- **`DEB` indexada a IPCA ou PREFIXADO** é exibida como `DEB 12.431` (incentivada).
- **Peso da Visão Anbima é automático:** outstanding real se a tabela tem dados; senão
  quantidade de emissão como proxy, com disclaimer na aba.

---

## CLI

```powershell
python codigos\scripts\gerar_relatorio_credito\gerar_relatorio_credito.py
python codigos\scripts\gerar_relatorio_credito\gerar_relatorio_credito.py --email-dia 2026-09-02
```

| argumento | efeito |
|---|---|
| *(nenhum)* | Toda a base, com o pregão de hoje como prévia |
| `--sem-previa` | Volta ao corte em D-1 |
| `--ate AAAA-MM-DD` | Última liquidação a publicar. **Vence os dois** |
| `--email-dia AAAA-MM-DD` | Salva rascunho com o top 20 por volume daquele pregão, com o HTML em anexo |

---

## Interação com a base

**Lê:** `NegociosProcessados`, `NegociosBrutos` (instrumento e o filtro de cancelado),
`InfoAtivos`, `AnbimaIndicativos`, `MtmAnbima`, `Outstanding`, `PuPar`.

**Grava:** nada na base. Só o HTML em `relatorios/` e, com `--email-dia`, o rascunho.

---

## Detalhes técnicos

Jinja2 sobre `template_relatorio_secundario.html`, que mora **ao lado do script** — é dado
dele. O prefixo `template_` evita confundi-lo com o HTML de saída, que se chamaria igual em
`relatorios/`.

Os dados vão para o template como um único `data_json`; os gráficos são ECharts, e o
arquivo final passa de 40 MB porque embute a base inteira.

---

## Armadilhas

**O número de aceitação é 34 pregões · 2.568 ativos · R$ 52.315,17 MM**, medido sobre a
base até 28/07/2026. Qualquer mudança que não deveria mexer em nada tem de reproduzi-lo.

**Muitos papéis com o MESMO `dtPuPar` não são defaults.** É o `calc_pu_par` que não rodou
para o dia. Poucos papéis com datas **distintas** entre si é o caso real. O disclaimer da
aba ensina as duas leituras.

**A prévia não é erro.** Se o pregão de hoje está no corte mas ainda não tem negócio na
base (rodada da manhã), o resumo diz "sem negócio na base ainda" — em vez de sumir.

**`--ate` vence `--sem-previa`.** Se os dois forem passados, o corte explícito manda.

**O template é `.html`, não `.j2`.** O Jinja não liga para a extensão; o `FileSystemLoader`
aponta para a pasta do script.
