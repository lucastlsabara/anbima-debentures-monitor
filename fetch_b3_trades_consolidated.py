"""Fetch de NEGOCIOS CONSOLIDADOS de renda fixa da B3.

Diferenca de fonte:
  - fetch_b3_trades.py        -> negocio a negocio (trade-by-trade), endpoint
                                 /bdi/table/Trade. 1 linha = 1 operacao.
  - fetch_b3_trades_consolidated.py (este) -> negocios consolidados por
                                 instrumento/dia (volume total, pmp, etc).

Endpoint:
  POST https://arquivos.b3.com.br/bdi/table/ConsolidatedRecords/{ini}/{fim}/{page}/{page_size}
  Headers: Content-Type: application/json
  Body: {}

Nome da tabela fixo: `ConsolidatedRecords` (renda fixa). Confirmado via
inspecao manual da pagina B3 (boletim-diario-do-mercado) com network tracking.
PR #105 introduziu probing automatico sobre 14 candidatos — todos retornaram
200 com 0 rows porque o metodo era GET (deveria ser POST). Limpo aqui.

Comportamento (analogo a fetch_b3_trades.py):
  - Janela: 5 dias uteis B3 mais recentes (HOJE se dia util + D-1..D-4).
  - Para cada dia: paginacao interna ate n_rows<pageSize OU teto de 2000
    paginas. Sucesso -> DELETA arquivo existente + ESCREVE nova versao
    (atomico via .tmp + rename).
  - Falha de rede/timeout/5xx apos retry: preserva arquivo existente.
  - 403/404: pula (FDS/feriado/nao publicado), preserva arquivo.
  - Exit != 0 se qualquer dia falhar.
  - NUNCA gera dados sinteticos/fallback.

Persistencia: data/b3_trades_consolidated/{YYYY-MM-DD}.json + manifest.json.

Schema das 17 colunas (ordem fixa retornada pela B3):
  1 data_negocio    2 data_referencia 3 codigo_if    4 instrumento
  5 isin            6 emissor         7 data_liquidacao
  8 quantidade      9 pmp            10 preco       11 volume_unit
 12 min            13 max            14 n_negocios  15 volume_total
 16 grupo (INTRAGRUPO / "-")          17 extra
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from b3_calendar import is_b3_business_day, today_brt


TABLE_NAME = "ConsolidatedRecords"
B3_URL_TEMPLATE = (
    "https://arquivos.b3.com.br/bdi/table/{table}/{ini}/{fim}/{page}/{page_size}"
)
PAGE_SIZE = 100
MAX_PAGES = 2000
TIMEOUT = 60
MAX_RETRIES = 3
SLEEP_BETWEEN_PAGES = 0.1
SLEEP_BETWEEN_DAYS = 1.0
REFRESH_WINDOW_DAYS = 5

# Schema fixo (ordem que a B3 retorna em ConsolidatedRecords).
COLUMNS = [
    "data_negocio",
    "data_referencia",
    "codigo_if",
    "instrumento",
    "isin",
    "emissor",
    "data_liquidacao",
    "quantidade",
    "pmp",
    "preco",
    "volume_unit",
    "min",
    "max",
    "n_negocios",
    "volume_total",
    "grupo",
    "extra",
]

SOURCE_TAG = "b3_api_consolidated_records"

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


class SandboxBlockedError(Exception):
    """Resposta 403 com header x-deny-reason: bloqueio de allowlist do
    ambiente (sandbox), nao da B3. Falha real — nao silenciar como
    'indisponivel'."""


def _post_page(date_iso: str, page: int) -> dict:
    url = B3_URL_TEMPLATE.format(
        table=TABLE_NAME, ini=date_iso, fim=date_iso, page=page, page_size=PAGE_SIZE
    )
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(url, headers=REQUEST_HEADERS, json={}, timeout=TIMEOUT)
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


def fetch_day(date_iso: str) -> dict:
    """Busca consolidated de UM dia (delete+write atomico).

    Retorna dict com `status` em {'updated', 'unavailable'}.
    Levanta RuntimeError em falha de rede pos-retries — chamador trata
    sem alterar arquivo (delete so ocorre apos sucesso confirmado).
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

    tbl = first.get("table") or {}
    last_b3_update = first.get("lastUpdateDate")
    rows: list = list(tbl.get("values") or [])
    pages_fetched = 1
    last_page_size = len(rows)

    # Paginacao: condicao de parada = n_rows<PAGE_SIZE OU teto MAX_PAGES.
    # pageCount da B3 nem sempre confiavel; usa tamanho da pagina como sinal.
    while last_page_size >= PAGE_SIZE and pages_fetched < MAX_PAGES:
        time.sleep(SLEEP_BETWEEN_PAGES)
        next_page = pages_fetched + 1
        resp = _post_page(date_iso, next_page)
        values = ((resp.get("table") or {}).get("values")) or []
        rows.extend(values)
        pages_fetched += 1
        last_page_size = len(values)
        if last_page_size == 0:
            break

    if pages_fetched >= MAX_PAGES and last_page_size >= PAGE_SIZE:
        print(
            f"[{date_iso}] AVISO: teto de {MAX_PAGES} paginas atingido com pagina cheia; "
            f"possivel truncamento. Verificar PAGE_SIZE/MAX_PAGES."
        )

    payload = {
        "date": date_iso,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "last_b3_update": last_b3_update,
        "source": SOURCE_TAG,
        "table_name": TABLE_NAME,
        "columns": COLUMNS,
        "rows": rows,
        "stats": {
            "n_rows": len(rows),
            "n_pages_fetched": pages_fetched,
        },
    }

    tmp_path = out_path.parent / f"{date_iso}.json.tmp"
    tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    if out_path.exists():
        out_path.unlink()
    tmp_path.replace(out_path)
    size_mb = out_path.stat().st_size / (1024 * 1024)

    print(
        f"[{date_iso}] OK: {len(rows):,} linha(s), {pages_fetched} pagina(s), "
        f"{size_mb:.2f} MB".replace(",", ".")
    )

    _update_manifest(date_iso, len(rows), out_path.name)
    return {
        "date": date_iso,
        "status": "updated",
        "n_rows": len(rows),
    }


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


def refresh_recent_days(n: int = REFRESH_WINDOW_DAYS) -> int:
    days = _last_n_b3_business_days(n)
    print(
        f"Refresh consolidated dos ultimos {len(days)} dias uteis B3: "
        f"{days[0]} a {days[-1]} (tabela={TABLE_NAME})"
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
