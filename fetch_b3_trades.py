"""Fetch de trades de renda fixa da B3 (DEB/CRA/CRI/CFF/COE) — trade-by-trade.

Fonte de dados (fetch full): endpoint OFICIAL de export CSV
  POST https://arquivos.b3.com.br/bdi/table/export/csv?lang=pt-BR
  Body: {"Name":"Trade","Date":<dia>,"FinalDate":<dia>,
         "ClientId":"","Filters":{}}
  Response: text/csv (utf-8 com BOM, separator ';', 5-7 linhas de
  glossario antes do header tabular). 1 unico request, sem paginacao.

Migracao do endpoint paginado (PR atual): mesma motivacao do
fetch_b3_trades_consolidated — paginado degrada sob carga. TbT hoje
funciona razoavelmente em volume (~92 paginas) mas migramos pra ter
robustez contra crescimento + simetria com o consolidated. Validacao
empirica em 2026-05-15: 45.910 trades paginated -> 45.911 export, mesmo
conteudo. O ping@1 paginado continua usado APENAS pro cache check leve.

Comportamento:
  Para cada um dos 5 dias uteis B3 mais recentes [D-0..D-4]:
    1. CACHE CHECK (ping leve POST .../1/1, page_size=1) compara 3
       indicadores com o `meta` local (lastUpdateDate + totalRecords
       + firstPageBytes). Cache-hit -> pula fetch full.
    2. Cache-miss OU --force -> fetch full via export CSV.
    3. Apos fetch: escreve data/b3_trades/<date>.json (atomico via
       .tmp + rename) com `meta` populado a partir do ping@1.

Enriquecimento de setor PRESERVADO: apos parse do CSV, cada linha DEB
recebe `setor` via sectors.classify(ticker, issuer); demais
instrumentos recebem "Outros". Logica identica a antes da migracao.

Esquema do JSON local: inalterado (14 cols na ordem
instrument/issuer/ticker/setor/qtd/price/vol/rate/origin/time/
trade_code/isin/settlement_dt/situation), com `meta` no top-level
(schemaVersion=2). build_dashboard nao precisa saber da migracao.

Modo unitario (CLI: `python fetch_b3_trades.py YYYY-MM-DD`): aplica a
mesma logica (cache check + fetch on miss) pra um unico dia. Aceita
`--force` pra ignorar cache check.

Modo self-test (CLI: `python fetch_b3_trades.py --self-test`): 6
cenarios de cache check + 1 cenario de parse CSV (7 totais).

Em caso de erro (rede/timeout apos retry, 5xx) durante fetch full:
NAO toca no arquivo existente. 403/404: pula sem alterar arquivo.

Historico antigo (>5 dias uteis) NUNCA eh tocado por este script.
Persistencia: data/b3_trades/{YYYY-MM-DD}.json + manifest incremental.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import sectors
from b3_api import (
    B3UnavailableError,
    build_meta_from_ping,
    check_cache,
    extract_total_records,
    fetch_export_csv,
    get_col,
    parse_br_date_iso,
    parse_br_float,
    parse_br_float_optional,
    parse_export_csv,
    ping_first_page,
    refresh_recent_days as _refresh_recent_days,
)


TABLE_NAME = "Trade"
REFRESH_WINDOW_DAYS = 5

DATA_DIR = Path(__file__).parent / "data" / "b3_trades"
MANIFEST_PATH = DATA_DIR / "manifest.json"

COLUMNS = [
    "instrument", "issuer", "ticker", "setor", "qtd", "price", "vol",
    "rate", "origin", "time", "trade_code", "isin", "settlement_dt", "situation",
]


def _map_trade_row(raw: list, col_index: dict[str, int]) -> list:
    """Mapeia uma linha do CSV B3 (14 cols pt-BR) pro schema local
    (14 cols ingles + setor enriquecido). `Data negocio` do CSV eh
    descartada (redundante com o dia alvo do arquivo). `setor` eh
    inferido apenas pra DEB via sectors.classify; demais -> 'Outros'
    (logica identica a antes da migracao pro export CSV).

    `isin` em '-'/'' vira None (compat com schema atual: visto None pra
    nao-DEB e str pra DEB). `rate` em '-' vira None.
    """
    g = lambda *names: get_col(raw, col_index, *names)
    instrument = g("Instrumento financeiro").strip()
    issuer = g("Emissor").strip()
    ticker = g("Codigo IF", "Código IF").strip()
    if instrument == "DEB":
        setor = sectors.classify(ticker, issuer)
    else:
        setor = "Outros"
    isin_raw = g("Codigo ISIN", "Código ISIN").strip()
    isin = None if isin_raw in ("", "-") else isin_raw
    return [
        instrument,
        issuer,
        ticker,
        setor,
        parse_br_float(g("Quantidade negociada")),
        parse_br_float(g("Preco negocio", "Preço negócio")),
        parse_br_float(g("Volume financeiro (R$)", "Volume financeiro")),
        parse_br_float_optional(g("Taxa negocio", "Taxa negócio")),
        g("Origem negocio", "Origem negócio").strip(),
        g("Horario negocio", "Horário negócio").strip(),
        g(
            "Cod. identificador do negocio",
            "Cód. identificador do negócio",
            "Codigo identificador do negocio",
        ).strip(),
        isin,
        parse_br_date_iso(g("Data liquidacao", "Data liquidação")),
        g("Situacao negocio", "Situação negócio").strip(),
    ]


def _write_payload_atomic(out_path: Path, payload: dict) -> None:
    """Grava payload em <out_path>.tmp e renomeia atomicamente.

    os.replace eh atomico no mesmo filesystem (POSIX e Windows). Evita
    arquivos meio-gravados que confundiriam o proximo cache check.
    """
    tmp_path = out_path.parent / f"{out_path.name}.tmp"
    tmp_path.write_text(
        json.dumps(payload, separators=(",", ":")), encoding="utf-8"
    )
    os.replace(tmp_path, out_path)


def _fetch_full_day(date_iso: str, ping_envelope: dict | None) -> dict:
    """Fetch full via export CSV + escrita atomica de UM dia.

    `ping_envelope`: envelope do ping@1 usado no cache check (pode ser
    None se o ping falhou). Se nao-None, eh reaproveitado pra popular
    o `meta` apos o fetch — economiza uma chamada extra.

    Retorna dict do `result` (status, n_trades, vol_brl).
    Levanta B3UnavailableError se o export topar com 403/404.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{date_iso}.json"

    csv_text = fetch_export_csv(TABLE_NAME, date_iso)
    col_index, raw_rows = parse_export_csv(csv_text)

    rows: list[list] = []
    instruments_count: dict[str, int] = {}
    vol_total = 0.0
    for raw in raw_rows:
        row = _map_trade_row(raw, col_index)
        rows.append(row)
        inst = row[0] or "?"
        instruments_count[inst] = instruments_count.get(inst, 0) + 1
        try:
            vol_total += float(row[6] or 0)
        except (TypeError, ValueError):
            pass

    last_b3_update = None
    total_b3_records: int | None = None
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
        last_b3_update = ping_envelope.get("lastUpdateDate")
        total_b3_records = extract_total_records(ping_envelope)
    if total_b3_records is None:
        total_b3_records = len(rows)

    meta = None
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
            "csv_bytes": len(csv_text),
            "instruments_count": instruments_count,
            "vol_brl_total": vol_total,
        },
    }
    if meta is not None:
        payload["meta"] = meta

    _write_payload_atomic(out_path, payload)
    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(
        f"[{date_iso}] OK: {len(rows):,} trades, {size_mb:.2f} MB (export CSV)"
        .replace(",", ".")
    )

    _update_manifest(date_iso, len(rows), vol_total, out_path.name)

    return {
        "date": date_iso,
        "status": "updated",
        "n_trades": len(rows),
        "vol_brl": vol_total,
    }


