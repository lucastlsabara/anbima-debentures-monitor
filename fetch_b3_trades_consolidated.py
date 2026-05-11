"""Fetch de NEGOCIOS CONSOLIDADOS de renda fixa da B3.

Diferenca de fonte:
  - fetch_b3_trades.py        -> negocio a negocio (trade-by-trade), endpoint
                                 /bdi/table/Trade. 1 linha = 1 operacao.
  - fetch_b3_trades_consolidated.py (este) -> negocios consolidados por
                                 instrumento/dia (vol total, preco medio, etc).

Endpoint publico (mesmo padrao do trade-by-trade):
  POST https://arquivos.b3.com.br/bdi/table/{TABLE_NAME}/{ini}/{fim}/{page}/{page_size}
Headers: Content-Type: application/json
Body: {}

IMPORTANTE — table name:
  O nome canonico da tabela /bdi/table/ para "Negocios Consolidados de Renda
  Fixa" nao foi confirmado em documentacao publica acessivel. O default
  abaixo (TradeInformationConsolidated) eh uma HIPOTESE derivada da tabela
  homonima no portal /tabelas/ da B3 (segmento listado). Se ela cobrir
  apenas equities, ajustar para a tabela correta de renda fixa via env
  var ou flag --table:

      B3_CONSOLIDATED_TABLE=NomeCorreto python fetch_b3_trades_consolidated.py
      python fetch_b3_trades_consolidated.py --table NomeCorreto

Comportamento (analogo a fetch_b3_trades.py):
  - Janela: 5 dias uteis B3 mais recentes (HOJE se dia util + D-1..D-4).
  - Para cada dia com sucesso na API: DELETA arquivo existente + ESCREVE
    nova versao (atomico via .tmp + rename).
  - Falha de rede/timeout/5xx apos retry: preserva arquivo existente.
  - 403/404: pula (FDS/feriado/nao publicado), preserva arquivo.
  - Exit != 0 se qualquer dia falhar (todos sao tentados antes do return).
  - NUNCA gera dados sinteticos/fallback.

Persistencia: data/b3_trades_consolidated/{YYYY-MM-DD}.json + manifest.json.

Schema do JSON salvo:
  {
    "date": "YYYY-MM-DD",
    "fetched_at": ISO8601 UTC,
    "last_b3_update": ISO8601 da B3 (lastUpdateDate),
    "table_name": "<nome usado>",
    "columns": [...]  // colunas reportadas pela B3, se houver
    "rows": [[...], ...]  // values bruto retornado pela B3 (passthrough)
    "stats": { "n_rows": int, "n_pages_fetched": int }
  }

Por nao termos schema confirmado, NAO inferimos colunas — persistimos como
retornado. Build_dashboard.py / consumidores posteriores adaptam quando o
schema for confirmado.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from b3_calendar import is_b3_business_day, today_brt


DEFAULT_TABLE_NAME = os.environ.get(
    "B3_CONSOLIDATED_TABLE", "TradeInformationConsolidated"
)
B3_URL_TEMPLATE = (
    "https://arquivos.b3.com.br/bdi/table/{table}/{ini}/{fim}/{page}/{page_size}"
)
PAGE_SIZE = 500
TIMEOUT = 60
MAX_RETRIES = 3
SLEEP_BETWEEN_PAGES = 0.2
SLEEP_BETWEEN_DAYS = 1.0
REFRESH_WINDOW_DAYS = 5

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

DATA_DIR = Path(__file__).parent / "data" / "b3_trades_consolidated"
MANIFEST_PATH = DATA_DIR / "manifest.json"


class B3UnavailableError(Exception):
    """B3 retornou 403/404: dia em FDS/feriado/ainda nao publicado."""


def _post_page(table: str, date_iso: str, page: int) -> dict:
    url = B3_URL_TEMPLATE.format(
        table=table, ini=date_iso, fim=date_iso, page=page, page_size=PAGE_SIZE
    )
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(url, headers=REQUEST_HEADERS, json={}, timeout=TIMEOUT)
            if r.status_code in (403, 404):
                raise B3UnavailableError(f"HTTP {r.status_code}")
            if r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except B3UnavailableError:
            raise
        except (requests.Timeout, requests.HTTPError, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt == MAX_RETRIES - 1:
                break
            backoff = 2 ** attempt
            print(f"  [retry {attempt + 1}/{MAX_RETRIES}] {exc}; aguardando {backoff}s")
            time.sleep(backoff)
    raise RuntimeError(f"Falha apos {MAX_RETRIES} tentativas: {last_exc}")


def _extract_columns(payload: dict) -> list:
    """Tenta extrair lista de colunas do payload B3 (varia entre tabelas).

    Retorna lista vazia se nao encontrar — rows ainda sao persistidos brutos.
    """
    table = payload.get("table") or {}
    cols = table.get("columns") or payload.get("columns") or []
    if isinstance(cols, list):
        return cols
    return []


def fetch_day(date_iso: str, table: str = DEFAULT_TABLE_NAME) -> dict:
    """Busca consolidated de UM dia (delete+write atomico).

    Retorna dict com `status` em {'updated', 'unavailable'}.
    Levanta RuntimeError em falha de rede pos-retries — chamador trata
    sem alterar arquivo (delete so ocorre apos sucesso confirmado).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{date_iso}.json"

    try:
        first = _post_page(table, date_iso, 1)
    except B3UnavailableError as exc:
        print(
            f"[{date_iso}] {exc} - esperado em FDS/feriado/dia ainda nao publicado, pulando"
        )
        return {"date": date_iso, "status": "unavailable"}

    tbl = first.get("table") or {}
    page_count = int(tbl.get("pageCount") or 0)
    last_b3_update = first.get("lastUpdateDate")
    columns = _extract_columns(first)

    print(f"[{date_iso}] tabela={table}, {page_count} pagina(s)")

    rows: list = []
    rows.extend(tbl.get("values") or [])

    for page in range(2, page_count + 1):
        time.sleep(SLEEP_BETWEEN_PAGES)
        resp = _post_page(table, date_iso, page)
        rows.extend(((resp.get("table") or {}).get("values")) or [])

    payload = {
        "date": date_iso,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "last_b3_update": last_b3_update,
        "table_name": table,
        "columns": columns,
        "rows": rows,
        "stats": {
            "n_rows": len(rows),
            "n_pages_fetched": max(page_count, 1),
        },
    }

    # Delete + write atomico: delete so apos sucesso confirmado da API.
    tmp_path = out_path.parent / f"{date_iso}.json.tmp"
    tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    if out_path.exists():
        out_path.unlink()
    tmp_path.replace(out_path)
    size_mb = out_path.stat().st_size / (1024 * 1024)

    print(f"[{date_iso}] OK: {len(rows):,} linha(s), {size_mb:.2f} MB".replace(",", "."))

    _update_manifest(date_iso, len(rows), out_path.name)

    return {"date": date_iso, "status": "updated", "n_rows": len(rows)}


