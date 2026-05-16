"""Fetch de NEGOCIOS CONSOLIDADOS de renda fixa da B3.

Diferenca de fonte:
  - fetch_b3_trades.py        -> negocio a negocio (trade-by-trade).
  - fetch_b3_trades_consolidated.py (este) -> negocios consolidados por
                                 instrumento/dia (volume total, pmp, etc).

Fonte de dados (fetch full): endpoint OFICIAL de export CSV
  POST https://arquivos.b3.com.br/bdi/table/export/csv?lang=pt-BR
  Body: {"Name":"ConsolidatedRecords","Date":<dia>,"FinalDate":<dia>,
         "ClientId":"","Filters":{}}
  Response: text/csv (utf-8 com BOM, separator ';', 5-7 linhas de
  glossario/preambulo antes do header tabular). 1 unico request, sem
  paginacao -> dados completos e consistentes.

Migracao do endpoint paginado (PR atual): o paginado
/bdi/table/ConsolidatedRecords/{ini}/{fim}/{page}/{page_size} degrada
sob carga — em 2026-05-15 com ~413 paginas ate ~28% delas retornaram
conteudo CACHED de paginas anteriores em vez do conteudo real (11.430
linhas duplicadas espurias + 11.4k linhas REAIS perdidas, sum ~9.6 Bi
vs 12.5 Bi). O endpoint export resolve esse problema na raiz. O ping@1
paginado continua usado APENAS pro cache check leve (1 row, sem
exposicao ao bug de overlap).

Comportamento:
  - Janela: 5 dias uteis B3 mais recentes (HOJE se dia util + D-1..D-4).
  - Para cada dia: cache check (ping leve POST .../1/1) com 3
    indicadores (lastUpdateDate + totalRecords + firstPageBytes).
    Cache-hit -> pula fetch full. Cache-miss / --force -> fetch full
    via export CSV.
  - Sucesso -> grava data/b3_trades_consolidated/<date>.json (atomico
    via .tmp + rename) com `meta` populado a partir do ping@1.
  - Falha de rede/timeout/5xx apos retry: preserva arquivo existente.
  - 403/404: pula (FDS/feriado/nao publicado), preserva arquivo.
  - Exit != 0 se qualquer dia falhar (cache-hit + indisponivel nao
    contam como falha).
  - NUNCA gera dados sinteticos/fallback.

Esquema do JSON local: inalterado (compat com build_dashboard).
17 colunas na ordem fixa abaixo. `meta` continua existindo no top-level
com a mesma estrutura (schemaVersion=2).

Modo self-test (CLI: `--self-test`): roda 6 cenarios de cache check
sem rede + teste de parse CSV (7 cenarios totais).

Modo `--force` (CLI: `--force [YYYY-MM-DD]`): ignora cache check e
refetch full via export CSV.

Persistencia: data/b3_trades_consolidated/{YYYY-MM-DD}.json + manifest.

Schema das 17 colunas (ordem fixa):
  1 data_negocio    2 data_referencia 3 codigo_if    4 instrumento
  5 isin            6 emissor         7 data_liquidacao
  8 quantidade      9 pmp            10 preco       11 volume_unit
 12 min            13 max            14 n_negocios  15 volume_total
 16 grupo (INTRAGRUPO / EXTRAGRUPO / "-")            17 extra
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
    build_meta_from_ping,
    check_cache,
    extract_total_records,
    fetch_export_csv,
    get_col,
    last_n_business_days,
    parse_br_date_iso,
    parse_br_float,
    parse_br_int_optional,
    parse_export_csv,
    ping_first_page,
)


TABLE_NAME = "ConsolidatedRecords"
REFRESH_WINDOW_DAYS = 5

# Schema fixo (ordem que o JSON local persiste). Mantido inalterado pra
# compat com build_dashboard.py (que indexa por posicao em _CONS_COL_*).
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


def _map_consolidated_row(raw: list, col_index: dict[str, int]) -> list:
    """Mapeia uma linha do CSV B3 (16 cols pt-BR) pro schema local
    (17 cols snake_case). `data_negocio` eh duplicado em `data_referencia`
    (mesmo valor) — historico mantido pelo build_dashboard.

    `isin` em '-'/'' fica '-' (string), como no schema atual. `grupo`
    em '' fica '-' (compat com rows sem classificacao). `n_negocios`
    em '-' fica None (visto no JSON atual; build_dashboard ignora None).
    `extra` (Oscilacao) eh armazenado como string raw — no schema atual
    a coluna 16 tem strings tanto pra valor numerico quanto '-'.
    """
    g = lambda *names: get_col(raw, col_index, *names)
    data_neg = parse_br_date_iso(g("Data negocio", "Data negócio"))
    return [
        data_neg,
        data_neg,
        g("Codigo IF", "Código IF").strip(),
        g("Instrumento financeiro").strip(),
        g("Codigo ISIN", "Código ISIN").strip() or "-",
        g("Emissor").strip(),
        parse_br_date_iso(g("Data liquidacao", "Data liquidação")),
        parse_br_float(g("Quantidade negociada")),
        parse_br_float(g("Preco medio", "Preço médio")),
        parse_br_float(g("Ultimo preco", "Último preço")),
        parse_br_float(g("Preco de referencia", "Preço de referência")),
        parse_br_float(g("Preco minimo", "Preço mínimo")),
        parse_br_float(g("Preco maximo", "Preço máximo")),
        parse_br_int_optional(g("Numero de negocios", "Número de negócios")),
        parse_br_float(g("Volume financeiro (R$)", "Volume financeiro")),
        g("Classificacao do negocio", "Classificação do negócio").strip() or "-",
        g("Oscilacao", "Oscilação").strip() or "-",
    ]


def _fetch_full_day(date_iso: str, ping_envelope: dict | None) -> dict:
    """Fetch full via export CSV + escrita atomica pra um dia.

    `ping_envelope` (do cache check ou do --force) eh reaproveitado pra
    popular o `meta` (e last_b3_update/total_b3_records). Se None
    (ping falhou), tenta re-pingar ao final; se isso tambem falhar,
    grava sem `meta` (proximo run dara cache-miss com
    motivo=metadata_ausente).

    Sem paginacao, sem dedup defensivo: o export CSV ja entrega o dump
    completo num unico request. A escrita atomica via .tmp + rename
    SUBSTITUI o JSON do dia completamente — nunca acumula.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{date_iso}.json"

    csv_text = fetch_export_csv(TABLE_NAME, date_iso)
    col_index, raw_rows = parse_export_csv(csv_text)
    rows = [_map_consolidated_row(r, col_index) for r in raw_rows]

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
        "source": SOURCE_TAG,
        "table_name": TABLE_NAME,
        "columns": COLUMNS,
        "rows": rows,
        "stats": {
            "n_rows": len(rows),
            "csv_bytes": len(csv_text),
        },
    }
    if meta is not None:
        payload["meta"] = meta

    _write_payload_atomic(out_path, payload)
    size_mb = out_path.stat().st_size / (1024 * 1024)

    uniq_fmt = f"{len(rows):,}".replace(",", ".")
    csv_kb = len(csv_text) / 1024
    print(
        f"[{date_iso}] OK: {uniq_fmt} linha(s) | "
        f"CSV {csv_kb:.0f}KB | {size_mb:.2f} MB (export)"
    )

    _update_manifest(date_iso, len(rows), out_path.name)
    return {
        "date": date_iso,
        "status": "updated",
        "n_rows": len(rows),
        "size_mb": size_mb,
    }


