"""Pré-agrega snapshots em ``history/`` e gera dashboard estático.

Saídas (todas em ``data/`` para fetch lazy do frontend):
  - manifest.json            : datas disponíveis + indexadores permitidos +
                                lista de setores
  - overview.json            : por-data (KPIs, top movers, histograma) + curva
                                ETTJ por data + spread mediano por indexador
  - curves_history.json      : matriz dates x vértices_du da ETTJ NTN-B
                                + matriz da ETTJ Pré (quando disponível)
  - heatmap_history.json     : por-data, grid setor × bucket-duration
  - movements.json           : tabela completa por data, frontend computa Δ
  - dispersion/_index.json   : datas com snapshot de dispersão disponível
  - dispersion/<date>.json   : papéis (codigo, emissor, setor, dur, taxa, spread)

Também emite ``index.html`` (single file, hash routing, 5 tabs, Plotly +
Tabulator + Flatpickr via CDN).

Filtro global de indexadores: apenas IPCA+, DI+ e Prefixado são exibidos.
A aba Dispersão é restrita ainda mais (apenas IPCA+ e DI+ via
``DISPERSION_INDEXADORES``). Os snapshots em ``history/`` permanecem com
TODOS os indexadores; a remoção é apenas em tempo de exibição (basta editar
as listas para reverter).

REGRA: nunca inventa dados. Tudo vem de ``history/<YYYY-MM-DD>.json`` real.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

from compute_spreads import indexador_group as _indexador_group
from sectors import SECTORS, classify as _classify_setor, clean_emissor

DU_POR_ANO = 252.0

ROOT = Path(__file__).parent
HIST_DIR = ROOT / "history"
DATA_DIR = ROOT / "data"
B3_CONS_DIR = DATA_DIR / "b3_trades_consolidated"

# Indexadores efetivamente exibidos no dashboard. Para reincluir % do DI ou
# IGP-M+ no futuro, basta acrescentá-los aqui — os snapshots em history/
# preservam todos os papéis.
ALLOWED_INDEXADORES = ["IPCA+", "DI+", "Prefixado"]

# Subset usado APENAS na aba Dispersão (Prefixado fica de fora dessa aba).
DISPERSION_INDEXADORES = ["IPCA+", "DI+"]


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
            snap = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[warn ] {p} inválido, pulando", file=sys.stderr)
            continue
        snaps.append(snap)
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


def _allowed(p: dict) -> bool:
    return _indexador_group(p.get("indice")) in ALLOWED_INDEXADORES


def _filter_allowed(papers: list[dict]) -> list[dict]:
    return [p for p in papers if _allowed(p)]


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


def _enrich(papers: list[dict]) -> list[dict]:
    """Anota cada papel com emissor normalizado + grupo de indexador + setor."""
    out = []
    for p in papers:
        e = dict(p)
        emissor_clean = clean_emissor(p.get("emissor"))
        e["emissor_clean"] = emissor_clean
        e["indexador_grupo"] = _indexador_group(p.get("indice"))
        e["setor"] = _classify_setor(p.get("codigo"), p.get("emissor"))
        out.append(e)
    return out


# ----------------------------------------------------------------------------
# manifest
# ----------------------------------------------------------------------------

def build_manifest(snaps: list[dict]) -> dict:
    counts: dict[str, int] = defaultdict(int)
    com_spread: dict[str, int] = defaultdict(int)
    if snaps:
        for d in _filter_allowed(snaps[-1]["debentures"]):
            grp = _indexador_group(d.get("indice"))
            counts[grp] += 1
            if d.get("spread_pp") is not None:
                com_spread[grp] += 1
    indexadores_presentes = [
        {"label": grp, "n": counts[grp], "n_com_spread": com_spread[grp]}
        for grp in ALLOWED_INDEXADORES if counts[grp] > 0
    ]
    return {
        "dates": [s["data_referencia"] for s in snaps],
        "latest": snaps[-1]["data_referencia"] if snaps else None,
        "n_snapshots": len(snaps),
        "indexadores": ALLOWED_INDEXADORES,
        "indexadores_presentes": indexadores_presentes,
        "setores": SECTORS,
        "diagnostico_metodo": (snaps[-1].get("diagnostico_metodo") if snaps else None),
        "buckets_duration": [
            {"label": "0–2a", "min": 0.0, "max": 2.0},
            {"label": "2–5a", "min": 2.0, "max": 5.0},
            {"label": "5–10a", "min": 5.0, "max": 10.0},
            {"label": "10a+", "min": 10.0, "max": 9999.0},
        ],
    }


# ----------------------------------------------------------------------------
# overview — agora indexado por data (item 9)
# ----------------------------------------------------------------------------

def _curve_for(snap: dict, key: str = "ettj_ipca",
               taxa_field: str = "taxa_ipca") -> list[dict]:
    rows = snap.get(key) or []
    return [
        {"a": round(r["vertice_anos"], 3), "t": r[taxa_field]}
        for r in sorted(rows, key=lambda r: r["vertice_du"])
        if r.get(taxa_field) is not None
    ]


def _kpis_for(papers: list[dict], date_atual: str, date_anterior: str | None,
              prev_spread_map: dict[str, float] | None) -> dict:
    liq = [p for p in papers if not p.get("flag_iliquido")]
    iliq = [p for p in papers if p.get("flag_iliquido")]
    estag = [p for p in papers if p.get("flag_estagnado")]
    deltas_bps: list[float] = []
    if prev_spread_map:
        for p in liq:
            sp = p.get("spread_pp")
            old = prev_spread_map.get(p["codigo"])
            if sp is not None and old is not None:
                deltas_bps.append(round((sp - old) * 100.0, 2))
    n_com_spread = sum(1 for p in papers if p.get("spread_pp") is not None)
    termometro_med = _mean(deltas_bps)
    abs_spreads = [p["spread_pp"] for p in liq if p.get("spread_pp") is not None]
    mean_abs_spread = _mean(abs_spreads)
    return {
        "data_referencia": date_atual,
        "data_referencia_br": _br_date(date_atual),
        "data_anterior": date_anterior,
        "data_anterior_br": _br_date(date_anterior),
        "n_total": len(papers),
        "n_liquidos": len(liq),
        "n_iliquidos": len(iliq),
        "pct_iliquidos": round(100.0 * len(iliq) / len(papers), 2) if papers else None,
        "n_estagnados": len(estag),
        "pct_estagnados": round(100.0 * len(estag) / len(papers), 2) if papers else None,
        "n_com_spread": n_com_spread,
        # Item 5: termômetro agora é a média da variação spread em bps.
        "termometro_med_bps": round(termometro_med, 1) if termometro_med is not None else None,
        "n_com_d1": len(deltas_bps),
        # Spread médio absoluto (em pp) — usado no card IPCA+.
        "mean_spread_abs_pp": round(mean_abs_spread, 4) if mean_abs_spread is not None else None,
        "n_spread_abs": len(abs_spreads),
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


def _top_movers(papers: list[dict],
                prev_spread_map: dict[str, float] | None) -> dict:
    rows = []
    for p in papers:
        sp = p.get("spread_pp")
        old = prev_spread_map.get(p["codigo"]) if prev_spread_map else None
        if sp is None or old is None:
            continue
        rows.append({
            "codigo": p["codigo"],
            "emissor": p.get("emissor_clean"),
            "setor": p.get("setor"),
            "duration_anos": p.get("duration_anos"),
            "spread_pp": sp,
            "taxa": p.get("taxa_indicativa"),
            "delta_spread_bps": round((sp - old) * 100.0, 2),
            "indexador": p.get("indexador_grupo"),
        })
    rows.sort(key=lambda r: r["delta_spread_bps"], reverse=True)
    return {
        "abrindo": rows[:10],
        "fechando": rows[-10:][::-1],
        "n_com_d1": len(rows),
    }


def _spread_map(papers: list[dict]) -> dict[str, float]:
    return {p["codigo"]: p["spread_pp"] for p in papers
            if p.get("spread_pp") is not None}


def build_overview(snaps: list[dict]) -> dict:
    """Por data: KPIs/top movers/histograma para Data Atual vs Data Anterior.

    O frontend escolhe o par (Data Atual, Data Anterior) e busca cada lado
    em ``by_date``. Δ é computado no servidor (mais rápido pra UI).
    """
    by_date: dict[str, dict] = {}
    spread_maps_by_date: dict[str, dict[str, float]] = {}
    enriched_by_date: dict[str, list[dict]] = {}
    for s in snaps:
        date = s["data_referencia"]
        enriched = _enrich(_filter_allowed(s["debentures"]))
        enriched_by_date[date] = enriched
        spread_maps_by_date[date] = _spread_map(enriched)
        by_date[date] = {
            "curve_ipca": _curve_for(s, "ettj_ipca", "taxa_ipca"),
            "curve_pre":  _curve_for(s, "ettj_pre", "taxa_pre")
                          if s.get("ettj_pre")
                          else _curve_for(s, "ettj_ipca", "taxa_pref"),
        }

    dates = [s["data_referencia"] for s in snaps]
    # Pré-computa, para cada par (atual, anterior), KPIs + top movers
    # cobrindo TODAS as combinações possíveis (não só o anterior imediato).
    # Histograma depende só de `atual`, então fica fora do dicionário de
    # pares para evitar duplicação. Custo: O(N²) pares mas cada par é leve
    # (4 KPIs + 20 top movers); evita o frontend baixar movements.json
    # (~4 MB) só para recalcular um par diferente do default.
    pairs: dict[str, dict] = {}
    for i, atual in enumerate(dates):
        enr = enriched_by_date[atual]
        # Histograma e by_grp só dependem de `atual`.
        by_grp: dict[str, list[dict]] = defaultdict(list)
        for p in enr:
            by_grp[p["indexador_grupo"]].append(p)
        hist_by_idx: dict[str, dict] = {}
        for grp in ALLOWED_INDEXADORES:
            ps = by_grp.get(grp, [])
            sp = [p["spread_pp"] for p in ps
                  if p.get("spread_pp") is not None and not p.get("flag_iliquido")]
            if sp:
                hist_by_idx[grp] = _histogram_for(sp)
        # by_anterior[anterior] = {kpis, top_movements} para cada anterior
        # estritamente menor que `atual`, mais a chave None (sem anterior).
        by_anterior: dict[str | None, dict] = {}
        anteriores: list[str | None] = [None] + [d for d in dates if d < atual]
        for anterior in anteriores:
            prev_map = spread_maps_by_date.get(anterior) if anterior else None
            kpis_by_idx = {"Todos": _kpis_for(enr, atual, anterior, prev_map)}
            top_by_idx = {"Todos": _top_movers(enr, prev_map)}
            for grp in ALLOWED_INDEXADORES:
                ps = by_grp.get(grp, [])
                if not ps:
                    continue
                kpis_by_idx[grp] = _kpis_for(ps, atual, anterior, prev_map)
                top_by_idx[grp] = _top_movers(ps, prev_map)
            # Chave do dict precisa ser string para serializar em JSON.
            key = anterior if anterior is not None else ""
            by_anterior[key] = {"kpis": kpis_by_idx, "top_movements": top_by_idx}
        anterior_default = dates[i - 1] if i > 0 else None
        pairs[atual] = {
            "anterior": anterior_default,
            "anterior_default": anterior_default,
            "histogram": hist_by_idx,
            "by_anterior": by_anterior,
            # Back-compat: o frontend velho lê pair.kpis / pair.top_movements
            # do par default. Mantido até trocarmos a leitura no JS.
            "kpis": by_anterior[anterior_default if anterior_default else ""]["kpis"],
            "top_movements":
                by_anterior[anterior_default if anterior_default else ""]["top_movements"],
        }

    today = dates[-1] if dates else None
    return {
        "dates": dates,
        "latest": today,
        "by_date": by_date,
        "by_pair": pairs,
        "diagnostico_metodo": snaps[-1].get("diagnostico_metodo") if snaps else None,
        "vg_b3_consolidated": build_vg_b3_consolidated(),
    }


# ----------------------------------------------------------------------------
# vg_b3_consolidated — pre-agregação da seção B3 da Visão Geral
# ----------------------------------------------------------------------------
# Lê data/b3_trades_consolidated/<date>.json (schema fixo da B3
# ConsolidatedRecords, 17 colunas) e pré-computa, por data, os campos usados
# pela Visão Geral: KPIs (Volume Total, Nº Trades), série diária com split
# Intra/Extragrupo (chart stacked) e Top 20 Emissores/Papéis com %
# Extragrupo. Filtra somente DEB. `grupo == "INTRAGRUPO"` é intragrupo;
# qualquer outro valor (`EXTRAGRUPO`, `-`) é extragrupo.

_CONS_COL_TICKER = 2
_CONS_COL_INSTR = 3
_CONS_COL_EMISSOR = 5
_CONS_COL_N_NEG = 13
_CONS_COL_VOLUME = 14
_CONS_COL_GRUPO = 15


def _is_intragrupo(g) -> bool:
    if g is None:
        return False
    return str(g).strip().upper() == "INTRAGRUPO"


def _top_n_aggregate(rows: list, key_idx: int, *, top_n: int = 20,
                     extra_field: str | None = None) -> list[dict]:
    """Agrega `rows` (já filtrado por DEB) por chave e retorna top N por volume.

    Considera apenas linhas com `grupo == "EXTRAGRUPO"`; INTRAGRUPO e sem
    classificação ("-" / vazio) são descartados antes da agregação. O
    parâmetro `extra_field` controla se inclui o `emissor` no dict (top
    papéis), que não faz sentido para top emissores (a chave já é o emissor).
    """
    extra_rows = [
        r for r in rows
        if str(r[_CONS_COL_GRUPO] or "").strip().upper() == "EXTRAGRUPO"
    ]
    agg: dict[str, dict] = {}
    for r in extra_rows:
        key = r[key_idx]
        if not key:
            continue
        cur = agg.get(key)
        if cur is None:
            cur = {
                "_key": key,
                "_emissor": r[_CONS_COL_EMISSOR],
                "volume_total": 0.0,
                "n_trades": 0,
            }
            agg[key] = cur
        cur["volume_total"] += r[_CONS_COL_VOLUME] or 0.0
        n = r[_CONS_COL_N_NEG]
        if isinstance(n, (int, float)):
            cur["n_trades"] += int(n)
    items = sorted(agg.values(), key=lambda d: d["volume_total"], reverse=True)
    out: list[dict] = []
    for d in items[:top_n]:
        row = {
            "volume_total": round(d["volume_total"], 2),
            "n_trades": d["n_trades"],
        }
        if extra_field == "papel":
            row["ticker"] = d["_key"]
            row["emissor"] = d["_emissor"]
        else:
            row["emissor"] = d["_key"]
        out.append(row)
    return out


def build_vg_b3_consolidated() -> dict:
    if not B3_CONS_DIR.exists():
        return {"by_date": {}, "dates": []}
    by_date: dict[str, dict] = {}
    for path in sorted(B3_CONS_DIR.glob("*.json")):
        if path.name == "manifest.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[warn ] {path} inválido, pulando", file=sys.stderr)
            continue
        date = payload.get("date") or path.stem
        rows = payload.get("rows") or []
        deb_rows = [r for r in rows if r[_CONS_COL_INSTR] == "DEB"]
        vol_intra = 0.0
        vol_extra = 0.0
        n_trades = 0
        for r in deb_rows:
            v = r[_CONS_COL_VOLUME] or 0.0
            if _is_intragrupo(r[_CONS_COL_GRUPO]):
                vol_intra += v
            else:
                vol_extra += v
            n = r[_CONS_COL_N_NEG]
            if isinstance(n, (int, float)):
                n_trades += int(n)
        by_date[date] = {
            "volume_total_brl": round(vol_intra + vol_extra, 2),
            "volume_intra_brl": round(vol_intra, 2),
            "volume_extra_brl": round(vol_extra, 2),
            "n_trades": n_trades,
            "n_instrumentos": len(deb_rows),
            "top_emissores": _top_n_aggregate(deb_rows, _CONS_COL_EMISSOR),
            "top_papeis": _top_n_aggregate(deb_rows, _CONS_COL_TICKER,
                                           extra_field="papel"),
        }
    return {"by_date": by_date, "dates": sorted(by_date.keys())}


# ----------------------------------------------------------------------------
# curves history (matriz completa para Tab Curvas Históricas)
# ----------------------------------------------------------------------------

def build_curves_history(snaps: list[dict]) -> dict:
    """Emite matriz IPCA + Pré para overlay temporal."""
    vert_ipca: set[int] = set()
    vert_pre: set[int] = set()
    du_to_anos: dict[int, float] = {}
    for s in snaps:
        for r in s.get("ettj_ipca") or []:
            vert_ipca.add(r["vertice_du"])
            du_to_anos[r["vertice_du"]] = round(r["vertice_anos"], 3)
        for r in s.get("ettj_pre") or []:
            vert_pre.add(r["vertice_du"])
            du_to_anos[r["vertice_du"]] = round(r["vertice_anos"], 3)

    vertices_ipca_du = sorted(vert_ipca)
    vertices_pre_du = sorted(vert_pre or vert_ipca)
    vertices_ipca_anos = [du_to_anos[d] for d in vertices_ipca_du]
    vertices_pre_anos = [du_to_anos[d] for d in vertices_pre_du]

    matrix_ipca: list[list[float | None]] = []
    matrix_pre: list[list[float | None]] = []
    dates: list[str] = []
    for s in snaps:
        dates.append(s["data_referencia"])
        by_du_ipca = {r["vertice_du"]: r["taxa_ipca"] for r in (s.get("ettj_ipca") or [])}
        matrix_ipca.append([
            round(by_du_ipca[v], 4) if v in by_du_ipca and by_du_ipca[v] is not None else None
            for v in vertices_ipca_du
        ])
        # Pré: usa ettj_pre se houver; senão deriva de ettj_ipca.taxa_pref.
        if s.get("ettj_pre"):
            by_du_pre = {r["vertice_du"]: r["taxa_pre"] for r in s["ettj_pre"]}
        else:
            by_du_pre = {r["vertice_du"]: r.get("taxa_pref")
                         for r in (s.get("ettj_ipca") or [])
                         if r.get("taxa_pref") is not None}
        matrix_pre.append([
            round(by_du_pre[v], 4) if v in by_du_pre and by_du_pre[v] is not None else None
            for v in vertices_pre_du
        ])

    return {
        "vertices_du": vertices_ipca_du,         # back-compat
        "vertices_anos": vertices_ipca_anos,     # back-compat
        "dates": dates,
        "matrix": matrix_ipca,                   # back-compat (IPCA)
        "matrix_ipca": matrix_ipca,
        "matrix_pre": matrix_pre,
        "vertices_pre_du": vertices_pre_du,
        "vertices_pre_anos": vertices_pre_anos,
    }


# ----------------------------------------------------------------------------
# heatmap setor x bucket duration — agora por data (item 9)
# ----------------------------------------------------------------------------

_BUCKETS: list[tuple[str, float, float]] = [
    ("0–2a", 0.0, 2.0),
    ("2–5a", 2.0, 5.0),
    ("5–10a", 5.0, 10.0),
    ("10a+", 10.0, 9999.0),
]


def build_heatmap_history(snaps: list[dict]) -> dict:
    """Emite papéis raw (setor, indexador, duration, spread) por data.

    Frontend bucketiza on-the-fly conforme indexador selecionado:
      - IPCA+ ou Ambos: buckets ano-a-ano até 10a + 10a+
      - DI+ apenas:     buckets ano-a-ano até 5a + 5a+
    """
    by_date: dict[str, dict] = {}
    for s in snaps:
        enr = _enrich(_filter_allowed(s["debentures"]))
        rows = []
        for d in enr:
            sp = d.get("spread_pp")
            dur = d.get("duration_anos")
            if sp is None or dur is None or d.get("flag_iliquido"):
                continue
            grp = d.get("indexador_grupo")
            if grp not in ("IPCA+", "DI+"):
                continue
            rows.append({
                "s": d.get("setor") or "Outros",
                "i": grp,
                "d": dur,
                "sp": round(sp, 4),
                "c": d.get("codigo"),
                "e": d.get("emissor"),
            })
        by_date[s["data_referencia"]] = {"papers": rows}
    return {
        "setores": SECTORS,
        "buckets": [b[0] for b in _BUCKETS],  # legado, ainda emitido p/ back-compat
        "dates": [s["data_referencia"] for s in snaps],
        "by_date": by_date,
    }


# ----------------------------------------------------------------------------
# movements (Tab 4): tabela completa do dia + Δ vs qualquer data
# ----------------------------------------------------------------------------

def build_movements(snaps: list[dict]) -> dict:
    def _rows_for(enriched: list[dict]) -> list[dict]:
        out = []
        for p in enriched:
            out.append({
                "codigo": p["codigo"],
                "emissor": p.get("emissor_clean"),
                "setor": p.get("setor"),
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
    for s in snaps:
        date = s["data_referencia"]
        enr = _enrich(_filter_allowed(s["debentures"]))
        by_date[date] = {"rows": _rows_for(enr)}
    dates = [s["data_referencia"] for s in snaps]
    return {
        "dates": dates,
        "latest": dates[-1] if dates else None,
        "by_date": by_date,
    }


# ----------------------------------------------------------------------------
# dispersion (Tab 3): scatter por data, lazy load
# ----------------------------------------------------------------------------

def build_dispersion(snaps: list[dict], dispersion_dir: Path) -> dict:
    dates = []
    indexadores_set: set[str] = set()
    setores_set: set[str] = set()
    for s in snaps:
        date = s["data_referencia"]
        dates.append(date)
        enr = _enrich(_filter_allowed(s["debentures"]))
        papers = []
        for p in enr:
            grp = p.get("indexador_grupo")
            # Aba Dispersão: somente IPCA+ e DI+ (Prefixado fica fora).
            if grp not in DISPERSION_INDEXADORES:
                continue
            sp = p.get("spread_pp")
            dur = p.get("duration_anos")
            taxa = p.get("taxa_indicativa")
            if sp is None or dur is None or taxa is None:
                continue
            pp = p.get("pct_pu_par")
            papers.append({
                "c": p["codigo"],
                "e": p.get("emissor_clean"),
                "i": grp,
                "s": p.get("setor"),
                "d": dur,
                "t": taxa,
                "sp": round(sp, 4),
                "pp": round(pp, 4) if pp is not None else None,
            })
            indexadores_set.add(grp)
            if p.get("setor"):
                setores_set.add(p["setor"])
        _write_json(
            dispersion_dir / f"{date}.json",
            {"date": date, "papers": papers},
        )
    return {
        "dates": dates,
        "indexadores": [g for g in DISPERSION_INDEXADORES if g in indexadores_set],
        "setores": [s for s in SECTORS if s in setores_set],
    }


# ----------------------------------------------------------------------------
# titulos públicos history (Tab 6: Títulos Públicos)
# ----------------------------------------------------------------------------

# Ordem de exibição preferencial dos tipos. Tipos não listados aqui aparecem
# depois, em ordem alfabética.
_TITPUB_TIPOS_ORDEM = ["LTN", "NTN-F", "NTN-B", "NTN-C", "LFT"]


def build_titpub_history(snaps: list[dict]) -> dict:
    """Agrega rows de mercado secundário de títulos públicos por data.

    Saída:
      {
        'datas': [...],                  # apenas datas com titpub_rows não-vazio
        'tipos_disponiveis': [...],      # união ao longo de todas as datas
        'rows_by_date': {date: [rows...]}
      }

    Snapshots gerados antes deste campo simplesmente não aparecem em 'datas';
    o frontend exibe mensagem 'indisponível' para essas datas (no flatpickr,
    apenas datas em 'datas' ficam habilitadas).
    """
    datas: list[str] = []
    rows_by_date: dict[str, list[dict]] = {}
    tipos_set: set[str] = set()
    for s in snaps:
        rows_raw = s.get("titpub_rows") or []
        if not rows_raw:
            continue
        date = s["data_referencia"]
        clean: list[dict] = []
        for r in rows_raw:
            tipo = r.get("tipo")
            if not tipo:
                continue
            tipos_set.add(tipo)
            clean.append({
                "tipo": tipo,
                "vencimento": r.get("vencimento"),
                "cod_selic": r.get("cod_selic"),
                "taxa_compra": r.get("taxa_compra"),
                "taxa_venda": r.get("taxa_venda"),
                "taxa_indicativa": r.get("taxa_indicativa"),
                "pu": r.get("pu"),
                "desvio_padrao": r.get("desvio_padrao"),
                "intervalo_min": r.get("intervalo_min"),
                "intervalo_max": r.get("intervalo_max"),
                "duration_dias": r.get("duration_dias"),
                "duration_anos": r.get("duration_anos"),
            })
        datas.append(date)
        rows_by_date[date] = clean
    tipos = [t for t in _TITPUB_TIPOS_ORDEM if t in tipos_set]
    tipos += sorted(t for t in tipos_set if t not in _TITPUB_TIPOS_ORDEM)
    return {
        "datas": datas,
        "tipos_disponiveis": tipos,
        "rows_by_date": rows_by_date,
    }


# ----------------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------------

def _build_version(data_dir: Path) -> str:
    """Hash curto do conteúdo dos JSONs gerados em data/.

    Usado como query string ?v= em todos os fetch() do dashboard para
    invalidar o cache do GitHub Pages quando os dados mudam. Se o build
    rodar com o mesmo input (mesmos snapshots em history/), o hash é
    idêntico — preserva idempotência. Quando snapshots novos chegam, o
    hash muda e os browsers re-baixam os JSONs.
    """
    h = hashlib.sha256()
    for p in sorted(data_dir.rglob("*.json")):
        h.update(p.relative_to(data_dir).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:12]


def write_html(out_path: Path, build_version: str) -> int:
    html = (ROOT / "index.template.html").read_text(encoding="utf-8")
    # Substitui placeholder; se o template já estiver com a versão expandida
    # (artefato do último build), normaliza primeiro para o placeholder.
    html = re.sub(
        r'const BUILD_VERSION = "[^"]*";',
        f'const BUILD_VERSION = "{build_version}";',
        html,
        count=1,
    )
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

    sizes: list[tuple[str, int]] = []

    sizes.append(("manifest.json",
        _write_json(DATA_DIR / "manifest.json", build_manifest(snaps))))
    sizes.append(("overview.json",
        _write_json(DATA_DIR / "overview.json", build_overview(snaps))))
    sizes.append(("curves_history.json",
        _write_json(DATA_DIR / "curves_history.json", build_curves_history(snaps))))
    sizes.append(("heatmap_history.json",
        _write_json(DATA_DIR / "heatmap_history.json", build_heatmap_history(snaps))))
    sizes.append(("movements.json",
        _write_json(DATA_DIR / "movements.json", build_movements(snaps))))
    sizes.append(("titpub_history.json",
        _write_json(DATA_DIR / "titpub_history.json", build_titpub_history(snaps))))

    disp_index = build_dispersion(snaps, disp_dir)
    sizes.append(("dispersion/_index.json",
        _write_json(disp_dir / "_index.json", disp_index)))

    for date in disp_index["dates"]:
        path = disp_dir / f"{date}.json"
        sizes.append((f"dispersion/{date}.json", path.stat().st_size))

    build_version = _build_version(DATA_DIR)
    html_size = write_html(Path(args.out_html), build_version)
    sizes.append((args.out_html, html_size))
    print(f"[build] BUILD_VERSION={build_version}", file=sys.stderr)

    max_bytes = 0
    size_warn_threshold = 10 * 1024 * 1024
    print("\n[build] arquivos gerados:", file=sys.stderr)
    for name, n in sizes:
        kb = n / 1024
        flag = " ⚠️ >10MB" if n > size_warn_threshold else ""
        print(f"  {name:40} {kb:>10.1f} KB{flag}", file=sys.stderr)
        max_bytes = max(max_bytes, n)

    if max_bytes > size_warn_threshold:
        print(f"[warn] algum arquivo excedeu 10MB", file=sys.stderr)

    print(f"\n[build] OK — {len(snaps)} snapshot(s), maior arquivo {max_bytes/1024:.1f} KB",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
