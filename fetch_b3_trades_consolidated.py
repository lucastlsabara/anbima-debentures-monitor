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
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from b3_api import (
    B3UnavailableError,
    SandboxBlockedError,
    extract_total_records,
    fetch_metadata,
    last_n_business_days,
    post_page,
)
from b3_calendar import today_brt


TABLE_NAME = "ConsolidatedRecords"
PAGE_SIZE = 100
MAX_PAGES = 2000
SLEEP_BETWEEN_PAGES = 0.1
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

DATA_DIR = Path(__file__).parent / "data" / "b3_trades_consolidated"
MANIFEST_PATH = DATA_DIR / "manifest.json"


def _cache_check(date_iso: str, out_path: Path) -> tuple[bool, str]:
    """Decide se podemos pular o fetch completo de date_iso (consolidated).

    Mesma logica de fetch_b3_trades._cache_check: cache hit somente quando
    last_b3_update + total_b3_records locais batem com o ping leve da B3.
    Qualquer adversidade -> cache miss com motivo explicativo.
    """
    if not out_path.exists():
        return False, "arquivo local ausente"
    try:
        local = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"erro lendo arquivo local: {exc}"
    local_last_update = local.get("last_b3_update")
    local_total = local.get("total_b3_records")
    if local_last_update is None:
        return False, "campo last_b3_update nao persistido"
    if local_total is None:
        return False, "campo total_b3_records nao persistido (deploy novo)"

    try:
        meta = fetch_metadata(TABLE_NAME, date_iso)
    except SandboxBlockedError:
        raise
    except Exception as exc:
        return False, f"falha no ping ({exc})"

    remote_last_update = meta["last_update"]
    remote_total = meta["total_records"]
    if remote_last_update is None:
        return False, "campo lastUpdateDate ausente no envelope"
    if remote_total is None:
        return False, "campo totalRecords ausente no envelope"
    if remote_last_update != local_last_update:
        return False, (
            f"last_update remoto={remote_last_update} local={local_last_update}"
        )
    if remote_total != local_total:
        return False, (
            f"total_records remoto={remote_total} local={local_total}"
        )
    return True, f"last_update={remote_last_update} total_records={remote_total}"


