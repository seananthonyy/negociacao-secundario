"""
conferir_dados.py
=================
Confere o contrato de codigos/helpers/dados.py: a mesclagem coluna a coluna, as tres
politicas, a invalidacao de fluxo (o que era o trigger) e o round-trip de tipos.

Existe porque foi ele que achou tres bugs que teriam passado em silencio:
  - NULL volta como NaN e NaN != NaN, entao agenda identica parecia ter mudado e
    invalidava a validacao TODO dia;
  - groupby.nth deixou de agregar no pandas 2 e quebrava o SOBRESCREVER;
  - coluna inteira com NULL estourava no pyarrow.

NAO encosta na base real: aponta [dados] raiz para uma pasta temporaria antes de
importar o dados.py. Rodar de qualquer lugar:

    python codigos/conferir_dados/conferir_dados.py
    python codigos/conferir_dados/conferir_dados.py /tmp/minha_raiz
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers"))

import config
# Pasta temporaria do SISTEMA, nao `./tmp_parquet` (02/09/2026). O default relativo
# escrevia DENTRO do repositorio, e os 4 parquets que ele gerou foram parar no git no
# commit 895fd88 — de modo que toda execucao deste teste sujava a arvore. O
# tests_fase1.py sempre fez certo (tempfile); este era o unico fora do padrao.
# O override posicional continua valendo, para inspecionar a base depois.
TMP = (Path(sys.argv[1]).resolve() if len(sys.argv) > 1
       else Path(tempfile.mkdtemp(prefix="conferir_dados_")))
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)
config.cfg["dados"]["raiz"] = str(TMP).replace("\\", "/")
print(f"base de teste em {TMP}")

import pandas as pd
import dados as D

falhas = []
def Conferir(nome, ok, detalhe=""):
    print(("  ok   " if ok else "  FALHA") + f"  {nome} {detalhe}")
    if not ok:
        falhas.append(nome)

# ---------------------------------------------------------------------------
print("\n1. Mesclar em tabela vazia (linha nova)")
D.Mesclar("InfoAtivos", pd.DataFrame([
    {"cdTicker": "AAAA11", "cdEmissor": "Emissor A", "cdIndexador": "IPCA",
     "vrTaxaEmissao": 6.5, "cdFonteCadastro": "AnbimaData"},
    {"cdTicker": "BBBB11", "cdEmissor": "Emissor B", "cdIndexador": "CDI+"},
]))
info = D.Ler("InfoAtivos")
Conferir("2 linhas gravadas", len(info) == 2, f"({len(info)})")
Conferir("coluna ausente nasce NULL", pd.isna(info.set_index("cdTicker").loc["BBBB11", "vrVNE"]))

# ---------------------------------------------------------------------------
print("\n2. Escritor parcial nao apaga o que o outro escreveu")
D.Mesclar("InfoAtivos", pd.DataFrame([
    {"cdTicker": "AAAA11", "vrDuration": 3.2, "dtAtualizacaoDuration": "2026-08-31"},
]))
info = D.Ler("InfoAtivos").set_index("cdTicker")
Conferir("vrDuration entrou", info.loc["AAAA11", "vrDuration"] == 3.2)
Conferir("cdEmissor sobreviveu", info.loc["AAAA11", "cdEmissor"] == "Emissor A")
Conferir("vrTaxaEmissao sobreviveu", info.loc["AAAA11", "vrTaxaEmissao"] == 6.5)
Conferir("linha nao tocada intacta", info.loc["BBBB11", "cdEmissor"] == "Emissor B")

# ---------------------------------------------------------------------------
print("\n3. Politicas por coluna")
# PREFERIR_ATUAL: so preenche buraco -> nao muda o que ja tem valor
D.Mesclar("InfoAtivos", pd.DataFrame([
    {"cdTicker": "AAAA11", "cdIndexador": "PREFIXADO", "cdEmissor": "Emissor Z"},
]), politica={"cdIndexador": D.PREFERIR_ATUAL, "cdEmissor": D.SOBRESCREVER})
info = D.Ler("InfoAtivos").set_index("cdTicker")
Conferir("PREFERIR_ATUAL preserva", info.loc["AAAA11", "cdIndexador"] == "IPCA")
Conferir("SOBRESCREVER troca", info.loc["AAAA11", "cdEmissor"] == "Emissor Z")

# PREFERIR_NOVO com NULL no novo -> mantem o atual
D.Mesclar("InfoAtivos", pd.DataFrame([
    {"cdTicker": "AAAA11", "vrDuration": None},
]))
info = D.Ler("InfoAtivos").set_index("cdTicker")
Conferir("PREFERIR_NOVO ignora NULL", info.loc["AAAA11", "vrDuration"] == 3.2)

# SOBRESCREVER com NULL -> apaga mesmo
D.Mesclar("InfoAtivos", pd.DataFrame([{"cdTicker": "BBBB11", "cdEmissor": None}]),
          politica={"cdEmissor": D.SOBRESCREVER})
info = D.Ler("InfoAtivos").set_index("cdTicker")
Conferir("SOBRESCREVER apaga com NULL", pd.isna(info.loc["BBBB11", "cdEmissor"]))

# ---------------------------------------------------------------------------
print("\n4. Dobrar: duas linhas da mesma chave no mesmo lote")
D.Mesclar("InfoAtivos", pd.DataFrame([
    {"cdTicker": "CCCC11", "cdEmissor": "primeiro", "cdISIN": "BRXXX1"},
    {"cdTicker": "CCCC11", "cdEmissor": "segundo",  "cdISIN": None},
]))
info = D.Ler("InfoAtivos").set_index("cdTicker")
Conferir("uma linha so por chave", len(D.Ler("InfoAtivos")) == 3)
Conferir("ultimo nao-nulo vence", info.loc["CCCC11", "cdEmissor"] == "segundo")
Conferir("NULL do 2o nao apaga o 1o", info.loc["CCCC11", "cdISIN"] == "BRXXX1")

# Chave repetida no lote SOB a politica SOBRESCREVER — o caminho que o groupby.nth
# quebrava (no pandas 2 ele deixou de agregar). Aqui o NULL do segundo tem de vencer.
D.Mesclar("InfoAtivos", pd.DataFrame([
    {"cdTicker": "DDDD11", "cdEmissor": "primeiro"},
    {"cdTicker": "DDDD11", "cdEmissor": None},
]), politica={"cdEmissor": D.SOBRESCREVER})
info = D.Ler("InfoAtivos").set_index("cdTicker")
Conferir("SOBRESCREVER dobra sem quebrar", len(D.Ler("InfoAtivos")) == 4)
Conferir("SOBRESCREVER: NULL do 2o vence", pd.isna(info.loc["DDDD11", "cdEmissor"]))

# ---------------------------------------------------------------------------
print("\n5. O trigger: mudanca em coluna de fluxo zera a validacao")
info = D.Ler("InfoAtivos")
info.loc[info["cdTicker"] == "AAAA11", ["stFluxoValidado", "dtValidacaoFluxo",
                                        "cdFonteValidacaoFluxo"]] = [1, "2026-08-30", "B3"]
D.GravarTudo("InfoAtivos", info)

# (a) reescrever o MESMO valor nao invalida
D.Mesclar("InfoAtivos", pd.DataFrame([{"cdTicker": "AAAA11", "vrTaxaEmissao": 6.5}]))
info = D.Ler("InfoAtivos").set_index("cdTicker")
Conferir("mesmo valor NAO invalida", info.loc["AAAA11", "stFluxoValidado"] == 1)

# (b) coluna que nao define fluxo nao invalida
D.Mesclar("InfoAtivos", pd.DataFrame([{"cdTicker": "AAAA11", "cdEmissor": "Outro"}]))
info = D.Ler("InfoAtivos").set_index("cdTicker")
Conferir("coluna fora da lista NAO invalida", info.loc["AAAA11", "stFluxoValidado"] == 1)

# (c) mudanca de verdade invalida
D.Mesclar("InfoAtivos", pd.DataFrame([{"cdTicker": "AAAA11", "vrTaxaEmissao": 7.0}]))
info = D.Ler("InfoAtivos").set_index("cdTicker")
Conferir("mudanca INVALIDA", info.loc["AAAA11", "stFluxoValidado"] == 0)
Conferir("dtValidacaoFluxo zerada", pd.isna(info.loc["AAAA11", "dtValidacaoFluxo"]))
Conferir("outro ativo intacto", info.loc["CCCC11", "cdEmissor"] == "segundo")

# (d) preencher um NULL tambem invalida
info = D.Ler("InfoAtivos")
info.loc[info["cdTicker"] == "BBBB11", "stFluxoValidado"] = 1
D.GravarTudo("InfoAtivos", info)
D.Mesclar("InfoAtivos", pd.DataFrame([{"cdTicker": "BBBB11", "vrVNE": 1000.0}]))
info = D.Ler("InfoAtivos").set_index("cdTicker")
Conferir("preencher NULL invalida", info.loc["BBBB11", "stFluxoValidado"] == 0)

# ---------------------------------------------------------------------------
print("\n6. Tabela particionada: Mesclar por dia")
D.Mesclar("AnbimaIndicativos", pd.DataFrame([
    {"cdTicker": "AAAA11", "dtReferencia": "2026-08-28", "vrTaxaAnbima": 6.1},
    {"cdTicker": "BBBB11", "dtReferencia": "2026-08-28", "vrTaxaAnbima": 6.2},
]), data="2026-08-28")
D.Mesclar("AnbimaIndicativos", pd.DataFrame([
    {"cdTicker": "AAAA11", "dtReferencia": "2026-08-28", "vrSpreadAnbima": 210.0},
]), data="2026-08-28")
ai = D.Ler("AnbimaIndicativos", "2026-08-28").set_index("cdTicker")
Conferir("2 linhas no dia", len(ai) == 2, f"({len(ai)})")
Conferir("taxa sobreviveu ao 2o escritor", ai.loc["AAAA11", "vrTaxaAnbima"] == 6.1)
Conferir("spread entrou", ai.loc["AAAA11", "vrSpreadAnbima"] == 210.0)
D.Mesclar("AnbimaIndicativos", pd.DataFrame([
    {"cdTicker": "AAAA11", "dtReferencia": "2026-08-27", "vrTaxaAnbima": 5.9},
]), data="2026-08-27")
Conferir("particao anterior intacta",
         D.Escalar('SELECT vrTaxaAnbima FROM "AnbimaIndicativos" '
                   "WHERE cdTicker='AAAA11' AND dtReferencia='2026-08-28'") == 6.1)
Conferir("as duas particoes leem juntas",
         len(D.Consultar('SELECT * FROM "AnbimaIndicativos"')) == 3)

# ---------------------------------------------------------------------------
print("\n7. SincronizarFluxoAtivos")
linhas = [{"cdTicker": "CCCC11", "dtEvento": "2027-01-15", "vrPctAmortizacao": 50.0,
           "vrPctIncorporacao": None, "dtAtualizacao": "2026-08-31"},
          {"cdTicker": "CCCC11", "dtEvento": "2028-01-15", "vrPctAmortizacao": 50.0,
           "vrPctIncorporacao": None, "dtAtualizacao": "2026-08-31"}]
Conferir("1a gravacao muda", D.SincronizarFluxoAtivos("CCCC11", linhas) is True)
Conferir("2 eventos", len(D.Ler("FluxoAtivos")) == 2)
Conferir("stTemFluxo marcado",
         D.Escalar('SELECT stTemFluxo FROM "InfoAtivos" WHERE cdTicker=\'CCCC11\'') == 1)
Conferir("agenda identica NAO reescreve",
         D.SincronizarFluxoAtivos("CCCC11", linhas) is False)

# fonte B3 e intocavel
info = D.Ler("InfoAtivos")
info.loc[info["cdTicker"] == "CCCC11", "cdFonteCadastro"] = "B3"
D.GravarTudo("InfoAtivos", info)
mudadas = [dict(l, vrPctAmortizacao=99.0) for l in linhas]
Conferir("fonte B3 recusa escrita", D.SincronizarFluxoAtivos("CCCC11", mudadas) is False)
Conferir("fluxo da B3 intacto",
         D.Escalar('SELECT vrPctAmortizacao FROM "FluxoAtivos" '
                   "WHERE cdTicker='CCCC11' AND dtEvento='2027-01-15'") == 50.0)

# ---------------------------------------------------------------------------
print("\n8. Tipos: inteiro nulavel sobrevive ao round-trip")
info = D.Ler("InfoAtivos")
Conferir("stTemFluxo e inteiro", str(info["stTemFluxo"].dtype) in ("int64", "Int64"),
         str(info["stTemFluxo"].dtype))
Conferir("vrAniversario NULL preservado", info["vrAniversario"].isna().all())

# ---------------------------------------------------------------------------
print("\n9. PuPar: mudanca de cadastro ou de fluxo descarta o PU par do ativo")
# O puPar e funcao do fluxo, do VNE, do indexador e da taxa de emissao. Se qualquer um
# muda, TODO puPar ja gravado daquele ativo foi calculado com a premissa errada --
# inclusive os de datas passadas, porque a agenda velha valia para elas tambem. Sem
# este descarte, o %par do relatorio (calculado na leitura, dividindo pelo puPar) sairia
# de um denominador em que ninguem mais acredita, sem erro e sem log.
puPar = [{"cdTicker": "PPPP11", "dtReferencia": d, "vrPuPar": 1000.0,
          "cdFontePuPar": "Calc", "dtCriacao": "2026-09-02 00:00:00"}
         for d in ("2026-07-27", "2026-07-28")]
for linha in puPar:
    D.Mesclar("PuPar", pd.DataFrame([linha]), data=linha["dtReferencia"])
D.Mesclar("InfoAtivos", pd.DataFrame([
    {"cdTicker": "PPPP11", "vrTaxaEmissao": 7.0, "cdIndexador": "IPCA",
     "cdFonteCadastro": "Anbima"}]))
Conferir("PuPar aceita duas datas do mesmo ticker",
         D.Escalar('SELECT COUNT(*) FROM "PuPar" WHERE cdTicker=\'PPPP11\'') == 2)

# reescrever o MESMO valor nao descarta (a comparacao e null-safe, igual ao trigger)
D.Mesclar("InfoAtivos", pd.DataFrame([{"cdTicker": "PPPP11", "vrTaxaEmissao": 7.0}]))
Conferir("cadastro reescrito igual NAO descarta",
         D.Escalar('SELECT COUNT(*) FROM "PuPar" WHERE cdTicker=\'PPPP11\'') == 2)

# mudar a taxa de emissao muda o par: o historico inteiro do ticker sai
D.Mesclar("InfoAtivos", pd.DataFrame([{"cdTicker": "PPPP11", "vrTaxaEmissao": 8.5}]),
          politica={"vrTaxaEmissao": D.SOBRESCREVER})
Conferir("mudar vrTaxaEmissao descarta as DUAS datas",
         D.Escalar('SELECT COUNT(*) FROM "PuPar" WHERE cdTicker=\'PPPP11\'') == 0)

# e o mesmo vale pelo caminho do fluxo
D.Mesclar("PuPar", pd.DataFrame([puPar[0]]), data=puPar[0]["dtReferencia"])
D.SincronizarFluxoAtivos("PPPP11", [
    {"cdTicker": "PPPP11", "dtEvento": "2028-03-15", "vrPctAmortizacao": 100.0,
     "vrPctIncorporacao": None, "dtAtualizacao": "2026-09-02 00:00:00"}])
Conferir("agenda nova descarta o PU par",
         D.Escalar('SELECT COUNT(*) FROM "PuPar" WHERE cdTicker=\'PPPP11\'') == 0)

# ativo alheio nao pode ser afetado
D.Mesclar("PuPar", pd.DataFrame([dict(puPar[0], cdTicker="QQQQ11")]),
          data=puPar[0]["dtReferencia"])
D.Mesclar("InfoAtivos", pd.DataFrame([{"cdTicker": "PPPP11", "vrTaxaEmissao": 9.9}]),
          politica={"vrTaxaEmissao": D.SOBRESCREVER})
Conferir("descarte nao encosta em outro ticker",
         D.Escalar('SELECT COUNT(*) FROM "PuPar" WHERE cdTicker=\'QQQQ11\'') == 1)

print("\n" + ("TODOS OS TESTES PASSARAM" if not falhas
              else f"{len(falhas)} FALHA(S): {falhas}"))

# Passou e a pasta e nossa: nao ha o que inspecionar. Se FALHOU, a base fica de pe
# para o diagnostico — e o caminho foi impresso no comeco da rodada.
if not falhas and len(sys.argv) == 1:
    shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if falhas else 0)
