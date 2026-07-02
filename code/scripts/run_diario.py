"""
run_diario.py
=============
Entrypoint da rotina diária — pensado para o Task Scheduler chamar o python.exe
diretamente (sem .bat), ex.:

    pythonw.exe  Z:\\AntonioOliveira\\NegociacaoSecundario\\code\\scripts\\run_diario.py

Uso:
    python scripts/run_diario.py                          # últimos 5 dias úteis
    python scripts/run_diario.py --last 10                # últimos 10 dias úteis
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
    p.add_argument("--last", type=int, default=5,
                   help="Nº de dias úteis a reprocessar na rotina diária (default 5).")
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
    else:
        pipeline_core.run_ultimos_n(args.last)


if __name__ == "__main__":
    main()
