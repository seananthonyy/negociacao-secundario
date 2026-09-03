# validar_calc_b3.py

**Passo 13** do pipeline. **O gate** — e o único validador do projeto.

---

## Overview

Responde uma pergunta só: **a nossa calculadora reproduz o que a B3 ou a FI devolvem para
este papel?** Quem passa ganha `stFluxoValidado = 1` e pode ser precificado localmente;
quem não passa cai na cascata de API.

É **bidirecional**: promove quem estava fora e rebaixa quem parou de bater.

O `validar_fluxos`, que conferia a agenda evento a evento contra a FI, foi removido em
24/08/2026 — é redundante com o teste de PU, e a cobertura da FI era magra. O que se
perdeu está em Armadilhas.

---

## Regras de negócio

### Os cinco passos

Premissa: **as duas calculadoras externas estão certas**. O gate só pergunta se a nossa
bate com uma delas, dentro da margem.

| # | data | teste | régua |
|---|---|---|---|
| 1 | — | `CarregarAtivo` monta (fluxo, VNE, indexador, taxa de emissão) | — |
| 2 | **~8 pregões atrás** | PU na taxa de emissão | `1e-5` relativo |
| 3 | **hoje** | PU a **+100 bps** | `1e-5` relativo |
| 4 | **D+1** | PU a +100 bps | `1e-5` relativo |
| 5 | **hoje** | taxa implícita no PU do passo 3 | **5 bps** |

Cada data tem papel próprio: a **passada** confere o fluxo e o VNA contra curva
**realizada**; **hoje** confere o desconto contra a curva do dia; **D+1** confere o
desconto contra a curva **projetada**.

**Por que o passo 5, se o 3 já compara a mesma curva.** A régua de PU (`1e-5` = R$ 0,01
por R$ 1.000) vale bps **diferentes** conforme a duration — é rígida demais em papel curto
e frouxa em papel longo, medida no que a mesa lê. O passo 5 mede na grandeza publicada, e
aí `5 bps` significa a mesma coisa para todo indexador (o `DiffTaxaEmBps` normaliza o
`%CDI`).

### O oráculo

Uma função só, `PuDoOraculo(ticker, data, taxa)`: tenta a **B3**, e se ela não responder
tenta a **FI**. A B3 vem primeiro porque é a fonte primária do cadastro e a régua do
próprio gate — usar a FI antes daria um PU de uma fonte com validação de outra.

Até 03/09/2026 a FI respondia **outra pergunta** aqui (round-trip taxa→PU), o que obrigava
o gate a ter dois caminhos de avaliação. Ela também faz PU dado a taxa
(`ChamarCompleto` em modo `rate`, campo `m2m`), então passou a responder a mesma coisa que
a B3.

### Veredito

| situação | resultado |
|---|---|
| Todos os testes que rodaram passaram, e alguém respondeu | **confiável** — `stFluxoValidado = 1` |
| Algum oráculo respondeu e divergiu | **reprovado** → 0 |
| **Nenhum** oráculo respondeu | **não-confirmável** → 0 |

**Oráculo mudo não penaliza.** HTTP 500, timeout ou papel não coberto é *não sei*, e a
data é pulada. Erro só é falha quando o oráculo **respondeu** e divergiu.

**Refresh antes de rebaixar.** Quem reprova ganha um cadastro fresco da B3 e é re-testado.
Só cai quem **ainda** falha — isso separa fluxo velho de erro de metodologia.

**Revalidação:** quem foi validado há menos de `--revalidar-dias` (15) é pulado. Mudança
real de cadastro ou fluxo zera a validação na hora e joga o ativo para o topo da fila.

---

## CLI

```powershell
python codigos\scripts\validar_calc_b3\validar_calc_b3.py
python codigos\scripts\validar_calc_b3\validar_calc_b3.py --dry-run --tickers ABCD11,EFGH22
```

| argumento | efeito |
|---|---|
| `--dry-run` | Só reporta; não grava |
| `--tickers A,B` | Só esses. **Ignora a janela de revalidação** |
| `--datas d1,d2,d3` | Posicional: passada, hoje, futura |
| `--sem-taxa` | Pula o passo 5 e valida só por PU. É o único teste na grandeza publicada — desligue só para diagnóstico rápido |
| `--sem-fi` | Só a B3 como oráculo |
| `--sem-refresh` | Não refresca o cadastro antes de rebaixar |
| `--negociados-dias N` | Só validados que negociaram nos últimos N dias |
| `--revalidar-dias N` | Default 15. `0` = re-testa tudo |
| `--limite N` | Corta a fila |

---

## Interação com a base

