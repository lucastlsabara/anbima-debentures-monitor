"""Calcula spreads IPCA+ sobre a curva NTN-B (ETTJ ANBIMA).

Lê parsed.json + history/<dia anterior>.json e gera data.json + history/<hoje>.json:

  data.json = {
    "data_referencia": "YYYY-MM-DD",
    "data_anterior":   "YYYY-MM-DD" | null,
    "ettj_ipca":       [ {vertice_anos, taxa_ipca}, ...],
    "debentures":      [
       { codigo, emissor, vencimento, indice, taxa_indicativa,
         duration_dias, duration_anos, pu,
         spread_pp,                 # taxa_indicativa - NTN-B(duration)
         taxa_ntnb_interp,          # NTN-B interpolada na duration
         taxa_indicativa_d1, spread_pp_d1, delta_spread_bps, delta_taxa_bps,
         dias_sem_variacao,
         flag_iliquido,             # taxa_indicativa não publicada
         flag_estagnado,            # >= 5 d.u. sem variação de taxa indicativa
       }, ...
    ]
  }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from scipy.interpolate import CubicSpline

ROOT = Path(__file__).parent
HIST_DIR = ROOT / "history"

# 252 dias úteis = 1 ano (convenção ANBIMA para a ETTJ)
DU_POR_ANO = 252.0


def build_curve(ettj_rows: list[dict]) -> CubicSpline:
    """Spline cúbica (not-a-knot) sobre a ETTJ IPCA, x = vértice em dias úteis."""
    xs = np.array([r["vertice_du"] for r in ettj_rows], dtype=float)
    ys = np.array([r["taxa_ipca"] for r in ettj_rows], dtype=float)
    order = np.argsort(xs)
    xs, ys = xs[order], ys[order]
    return CubicSpline(xs, ys, bc_type="not-a-knot", extrapolate=True)


def previous_snapshot(today_iso: str) -> dict | None:
    """Carrega o snapshot mais recente em history/ anterior a today_iso."""
    if not HIST_DIR.exists():
        return None
    candidates = sorted(p for p in HIST_DIR.glob("*.json") if p.stem < today_iso)
    if not candidates:
        return None
    return json.loads(candidates[-1].read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Calcula spreads IPCA+ sobre ETTJ NTN-B.")
    p.add_argument("--in", dest="inp", default="parsed.json")
    p.add_argument("--out", default="data.json")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    raw = json.loads(Path(args.inp).read_text(encoding="utf-8"))
    today = raw["data_referencia"]

    curve = build_curve(raw["ettj_ipca"])

    prev = previous_snapshot(today)
    prev_by_code: dict[str, dict] = {}
    if prev:
        for d in prev.get("debentures", []):
            prev_by_code[d["codigo"]] = d

    out_debs: list[dict] = []
    for d in raw["debentures"]:
        if not d["indice"].startswith("IPCA"):
            continue

        codigo = d["codigo"]
        taxa = d["taxa_indicativa"]
        dur_dias = d["duration_dias"]
        flag_iliquido = taxa is None or dur_dias is None

        spread_pp = None
        ntnb_interp = None
        dur_anos = None
        if not flag_iliquido:
            dur_anos = dur_dias / DU_POR_ANO
            ntnb_interp = float(curve(dur_dias))
            spread_pp = taxa - ntnb_interp

        prev_d = prev_by_code.get(codigo, {})
        taxa_d1 = prev_d.get("taxa_indicativa")
        spread_d1 = prev_d.get("spread_pp")
        dias_estag_prev = prev_d.get("dias_sem_variacao", 0) or 0

        delta_spread_bps = None
        delta_taxa_bps = None
        if spread_pp is not None and spread_d1 is not None:
            delta_spread_bps = round((spread_pp - spread_d1) * 100.0, 2)
        if taxa is not None and taxa_d1 is not None:
            delta_taxa_bps = round((taxa - taxa_d1) * 100.0, 2)

        if taxa is None:
            dias_sem_variacao = dias_estag_prev  # papel ilíquido: mantém contador
        elif taxa_d1 is None:
            dias_sem_variacao = 0
        elif abs(taxa - taxa_d1) < 1e-9:
            dias_sem_variacao = dias_estag_prev + 1
        else:
            dias_sem_variacao = 0

        flag_estagnado = dias_sem_variacao >= 5

        out_debs.append({
            "codigo": codigo,
            "emissor": d["emissor"],
            "vencimento": d["vencimento"],
            "indice": d["indice"],
            "taxa_indicativa": taxa,
            "taxa_compra": d["taxa_compra"],
            "taxa_venda": d["taxa_venda"],
            "desvio_padrao": d["desvio_padrao"],
            "pu": d["pu"],
            "pct_pu_par": d["pct_pu_par"],
            "duration_dias": dur_dias,
            "duration_anos": round(dur_anos, 3) if dur_anos is not None else None,
            "referencia_ntnb": d["referencia_ntnb"],
            "taxa_ntnb_interp": round(ntnb_interp, 4) if ntnb_interp is not None else None,
            "spread_pp": round(spread_pp, 4) if spread_pp is not None else None,
            "taxa_indicativa_d1": taxa_d1,
            "spread_pp_d1": spread_d1,
            "delta_spread_bps": delta_spread_bps,
            "delta_taxa_bps": delta_taxa_bps,
            "dias_sem_variacao": dias_sem_variacao,
            "flag_iliquido": flag_iliquido,
            "flag_estagnado": flag_estagnado,
        })

    # ETTJ resumida em anos para o dashboard (vértices "redondos" + os reais).
    ettj_out = []
    for r in sorted(raw["ettj_ipca"], key=lambda x: x["vertice_du"]):
        ettj_out.append({
            "vertice_du": r["vertice_du"],
            "vertice_anos": round(r["vertice_du"] / DU_POR_ANO, 3),
            "taxa_ipca": r["taxa_ipca"],
        })

    out = {
        "data_referencia": today,
        "data_anterior": prev["data_referencia"] if prev else None,
        "ettj_data_publicada": raw.get("ettj_data_publicada"),
        "ettj_ipca": ettj_out,
        "debentures": out_debs,
    }

    Path(args.out).write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    HIST_DIR.mkdir(exist_ok=True)
    (HIST_DIR / f"{today}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    n_iliq = sum(1 for d in out_debs if d["flag_iliquido"])
    n_estag = sum(1 for d in out_debs if d["flag_estagnado"])
    print(
        f"[spread] {today}: {len(out_debs)} IPCA+ | ilíquidos={n_iliq} | "
        f"estagnados (>=5 d.u.)={n_estag} | data anterior={out['data_anterior']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