def _update_manifest(date_iso: str, n_rows: int, filename: str) -> None:
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
        "n_rows": n_rows,
        "filename": filename,
    }
    dates_sorted = sorted(by_date.values(), key=lambda e: e["date"])

    manifest = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "earliest_date": dates_sorted[0]["date"] if dates_sorted else None,
        "latest_date": dates_sorted[-1]["date"] if dates_sorted else None,
        "dates": dates_sorted,
        "total_rows": sum(e["n_rows"] for e in dates_sorted),
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, separators=(",", ":")), encoding="utf-8"
    )


def _last_n_b3_business_days(n: int, today: date | None = None) -> list[date]:
    if today is None:
        today = today_brt()
    out: list[date] = []
    d = today if is_b3_business_day(today) else today - timedelta(days=1)
    while len(out) < n:
        if is_b3_business_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def refresh_recent_days(n: int = REFRESH_WINDOW_DAYS, table: str = DEFAULT_TABLE_NAME) -> int:
    days = _last_n_b3_business_days(n)
    print(
        f"Refresh consolidated dos ultimos {len(days)} dias uteis B3: "
        f"{days[0]} a {days[-1]} (tabela={table})"
    )

    updated = 0
    unavailable = 0
    failed = 0

    for i, d in enumerate(days):
        date_iso = d.isoformat()
        try:
            result = fetch_day(date_iso, table=table)
            if result["status"] == "updated":
                updated += 1
            else:
                unavailable += 1
        except Exception as exc:
            print(
                f"[{date_iso}] FALHA apos retries: {exc} - "
                f"mantendo arquivo existente (se houver)"
            )
            failed += 1
        if i < len(days) - 1:
            time.sleep(SLEEP_BETWEEN_DAYS)

    print()
    print(
        f"Resumo: {updated} atualizado(s), {unavailable} indisponivel(eis) "
        f"(FDS/feriado/nao publicado), {failed} falha(s)"
    )

    return 0 if failed == 0 else 1


def _parse_argv(argv: list[str]) -> tuple[str | None, str]:
    """Retorna (date_iso_or_None, table_name).

    Aceita:
      python fetch_b3_trades_consolidated.py
      python fetch_b3_trades_consolidated.py YYYY-MM-DD
      python fetch_b3_trades_consolidated.py --table Nome
      python fetch_b3_trades_consolidated.py YYYY-MM-DD --table Nome
    """
    table = DEFAULT_TABLE_NAME
    date_iso: str | None = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--table" and i + 1 < len(argv):
            table = argv[i + 1]
            i += 2
            continue
        if date_iso is None:
            date_iso = a
            i += 1
            continue
        raise SystemExit(f"argumento inesperado: {a}")
    return date_iso, table


def main(argv: list[str]) -> int:
    date_iso, table = _parse_argv(argv)
    if date_iso is not None:
        date.fromisoformat(date_iso)
        try:
            fetch_day(date_iso, table=table)
        except Exception as exc:
            print(f"FALHA: {exc}")
            return 1
        return 0
    return refresh_recent_days(table=table)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
