"""Fetch do registro de instrumentos da B3 (universo completo).

Endpoint publico nao documentado:
  POST https://arquivos.b3.com.br/bdi/table/InstrumentRegistration/{ini}/{fim}/{page}/{page_size}
Headers: Content-Type: application/json + User-Agent Chrome.
Body: {}

Persiste data/b3_instruments/{YYYY-MM-DD}.json em formato colunar minificado
e atualiza data/b3_instruments/manifest.json. Idempotente por dia.

Schema reduzido (8 campos extraidos das 18 colunas):
  ticker (col 1), isin (2), issuer (3), instType (4),
  indexer (8), indexerPct (9), additionalFee (10), maturity (12)
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from b3_calendar import last_b3_business_day, today_brt


B3_URL = "https://arquivos.b3.com.br/bdi/table/InstrumentRegistration/{ini}/{fim}/{page}/{page_size}"
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

DATA_DIR = Path(__file__).parent / "data" / "b3_instruments"
MANIFEST_PATH = DATA_DIR / "manifest.json"

B3_TRADES_DIR = Path(__file__).parent / "data" / "b3_trades"
B3_TRADES_MANIFEST = B3_TRADES_DIR / "manifest.json"

COLUMNS = [
    "ticker", "isin", "issuer", "instType",
    "indexer", "indexerPct", "additionalFee", "maturity",
]
# Indices das 18 colunas brutas correspondentes (0-indexado).
_COL_IDX = [1, 2, 3, 4, 8, 9, 10, 12]


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
    return [arr[i] if i < len(arr) else None for i in _COL_IDX]


def _load_traded_tickers() -> set[str] | None:
    """Le tickers unicos a partir de data/b3_trades/.

    Retorna:
      None  -> manifest ausente/corrompido (fallback: salvar cadastro completo)
      set() -> manifest existe mas sem dias (salvar arquivo vazio)
      set   -> universo de tickers a manter
    """
    if not B3_TRADES_MANIFEST.exists():
        print(
            "AVISO: data/b3_trades/manifest.json nao encontrado — "
            "salvando cadastro COMPLETO (fallback)."
        )
        return None

    try:
        manifest = json.loads(B3_TRADES_MANIFEST.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(
            f"AVISO: manifest de b3_trades corrompido ({exc}) — "
            "salvando cadastro COMPLETO (fallback)."
        )
        return None

    entries = manifest.get("dates") or []
    if not entries:
        print("AVISO: manifest de b3_trades sem dias — salvando arquivo vazio.")
        return set()

    tickers: set[str] = set()
    for entry in entries:
        filename = entry.get("filename")
        if not filename:
            continue
        path = B3_TRADES_DIR / filename
        if not path.exists():
            print(f"  AVISO: {filename} listado no manifest mas ausente — pulando.")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"  AVISO: {filename} corrompido ({exc}) — pulando.")
            continue
        cols = data.get("columns") or []
        try:
            ticker_idx = cols.index("ticker")
        except ValueError:
            print(f"  AVISO: {filename} sem coluna 'ticker' — pulando.")
            continue
        for row in data.get("rows") or []:
            if ticker_idx < len(row):
                t = row[ticker_idx]
                if t:
                    tickers.add(t)
    return tickers


def fetch_day(date_iso: str) -> dict:
    """Busca registro de instrumentos de UM dia, persiste + atualiza manifest."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    out_path = DATA_DIR / f"{date_iso}.json"
    if out_path.exists():
        print(f"{date_iso} ja existe em {out_path.name} — pulando.")
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            return {
                "date": date_iso,
                "n_records": len(existing.get("rows") or []),
                "status": "skipped",
            }
        except json.JSONDecodeError:
            pass  # arquivo corrompido, refaz

    first = _post_page(date_iso, 1)
    table = first.get("table") or {}
    page_count = int(table.get("pageCount") or 0)
    last_b3_update = first.get("lastUpdateDate")

    print(f"Buscando InstrumentRegistration {date_iso} - {page_count} pagina(s)...")

    rows: list[list] = []

    def _ingest_values(values):
        for arr in values or []:
            rows.append(_row_from_array(arr))

    _ingest_values(table.get("values"))

    for page in range(2, page_count + 1):
        time.sleep(SLEEP_BETWEEN_PAGES)
        if page % 50 == 0:
            print(f"  pagina {page}/{page_count} ({len(rows):,} registros)".replace(",", "."))
        resp = _post_page(date_iso, page)
        _ingest_values((resp.get("table") or {}).get("values"))

    n_total = len(rows)
    traded = _load_traded_tickers()
    if traded is None:
        filtered_rows = rows
    elif not traded:
        filtered_rows = []
    else:
        ticker_idx = COLUMNS.index("ticker")
        filtered_rows = [r for r in rows if r[ticker_idx] in traded]

    payload = {
        "date": date_iso,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "last_b3_update": last_b3_update,
        "columns": COLUMNS,
        "rows": filtered_rows,
        "total_records": len(filtered_rows),
        "n_pages_fetched": max(page_count, 1),
    }

    payload_str = json.dumps(payload, separators=(",", ":"))
    out_path.write_text(payload_str, encoding="utf-8")
    size_kb = len(payload_str.encode("utf-8")) / 1024

    if traded is None:
        size_mb = size_kb / 1024
        print(
            f"OK: {len(filtered_rows):,} registros, {size_mb:.2f} MB "
            "(sem filtro — manifest b3_trades ausente)".replace(",", ".")
        )
    elif not traded:
        print(
            f"Filtrado: 0/{n_total:,} registros "
            f"(manifest b3_trades vazio, {size_kb:.0f} KB)".replace(",", ".")
        )
    else:
        pct = len(filtered_rows) / len(traded) * 100
        print(
            f"Filtrado: {len(filtered_rows):,}/{n_total:,} registros "
            f"({pct:.1f}% cobertura, {size_kb:.0f} KB)".replace(",", ".")
        )

    rows = filtered_rows

    _update_manifest(date_iso, len(rows), out_path.name)

    return {
        "date": date_iso,
        "n_records": len(rows),
        "status": "ok",
    }


def _update_manifest(date_iso: str, n_records: int, filename: str) -> None:
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
        "n_records": n_records,
        "filename": filename,
    }
    dates_sorted = sorted(by_date.values(), key=lambda e: e["date"])

    manifest = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "earliest_date": dates_sorted[0]["date"] if dates_sorted else None,
        "latest_date": dates_sorted[-1]["date"] if dates_sorted else None,
        "dates": dates_sorted,
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
