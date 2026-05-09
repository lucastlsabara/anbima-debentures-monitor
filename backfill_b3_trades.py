"""Backfill manual de trades B3 para um intervalo de dias uteis.

CLI:
  python backfill_b3_trades.py                       # 2026-04-24 ate hoje
  python backfill_b3_trades.py 2026-04-24 2026-05-01 # range customizado

Idempotente: pula dias ja existentes em data/b3_trades/{date}.json.
"""

from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path

import fetch_b3_trades
from b3_calendar import is_b3_business_day, today_brt


DEFAULT_START = date(2026, 4, 24)
SLEEP_BETWEEN_DAYS = 1.0
DATA_DIR = Path(__file__).parent / "data" / "b3_trades"


def _iter_business_days(start: date, end: date):
    d = start
    while d <= end:
        if is_b3_business_day(d):
            yield d
        d += timedelta(days=1)


def main(argv: list[str]) -> int:
    if len(argv) >= 3:
        start = date.fromisoformat(argv[1])
        end = date.fromisoformat(argv[2])
    else:
        start = DEFAULT_START
        end = today_brt()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    days = list(_iter_business_days(start, end))
    print(f"Backfill: {len(days)} dia(s) uteis entre {start} e {end}")

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
            fetch_b3_trades.fetch_day(date_iso)
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
