"""Fetch de trades de renda fixa da B3 (DEB/CRA/CRI/CFF/COE).

Endpoint publico nao documentado:
  POST https://arquivos.b3.com.br/bdi/table/Trade/{ini}/{fim}/{page}/{page_size}
Headers: Content-Type: application/json
Body: {}

Modo padrao (sem args): forca versao fresca dos 5 dias uteis B3 mais recentes
(HOJE se dia util + D-1..D-4). Para cada dia:
  1. Busca trades via API B3 (com retry).
  2. Em caso de sucesso: DELETA o arquivo data/b3_trades/<data>.json se existir
     e ESCREVE a nova versao (atomico via .tmp + rename).
  3. Em caso de erro (rede/timeout apos retry, 5xx): NAO toca no arquivo
     existente. Preserva versao anterior. Contabiliza falha.
  4. 403/404 (FDS/feriado/dia ainda nao publicado): pula sem alterar arquivo.

Exit code != 0 se QUALQUER dia falhar (mas todos sao tentados antes do
return). NUNCA gera dados sinteticos ou fallback — se a B3 nao responde,
o arquivo existente eh preservado e o exit code reflete a falha.

Historico antigo (>5 dias uteis) NUNCA eh tocado por este script. Para
backfill manual, use backfill_b3_trades.py.

Modo unitario (CLI: python fetch_b3_trades.py YYYY-MM-DD): mesmo padrao
delete+write para um unico dia (utilitario de inspecao manual).

Persiste data/b3_trades/{YYYY-MM-DD}.json em formato colunar minificado e
atualiza data/b3_trades/manifest.json incrementalmente (apenas as entradas
dos dias da janela sao sobrescritas; manifest preserva historico completo).
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import sectors
from b3_api import (
    B3UnavailableError,
    post_page,
    refresh_recent_days as _refresh_recent_days,
)


TABLE_NAME = "Trade"
PAGE_SIZE = 500
SLEEP_BETWEEN_PAGES = 0.2
REFRESH_WINDOW_DAYS = 5

DATA_DIR = Path(__file__).parent / "data" / "b3_trades"
MANIFEST_PATH = DATA_DIR / "manifest.json"

COLUMNS = [
    "instrument", "issuer", "ticker", "setor", "qtd", "price", "vol",
    "rate", "origin", "time", "trade_code", "isin", "settlement_dt", "situation",
]


def _row_from_array(arr: list) -> list:
    instrument = arr[2]
    issuer = arr[3]
    ticker = arr[4]
    if instrument == "DEB":
        setor = sectors.classify(ticker, issuer)
    else:
        setor = "Outros"
    return [
        instrument,
        issuer,
        ticker,
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
    """Busca trades de UM dia e escreve delete+write atomico.

    Retorna dict com `status` em {'updated', 'unavailable'}.
    'updated': arquivo escrito (delete + write feitos com sucesso).
    'unavailable': B3 retornou 403/404 (FDS/feriado/dia nao publicado) —
                   arquivo existente preservado.

    Levanta RuntimeError se houver falha de rede apos os retries
    (chamador deve capturar e contar como falha sem alterar arquivo —
    arquivo existente eh preservado pois delete so ocorre apos sucesso).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{date_iso}.json"

    try:
        first = post_page(TABLE_NAME, date_iso, 1, PAGE_SIZE)
    except B3UnavailableError as exc:
        print(
            f"[{date_iso}] {exc} - esperado em FDS/feriado/dia ainda nao publicado, pulando"
        )
        return {"date": date_iso, "status": "unavailable"}

    table = first.get("table") or {}
    page_count = int(table.get("pageCount") or 0)
    last_b3_update = first.get("lastUpdateDate")

    print(f"[{date_iso}] {page_count} pagina(s)")

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
        resp = post_page(TABLE_NAME, date_iso, page, PAGE_SIZE)
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

    # Delete + write atomico: so deletamos APOS sucesso confirmado da API.
    # Escrita via .tmp + rename garante que nunca ha estado intermediario
    # inconsistente no disco.
    tmp_path = out_path.parent / f"{date_iso}.json.tmp"
    tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    if out_path.exists():
        out_path.unlink()
    tmp_path.replace(out_path)
    size_mb = out_path.stat().st_size / (1024 * 1024)

    print(f"[{date_iso}] OK: {len(rows):,} trades, {size_mb:.2f} MB".replace(",", "."))

    _update_manifest(date_iso, len(rows), vol_total, out_path.name)

    return {
        "date": date_iso,
        "status": "updated",
        "n_trades": len(rows),
        "vol_brl": vol_total,
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


def refresh_recent_days(n: int = REFRESH_WINDOW_DAYS) -> int:
    return _refresh_recent_days(fetch_day, n)


def main(argv: list[str]) -> int:
    if len(argv) >= 2:
        date_iso = argv[1]
        date.fromisoformat(date_iso)
        try:
            fetch_day(date_iso)
        except Exception as exc:
            print(f"FALHA: {exc}")
            return 1
        return 0
    return refresh_recent_days()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
