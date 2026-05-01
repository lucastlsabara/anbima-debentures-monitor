"""Gera index.html (dashboard estático, client-side puro com Tabulator.js).

Lê data.json e produz um HTML self-contained: cabeçalho com KPIs do dia,
top movers, e tabela completa com filtros por emissor, índice e duration.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent

HTML = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<title>ANBIMA Debêntures — Mercado Secundário ({DATA_BR})</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://unpkg.com/tabulator-tables@6.2.5/dist/css/tabulator_simple.min.css" rel="stylesheet">
<script src="https://unpkg.com/tabulator-tables@6.2.5/dist/js/tabulator.min.js"></script>
<style>
  :root {{
    --fg:#1a1a1a; --muted:#6b7280; --bg:#fafafa; --card:#fff;
    --border:#e5e7eb; --accent:#0b5fff;
    --pos:#047857; --neg:#b91c1c; --warn:#b45309;
  }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         margin:0; padding:24px; background:var(--bg); color:var(--fg); }}
  h1 {{ margin:0 0 4px; font-size:22px; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:18px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
          gap:12px; margin-bottom:18px; }}
  .kpi {{ background:var(--card); border:1px solid var(--border); border-radius:8px;
         padding:12px 14px; }}
  .kpi .label {{ color:var(--muted); font-size:11px; text-transform:uppercase;
                letter-spacing:.04em; }}
  .kpi .val {{ font-size:22px; font-weight:600; margin-top:2px; }}
  .row {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:18px; }}
  @media (max-width:900px) {{ .row {{ grid-template-columns:1fr; }} }}
  .panel {{ background:var(--card); border:1px solid var(--border); border-radius:8px;
           padding:14px; }}
  .panel h2 {{ margin:0 0 8px; font-size:14px; text-transform:uppercase;
              letter-spacing:.04em; color:var(--muted); }}
  table.mini {{ width:100%; border-collapse:collapse; font-size:13px; }}
  table.mini th, table.mini td {{ padding:6px 8px; text-align:left;
        border-bottom:1px solid var(--border); }}
  table.mini th {{ color:var(--muted); font-weight:500; font-size:11px;
        text-transform:uppercase; }}
  .pos {{ color:var(--pos); }}
  .neg {{ color:var(--neg); }}
  .warn {{ color:var(--warn); }}
  .controls {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap;
              margin-bottom:8px; }}
  .controls input, .controls select {{
    padding:6px 8px; border:1px solid var(--border); border-radius:6px;
    font-size:13px; }}
  .badge {{ display:inline-block; padding:1px 6px; border-radius:10px;
          font-size:11px; background:#fef3c7; color:#92400e; margin-left:4px; }}
  footer {{ color:var(--muted); font-size:11px; margin-top:18px; text-align:center; }}
  .tabulator {{ font-size:12px; }}
</style>
</head>
<body>

<h1>ANBIMA — Mercado Secundário de Debêntures</h1>
<div class="sub">
  Data de referência: <strong>{DATA_BR}</strong>
  &nbsp;·&nbsp; Comparação D-1: <strong>{DATA_ANT_BR}</strong>
  &nbsp;·&nbsp; Spread = Taxa Indicativa − ETTJ NTN-B (cubic spline) na duration do papel
</div>

<div class="grid">
  <div class="kpi"><div class="label">Papéis IPCA+</div><div class="val">{N_TOTAL}</div></div>
  <div class="kpi"><div class="label">Com taxa publicada</div><div class="val">{N_LIQ}</div></div>
  <div class="kpi"><div class="label">Ilíquidos no dia</div><div class="val">{N_ILIQ}</div></div>
  <div class="kpi"><div class="label">Estagnados ≥5 d.u.</div><div class="val">{N_ESTAG}</div></div>
  <div class="kpi"><div class="label">Spread mediano</div><div class="val">{SPREAD_MED} pp</div></div>
  <div class="kpi"><div class="label">|Δ spread| mediano D-1</div><div class="val">{DELTA_MED} bps</div></div>
</div>

<div class="row">
  <div class="panel">
    <h2>Top 10 — Maior alta de spread (D-1)</h2>
    {TABLE_TOP_UP}
  </div>
  <div class="panel">
    <h2>Top 10 — Maior queda de spread (D-1)</h2>
    {TABLE_TOP_DOWN}
  </div>
</div>

<div class="row">
  <div class="panel">
    <h2>Top 10 — Maiores spreads absolutos</h2>
    {TABLE_HIGH_SPR}
  </div>
  <div class="panel">
    <h2>Top 10 — Spreads mais fechados (ou negativos)</h2>
    {TABLE_LOW_SPR}
  </div>
</div>

<div class="panel" style="margin-bottom:18px;">
  <h2>Curva ETTJ NTN-B (IPCA) — vértices selecionados</h2>
  {TABLE_ETTJ}
</div>

<div class="panel">
  <h2>Tabela completa — Debêntures IPCA+</h2>
  <div class="controls">
    <input id="q" placeholder="Filtrar por código ou emissor…" style="flex:1;min-width:240px;">
    <label>Esconder ilíquidos
      <input type="checkbox" id="hideIliq" checked>
    </label>
    <label>Só estagnados (≥5 d.u.)
      <input type="checkbox" id="onlyEstag">
    </label>
  </div>
  <div id="tbl"></div>
</div>

<footer>
  Fonte: ANBIMA (mercado secundário de debêntures + ETTJ).
  Gerado por <code>fetch_anbima.py</code> + <code>compute_spreads.py</code> + <code>build_dashboard.py</code>.
</footer>

<script>
const DATA = {DATA_JSON};

const fmtPct = v => v == null ? "—" : v.toFixed(2) + "%";
const fmtPp  = v => v == null ? "—" : (v >= 0 ? "+" : "") + v.toFixed(2) + " pp";
const fmtBps = v => v == null ? "—" : (v >= 0 ? "+" : "") + v.toFixed(0) + " bps";
const fmtAnos = v => v == null ? "—" : v.toFixed(2) + "a";

function colorDelta(cell) {{
  const v = cell.getValue();
  if (v == null) return "";
  if (v > 0) cell.getElement().classList.add("neg");
  else if (v < 0) cell.getElement().classList.add("pos");
  return fmtBps(v);
}}

const cols = [
  {{title:"Código", field:"codigo", width:90, frozen:true,
    formatter:c => c.getValue() + (c.getRow().getData().flag_estagnado
      ? '<span class="badge">estag</span>' : '')}},
  {{title:"Emissor", field:"emissor", widthGrow:3,
    formatter:c => c.getValue().replace(/ \\(\\*+\\)/g,'')}},
  {{title:"Vencimento", field:"vencimento", width:110}},
  {{title:"Índice", field:"indice", width:130}},
  {{title:"Duration", field:"duration_anos", width:90, hozAlign:"right",
    formatter:c => fmtAnos(c.getValue())}},
  {{title:"Taxa Ind.", field:"taxa_indicativa", width:90, hozAlign:"right",
    formatter:c => fmtPct(c.getValue())}},
  {{title:"NTN-B interp.", field:"taxa_ntnb_interp", width:110, hozAlign:"right",
    formatter:c => fmtPct(c.getValue())}},
  {{title:"Spread", field:"spread_pp", width:100, hozAlign:"right",
    formatter:c => fmtPp(c.getValue())}},
  {{title:"Δ spread D-1", field:"delta_spread_bps", width:120, hozAlign:"right",
    formatter:colorDelta}},
  {{title:"Δ taxa D-1", field:"delta_taxa_bps", width:110, hozAlign:"right",
    formatter:colorDelta}},
  {{title:"d.u. sem var.", field:"dias_sem_variacao", width:110, hozAlign:"right"}},
  {{title:"PU", field:"pu", width:100, hozAlign:"right",
    formatter:c => c.getValue() == null ? "—" : c.getValue().toFixed(2)}},
];

const table = new Tabulator("#tbl", {{
  data: DATA.debentures,
  columns: cols,
  layout: "fitDataStretch",
  height: "70vh",
  initialSort: [{{column:"spread_pp", dir:"desc"}}],
  pagination: false,
}});

function applyFilter() {{
  const q = document.getElementById("q").value.toLowerCase();
  const hideIliq = document.getElementById("hideIliq").checked;
  const onlyEstag = document.getElementById("onlyEstag").checked;
  table.setFilter(d => {{
    if (hideIliq && d.flag_iliquido) return false;
    if (onlyEstag && !d.flag_estagnado) return false;
    if (q && !(d.codigo.toLowerCase().includes(q)
            || (d.emissor || '').toLowerCase().includes(q))) return false;
    return true;
  }});
}}
document.getElementById("q").addEventListener("input", applyFilter);
document.getElementById("hideIliq").addEventListener("change", applyFilter);
document.getElementById("onlyEstag").addEventListener("change", applyFilter);
applyFilter();
</script>
</body>
</html>
"""


