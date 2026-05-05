"""Calcula spreads pelo método oficial ANBIMA (lookup exato por referência).

Lê parsed.json + history/<dia anterior>.json e gera data.json + history/<hoje>.json.

Metodologia:
  IPCA+      → lookup `referencia_ntnb` em `titulos_publicos["NTN-B"]` (vencimento
               exato). Spread composto:
                 spread_pp = ((1 + taxa/100) / (1 + taxa_NTNB_ref/100) − 1) * 100
               (pp em base 252 d.u., consistente com a composição da taxa).
  DI+ (=CDI+)→ a taxa indicativa publicada pela ANBIMA já é o spread aditivo
               sobre o DI/CDI. spread_pp = taxa_indicativa, benchmark = "CDI",
               spread_metodo = "indexador_aditivo".
  Prefixado  → ANBIMA não publica referência LTN/NTN-F na coluna do db.txt para
               prefixados. spread = null (spread_metodo = "sem_referencia").
  %DI        → spread multiplicativo (não em pp); spread_pp = null
               (spread_metodo = "nao_aplicavel").
  IGP-M+     → sem fonte oficial de benchmark; nao_aplicavel.
  Outros     → idem.

Sem interpolação, sem spline, sem síntese de referência. A variação D-1 do
spread (delta_spread_bps) continua como subtração simples — é variação de
spread, não spread em si.

Para diagnóstico de migração, computa também `spread_pp_legado` para IPCA+ via
o método antigo (cubic spline na ETTJ NTN-B). NÃO é usado pelo dashboard;
serve para a comparação metodológica reportada.

Schema de saída (`data.json`):
  {
    "data_referencia": "YYYY-MM-DD",
    "data_anterior":   "YYYY-MM-DD" | null,
    "ettj_ipca":       [...],   # mantido para o tab Curvas
    "ettj_pref":       [...],
    "diagnostico_metodo": {
      "IPCA+": { "n": 665, "com_referencia": 631, "sem_referencia": 34,
                 "diff_legado_bps": { "n_gt_5": .., "media_abs": .., "max_abs": .. } },
      ...
    },
    "debentures": [
       { codigo, emissor, vencimento, indice, indexador_grupo,
         taxa_indicativa, duration_dias, duration_anos, pu,
         benchmark_titulo,             # "NTN-B"/"LTN"/"NTN-F"/None
         benchmark_vencimento,         # ISO ou None (= referencia publicada)
         taxa_benchmark,               # taxa do título público de referência (lookup exato)
         spread_pp,                    # taxa - taxa_benchmark, ou None
         spread_metodo,                # "referencia" | "indexador_aditivo" | "sem_referencia" | "nao_aplicavel"
         spread_pp_legado,             # apenas IPCA+: spline interpolada (diagnóstico)
         taxa_indicativa_d1, spread_pp_d1, delta_spread_bps, delta_taxa_bps,
         dias_sem_variacao,
         flag_iliquido, flag_estagnado,
       }, ...
    ]
  }
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.interpolate import CubicSpline

from b3_calendar import resolve_default_date

ROOT = Path(__file__).parent
HIST_DIR = ROOT / "history"

DU_POR_ANO = 252.0


def indexador_group(indice: str | None) -> str:
    """Reduz o `indice` ANBIMA a um dos grupos canônicos do dashboard."""
    if not indice:
        return "Outros"
    s = indice.strip().upper()
    if s.startswith("IPCA"):
        return "IPCA+"
    if s.startswith("IGP"):
        return "IGP-M+"
    if "% DO DI" in s or s.startswith("PERCENTUAL"):
        return "%DI"
    if s.startswith("DI "):
        return "DI+"
    if s.startswith("PRE") or s.startswith("PRÉ"):
        return "Prefixado"
    return "Outros"


def build_spline(rows: list[dict], key: str) -> CubicSpline | None:
    pairs = [(r["vertice_du"], r[key]) for r in rows
             if r.get(key) is not None and r.get("vertice_du") is not None]
    if len(pairs) < 4:
        return None
    pairs.sort()
    xs = np.array([p[0] for p in pairs], dtype=float)
    ys = np.array([p[1] for p in pairs], dtype=float)
    return CubicSpline(xs, ys, bc_type="not-a-knot", extrapolate=True)


def previous_snapshot(today_iso: str) -> dict | None:
    if not HIST_DIR.exists():
        return None
    candidates = sorted(p for p in HIST_DIR.glob("*.json") if p.stem < today_iso)
    if not candidates:
        return None
    return json.loads(candidates[-1].read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Calcula spreads ANBIMA por lookup exato.")
    p.add_argument("--in", dest="inp", default="parsed.json")
    p.add_argument("--out", default="data.json")
    p.add_argument(
        "--date",
        default=None,
        help="Data de referência YYYY-MM-DD. Default: último dia útil B3 anterior a hoje (BRT). "
             "Se informada, deve coincidir com data_referencia do parsed.json.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    raw = json.loads(Path(args.inp).read_text(encoding="utf-8"))
    today = raw["data_referencia"]

    if args.date:
        ref = datetime.strptime(args.date, "%Y-%m-%d").date().isoformat()
    else:
        ref = resolve_default_date().isoformat()
        print(f"[spread] --date não informado; usando default {ref}", file=sys.stderr)
    if ref != today:
        print(
            f"[spread] AVISO: --date {ref} difere de data_referencia do parsed.json ({today}); "
            f"usando {today} (parsed.json é a fonte autoritativa).",
            file=sys.stderr,
        )

    titpub: dict[str, dict[str, float]] = raw.get("titulos_publicos") or {}
    ntnb_by_venc = titpub.get("NTN-B", {})

    # Spline IPCA legado: usado APENAS como diagnóstico para reporte da migração.
    legacy_curve = build_spline(raw.get("ettj_ipca") or [], "taxa_ipca")

    prev = previous_snapshot(today)
    prev_by_code: dict[str, dict] = {}
    if prev:
        for d in prev.get("debentures", []):
            prev_by_code[d["codigo"]] = d

    out_debs: list[dict] = []
    counts_by_grp: dict[str, int] = {}
    com_ref_by_grp: dict[str, int] = {}
    sem_ref_by_grp: dict[str, int] = {}
    diff_bps_ipca: list[float] = []  # |novo - legado| em bps, p/ diagnóstico

    for d in raw["debentures"]:
        codigo = d["codigo"]
        taxa = d["taxa_indicativa"]
        dur_dias = d["duration_dias"]
        flag_iliquido = taxa is None or dur_dias is None
        grupo = indexador_group(d["indice"])
        counts_by_grp[grupo] = counts_by_grp.get(grupo, 0) + 1

        bench_titulo = None
        bench_venc = None
        taxa_benchmark = None
        spread_pp = None
        spread_metodo = "nao_aplicavel"
        spread_legado = None

        if grupo == "IPCA+":
            ref = d.get("referencia_ntnb")
            bench_titulo = "NTN-B"
            bench_venc = ref
            if ref:
                taxa_benchmark = ntnb_by_venc.get(ref)
                if taxa_benchmark is not None and not flag_iliquido:
                    # Spread por composição (252 d.u.): (1+i_deb)/(1+i_ref) − 1.
                    # Taxas vêm em formato percentual; resultado em pp.
                    spread_pp = (
                        (1 + taxa / 100.0) / (1 + taxa_benchmark / 100.0) - 1
                    ) * 100.0
                    spread_metodo = "referencia"
                    com_ref_by_grp[grupo] = com_ref_by_grp.get(grupo, 0) + 1
                else:
                    spread_metodo = "sem_referencia"
                    sem_ref_by_grp[grupo] = sem_ref_by_grp.get(grupo, 0) + 1
            else:
                spread_metodo = "sem_referencia"
                sem_ref_by_grp[grupo] = sem_ref_by_grp.get(grupo, 0) + 1

            # Diagnóstico legacy: mesma fórmula composta, porém com a NTN-B
            # interpolada por spline em vez do lookup oficial. Isola o efeito
            # da troca de método (lookup vs spline), não da aritmética.
            if legacy_curve is not None and not flag_iliquido and dur_dias is not None:
                ref_legado = float(legacy_curve(dur_dias))
                spread_legado = round(
                    ((1 + taxa / 100.0) / (1 + ref_legado / 100.0) - 1) * 100.0, 4,
                )
                if spread_pp is not None:
                    diff_bps_ipca.append(abs((spread_pp - spread_legado) * 100.0))

        elif grupo == "DI+":
            # DI+ / CDI+ (mesmo grupo na ANBIMA): a taxa indicativa publicada já
            # é o spread aditivo sobre o DI/CDI — não há cálculo, é leitura direta.
            bench_titulo = "CDI"
            if not flag_iliquido and taxa is not None:
                spread_pp = taxa
                spread_metodo = "indexador_aditivo"
                com_ref_by_grp[grupo] = com_ref_by_grp.get(grupo, 0) + 1
            else:
                spread_metodo = "sem_referencia"
                sem_ref_by_grp[grupo] = sem_ref_by_grp.get(grupo, 0) + 1

        elif grupo == "Prefixado":
            # ANBIMA não publica referência LTN/NTN-F no db.txt para prefixados.
            spread_metodo = "sem_referencia"
            sem_ref_by_grp[grupo] = sem_ref_by_grp.get(grupo, 0) + 1
        # %DI, IGP-M+, Outros → spread_metodo = "nao_aplicavel" (default)

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
            dias_sem_variacao = dias_estag_prev
        elif taxa_d1 is None:
            dias_sem_variacao = 0
        elif abs(taxa - taxa_d1) < 1e-9:
            dias_sem_variacao = dias_estag_prev + 1
        else:
            dias_sem_variacao = 0

        flag_estagnado = dias_sem_variacao >= 5
        dur_anos = dur_dias / DU_POR_ANO if dur_dias is not None else None

        out_debs.append({
            "codigo": codigo,
            "emissor": d["emissor"],
            "vencimento": d["vencimento"],
            "indice": d["indice"],
            "indexador_grupo": grupo,
            "taxa_indicativa": taxa,
            "taxa_compra": d["taxa_compra"],
            "taxa_venda": d["taxa_venda"],
            "desvio_padrao": d["desvio_padrao"],
            "pu": d["pu"],
            "pct_pu_par": d["pct_pu_par"],
            "duration_dias": dur_dias,
            "duration_anos": round(dur_anos, 3) if dur_anos is not None else None,
            "referencia_ntnb": d["referencia_ntnb"],
            "benchmark_titulo": bench_titulo,
            "benchmark_vencimento": bench_venc,
            "taxa_benchmark": round(taxa_benchmark, 4) if taxa_benchmark is not None else None,
            "spread_pp": round(spread_pp, 4) if spread_pp is not None else None,
            "spread_metodo": spread_metodo,
            "spread_pp_legado": spread_legado,
            "taxa_indicativa_d1": taxa_d1,
            "spread_pp_d1": spread_d1,
            "delta_spread_bps": delta_spread_bps,
            "delta_taxa_bps": delta_taxa_bps,
            "dias_sem_variacao": dias_sem_variacao,
            "flag_iliquido": flag_iliquido,
            "flag_estagnado": flag_estagnado,
        })

    # ETTJ resumida em anos para o dashboard (mantida em data.json).
    ettj_out: list[dict] = []
    ettj_pref_out: list[dict] = []
    for r in sorted(raw.get("ettj_ipca") or [], key=lambda x: x["vertice_du"]):
        anos = round(r["vertice_du"] / DU_POR_ANO, 3)
        ettj_out.append({
            "vertice_du": r["vertice_du"],
            "vertice_anos": anos,
            "taxa_ipca": r["taxa_ipca"],
            "taxa_pref": r.get("taxa_pref"),
            "inflacao_implicita": r.get("inflacao_implicita"),
        })
        if r.get("taxa_pref") is not None:
            ettj_pref_out.append({
                "vertice_du": r["vertice_du"],
                "vertice_anos": anos,
                "taxa_pref": r["taxa_pref"],
            })

    diag: dict[str, dict] = {}
    for grp, n in counts_by_grp.items():
        diag[grp] = {
            "n_papeis": n,
            "com_referencia": com_ref_by_grp.get(grp, 0),
            "sem_referencia": sem_ref_by_grp.get(grp, 0),
            "nao_aplicavel": n - com_ref_by_grp.get(grp, 0) - sem_ref_by_grp.get(grp, 0),
        }
    if diff_bps_ipca:
        diag.setdefault("IPCA+", {})["diff_vs_legado_bps"] = {
            "n": len(diff_bps_ipca),
            "n_gt_5bps": sum(1 for x in diff_bps_ipca if x > 5),
            "media_abs": round(sum(diff_bps_ipca) / len(diff_bps_ipca), 2),
            "max_abs": round(max(diff_bps_ipca), 2),
        }

    out = {
        "data_referencia": today,
        "data_anterior": prev["data_referencia"] if prev else None,
        "ettj_data_publicada": raw.get("ettj_data_publicada"),
        "ettj_ipca": ettj_out,
        "ettj_pref": ettj_pref_out,
        "diagnostico_metodo": diag,
        "debentures": out_debs,
    }

    Path(args.out).write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    HIST_DIR.mkdir(exist_ok=True)
    hist_path = HIST_DIR / f"{today}.json"
    if hist_path.exists():
        print(f"[spread] {hist_path.name} já existe; sobrescrevendo (idempotente)", file=sys.stderr)
    hist_path.write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    n_iliq = sum(1 for d in out_debs if d["flag_iliquido"])
    n_estag = sum(1 for d in out_debs if d["flag_estagnado"])
    n_spread = sum(1 for d in out_debs if d["spread_pp"] is not None)
    grp_summary = ", ".join(f"{g}={n}" for g, n in sorted(counts_by_grp.items()))
    print(
        f"[spread] {today}: {len(out_debs)} papéis ({grp_summary}) | "
        f"com spread (lookup exato)={n_spread} | ilíquidos={n_iliq} | "
        f"estagnados={n_estag} | data anterior={out['data_anterior']}",
        file=sys.stderr,
    )
    if diff_bps_ipca:
        d = diag["IPCA+"]["diff_vs_legado_bps"]
        print(
            f"[spread] diagnóstico IPCA+: vs spline legado em {d['n']} papéis | "
            f"|diff| média={d['media_abs']} bps · máx={d['max_abs']} bps · "
            f">5 bps em {d['n_gt_5bps']} papéis",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
