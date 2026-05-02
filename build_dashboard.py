"""Pré-agrega snapshots em ``history/`` e gera dashboard estático.

Saídas (todas em ``data/`` para fetch lazy do frontend):
  - manifest.json            : datas disponíveis + lista de setores
  - overview.json            : KPIs, curvas overlay (T, T-1, T-5, T-21, T-63),
                                spread por indexador, histograma, top movers
  - curves_history.json      : matriz dates x vértices_du da ETTJ NTN-B
  - heatmap_history.json     : grid setor x bucket-duration (atual + Δ7d + Δ30d)
  - movements.json           : tabela completa do dia com Δ D-1/D-5/D-21
  - dispersion/_index.json   : datas com snapshot de dispersão disponível
  - dispersion/<date>.json   : papéis (codigo, emissor, setor, dur, taxa, spread)

Também emite ``index.html`` (single file, hash routing, 5 tabs, Plotly +
Tabulator + Flatpickr via CDN).

REGRA: nunca inventa dados. Tudo vem de ``history/<YYYY-MM-DD>.json`` real.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

from sectors import SECTORS, classify, clean_emissor, cobertura_label
from compute_spreads import indexador_group as _indexador_group

ROOT = Path(__file__).parent
HIST_DIR = ROOT / "history"
DATA_DIR = ROOT / "data"


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def _load_history() -> list[dict]:
    """Lê todos os snapshots em history/, ordenados por data crescente."""
    if not HIST_DIR.exists():
        return []
    snaps = []
    for p in sorted(HIST_DIR.glob("*.json")):
        try:
            snaps.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            print(f"[warn ] {p} inválido, pulando", file=sys.stderr)
    snaps.sort(key=lambda s: s["data_referencia"])
    return snaps


def _write_json(path: Path, payload: dict, *, compact: bool = True) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def _br_date(iso: str | None) -> str:
    if not iso:
        return "—"
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


def _indexador_order() -> list[str]:
    return ["IPCA+", "DI+", "%DI", "Prefixado", "IGP-M+", "Outros"]


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    vs = sorted(values)
    k = (len(vs) - 1) * q
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return vs[int(k)]
    return vs[lo] * (hi - k) + vs[hi] * (k - lo)


def _median(values: list[float]) -> float | None:
    return _percentile(values, 0.5)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


# ----------------------------------------------------------------------------
# enrichment: anota cada papel com setor + cobertura (uma vez)
# ----------------------------------------------------------------------------

def _enrich(papers: list[dict]) -> list[dict]:
    out = []
    for p in papers:
        codigo = p.get("codigo")
        emissor = clean_emissor(p.get("emissor"))
        setor = classify(codigo, emissor)
        cob = cobertura_label(codigo, p.get("emissor"))
        e = dict(p)
        e["emissor_clean"] = emissor
        e["setor"] = setor
        e["cobertura"] = cob
        e["indexador_grupo"] = _indexador_group(p.get("indice"))
        out.append(e)
    return out


# ----------------------------------------------------------------------------
# manifest
# ----------------------------------------------------------------------------

def build_manifest(snaps: list[dict]) -> dict:
    counts: dict[str, int] = defaultdict(int)
    com_spread: dict[str, int] = defaultdict(int)
    if snaps:
        for d in snaps[-1]["debentures"]:
            grp = _indexador_group(d.get("indice"))
            counts[grp] += 1
            if d.get("spread_pp") is not None:
                com_spread[grp] += 1
    indexadores_presentes = [
        {"label": grp, "n": counts[grp], "n_com_spread": com_spread[grp]}
        for grp in _indexador_order() if counts[grp] > 0
    ]
    return {
        "dates": [s["data_referencia"] for s in snaps],
        "latest": snaps[-1]["data_referencia"] if snaps else None,
        "n_snapshots": len(snaps),
        "sectors": SECTORS,
        "indexadores": _indexador_order(),
        "indexadores_presentes": indexadores_presentes,
        "diagnostico_metodo": (snaps[-1].get("diagnostico_metodo") if snaps else None),
        "buckets_duration": [
            {"label": "0–2a", "min": 0.0, "max": 2.0},
            {"label": "2–5a", "min": 2.0, "max": 5.0},
            {"label": "5–10a", "min": 5.0, "max": 10.0},
            {"label": "10a+", "min": 10.0, "max": 9999.0},
        ],
    }


# ----------------------------------------------------------------------------
# overview
# ----------------------------------------------------------------------------

def _curve_for(snap: dict) -> list[dict]:
    """ETTJ IPCA do snapshot, em formato compacto (anos, taxa)."""
    return [
        {"a": round(r["vertice_anos"], 3), "t": r["taxa_ipca"]}
        for r in sorted(snap["ettj_ipca"], key=lambda r: r["vertice_du"])
    ]


def _snap_at_offset(snaps: list[dict], idx_today: int, offset: int) -> dict | None:
    j = idx_today - offset
    if j < 0:
        return None
    return snaps[j]


def _kpis_for(papers: list[dict], today: dict) -> dict:
    liq = [p for p in papers if not p.get("flag_iliquido")]
    iliq = [p for p in papers if p.get("flag_iliquido")]
    estag = [p for p in papers if p.get("flag_estagnado")]
    deltas_bps = [
        p["delta_spread_bps"] for p in liq if p.get("delta_spread_bps") is not None
    ]
    n_com_spread = sum(1 for p in papers if p.get("spread_pp") is not None)
    termometro_bps = sum(deltas_bps) if deltas_bps else None
    termometro_med = _median(deltas_bps)
    today_iso = today["data_referencia"]
    return {
        "data_referencia": today_iso,
        "data_referencia_br": _br_date(today_iso),
        "data_anterior": today.get("data_anterior"),
        "data_anterior_br": _br_date(today.get("data_anterior")),
        "n_total": len(papers),
        "n_liquidos": len(liq),
        "n_iliquidos": len(iliq),
        "pct_iliquidos": round(100.0 * len(iliq) / len(papers), 2) if papers else None,
        "n_estagnados": len(estag),
        "pct_estagnados": round(100.0 * len(estag) / len(papers), 2) if papers else None,
        "n_com_spread": n_com_spread,
        "termometro_bps": round(termometro_bps, 1) if termometro_bps is not None else None,
        "termometro_med_bps": round(termometro_med, 1) if termometro_med is not None else None,
        "n_com_d1": len(deltas_bps),
    }


def _histogram_for(spreads: list[float], bin_pp: float = 0.25,
                   cap_lo: float = -2.0, cap_hi: float = 9.0) -> dict | None:
    if not spreads:
        return None
    spreads = sorted(spreads)
    lo = max(math.floor(min(spreads) * 4) / 4, cap_lo)
    hi = min(math.ceil(max(spreads) * 4) / 4, cap_hi)
    edges: list[float] = []
    x = lo
    while x <= hi + 1e-9:
        edges.append(round(x, 4))
        x += bin_pp
    counts = [0] * (len(edges) - 1)
    for v in spreads:
        v_clip = min(max(v, edges[0]), edges[-1] - 1e-9)
        for i in range(len(edges) - 1):
            if edges[i] <= v_clip < edges[i + 1]:
                counts[i] += 1
                break
    return {
        "edges": edges, "counts": counts, "n": len(spreads),
        "p25": round(_percentile(spreads, 0.25), 4),
        "p50": round(_percentile(spreads, 0.50), 4),
        "p75": round(_percentile(spreads, 0.75), 4),
        "min": round(min(spreads), 4),
        "max": round(max(spreads), 4),
    }


def _top_movers_for(papers: list[dict]) -> dict:
    movers = [p for p in papers if p.get("delta_spread_bps") is not None]
    movers_sorted = sorted(movers, key=lambda p: p["delta_spread_bps"], reverse=True)

    def _row(p: dict) -> dict:
        return {
            "codigo": p["codigo"],
            "emissor": p.get("emissor_clean"),
            "setor": p.get("setor"),
            "duration_anos": p.get("duration_anos"),
            "spread_pp": p.get("spread_pp"),
            "taxa": p.get("taxa_indicativa"),
            "delta_spread_bps": p.get("delta_spread_bps"),
            "delta_taxa_bps": p.get("delta_taxa_bps"),
            "indexador": p.get("indexador_grupo"),
        }

    return {
        "abrindo": [_row(p) for p in movers_sorted[:10]],
        "fechando": [_row(p) for p in movers_sorted[-10:][::-1]],
        "n_com_d1": len(movers),
    }


def build_overview(snaps: list[dict], enriched_today: list[dict]) -> dict:
    today = snaps[-1]
    idx_today = len(snaps) - 1

    kpis_all = _kpis_for(enriched_today, today)
    kpis_by_indexador: dict[str, dict] = {"Todos": kpis_all}
    histogram_by_indexador: dict[str, dict] = {}
    top_movers_by_indexador: dict[str, dict] = {"Todos": _top_movers_for(enriched_today)}

    by_grp: dict[str, list[dict]] = defaultdict(list)
    for p in enriched_today:
        by_grp[p["indexador_grupo"]].append(p)

    for grp in _indexador_order():
        ps = by_grp.get(grp, [])
        if not ps:
            continue
        kpis_by_indexador[grp] = _kpis_for(ps, today)
        top_movers_by_indexador[grp] = _top_movers_for(ps)
        spreads = [p["spread_pp"] for p in ps
                   if p.get("spread_pp") is not None and not p.get("flag_iliquido")]
        if spreads:
            histogram_by_indexador[grp] = _histogram_for(spreads)

    overlay_offsets = [("today", 0), ("d1", 1), ("d5", 5), ("d21", 21), ("d63", 63)]
    curves_overlay: dict[str, dict | None] = {}
    for label, off in overlay_offsets:
        s = _snap_at_offset(snaps, idx_today, off)
        curves_overlay[label] = {
            "date": s["data_referencia"], "points": _curve_for(s),
        } if s is not None else None

    by_grp_today: dict[str, list[float]] = defaultdict(list)
    for p in enriched_today:
        if p.get("spread_pp") is not None and not p.get("flag_iliquido"):
            by_grp_today[p["indexador_grupo"]].append(p["spread_pp"])

    sparkline_window = 21
    spark_dates: list[str] = []
    spark_by_grp: dict[str, list[float | None]] = defaultdict(list)
    start = max(0, idx_today - sparkline_window + 1)
    for j in range(start, idx_today + 1):
        s = snaps[j]
        spark_dates.append(s["data_referencia"])
        bucket: dict[str, list[float]] = defaultdict(list)
        for d in s["debentures"]:
            sp = d.get("spread_pp")
            if sp is None or d.get("flag_iliquido"):
                continue
            bucket[_indexador_group(d.get("indice"))].append(sp)
        for grp in by_grp_today.keys():
            vals = bucket.get(grp, [])
            spark_by_grp[grp].append(_median(vals))

    spread_by_indexador = []
    for grp in _indexador_order():
        if grp not in by_grp_today:
            continue
        vals = by_grp_today[grp]
        spread_by_indexador.append({
            "label": grp,
            "median_spread_pp": round(_median(vals), 4),
            "mean_spread_pp": round(_mean(vals), 4),
            "count": len(vals),
            "sparkline_dates": spark_dates,
            "sparkline_median": [
                round(v, 4) if v is not None else None for v in spark_by_grp[grp]
            ],
        })

    return {
        "kpis_by_indexador": kpis_by_indexador,
        "curves_overlay": curves_overlay,
        "spread_by_indexador": spread_by_indexador,
        "histogram_by_indexador": histogram_by_indexador,
        "top_movements_by_indexador": top_movers_by_indexador,
        "diagnostico_metodo": today.get("diagnostico_metodo"),
    }


# ----------------------------------------------------------------------------
# curves history (matriz completa para Tab Curvas Históricas)
# ----------------------------------------------------------------------------

def build_curves_history(snaps: list[dict]) -> dict:
    # União de vértices_du em todos os snapshots
    vert_set: set[int] = set()
    for s in snaps:
        for r in s["ettj_ipca"]:
            vert_set.add(r["vertice_du"])
    vertices_du = sorted(vert_set)
    du_to_anos = {
        r["vertice_du"]: round(r["vertice_anos"], 3)
        for s in snaps for r in s["ettj_ipca"]
    }
    vertices_anos = [du_to_anos[d] for d in vertices_du]

    matrix: list[list[float | None]] = []
    dates: list[str] = []
    for s in snaps:
        dates.append(s["data_referencia"])
        by_du = {r["vertice_du"]: r["taxa_ipca"] for r in s["ettj_ipca"]}
        row = [round(by_du[v], 4) if v in by_du else None for v in vertices_du]
        matrix.append(row)

    return {
        "vertices_du": vertices_du,
        "vertices_anos": vertices_anos,
        "dates": dates,
        "matrix": matrix,
    }


# ----------------------------------------------------------------------------
# heatmap setor x bucket duration  (atual + Δ7d + Δ30d)
# ----------------------------------------------------------------------------

_BUCKETS: list[tuple[str, float, float]] = [
    ("0–2a", 0.0, 2.0),
    ("2–5a", 2.0, 5.0),
    ("5–10a", 5.0, 10.0),
    ("10a+", 10.0, 9999.0),
]


def _heatmap_grid(papers: list[dict]) -> tuple[list[list[float | None]], list[list[int]]]:
    # papers já filtrados por liq + indexador IPCA+
    by_cell: dict[tuple[str, str], list[float]] = defaultdict(list)
    for p in papers:
        sp = p.get("spread_pp")
        dur = p.get("duration_anos")
        if sp is None or dur is None:
            continue
        setor = p.get("setor") or "Outros"
        bucket = None
        for label, lo, hi in _BUCKETS:
            if lo <= dur < hi:
                bucket = label
                break
        if bucket is None:
            continue
        by_cell[(setor, bucket)].append(sp)
    means = []
    counts = []
    for setor in SECTORS:
        row_m = []
        row_c = []
        for label, _, _ in _BUCKETS:
            vs = by_cell.get((setor, label), [])
            row_m.append(round(_mean(vs), 4) if vs else None)
            row_c.append(len(vs))
        means.append(row_m)
        counts.append(row_c)
    return means, counts


def _delta_bps_grid(cur: list[list[float | None]],
                    old: list[list[float | None]] | None) -> list[list[float | None]] | None:
    if old is None:
        return None
    out = []
    for i in range(len(cur)):
        row = []
        for j in range(len(cur[i])):
            a, b = cur[i][j], old[i][j]
            row.append(round((a - b) * 100.0, 1) if (a is not None and b is not None) else None)
        out.append(row)
    return out


def build_heatmap_history(snaps: list[dict]) -> dict:
    """Heatmap por indexador. Apenas grupos com spread fazem sentido."""
    today = snaps[-1]
    grupos_com_spread: list[str] = []
    by_indexador: dict[str, dict] = {}

    def _enr_filter(snap: dict, grp: str) -> list[dict]:
        return _enrich([
            d for d in snap["debentures"]
            if _indexador_group(d.get("indice")) == grp
            and not d.get("flag_iliquido")
            and d.get("spread_pp") is not None
        ])

    for grp in _indexador_order():
        today_filt = _enr_filter(today, grp)
        if not today_filt:
            continue
        grupos_com_spread.append(grp)
        cur_means, cur_counts = _heatmap_grid(today_filt)

        def _grid_at(off: int, grupo: str = grp) -> list[list[float | None]] | None:
            j = len(snaps) - 1 - off
            if j < 0:
                return None
            return _heatmap_grid(_enr_filter(snaps[j], grupo))[0]

        by_indexador[grp] = {
            "current_pp": cur_means,
            "current_count": cur_counts,
            "delta_7d_bps": _delta_bps_grid(cur_means, _grid_at(7)),
            "delta_30d_bps": _delta_bps_grid(cur_means, _grid_at(30)),
        }

    return {
        "sectors": SECTORS,
        "buckets": [b[0] for b in _BUCKETS],
        "indexadores": grupos_com_spread,
        "by_indexador": by_indexador,
    }


# ----------------------------------------------------------------------------
# movements (Tab 4): tabela completa do dia com Δ D-1 / D-5 / D-21
# ----------------------------------------------------------------------------

def build_movements(snaps: list[dict], enriched_today: list[dict]) -> dict:
    """Emite payload com rows + spread_map por data, para o frontend
    comparar livremente qualquer par de datas disponíveis em history/."""

    def _rows_for(enriched: list[dict]) -> list[dict]:
        out = []
        for p in enriched:
            out.append({
                "codigo": p["codigo"],
                "emissor": p.get("emissor_clean"),
                "setor": p.get("setor"),
                "cobertura": p.get("cobertura"),
                "indexador": p.get("indexador_grupo"),
                "vencimento": p.get("vencimento"),
                "duration_anos": p.get("duration_anos"),
                "taxa": p.get("taxa_indicativa"),
                "benchmark_titulo": p.get("benchmark_titulo"),
                "benchmark_vencimento": p.get("benchmark_vencimento"),
                "taxa_benchmark": p.get("taxa_benchmark"),
                "spread_pp": p.get("spread_pp"),
                "spread_metodo": p.get("spread_metodo"),
                "pu": p.get("pu"),
                "pct_pu_par": p.get("pct_pu_par"),
                "iliquido": bool(p.get("flag_iliquido")),
                "estagnado": bool(p.get("flag_estagnado")),
                "dias_sem_variacao": p.get("dias_sem_variacao"),
            })
        return out

    by_date: dict[str, dict] = {}
    today_iso = snaps[-1]["data_referencia"]
    for s in snaps:
        date = s["data_referencia"]
        if date == today_iso:
            enriched = enriched_today
        else:
            enriched = _enrich(s["debentures"])
        by_date[date] = {"rows": _rows_for(enriched)}

    dates = [s["data_referencia"] for s in snaps]
    return {
        "dates": dates,
        "latest": today_iso,
        "by_date": by_date,
    }


# ----------------------------------------------------------------------------
# dispersion (Tab 3): scatter por data, lazy load
# ----------------------------------------------------------------------------

def build_dispersion(snaps: list[dict], dispersion_dir: Path) -> dict:
    """Scatter Duration × Spread por data, todos os indexadores que tenham spread."""
    dates = []
    indexadores_set: set[str] = set()
    for s in snaps:
        date = s["data_referencia"]
        dates.append(date)
        enr = _enrich(s["debentures"])
        papers = []
        for p in enr:
            sp = p.get("spread_pp")
            dur = p.get("duration_anos")
            taxa = p.get("taxa_indicativa")
            if sp is None or dur is None or taxa is None:
                continue
            pp = p.get("pct_pu_par")
            papers.append({
                "c": p["codigo"],
                "e": p.get("emissor_clean"),
                "s": p.get("setor"),
                "cob": p.get("cobertura"),
                "i": p.get("indexador_grupo"),
                "d": dur,
                "t": taxa,
                "sp": round(sp, 4),
                "pp": round(pp, 4) if pp is not None else None,
            })
            indexadores_set.add(p.get("indexador_grupo"))
        _write_json(
            dispersion_dir / f"{date}.json",
            {"date": date, "papers": papers},
        )
    return {
        "dates": dates,
        "sectors": SECTORS,
        "indexadores": [g for g in _indexador_order() if g in indexadores_set],
    }


# ----------------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------------

def write_html(out_path: Path) -> int:
    html = (ROOT / "index.template.html").read_text(encoding="utf-8")
    out_path.write_text(html, encoding="utf-8")
    return len(html.encode("utf-8"))


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Gera dashboard estático ANBIMA debêntures.")
    p.add_argument("--out-html", default="index.html")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    snaps = _load_history()
    if not snaps:
        print("[err] history/ vazio — rode fetch_anbima.py + compute_spreads.py antes", file=sys.stderr)
        return 1

    DATA_DIR.mkdir(exist_ok=True)
    disp_dir = DATA_DIR / "dispersion"
    disp_dir.mkdir(exist_ok=True)

    today = snaps[-1]
    enriched_today = _enrich(today["debentures"])

    sizes: list[tuple[str, int]] = []

    sizes.append(("manifest.json",
        _write_json(DATA_DIR / "manifest.json", build_manifest(snaps))))
    sizes.append(("overview.json",
        _write_json(DATA_DIR / "overview.json", build_overview(snaps, enriched_today))))
    sizes.append(("curves_history.json",
        _write_json(DATA_DIR / "curves_history.json", build_curves_history(snaps))))
    sizes.append(("heatmap_history.json",
        _write_json(DATA_DIR / "heatmap_history.json", build_heatmap_history(snaps))))
    sizes.append(("movements.json",
        _write_json(DATA_DIR / "movements.json", build_movements(snaps, enriched_today))))

    disp_index = build_dispersion(snaps, disp_dir)
    sizes.append(("dispersion/_index.json",
        _write_json(disp_dir / "_index.json", disp_index)))

    # Sample one dispersion file to report a representative size
    for date in disp_index["dates"]:
        path = disp_dir / f"{date}.json"
        sizes.append((f"dispersion/{date}.json", path.stat().st_size))

    html_size = write_html(Path(args.out_html))
    sizes.append((args.out_html, html_size))

    max_bytes = 0
    print("\n[build] arquivos gerados:", file=sys.stderr)
    for name, n in sizes:
        kb = n / 1024
        flag = " ⚠️ >5MB" if n > 5 * 1024 * 1024 else ""
        print(f"  {name:40} {kb:>10.1f} KB{flag}", file=sys.stderr)
        max_bytes = max(max_bytes, n)

    if max_bytes > 5 * 1024 * 1024:
        print(f"[warn] algum arquivo excedeu 5MB", file=sys.stderr)
        return 2

    print(f"\n[build] OK — {len(snaps)} snapshot(s), maior arquivo {max_bytes/1024:.1f} KB",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