def fetch_day(date_iso: str) -> dict:
    """Busca consolidated de UM dia (delete+write atomico).

    Retorna dict com `status` em {'updated', 'cached', 'unavailable'}.
    'cached': D-1..D-4 inalterado na B3, arquivo local preservado.
    Levanta RuntimeError em falha de rede pos-retries — chamador trata
    sem alterar arquivo (delete so ocorre apos sucesso confirmado).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{date_iso}.json"

    # D-0 (hoje BRT) sempre faz fetch completo: dataset esta em construcao
    # ao longo do dia, lastUpdateDate muda toda hora.
    d0_iso = today_brt().isoformat()
    if date_iso != d0_iso:
        cache_hit, motivo = _cache_check(date_iso, out_path)
        if cache_hit:
            print(f"[cache-hit] Dia {date_iso}: {motivo} (skip)", file=sys.stderr)
            return {"date": date_iso, "status": "cached"}
        print(f"[cache-miss] Dia {date_iso}: {motivo} -> fetch completo", file=sys.stderr)

    try:
        first = post_page(TABLE_NAME, date_iso, 1, PAGE_SIZE)
    except B3UnavailableError as exc:
        print(
            f"[{date_iso}] {exc} - esperado em FDS/feriado/dia ainda nao publicado, pulando"
        )
        return {"date": date_iso, "status": "unavailable"}

    tbl = first.get("table") or {}
    last_b3_update = first.get("lastUpdateDate")
    total_b3_records_envelope = extract_total_records(first)
    rows: list = list(tbl.get("values") or [])
    pages_fetched = 1
    last_page_size = len(rows)

    # Paginacao: condicao de parada = n_rows<PAGE_SIZE OU teto MAX_PAGES.
    # pageCount da B3 nem sempre confiavel; usa tamanho da pagina como sinal.
    while last_page_size >= PAGE_SIZE and pages_fetched < MAX_PAGES:
        time.sleep(SLEEP_BETWEEN_PAGES)
        next_page = pages_fetched + 1
        resp = post_page(TABLE_NAME, date_iso, next_page, PAGE_SIZE)
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

    total_b3_records = (
        total_b3_records_envelope
        if total_b3_records_envelope is not None
        else len(rows)
    )

    payload = {
        "date": date_iso,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "last_b3_update": last_b3_update,
        "total_b3_records": total_b3_records,
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
        "n_pages": pages_fetched,
        "size_mb": size_mb,
    }


def _update_manifest(date_iso: str, n_rows: int, filename: str) -> None:
    if MANIFEST_PATH.exists():
        try:
            manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"manifest corrompido em {MANIFEST_PATH} ({exc}); "
                "abortando para preservar historico — investigar markers de merge/stash"
            ) from exc
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


def refresh_recent_days(n: int = REFRESH_WINDOW_DAYS) -> int:
    """Refresh dos N du B3 mais recentes com wall-clock budget.

    Wall-clock budget (env `B3_CONSOLIDATED_BUDGET_SECONDS`, default 9000s)
    complementa o HTTP timeout: cobre LENTIDAO genuina e bugs de paginacao
    onde requests individuais respondem mas o conjunto extrapola. Quando
    estoura, dias restantes sao pulados; arquivos existentes preservados.

    Exit code:
      - 0: TODOS os N dias completados (status 'updated' OU 'unavailable'
        legitimo da B3 — FDS/feriado/dia ainda nao publicado).
      - 1: qualquer dia pulado por wall-clock OU falha apos retries. O
        workflow usa esse contraste para nao marcar o marker de
        idempotencia em parcial (proxima retry reprocessa).
    """
    budget_seconds = int(os.environ.get("B3_CONSOLIDATED_BUDGET_SECONDS", "9000"))
    start_time = time.monotonic()
    days = last_n_business_days(n)
    print(
        f"Refresh consolidated dos ultimos {len(days)} dias uteis B3: "
        f"{days[0]} a {days[-1]}"
    )
    print(f"Wall-clock budget: {budget_seconds}s")
    print("HTTP timeout: (15, 120)s, max 3 tentativas")

    completed_days: list[str] = []
    cached_days: list[str] = []
    skipped_days: list[tuple[str, str]] = []

    for i, d in enumerate(days):
        date_iso = d.isoformat()
        elapsed = time.monotonic() - start_time
        if elapsed > budget_seconds:
            print(
                f"[wall-clock] Budget esgotado ({elapsed:.0f}s > "
                f"{budget_seconds}s), pulando dias restantes",
                file=sys.stderr,
            )
            for remaining in days[i:]:
                rem_iso = remaining.isoformat()
                skipped_days.append((rem_iso, "wall-clock budget esgotado"))
                print(f"[skip] Dia {rem_iso} pulado: wall-clock budget esgotado")
            break

        try:
            result = fetch_day(date_iso)
        except Exception as exc:
            skipped_days.append((date_iso, f"falha apos retries: {exc}"))
            print(
                f"[skip] Dia {date_iso} pulado: falha apos retries: {exc}",
                file=sys.stderr,
            )
            continue

        status = result["status"]
        if status == "updated":
            completed_days.append(date_iso)
            print(
                f"[ok] Dia {date_iso} ({result['n_pages']} paginas, "
                f"{result['size_mb']:.2f}MB)"
            )
        elif status == "cached":
            completed_days.append(date_iso)
            cached_days.append(date_iso)
            print(f"[ok] Dia {date_iso} (cache hit -- arquivo local preservado)")
        else:
            # 'unavailable' (B3UnavailableError 403/404): FDS/feriado/nao
            # publicado. Conta como completed: nao temos o que fazer aqui,
            # gap-fill em proxima execucao se a B3 publicar.
            completed_days.append(date_iso)
            print(f"[ok] Dia {date_iso} (indisponivel na B3 -- FDS/feriado/nao publicado)")

        if i < len(days) - 1:
            time.sleep(1.0)

    elapsed_total = time.monotonic() - start_time
    print(
        f"[summary] Completados: {len(completed_days)}/{len(days)} "
        f"(cached: {len(cached_days)}) | Pulados: {len(skipped_days)} | "
        f"Tempo: {elapsed_total:.0f}s"
    )

    return 0 if len(completed_days) == len(days) else 1


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
