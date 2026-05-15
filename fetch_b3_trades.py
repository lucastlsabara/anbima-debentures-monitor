"""Fetch de trades de renda fixa da B3 (DEB/CRA/CRI/CFF/COE).

Endpoint publico nao documentado:
  POST https://arquivos.b3.com.br/bdi/table/Trade/{ini}/{fim}/{page}/{page_size}
Headers: Content-Type: application/json
Body: {}

Comportamento (sem args, modo canonico):
  Para cada um dos 5 dias uteis B3 mais recentes [D-0..D-4]:
    1. CACHE CHECK (ping leve POST .../1/1, page_size=1) compara 3
       indicadores com o `meta` local: lastUpdateDate, totalRecords
       (=table.pageCount@page_size=1) e firstPageBytes (tamanho do
       envelope do ping serializado deterministico).
    2. Se TODOS os 3 indicadores baterem -> cache-hit, PULA fetch full.
    3. Se QUALQUER indicador divergir / meta ausente / JSON corrompido
       / ping falhar -> cache-miss, faz fetch full paginado.
    4. Apos fetch full bem-sucedido: escreve data/b3_trades/<date>.json
       (atomico via .tmp + rename) com `meta` populado a partir do ping.

Esquema do JSON local (decisao de design):
  Adicionamos a chave top-level `meta` ao schema existente — NAO
  movemos `rows` para uma sub-chave nem criamos arquivo paralelo
  .meta.json. Motivacao: zero impacto em consumers existentes
  (`build_dashboard.py` continua lendo `payload['rows']`), e o cache
  check ja conta com `meta.schemaVersion` para distinguir formato
  novo do antigo. JSONs sem `meta` (gerados por versoes anteriores)
  sao tratados como cache-miss na primeira execucao apos este merge —
  refetch full popula o meta e nas execucoes seguintes o cache hit
  funciona normalmente.

Modo unitario (CLI: python fetch_b3_trades.py YYYY-MM-DD): aplica a
mesma logica (cache check + fetch on miss) para um unico dia.

Modo self-test (CLI: python fetch_b3_trades.py --self-test): roda os
6 cenarios sem rede (cache-hit + 5 variantes de cache-miss) usando um
mock local de post_page. Exit 0 se todos passarem.

Em caso de erro (rede/timeout apos retry, 5xx) durante fetch full:
NAO toca no arquivo existente. Preserva versao anterior. Contabiliza
falha. 403/404 (FDS/feriado/dia ainda nao publicado): pula sem alterar
arquivo.

Historico antigo (>5 dias uteis) NUNCA eh tocado por este script.
Para backfill manual, use backfill_b3_trades.py.

Persiste data/b3_trades/{YYYY-MM-DD}.json em formato colunar minificado
+ `meta` e atualiza data/b3_trades/manifest.json incrementalmente
(apenas as entradas dos dias da janela sao sobrescritas; manifest
preserva historico completo).
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import sectors
from b3_api import (
    B3UnavailableError,
    CACHE_SCHEMA_VERSION,
    build_meta_from_ping,
    check_cache,
    extract_ping_indicators,
    extract_total_records,
    ping_first_page,
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


def _write_payload_atomic(out_path: Path, payload: dict) -> None:
    """Grava payload em <out_path>.tmp e renomeia atomicamente.

    os.replace eh atomico no mesmo filesystem (POSIX e Windows). Evita
    arquivos meio-gravados que confundiriam o proximo cache check
    (caso tipico que motivou a robustez extra desta versao).
    """
    tmp_path = out_path.parent / f"{out_path.name}.tmp"
    tmp_path.write_text(
        json.dumps(payload, separators=(",", ":")), encoding="utf-8"
    )
    os.replace(tmp_path, out_path)


def _fetch_full_day(date_iso: str, ping_envelope: dict | None) -> dict:
    """Fetch full paginado de UM dia + escrita atomica.

    `ping_envelope`: envelope do ping@1 usado no cache check (pode ser
    None se o ping falhou). Se nao-None, eh reaproveitado para popular
    o `meta` apos o fetch — economiza uma chamada extra.

    Retorna dict do `result` (status, n_trades, vol_brl).
    Levanta B3UnavailableError se o fetch full topar com 403/404.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{date_iso}.json"

    first = post_page(TABLE_NAME, date_iso, 1, PAGE_SIZE)
    table = first.get("table") or {}
    page_count = int(table.get("pageCount") or 0)
    last_b3_update = first.get("lastUpdateDate")
    total_b3_records_envelope = extract_total_records(first)

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

    total_b3_records = (
        total_b3_records_envelope
        if total_b3_records_envelope is not None
        else len(rows)
    )

    # Se o ping do cache check falhou antes, tenta um ping agora para
    # popular o `meta`. Falha no re-ping nao bloqueia a gravacao: meta
    # fica ausente e o proximo run dara cache-miss com motivo=metadata_ausente.
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
        "columns": COLUMNS,
        "rows": rows,
        "stats": {
            "n_trades": len(rows),
            "n_pages_fetched": max(page_count, 1),
            "instruments_count": instruments_count,
            "vol_brl_total": vol_total,
        },
    }
    if meta is not None:
        payload["meta"] = meta

    _write_payload_atomic(out_path, payload)
    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"[{date_iso}] OK: {len(rows):,} trades, {size_mb:.2f} MB".replace(",", "."))

    _update_manifest(date_iso, len(rows), vol_total, out_path.name)

    return {
        "date": date_iso,
        "status": "updated",
        "n_trades": len(rows),
        "vol_brl": vol_total,
    }


