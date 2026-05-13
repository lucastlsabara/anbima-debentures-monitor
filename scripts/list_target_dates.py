#!/usr/bin/env python3
"""Lista as 5 datas alvo (dias uteis B3) para o catch-up diario da pipeline ANBIMA.

A primeira data e sempre HOJE (mesmo se HOJE for sabado/domingo/feriado B3 --
ANBIMA respondera 404 nesses casos e o workflow tratara como skip). As outras
quatro sao os quatro dias uteis B3 imediatamente anteriores ao primeiro dia
util B3 menor ou igual a HOJE.

Espelha a janela da Routine no claude.ai (5 dias uteis B3 contando HOJE).

Uso:
    python3 scripts/list_target_dates.py
    python3 scripts/list_target_dates.py --date 2026-05-13

Saida: uma data por linha em formato YYYY-MM-DD (stdout).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Permite executar `python3 scripts/list_target_dates.py` direto, sem precisar
# PYTHONPATH, importando b3_calendar do diretorio raiz do repo.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from b3_calendar import is_b3_business_day, today_brt  # noqa: E402


def list_target_dates(today: date) -> list[date]:
    """5 datas alvo: HOJE + 4 dias uteis B3 anteriores ao ultimo dia util <= HOJE."""
    out: list[date] = [today]
    if is_b3_business_day(today):
        anchor = today
    else:
        anchor = today - timedelta(days=1)
        while not is_b3_business_day(anchor):
            anchor -= timedelta(days=1)
    d = anchor - timedelta(days=1)
    collected = 0
    while collected < 4:
        if is_b3_business_day(d):
            out.append(d)
            collected += 1
        d -= timedelta(days=1)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--date",
        default=None,
        help="YYYY-MM-DD usado como HOJE (default: hoje em America/Sao_Paulo).",
    )
    args = p.parse_args()
    if args.date:
        today = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        today = today_brt()
    for d in list_target_dates(today):
        print(d.isoformat())
    return 0


if __name__ == "__main__":
    sys.exit(main())
