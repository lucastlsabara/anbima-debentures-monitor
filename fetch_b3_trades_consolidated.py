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
  - Para cada dia: cache check (ping leve page_size=1, compara 3
    indicadores com `meta` local — lastUpdateDate + totalRecords +
    firstPageBytes). Cache-hit -> pula fetch full. Cache-miss
    (qualquer divergencia, metadata ausente, JSON corrompido, ping
    falho) -> fetch full paginado (n_rows<pageSize OU teto 2000).
  - Sucesso -> grava data/b3_trades_consolidated/<date>.json (atomico
    via .tmp + rename) com `meta` populado a partir do ping.
  - Falha de rede/timeout/5xx apos retry: preserva arquivo existente.
  - 403/404: pula (FDS/feriado/nao publicado), preserva arquivo.
  - Exit != 0 se qualquer dia falhar (cache-hit + indisponivel nao
    contam como falha).
  - NUNCA gera dados sinteticos/fallback.

Esquema do JSON local: ver mesma justificativa em fetch_b3_trades.py
— adicionamos chave top-level `meta` ao schema existente sem mover
`rows` para sub-chave. `build_dashboard.py` (PR #151) continua lendo
`payload['rows']` sem alteracao.

Modo self-test (CLI: `--self-test`): roda 6 cenarios de cache check
sem rede.

Persistencia: data/b3_trades_consolidated/{YYYY-MM-DD}.json +
manifest.json.

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
    CACHE_SCHEMA_VERSION,
    build_meta_from_ping,
    check_cache,
    extract_total_records,
    last_n_business_days,
    ping_first_page,
    post_page,
)


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


def _write_payload_atomic(out_path: Path, payload: dict) -> None:
    tmp_path = out_path.parent / f"{out_path.name}.tmp"
    tmp_path.write_text(
        json.dumps(payload, separators=(",", ":")), encoding="utf-8"
    )
    os.replace(tmp_path, out_path)