**Lê:** `InfoAtivos` (os candidatos e o cadastro), `FluxoAtivos` (via `CarregarAtivo`),
`CurvaDi` (as datas disponíveis e a curva para semear o D+1).

**Grava:** `InfoAtivos` — `stFluxoValidado`, `dtValidacaoFluxo`, `cdFonteValidacaoFluxo`,
em lote. O refresh também pode reescrever o pacote de cadastro pela B3.

**Escreve fora da base:** `cache/validar_calc_b3/validar_calc_b3.csv`, com uma linha por
ativo: `piorPU`, `piorTaxaBps`, `nConfB3`, veredito.

---

## Detalhes técnicos

B3: `CalcularPuGov` e `CalcularYield`. FI: `ChamarCompleto` (modo `rate`) e
`ChamarPrimaria` (modo `pu`). Paraleliza com `ThreadPoolExecutor`, 10 workers — é I/O puro.

**A semeadura do D+1.** A calc procura a curva de projeção pela **chave da data exata**, e
D+1 não existe na base (a B3 só publica pregão fechado). O gate pega a curva de hoje e a
registra no cache **em memória** da calc sob a chave de D+1 (`SemearAccProj`). Não escreve
na base, não toca a `calculadora_rf`.

**Só importa para papel indexado a CDI:** `CDI+` e `%CDI` precisam da `CurvaDi`; `IPCA`
usa a projeção mensal da Anbima, que já cobre D+1; `PREFIXADO` não precisa de nenhuma.

---

## Armadilhas

**O `piorTaxa` já foi calculado e jogado fora.** Até 01/09/2026 ele entrava na decisão mas
não saía em relatório nenhum — por isso a re-medição do `%CDI`, pedida desde julho, nunca
acontecia. Hoje sai no CSV e numa seção do email. Se alguém "limpar" isso de novo, o
número volta a ser invisível.

**Falha da NOSSA calc não é ausência de medida.** Se o Newton não converge num PU que o
oráculo precificou, isso é **falha** (o código marca `9.9` no PU e `9999` na taxa). Tratar
como zero faria o ativo passar como se tivesse reproduzido perfeitamente.

**O passo 4 é pulado, não reprovado, quando não dá para semear.** A falta de curva é da
nossa base, não do ativo.

**Semear o D+1 tem DUAS condições, e esquecer a segunda derruba o livro de CDI+ inteiro.**
A curva projetada (`CurvaDi`, da B3) não basta: para chegar a D+1 a calc capitaliza o DI
**realizado** dia a dia até hoje, e essa série vem da `DiHistorico`, que é do **BCB**. As
duas fontes andam em ritmos diferentes — o BCB publica com um dia de atraso, então é
*normal* a `CurvaDi` estar um pregão à frente. Em 03/09/2026 isso reprovou **1.494 papéis
CDI+ de uma vez**: a curva de 02/09 existia, o DI realizado de 02/09 não, e a exceção da
calc virava divergência em vez de ausência. Hoje o `SemearCurvaCarryForward` confere as
duas tabelas antes de semear.

**Um motivo de falha que domina a lista NÃO é o ativo — é a nossa base.** O `9.9` do CSV
não distingue "a calc levantou" de "a calc divergiu", e foi essa indistinção que escondeu o
bug acima por uma rodada inteira. Por isso o resumo traz agora a seção *"Por que a NOSSA
calc não respondeu"*, com os motivos contados. Milhares de ocorrências da mesma mensagem
significam buraco de dado: **feche o buraco e refaça a rodada**, não aceite o
rebaixamento.

**Até 15 pregões de atraso para perceber que a B3 largou um papel.** A janela de
revalidação faz o gate não re-testar quem passou há pouco. Nesse meio-tempo a nossa calc
segue acretando um par contratual para um emissor que parou de pagar. Decisão consciente:
fechar isso exigiria sondar a B3 todo pregão para todo ativo validado.

**Ponto cego B3 × FI.** O gate pergunta "a nossa calc reproduz a B3?" usando o cadastro
**da própria B3**. Se a B3 tiver cadastro errado, a nossa calc reproduz o erro dela e o
ativo **passa**. Caso conhecido: `FGEN13` — a B3 diz 1.280, a FI diz 508, o mercado negocia
a 503. Era o `ConferirSaldo` que cruzava as duas fontes, e ele saiu em 24/08.

**A régua de PU é mais apertada que a de taxa, em termos econômicos.** Medido: ativos com
taxa reproduzindo a B3 a 0,06 bps são reprovados por `piorPU = 3e-05`. A `TOL_TAXA_BPS` do
mesmo gate permitiria 5 bps. É decisão aberta no backlog.
