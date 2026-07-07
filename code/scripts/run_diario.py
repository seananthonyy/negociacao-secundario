"""
run_diario.py
=============
Entrypoint da rotina diária — pensado para o Task Scheduler chamar o python.exe
diretamente (sem .bat), ex.:

    pythonw.exe  Z:\\AntonioOliveira\\NegociacaoSecundario\\code\\scripts\\run_diario.py

Uso:
    python scripts/run_diario.py                          # PADRÃO: últimos 5 dias úteis
    python scripts/run_diario.py --last 10                # últimos 10 dias úteis
    python scripts/run_diario.py --start 2026-06-01 --end 2026-07-01   # intervalo explícito
    python scripts/run_diario.py --setup --inicio-boletim 2026-03-02
    python scripts/run_diario.py --setup --inicio-boletim 2026-03-02 --outstanding

Toda a lógica está em pipeline_core.py (compartilhada com pipeline.ipynb).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # code/scripts no path
import pipeline_core


def main() -> None:
    p = argparse.ArgumentParser(description="Rotina diária / setup do pipeline de crédito secundário.")
    # Modo PADRÃO (sem argumentos): últimos 5 dias úteis.
    p.add_argument("--last", type=int, default=5,
                   help="PADRÃO: nº de dias úteis a reprocessar (default 5).")
    # Modo INTERVALO: range explícito de datas.
    p.add_argument("--start", metavar="YYYY-MM-DD",
                   help="(intervalo) 1º dia útil a reprocessar; requer --end.")
    p.add_argument("--end", metavar="YYYY-MM-DD",
                   help="(intervalo) último dia útil a reprocessar; requer --start.")
    # Modo SETUP: bootstrap inicial.
    p.add_argument("--setup", action="store_true",
                   help="Roda o bootstrap inicial da base em vez da rotina diária.")
    p.add_argument("--inicio-boletim", metavar="YYYY-MM-DD",
                   help="(setup) 1º dia do boletim B3 a raspar — define a janela do relatório.")
    p.add_argument("--outstanding", action="store_true",
                   help="(setup) também roda scrape_outstanding_bloomberg (só no banco).")
    args = p.parse_args()

    if args.setup:
        if not args.inicio_boletim:
            p.error("--setup requer --inicio-boletim YYYY-MM-DD")
        pipeline_core.run_setup(args.inicio_boletim, rodar_outstanding=args.outstanding)
    elif args.start or args.end:
        if not (args.start and args.end):
            p.error("--start e --end devem ser usados juntos.")
        pipeline_core.run_intervalo(args.start, args.end)
    else:
        pipeline_core.run_ultimos_n(args.last)


if __name__ == "__main__":
    main()
