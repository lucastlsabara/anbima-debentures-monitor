"""Backfill manual de negocios consolidados B3 para um intervalo de dias uteis.

CLI:
  python backfill_b3_trades_consolidated.py                       # 5 dias uteis recentes
  python backfill_b3_trades_consolidated.py 2026-04-24 2026-05-01 # range customizado
  python backfill_b3_trades_consolidated.py --start 2026-04-24 --end 2026-05-01

Idempotente: pula dias ja existentes em data/b3_trades_consolidated/{date}.json.
"""

from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path

import fetch_b3_trades_consolidated
from b3_calendar import is_b3_business_day


SLEEP_BETWEEN_DAYS = 1.0
DATA_DIR = Path(__file__).parent / "data" / "b3_trades_consolidated"


def _iter_business_days(start: date, end: date):
    d = start
    while d <= end:
        if is_b3_business_day(d):
            yield d
        d += timedelta(days=1)


def _parse_argv(argv: list[str]) -> tuple[date, date]:
    """Aceita posicional `start end` ou flags `--start/--end`. Sem args:
    usa a mesma janela de 5 dias uteis de fetch_b3_trades_consolidated.
    """
    start_s: str | None = None
    end_s: str | None = None
    positional: list[str] = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--start" and i + 1 < len(argv):
            start_s = argv[i + 1]
            i += 2
            continue
        if a == "--end" and i + 1 < len(argv):
            end_s = argv[i + 1]
            i += 2
            continue
        positional.append(a)
        i += 1

    if start_s and end_s:
        return date.fromisoformat(start_s), date.fromisoformat(end_s)
    if len(positional) >= 2:
        return date.fromisoformat(positional[0]), date.fromisoformat(positional[1])

    days = fetch_b3_trades_consolidated._last_n_b3_business_days(
        fetch_b3_trades_consolidated.REFRESH_WINDOW_DAYS
    )
    return days[0], days[-1]


def main(argv: list[str]) -> int:
    start, end = _parse_argv(argv)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    days = list(_iter_business_days(start, end))
    print(f"Backfill consolidated: {len(days)} dia(s) uteis entre {start} e {end}")

    skipped = 0
    fetched = 0
    failed: list[tuple[str, str]] = []

    for i, d in enumerate(days, start=1):
        date_iso = d.isoformat()
        out_path = DATA_DIR / f"{date_iso}.json"
        if out_path.exists():
            print(f"[{i}/{len(days)}] {date_iso} ja existe - pulando")
            skipped += 1
            continue
        print(f"[{i}/{len(days)}] {date_iso}")
        try:
            fetch_b3_trades_consolidated.fetch_day(date_iso)
            fetched += 1
        except Exception as exc:
            print(f"  ERRO em {date_iso}: {exc}")
            failed.append((date_iso, str(exc)))
        if i < len(days):
            time.sleep(SLEEP_BETWEEN_DAYS)

    print()
    print(f"Resumo: {fetched} buscado(s), {skipped} pulado(s), {len(failed)} falha(s)")
    if failed:
        for date_iso, err in failed:
            print(f"  - {date_iso}: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