def br_date(iso: str | None) -> str:
    if not iso:
        return "—"
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


def mini_table(rows: list[dict], cols: list[tuple[str, str, str]]) -> str:
    """cols: lista de (header, key, format) onde format ∈ {pct, pp, bps, str, anos}."""
    head = "".join(f"<th>{c[0]}</th>" for c in cols)
    body = []
    for r in rows:
        cells = []
        for hdr, key, fmt in cols:
            v = r.get(key)
            if v is None:
                txt = "—"
                cls = ""
            elif fmt == "pct":
                txt = f"{v:.2f}%"
                cls = ""
            elif fmt == "pp":
                txt = f"{v:+.2f} pp"
                cls = "pos" if v < 0 else "neg"
            elif fmt == "bps":
                txt = f"{v:+.0f} bps"
                cls = "pos" if v < 0 else "neg"
            elif fmt == "anos":
                txt = f"{v:.2f}a"
                cls = ""
            elif fmt == "emissor":
                txt = (v or "").replace(" (*)", "").replace(" (**)", "")
                if len(txt) > 32:
                    txt = txt[:31] + "…"
                cls = ""
            else:
                txt = str(v)
                cls = ""
            cells.append(f'<td class="{cls}">{txt}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f'<table class="mini"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Gera index.html a partir de data.json.")
    p.add_argument("--in", dest="inp", default="data.json")
    p.add_argument("--out", default="index.html")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    d = json.loads(Path(args.inp).read_text(encoding="utf-8"))
    debs = d["debentures"]
    liq = [x for x in debs if not x["flag_iliquido"]]
    iliq = [x for x in debs if x["flag_iliquido"]]
    estag = [x for x in debs if x["flag_estagnado"]]

    spreads = sorted(x["spread_pp"] for x in liq if x["spread_pp"] is not None)
    spread_med = spreads[len(spreads) // 2] if spreads else None
    deltas = sorted(abs(x["delta_spread_bps"]) for x in liq if x["delta_spread_bps"] is not None)
    delta_med = deltas[len(deltas) // 2] if deltas else None

    high_spr = sorted(liq, key=lambda x: x["spread_pp"], reverse=True)[:10]
    low_spr = sorted(liq, key=lambda x: x["spread_pp"])[:10]

    has_d1 = [x for x in liq if x["delta_spread_bps"] is not None]
    top_up = sorted(has_d1, key=lambda x: x["delta_spread_bps"], reverse=True)[:10]
    top_dn = sorted(has_d1, key=lambda x: x["delta_spread_bps"])[:10]

    main_cols = [
        ("Código", "codigo", "str"),
        ("Emissor", "emissor", "emissor"),
        ("Dur.", "duration_anos", "anos"),
        ("Spread", "spread_pp", "pp"),
        ("Δ spread D-1", "delta_spread_bps", "bps"),
    ]
    spr_cols = [
        ("Código", "codigo", "str"),
        ("Emissor", "emissor", "emissor"),
        ("Dur.", "duration_anos", "anos"),
        ("Taxa Ind.", "taxa_indicativa", "pct"),
        ("Spread", "spread_pp", "pp"),
    ]

    if not has_d1:
        no_d1 = (
            '<p style="color:var(--muted);font-size:12px">'
            "Sem snapshot anterior em <code>history/</code> — variação D-1 indisponível."
            "</p>"
        )
        table_top_up = no_d1
        table_top_dn = no_d1
    else:
        table_top_up = mini_table(top_up, main_cols)
        table_top_dn = mini_table(top_dn, main_cols)

    table_high = mini_table(high_spr, spr_cols)
    table_low = mini_table(low_spr, spr_cols)

    target_anos = [0.5, 1, 2, 3, 5, 7, 10, 15, 20, 25, 30]
    ettj_rows = []
    for ta in target_anos:
        # vértice mais próximo (em anos)
        best = min(d["ettj_ipca"], key=lambda r: abs(r["vertice_anos"] - ta))
        ettj_rows.append({
            "vertice": f"{ta:g}a",
            "vertice_real": f"{best['vertice_anos']:.2f}a ({best['vertice_du']} d.u.)",
            "taxa": best["taxa_ipca"],
        })
    table_ettj = mini_table(ettj_rows, [
        ("Vértice alvo", "vertice", "str"),
        ("Vértice ANBIMA mais próximo", "vertice_real", "str"),
        ("Taxa NTN-B", "taxa", "pct"),
    ])

    html = HTML.format(
        DATA_BR=br_date(d["data_referencia"]),
        DATA_ANT_BR=br_date(d.get("data_anterior")),
        N_TOTAL=len(debs),
        N_LIQ=len(liq),
        N_ILIQ=len(iliq),
        N_ESTAG=len(estag),
        SPREAD_MED=f"{spread_med:+.2f}" if spread_med is not None else "—",
        DELTA_MED=f"{delta_med:.0f}" if delta_med is not None else "—",
        TABLE_TOP_UP=table_top_up,
        TABLE_TOP_DOWN=table_top_dn,
        TABLE_HIGH_SPR=table_high,
        TABLE_LOW_SPR=table_low,
        TABLE_ETTJ=table_ettj,
        DATA_JSON=json.dumps(d, ensure_ascii=False),
    )
    Path(args.out).write_text(html, encoding="utf-8")
    print(f"[dash] -> {args.out} ({len(html):,} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
