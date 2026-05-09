"""Fetch de trades de renda fixa da B3 (DEB/CRA/CRI/CFF/COE) para um dia.

Endpoint publico nao documentado:
  POST https://arquivos.b3.com.br/bdi/table/Trade/{ini}/{fim}/{page}/{page_size}
Headers: Content-Type: application/json
Body: {}

Persiste data/b3_trades/{YYYY-MM-DD}.json em formato colunar minificado e
atualiza data/b3_trades/manifest.json. Idempotente por dia.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

import sectors
from b3_calendar import last_b3_business_day, today_brt


B3_URL = "https://arquivos.b3.com.br/bdi/table/Trade/{ini}/{fim}/{page}/{page_size}"
PAGE_SIZE = 500
TIMEOUT = 60
MAX_RETRIES = 3
SLEEP_BETWEEN_PAGES = 0.2

REQUEST_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Origin": "https://arquivos.b3.com.br",
    "Referer": "https://arquivos.b3.com.br/",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

DATA_DIR = Path(__file__).parent / "data" / "b3_trades"
MANIFEST_PATH = DATA_DIR / "manifest.json"

COLUMNS = [
    "instrument", "issuer", "ticker", "setor", "qtd", "price", "vol",
    "rate", "origin", "time", "trade_code", "isin", "settlement_dt", "situation",
]


def _post_page(date_iso: str, page: int) -> dict:
    url = B3_URL.format(ini=date_iso, fim=date_iso, page=page, page_size=PAGE_SIZE)
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(
                url,
                headers=REQUEST_HEADERS,
                json={},
                timeout=TIMEOUT,
            )
            if r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except (requests.Timeout, requests.HTTPError, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt == MAX_RETRIES - 1:
                break
            backoff = 2 ** attempt
            print(f"  [retry {attempt + 1}/{MAX_RETRIES}] {exc}; aguardando {backoff}s")
            time.sleep(backoff)
    raise RuntimeError(f"Falha apos {MAX_RETRIES} tentativas: {last_exc}")


def _row_from_array(arr: list) -> list:
    instrument = arr[2]
    issuer = arr[3]
    if instrument == "DEB":
        setor = sectors.clean_emissor(issuer)
    else:
        setor = "Outros"
    return [
        instrument,
        issuer,
        arr[4],
        setor,
        arr[5],
        arr[6],
        arr[7],
        arr[8],
        arr[9],
        arr[10],
        arr[12],
        arr[13],
        arr[14],
        arr[15],
    ]


def fetch_day(date_iso: str) -> dict:
    """Busca trades de UM dia, persiste arquivo + atualiza manifest."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    first = _post_page(date_iso, 1)
    table = first.get("table") or {}
    page_count = int(table.get("pageCount") or 0)
    last_b3_update = first.get("lastUpdateDate")

    print(f"Buscando {date_iso} - {page_count} pagina(s)...")

    rows: list[list] = []
    instruments_count: dict[str, int] = {}
    vol_total = 0.0

    def _ingest_values(values):
        nonlocal vol_total
        for arr in values or []:
            row = _row_from_array(arr)
            rows.append(row)
            inst = row[0] or "?"
            instruments_count[inst] = instruments_count.get(inst, 0) + 1
            try:
                vol_total += float(row[6] or 0)
            except (TypeError, ValueError):
                pass

    _ingest_values(table.get("values"))

    for page in range(2, page_count + 1):
        time.sleep(SLEEP_BETWEEN_PAGES)
        resp = _post_page(date_iso, page)
        _ingest_values((resp.get("table") or {}).get("values"))

    payload = {
        "date": date_iso,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "last_b3_update": last_b3_update,
        "columns": COLUMNS,
        "rows": rows,
        "stats": {
            "n_trades": len(rows),
            "n_pages_fetched": max(page_count, 1),
            "instruments_count": instruments_count,
            "vol_brl_total": vol_total,
        },
    }

    out_path = DATA_DIR / f"{date_iso}.json"
    out_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    size_mb = out_path.stat().st_size / (1024 * 1024)

    print(f"OK: {len(rows):,} trades, {size_mb:.2f} MB".replace(",", "."))

    _update_manifest(date_iso, len(rows), vol_total, out_path.name)

    return {
        "date": date_iso,
        "n_trades": len(rows),
        "vol_brl": vol_total,
        "status": "ok",
    }


def _update_manifest(date_iso: str, n_trades: int, vol_brl: float, filename: str) -> None:
    if MANIFEST_PATH.exists():
        try:
            manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
    else:
        manifest = {}

    dates = manifest.get("dates") or []
    by_date = {entry["date"]: entry for entry in dates}
    by_date[date_iso] = {
        "date": date_iso,
        "n_trades": n_trades,
        "vol_brl": vol_brl,
        "filename": filename,
    }
    dates_sorted = sorted(by_date.values(), key=lambda e: e["date"])

    total_trades = sum(e["n_trades"] for e in dates_sorted)
    total_vol = sum(e["vol_brl"] for e in dates_sorted)

    manifest = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "earliest_date": dates_sorted[0]["date"] if dates_sorted else None,
        "latest_date": dates_sorted[-1]["date"] if dates_sorted else None,
        "dates": dates_sorted,
        "total_trades": total_trades,
        "total_vol_brl": total_vol,
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, separators=(",", ":")), encoding="utf-8"
    )


def _resolve_default_date() -> str:
    return last_b3_business_day(today_brt()).isoformat()


def main(argv: list[str]) -> int:
    if len(argv) >= 2:
        date_iso = argv[1]
        date.fromisoformat(date_iso)
    else:
        date_iso = _resolve_default_date()
    fetch_day(date_iso)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
