"""Backfill manual de negocios consolidados B3 para um intervalo de dias uteis.

CLI:
  python backfill_b3_trades_consolidated.py                       # 2026-04-24 ate hoje
  python backfill_b3_trades_consolidated.py 2026-04-24 2026-05-01 # range customizado
  python backfill_b3_trades_consolidated.py --table NomeCorreto   # override tabela

Idempotente: pula dias ja existentes em data/b3_trades_consolidated/{date}.json.
"""

from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path

import fetch_b3_trades_consolidated
from b3_calendar import is_b3_business_day, today_brt


DEFAULT_START = date(2026, 4, 24)
SLEEP_BETWEEN_DAYS = 1.0
DATA_DIR = Path(__file__).parent / "data" / "b3_trades_consolidated"


def _iter_business_days(start: date, end: date):
    d = start
    while d <= end:
        if is_b3_business_day(d):
            yield d
        d += timedelta(days=1)


def _parse_argv(argv: list[str]) -> tuple[date, date, str | None]:
    table: str | None = fetch_b3_trades_consolidated.DEFAULT_TABLE_NAME
    positional: list[str] = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--table" and i + 1 < len(argv):
            cli_val = argv[i + 1].strip()
            table = cli_val or None
            i += 2
            continue
        positional.append(a)
        i += 1

    if len(positional) >= 2:
        start = date.fromisoformat(positional[0])
        end = date.fromisoformat(positional[1])
    else:
        start = DEFAULT_START
        end = today_brt()
    return start, end, table


def main(argv: list[str]) -> int:
    start, end, table = _parse_argv(argv)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    days = list(_iter_business_days(start, end))
    label = table if table else "PROBING (auto)"
    print(
        f"Backfill consolidated: {len(days)} dia(s) uteis entre {start} e {end} "
        f"(tabela={label})"
    )

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
            fetch_b3_trades_consolidated.fetch_day(date_iso, table=table)
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