def _fetch_full_day(date_iso: str, ping_envelope: dict | None) -> dict:
    """Fetch full paginado + escrita atomica para um dia.

    `ping_envelope` (do cache check) eh reaproveitado para popular o
    `meta`. Se None (ping falhou), tenta re-pingar ao final; se isso
    tambem falhar, grava sem `meta` (proximo run dara cache-miss com
    motivo=metadata_ausente).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{date_iso}.json"

    first = post_page(TABLE_NAME, date_iso, 1, PAGE_SIZE)
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

    meta = None
    if ping_envelope is None:
        try:
            ping_envelope = ping_first_page(TABLE_NAME, date_iso)
        except Exception as exc:
            print(
                f"[{date_iso}] AVISO: ping@1 pos-fetch falhou ({exc}); "
                f"meta nao sera gravado neste run (proximo run refetcha)"
            )
            ping_envelope = None
    if ping_envelope is not None:
        meta = build_meta_from_ping(
            ping_envelope, datetime.now(timezone.utc).isoformat()
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
    if meta is not None:
        payload["meta"] = meta

    _write_payload_atomic(out_path, payload)
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


def fetch_day(date_iso: str) -> dict:
    """Busca consolidated de UM dia com cache check + fetch on miss.

    Retorna dict com `status` em {'cache_hit', 'updated', 'unavailable'}
    e `cache_motivo` (cache_hit | metadata_ausente | json_corrompido |
    ping_falhou | lastUpdate_changed | totalRecords_changed |
    firstPageBytes_changed | unavailable).
    Levanta RuntimeError em falha de rede pos-retries durante fetch
    full — chamador trata sem alterar arquivo.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{date_iso}.json"

    try:
        cache_hit, motivo, ping_envelope = check_cache(
            TABLE_NAME, date_iso, out_path
        )
    except B3UnavailableError as exc:
        print(
            f"[{date_iso}] {exc} - esperado em FDS/feriado/dia ainda nao publicado, pulando"
        )
        return {"date": date_iso, "status": "unavailable", "cache_motivo": "unavailable"}

    if cache_hit:
        return {"date": date_iso, "status": "cache_hit", "cache_motivo": motivo}

    try:
        result = _fetch_full_day(date_iso, ping_envelope)
    except B3UnavailableError as exc:
        print(
            f"[{date_iso}] {exc} - esperado em FDS/feriado/dia ainda nao publicado, pulando"
        )
        return {"date": date_iso, "status": "unavailable", "cache_motivo": "unavailable"}
    result["cache_motivo"] = motivo
    return result


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
    """Refresh consolidated dos N du B3 mais recentes (com cache check).

    Wall-clock budget (env `B3_CONSOLIDATED_BUDGET_SECONDS`, default 9000s)
    complementa o HTTP timeout: cobre LENTIDAO genuina e bugs de paginacao
    onde requests individuais respondem mas o conjunto extrapola. Quando
    estoura, dias restantes sao pulados; arquivos existentes preservados.
    Cache-hits consomem ~poucos segundos (so o ping), entao o budget
    raramente eh atingido em slots noturnos onde a maioria dos dias bate
    cache.

    Exit code:
      - 0: TODOS os N dias completados ('cache_hit', 'updated' ou
        'unavailable' legitimo da B3).
      - 1: qualquer dia pulado por wall-clock OU falha apos retries.

    Se env `B3_CONSOLIDATED_CACHE_SUMMARY` estiver setado, grava
    summary (hits/misses/motivos) no path indicado para consumo pelo
    workflow YAML.
    """
    budget_seconds = int(os.environ.get("B3_CONSOLIDATED_BUDGET_SECONDS", "9000"))
    summary_path = os.environ.get("B3_CONSOLIDATED_CACHE_SUMMARY")
    start_time = time.monotonic()
    days = last_n_business_days(n)
    print(
        f"Refresh consolidated dos ultimos {len(days)} dias uteis B3: "
        f"{days[0]} a {days[-1]}"
    )
    print(f"Wall-clock budget: {budget_seconds}s")
    print("HTTP timeout: (15, 120)s, max 3 tentativas")

    completed_days: list[str] = []
    skipped_days: list[tuple[str, str]] = []
    cache_hits = 0
    cache_misses = 0
    miss_reasons: list[str] = []

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
        motivo = result.get("cache_motivo")
        if status == "cache_hit":
            cache_hits += 1
            completed_days.append(date_iso)
            print(f"[ok] Dia {date_iso} (cache-hit, skip)")
        elif status == "updated":
            cache_misses += 1
            if motivo and motivo != "cache_hit":
                miss_reasons.append(motivo)
            completed_days.append(date_iso)
            print(
                f"[ok] Dia {date_iso} ({result['n_pages']} paginas, "
                f"{result['size_mb']:.2f}MB)"
            )
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
        f"[summary] Completados: {len(completed_days)}/{len(days)} | "
        f"Pulados: {len(skipped_days)} | Cache-hit: {cache_hits} | "
        f"Cache-miss: {cache_misses} | Tempo: {elapsed_total:.0f}s"
    )
    if miss_reasons:
        print(f"[summary] Motivos de cache-miss: {','.join(miss_reasons)}")

    if summary_path:
        try:
            Path(summary_path).write_text(
                (
                    f"cache_hits={cache_hits}\n"
                    f"cache_misses={cache_misses}\n"
                    f"miss_reasons={','.join(miss_reasons)}\n"
                    f"completed={len(completed_days)}\n"
                    f"skipped={len(skipped_days)}\n"
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"[summary] AVISO: nao foi possivel gravar {summary_path}: {exc}")

    return 0 if len(completed_days) == len(days) else 1


# ----------------------------------------------------------------------------
# Self-test (offline): valida os 6 cenarios de cache check sem rede.
# ----------------------------------------------------------------------------
def _run_self_test() -> int:
    import tempfile
    import b3_api

    ping_envelope_base = {
        "lastUpdateDate": "2026-05-14T13:22:51.31",
        "table": {
            "pageCount": 1234,
            "values": [["2026-05-14", "2026-05-14", "DEB1", "DEB"] + [""] * 13],
        },
    }
    ping_envelope_changed_last = {**ping_envelope_base, "lastUpdateDate": "2026-05-14T14:00:00.00"}
    ping_envelope_changed_total = {
        **ping_envelope_base,
        "table": {**ping_envelope_base["table"], "pageCount": 4321},
    }
    ping_envelope_changed_bytes = {
        **ping_envelope_base,
        "table": {
            "pageCount": ping_envelope_base["table"]["pageCount"],
            # codigo_if 'DEB1_ALT' troca o comprimento do payload sem mexer
            # em lastUpdate nem pageCount -> isola firstPageBytes_changed.
            "values": [["2026-05-14", "2026-05-14", "DEB1_ALT_EXTRA", "DEB"] + [""] * 13],
        },
    }
    expected_meta = build_meta_from_ping(
        ping_envelope_base, "2026-05-15T03:00:00+00:00"
    )

    scenarios = [
        ("cache-hit (3 bateram)", ping_envelope_base, "match_meta", True, "cache_hit"),
        ("lastUpdate_changed", ping_envelope_changed_last, "match_meta", False, "lastUpdate_changed"),
        ("totalRecords_changed", ping_envelope_changed_total, "match_meta", False, "totalRecords_changed"),
        ("firstPageBytes_changed", ping_envelope_changed_bytes, "match_meta", False, "firstPageBytes_changed"),
        ("metadata_ausente", ping_envelope_base, "no_meta", False, "metadata_ausente"),
        ("json_corrompido", ping_envelope_base, "corrupted", False, "json_corrompido"),
    ]

    original_post_page = b3_api.post_page
    failures: list[str] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            for label, ping_env, file_mode, exp_hit, exp_motivo in scenarios:
                out_path = tmpdir / "2026-05-14.json"
                if out_path.exists():
                    out_path.unlink()
                if file_mode == "match_meta":
                    out_path.write_text(json.dumps({
                        "meta": expected_meta,
                        "rows": [],
                    }), encoding="utf-8")
                elif file_mode == "no_meta":
                    out_path.write_text(json.dumps({
                        "rows": [],
                        "last_b3_update": "old",
                    }), encoding="utf-8")
                elif file_mode == "corrupted":
                    out_path.write_text(
                        "{this is not :: valid json}}", encoding="utf-8"
                    )

                def _mock_post_page(table, date_iso, page, page_size, **kw):
                    assert page == 1 and page_size == 1, (
                        f"self-test so suporta ping@1; got {page}/{page_size}"
                    )
                    return ping_env

                b3_api.post_page = _mock_post_page

                hit, motivo, _ = b3_api.check_cache(
                    TABLE_NAME, "2026-05-14", out_path
                )
                ok = (hit == exp_hit) and (motivo == exp_motivo)
                status = "OK" if ok else "FAIL"
                print(
                    f"  [{status}] {label}: hit={hit} motivo={motivo} "
                    f"(esperado hit={exp_hit} motivo={exp_motivo})"
                )
                if not ok:
                    failures.append(label)
    finally:
        b3_api.post_page = original_post_page

    print()
    if failures:
        print(f"SELF-TEST FALHOU em {len(failures)} cenario(s): {failures}")
        return 1
    print("SELF-TEST OK (6/6 cenarios)")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--self-test":
        return _run_self_test()
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