def fetch_day(date_iso: str, *, force: bool = False) -> dict:
    """Busca trades de UM dia com cache check + fetch on miss.

    `force=True` ignora cache check e refaz o fetch full via export CSV.

    Retorna dict com `status` em {'cache_hit', 'updated', 'unavailable'}
    e `cache_motivo` (cache_hit | metadata_ausente | json_corrompido |
    ping_falhou | lastUpdate_changed | totalRecords_changed |
    firstPageBytes_changed | unavailable | forced).
    'cache_hit': arquivo existente preservado, fetch full pulado.
    'updated': fetch full completo, arquivo reescrito (atomico).
    'unavailable': B3 retornou 403/404 (FDS/feriado/dia nao publicado) —
                   arquivo existente preservado.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{date_iso}.json"

    if force:
        motivo = "forced"
        try:
            ping_envelope = ping_first_page(TABLE_NAME, date_iso)
        except B3UnavailableError as exc:
            print(
                f"[{date_iso}] {exc} - esperado em FDS/feriado/dia ainda nao publicado, pulando"
            )
            return {"date": date_iso, "status": "unavailable", "cache_motivo": "unavailable"}
        except Exception as exc:
            print(
                f"[{date_iso}] AVISO: ping@1 falhou em modo --force ({exc}); "
                f"prosseguindo sem meta inicial"
            )
            ping_envelope = None
    else:
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


def refresh_recent_days(n: int = REFRESH_WINDOW_DAYS, *, force: bool = False) -> int:
    summary_path = os.environ.get("B3_TRADES_CACHE_SUMMARY")
    return _refresh_recent_days(
        lambda d: fetch_day(d, force=force), n, label="B3 Trade",
        summary_path=summary_path,
    )


# ----------------------------------------------------------------------------
# Self-test (offline): 6 cenarios de cache check + 1 cenario de parse CSV.
# ----------------------------------------------------------------------------
_SAMPLE_CSV_TRADE = (
    "ANBIMA Servicos\n"
    "Negocios Renda Fixa - Trade by Trade\n"
    "Data Referencia: 15/05/2026\n"
    "\n"
    "Glossario...\n"
    "Instrumento financeiro;Emissor;Codigo IF;Quantidade negociada;"
    "Preco negocio;Volume financeiro (R$);Taxa negocio;Origem negocio;"
    "Horario negocio;Data negocio;Cod. identificador do negocio;"
    "Codigo ISIN;Data liquidacao;Situacao negocio\n"
    "DEB;TIM S.A.;TIMS24;1.000;1.000,00;1.000.000,00;5,5;Balcao;"
    "10:00:00;15/05/2026;#TC1;BRTIMSDB001;16/05/2026;Confirmado\n"
    "COE;BANCO X;BX1234;500;1,00;500,00;-;Registro;11:00:00;"
    "15/05/2026;#TC2;-;15/05/2026;Confirmado\n"
)


def _run_self_test() -> int:
    """6 cenarios de cache check + 1 cenario de parse CSV (=7 totais).

    Cobre: cache-hit, lastUpdate_changed, totalRecords_changed,
    firstPageBytes_changed, metadata_ausente, json_corrompido, parse CSV
    (incluindo enriquecimento de setor pra DEB).

    Mocka post_page via monkey-patch e usa diretorio temporario pra nao
    tocar em data/b3_trades/ real. Retorna 0 se TODOS passarem.
    """
    import tempfile
    import b3_api

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

    # Cenario adicional: parse CSV + enriquecimento de setor. Garante
    # que a migracao pro export CSV preserva o schema (14 cols) e que
    # sectors.classify continua sendo aplicado em DEB.
    col_index, raw_rows = b3_api.parse_export_csv(_SAMPLE_CSV_TRADE)
    parsed = [_map_trade_row(r, col_index) for r in raw_rows]
    csv_ok = (
        len(parsed) == 2
        and parsed[0][0] == "DEB"          # instrument
        and parsed[0][2] == "TIMS24"       # ticker
        and parsed[0][3] != "Outros"       # setor enriquecido (DEB)
        and parsed[0][4] == 1000.0         # qtd
        and parsed[0][5] == 1000.0         # price
        and abs(parsed[0][6] - 1000000.0) < 1e-6  # vol
        and parsed[0][7] == 5.5            # rate
        and parsed[0][9] == "10:00:00"     # time
        and parsed[0][10] == "#TC1"        # trade_code
        and parsed[0][11] == "BRTIMSDB001"  # isin
        and parsed[0][12] == "2026-05-16T00:00:00"  # settlement_dt
        and parsed[0][13] == "Confirmado"  # situation
        and parsed[1][0] == "COE"
        and parsed[1][3] == "Outros"       # nao-DEB
        and parsed[1][7] is None           # rate '-' -> None
        and parsed[1][11] is None          # isin '-' -> None
    )
    status = "OK" if csv_ok else "FAIL"
    print(
        f"  [{status}] parse CSV + setor: rows={len(parsed)} "
        f"setor_DEB={parsed[0][3] if parsed else None}"
    )
    if not csv_ok:
        failures.append("parse CSV")

    print()
    if failures:
        print(f"SELF-TEST FALHOU em {len(failures)} cenario(s): {failures}")
        return 1
    print("SELF-TEST OK (7/7 cenarios)")
    return 0


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    force = False
    if "--force" in args:
        force = True
        args = [a for a in args if a != "--force"]
    if args and args[0] == "--self-test":
        return _run_self_test()
    if args:
        date_iso = args[0]
        date.fromisoformat(date_iso)
        try:
            fetch_day(date_iso, force=force)
        except Exception as exc:
            print(f"FALHA: {exc}")
            return 1
        return 0
    return refresh_recent_days(force=force)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