def fetch_day(date_iso: str) -> dict:
    """Busca trades de UM dia com cache check + fetch on miss.

    Retorna dict com `status` em {'cache_hit', 'updated', 'unavailable'}
    e `cache_motivo` (cache_hit | metadata_ausente | json_corrompido |
    ping_falhou | lastUpdate_changed | totalRecords_changed |
    firstPageBytes_changed | unavailable).
    'cache_hit': arquivo existente preservado, fetch full pulado.
    'updated': fetch full completo, arquivo reescrito (atomico).
    'unavailable': B3 retornou 403/404 (FDS/feriado/dia nao publicado) —
                   arquivo existente preservado.

    Levanta RuntimeError se houver falha de rede apos os retries no
    fetch full (chamador deve capturar e contar como falha sem alterar
    arquivo).
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

    # Cache-miss: fetch full. Reaproveita ping_envelope para popular meta.
    try:
        result = _fetch_full_day(date_iso, ping_envelope)
    except B3UnavailableError as exc:
        print(
            f"[{date_iso}] {exc} - esperado em FDS/feriado/dia ainda nao publicado, pulando"
        )
        return {"date": date_iso, "status": "unavailable", "cache_motivo": "unavailable"}
    result["cache_motivo"] = motivo
    return result


def _update_manifest(date_iso: str, n_trades: int, vol_brl: float, filename: str) -> None:
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
    summary_path = os.environ.get("B3_TRADES_CACHE_SUMMARY")
    return _refresh_recent_days(
        fetch_day, n, label="B3 Trade", summary_path=summary_path
    )


# ----------------------------------------------------------------------------
# Self-test (offline): valida os 6 cenarios de cache check sem rede.
# ----------------------------------------------------------------------------
def _run_self_test() -> int:
    """Smoke test offline dos 6 cenarios documentados.

    Cobre: cache-hit, lastUpdate_changed, totalRecords_changed,
    firstPageBytes_changed, metadata_ausente, json_corrompido.

    Mocka post_page via monkey-patch e usa um diretorio temporario para
    nao tocar em data/b3_trades/ real. Retorna 0 se TODOS os 6
    cenarios passarem, 1 caso contrario.
    """
    import tempfile
    import b3_api

    # Fixtures: 1 registro fake (estrutura B3 nao importa para o cache).
    ping_envelope_base = {
        "lastUpdateDate": "2026-05-14T13:22:51.31",
        "table": {
            "pageCount": 38503,
            "values": [
                ["x", "y", "DEB", "EMI", "TIC1", 1, 100.0, 100000.0, 5.0,
                 "BAL", "10:00:00", "z", "TC1", "ISIN1", "2030-01-01", "OK"],
            ],
        },
    }
    ping_envelope_changed_last = {**ping_envelope_base, "lastUpdateDate": "2026-05-14T14:00:00.00"}
    ping_envelope_changed_total = {
        **ping_envelope_base,
        "table": {**ping_envelope_base["table"], "pageCount": 99999},
    }
    # Para mudar firstPageBytes sem mexer em lastUpdate nem pageCount:
    # altera o 'values' (mesmo numero de registros, conteudo diferente).
    ping_envelope_changed_bytes = {
        **ping_envelope_base,
        "table": {
            "pageCount": ping_envelope_base["table"]["pageCount"],
            "values": [
                ["X", "Y", "DEB", "EMI2", "TIC2", 9, 200.0, 200000.0, 6.0,
                 "BAL", "11:00:00", "Z", "TC2", "ISIN2", "2030-01-01", "OK"],
            ],
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
                # b3_api.ping_first_page chama post_page, ja monkey-patched.

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
