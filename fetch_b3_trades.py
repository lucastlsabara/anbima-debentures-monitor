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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

import sectors
from b3_calendar import is_b3_business_day, today_brt


B3_URL = "https://arquivos.b3.com.br/bdi/table/Trade/{ini}/{fim}/{page}/{page_size}"
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

DATA_DIR = Path(__file__).parent / "data" / "b3_trades"
MANIFEST_PATH = DATA_DIR / "manifest.json"

COLUMNS = [
    "instrument", "issuer", "ticker", "setor", "qtd", "price", "vol",
    "rate", "origin", "time", "trade_code", "isin", "settlement_dt", "situation",
]


class B3UnavailableError(Exception):
    """B3 retornou 403/404: dia em FDS/feriado/ainda nao publicado."""


class SandboxBlockedError(Exception):
    """Resposta 403 com header x-deny-reason: bloqueio de allowlist do
    ambiente (sandbox), nao da B3. Falha real — nao silenciar como
    'indisponivel'."""


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
            deny_reason = r.headers.get("x-deny-reason")
            if r.status_code == 403 and deny_reason:
                raise SandboxBlockedError(
                    f"HTTP 403 bloqueado pela sandbox (x-deny-reason={deny_reason}): "
                    f"host arquivos.b3.com.br fora da allowlist. Rode este script "
                    f"em ambiente com egress livre (ex: GitHub Actions)."
                )
            if r.status_code in (403, 404):
                raise B3UnavailableError(f"HTTP {r.status_code}")
            if r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except (B3UnavailableError, SandboxBlockedError):
            raise
        except (requests.Timeout, requests.HTTPError, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt == MAX_RETRIES - 1:
                break
            backoff = 2 ** (attempt + 1)
            print(f"  [retry {attempt + 1}/{MAX_RETRIES}] {exc}; aguardando {backoff}s")
            time.sleep(backoff)
    raise RuntimeError(f"Falha apos {MAX_RETRIES} tentativas: {last_exc}")


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
        first = _post_page(date_iso, 1)
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


def _last_n_b3_business_days(n: int, today: date | None = None) -> list[date]:
    """N ultimos dias uteis B3, ordem ascendente.

    Inclui `today` se for dia util B3 (padrao ANBIMA: [HOJE, D-1, ...]);
    caso contrario (FDS/feriado), retorna N dias uteis estritamente anteriores.
    """
    if today is None:
        today = today_brt()
    out: list[date] = []
    d = today if is_b3_business_day(today) else today - timedelta(days=1)
    while len(out) < n:
        if is_b3_business_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def refresh_recent_days(n: int = REFRESH_WINDOW_DAYS) -> int:
    """Forca versao fresca dos N dias uteis B3 mais recentes.

    Tenta todos os dias antes de retornar. Exit code != 0 se QUALQUER dia
    falhar (falha de rede apos retry; 403/404 nao conta como falha).
    """
    days = _last_n_b3_business_days(n)
    print(
        f"Refresh dos ultimos {len(days)} dias uteis B3: "
        f"{days[0]} a {days[-1]}"
    )

    updated = 0
    unavailable = 0
    failed = 0

    for i, d in enumerate(days):
        date_iso = d.isoformat()
        try:
            result = fetch_day(date_iso)
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