def fetch_day(date_iso: str, *, force: bool = False) -> dict:
    """Busca consolidated de UM dia com cache check + fetch on miss.

    `force=True` ignora cache check e refaz o fetch full via export CSV.

    Retorna dict com `status` em {'cache_hit', 'updated', 'unavailable'}
    e `cache_motivo` (cache_hit | metadata_ausente | json_corrompido |
    ping_falhou | lastUpdate_changed | totalRecords_changed |
    firstPageBytes_changed | unavailable | forced).
    Levanta RuntimeError em falha de rede pos-retries durante fetch
    full — chamador trata sem alterar arquivo.
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


def refresh_recent_days(n: int = REFRESH_WINDOW_DAYS, *, force: bool = False) -> int:
    """Refresh consolidated dos N du B3 mais recentes (com cache check).

    Wall-clock budget (env `B3_CONSOLIDATED_BUDGET_SECONDS`, default 9000s)
    cobre LENTIDAO genuina do export CSV (1 request mas pode ser grande).
    Quando estoura, dias restantes sao pulados; arquivos existentes
    preservados. Cache-hits consomem ~poucos segundos (so o ping@1).

    Exit code:
      - 0: TODOS os N dias completados ('cache_hit', 'updated' ou
        'unavailable' legitimo da B3).
      - 1: qualquer dia pulado por wall-clock OU falha apos retries.

    Se env `B3_CONSOLIDATED_CACHE_SUMMARY` estiver setado, grava
    summary (hits/misses/motivos) no path indicado pra consumo pelo
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
            result = fetch_day(date_iso, force=force)
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
                f"[ok] Dia {date_iso} ({result['n_rows']} rows, "
                f"{result['size_mb']:.2f}MB)"
            )
        else:
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
# Self-test (offline): 6 cenarios de cache check + 1 cenario de parse CSV.
# ----------------------------------------------------------------------------
_SAMPLE_CSV_CONSOLIDATED = (
    "ANBIMA Servicos\n"
    "Negocios Consolidados Renda Fixa\n"
    "Data Referencia: 15/05/2026\n"
    "\n"
    "Glossario...\n"
    "Data negocio;Codigo IF;Instrumento financeiro;Codigo ISIN;Emissor;"
    "Data liquidacao;Quantidade negociada;Preco minimo;Preco medio;"
    "Preco maximo;Ultimo preco;Preco de referencia;Numero de negocios;"
    "Volume financeiro (R$);Classificacao do negocio;Oscilacao\n"
    "15/05/2026;AALM12;DEB;BRAALMDBS017;EMISSOR1;16/05/2026;"
    "47.318;1.036,99;1.036,99;1.037,18;1.036,99;1.037,01;"
    "9;49.068.083,00;EXTRAGRUPO;0,10\n"
    "15/05/2026;LCA1234;LCA;-;EMISSOR2;16/05/2026;"
    "0;0,00;1.000,00;0,00;0,00;1.000,00;-;1.000,00;-;-\n"
)


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

    # Cenario adicional: parse CSV (substitui o teste de dedup defensivo
    # que era necessario contra o bug de paginas sobrepostas — agora o
    # export CSV resolve isso na raiz). Valida que parse_export_csv +
    # _map_consolidated_row produzem rows compativeis com o schema.
    col_index, raw_rows = b3_api.parse_export_csv(_SAMPLE_CSV_CONSOLIDATED)
    parsed = [_map_consolidated_row(r, col_index) for r in raw_rows]
    csv_ok = (
        len(parsed) == 2
        and parsed[0][2] == "AALM12"     # codigo_if
        and parsed[0][3] == "DEB"         # instrumento
        and parsed[0][4] == "BRAALMDBS017"  # isin
        and parsed[0][7] == 47318.0       # quantidade
        and parsed[0][8] == 1036.99       # pmp
        and parsed[0][13] == 9            # n_negocios
        and abs(parsed[0][14] - 49068083.0) < 1e-6  # volume_total
        and parsed[0][15] == "EXTRAGRUPO"  # grupo
        and parsed[1][4] == "-"           # isin '-' preservado
        and parsed[1][13] is None         # n_negocios '-' -> None
        and parsed[1][15] == "-"          # grupo '-' preservado
        and parsed[0][0] == "2026-05-15T00:00:00"  # data_negocio convertida
        and parsed[0][1] == parsed[0][0]  # data_referencia == data_negocio
    )
    status = "OK" if csv_ok else "FAIL"
    print(
        f"  [{status}] parse CSV: rows={len(parsed)} "
        f"codigo_if={parsed[0][2] if parsed else None} "
        f"quantidade={parsed[0][7] if parsed else None}"
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
