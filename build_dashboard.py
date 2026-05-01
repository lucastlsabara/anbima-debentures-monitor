"""
Gera o dashboard HTML a partir de data.json.
Client-side puro: HTML + JS + CSS, sem build step.
Usa Tabulator.js via CDN para tabela ordenável/filtrável.
"""

import sys
import json
import datetime
import zoneinfo
from pathlib import Path

TZ = zoneinfo.ZoneInfo("America/Sao_Paulo")
BASE_DIR = Path(__file__).parent
DATA_JSON = BASE_DIR / "data.json"
INDEX_HTML = BASE_DIR / "index.html"


def main() -> int:
    if not DATA_JSON.exists():
        print("ERRO: data.json não encontrado", file=sys.stderr)
        return 1

    dados = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    debentures = dados.get("debentures", [])
    data_ref = dados.get("data", "")
    timestamp = dados.get("timestamp", "")
    curva_ettj = dados.get("curva_ettj", [])
    total = dados.get("total_papeis", 0)
    baixa_liq = dados.get("papeis_baixa_liquidez", 0)

    # Destaques do dia
    com_delta = [d for d in debentures if d.get("delta_spread_d1_bps") is not None]
    top_abertura = sorted(com_delta, key=lambda d: d.get("delta_spread_d1_bps", 0), reverse=True)[:10]
    top_fechamento = sorted(com_delta, key=lambda d: d.get("delta_spread_d1_bps", 0))[:10]

    # Formatar data
    try:
        dt = datetime.date.fromisoformat(data_ref)
        data_fmt = dt.strftime("%d/%m/%Y")
    except Exception:
        data_fmt = data_ref

    # Curva ETTJ para exibição
    curva_html = ""
    if curva_ettj:
        vertices = " | ".join(
            f"{v['vertice_anos']}a: <strong>{v['taxa']:.2f}%</strong>"
            for v in curva_ettj
            if v["vertice_anos"] in [1, 2, 3, 5, 7, 10, 20, 40]
        )
        curva_html = f'<div class="curva-info">Curva NTN-B: {vertices}</div>'

    dados_js = json.dumps(debentures, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Monitor ANBIMA Debêntures — {data_fmt}</title>
  <link href="https://unpkg.com/tabulator-tables@6.3.0/dist/css/tabulator.min.css" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 13px;
      background: #f5f6fa;
      color: #1a1a2e;
      margin: 0;
      padding: 0;
    }}

    header {{
      background: #1a1a2e;
      color: #fff;
      padding: 16px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 8px;
    }}

    header h1 {{
      margin: 0;
      font-size: 18px;
      font-weight: 600;
    }}

    header .meta {{
      font-size: 12px;
      opacity: 0.75;
    }}

    .container {{
      max-width: 1600px;
      margin: 0 auto;
      padding: 16px 20px;
    }}

    .stats-bar {{
      display: flex;
      gap: 12px;
      margin-bottom: 16px;
      flex-wrap: wrap;
    }}

    .stat-card {{
      background: #fff;
      border-radius: 8px;
      padding: 12px 18px;
      box-shadow: 0 1px 4px rgba(0,0,0,.08);
      min-width: 140px;
    }}

    .stat-card .label {{
      font-size: 11px;
      color: #666;
      text-transform: uppercase;
      letter-spacing: .5px;
    }}

    .stat-card .value {{
      font-size: 22px;
      font-weight: 700;
      color: #1a1a2e;
    }}

    .curva-info {{
      background: #fff;
      border-radius: 8px;
      padding: 10px 18px;
      font-size: 12px;
      color: #444;
      box-shadow: 0 1px 4px rgba(0,0,0,.08);
      margin-bottom: 16px;
    }}

    .destaques {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      margin-bottom: 20px;
    }}

    @media (max-width: 900px) {{
      .destaques {{ grid-template-columns: 1fr; }}
    }}

    .destaque-card {{
      background: #fff;
      border-radius: 8px;
      padding: 14px 18px;
      box-shadow: 0 1px 4px rgba(0,0,0,.08);
    }}

    .destaque-card h3 {{
      margin: 0 0 10px;
      font-size: 13px;
      font-weight: 600;
      color: #444;
      text-transform: uppercase;
      letter-spacing: .5px;
    }}

    .destaque-item {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 5px 0;
      border-bottom: 1px solid #f0f0f0;
      gap: 8px;
    }}

    .destaque-item:last-child {{ border-bottom: none; }}

    .destaque-item .cod {{
      font-weight: 600;
      font-size: 12px;
      min-width: 80px;
    }}

    .destaque-item .emissor {{
      color: #666;
      font-size: 11px;
      flex: 1;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    .delta-pos {{ color: #c0392b; font-weight: 600; }}
    .delta-neg {{ color: #27ae60; font-weight: 600; }}
    .delta-neu {{ color: #888; }}

    .filters {{
      background: #fff;
      border-radius: 8px;
      padding: 12px 18px;
      box-shadow: 0 1px 4px rgba(0,0,0,.08);
      margin-bottom: 12px;
      display: flex;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
    }}

    .filters label {{
      font-size: 12px;
      color: #555;
      font-weight: 500;
    }}

    .filters select, .filters input {{
      font-size: 12px;
      padding: 5px 10px;
      border: 1px solid #ddd;
      border-radius: 5px;
      background: #fff;
      color: #1a1a2e;
    }}

    .btn-clear {{
      font-size: 12px;
      padding: 5px 14px;
      background: #1a1a2e;
      color: #fff;
      border: none;
      border-radius: 5px;
      cursor: pointer;
    }}

    .btn-clear:hover {{ background: #2c2c54; }}

    #tabela-debentures {{
      border-radius: 8px;
      overflow: hidden;
      box-shadow: 0 1px 4px rgba(0,0,0,.08);
    }}

    .tabulator {{ font-size: 12px; border: none; }}
    .tabulator .tabulator-header {{ background: #1a1a2e; color: #fff; }}
    .tabulator .tabulator-header .tabulator-col {{ background: #1a1a2e; color: #fff; border-right-color: #2c2c54; }}
    .tabulator .tabulator-header .tabulator-col:hover {{ background: #2c2c54; }}
    .tabulator-row:nth-child(even) {{ background: #fafafa; }}
    .tabulator-row:hover {{ background: #eef2ff !important; }}
    .tabulator-cell {{ padding: 6px 8px; }}

    .badge {{
      display: inline-block;
      padding: 2px 7px;
      border-radius: 10px;
      font-size: 10px;
      font-weight: 600;
    }}
    .badge-ipca {{ background: #dbeafe; color: #1d4ed8; }}
    .badge-di {{ background: #dcfce7; color: #15803d; }}
    .badge-cdi {{ background: #fef9c3; color: #a16207; }}
    .badge-pre {{ background: #fce7f3; color: #9d174d; }}
    .badge-baixa-liq {{ background: #fee2e2; color: #991b1b; font-size: 9px; }}

    footer {{
      text-align: center;
      font-size: 11px;
      color: #aaa;
      padding: 24px;
    }}
  </style>
</head>
<body>
  <header>
    <h1>Monitor ANBIMA — Mercado Secundário de Debêntures</h1>
    <div class="meta">Referência: {data_fmt} &nbsp;|&nbsp; Atualizado: {timestamp[:16].replace("T", " ")}</div>
  </header>

  <div class="container">
    <div class="stats-bar">
      <div class="stat-card">
        <div class="label">Total de Papéis</div>
        <div class="value" id="stat-total">{total}</div>
      </div>
      <div class="stat-card">
        <div class="label">Com Dado Hoje</div>
        <div class="value" id="stat-hoje">—</div>
      </div>
      <div class="stat-card">
        <div class="label">Baixa Liquidez (&ge;5d)</div>
        <div class="value" id="stat-liq">{baixa_liq}</div>
      </div>
      <div class="stat-card">
        <div class="label">Com Var. Spread D-1</div>
        <div class="value" id="stat-delta">—</div>
      </div>
    </div>

    {curva_html}

    <div class="destaques">
      <div class="destaque-card">
        <h3>&#9650; Top 10 Maiores Aberturas de Spread (D-1, bps)</h3>
        <div id="top-abertura"></div>
      </div>
      <div class="destaque-card">
        <h3>&#9660; Top 10 Maiores Fechamentos de Spread (D-1, bps)</h3>
        <div id="top-fechamento"></div>
      </div>
    </div>

    <div class="filters">
      <label>Indexador:
        <select id="filtro-indexador">
          <option value="">Todos</option>
          <option value="IPCA+">IPCA+</option>
          <option value="DI+">DI+</option>
          <option value="% DI">% DI</option>
          <option value="Pré">Pré</option>
        </select>
      </label>
      <label>Setor:
        <select id="filtro-setor"><option value="">Todos</option></select>
      </label>
      <label>Duration mín (anos):
        <input type="number" id="dur-min" min="0" step="0.5" style="width:70px">
      </label>
      <label>Duration máx (anos):
        <input type="number" id="dur-max" min="0" step="0.5" style="width:70px">
      </label>
      <label>
        <input type="checkbox" id="filtro-liq"> Só baixa liquidez
      </label>
      <button class="btn-clear" onclick="limparFiltros()">Limpar filtros</button>
    </div>

    <div id="tabela-debentures"></div>
  </div>

  <footer>
    Fonte: ANBIMA — Mercado Secundário de Debêntures &nbsp;|&nbsp;
    Spreads IPCA+ calculados sobre curva ETTJ NTN-B por interpolação cubic spline
  </footer>

  <script src="https://unpkg.com/tabulator-tables@6.3.0/dist/js/tabulator.min.js"></script>
  <script>
    const DADOS = {dados_js};

    // Helpers de formatação
    const fmtNum = (v, dec=2) => v == null ? "—" : Number(v).toFixed(dec);
    const fmtBps = (v) => {{
      if (v == null) return "—";
      const s = v >= 0 ? "+" + v.toFixed(1) : v.toFixed(1);
      return s;
    }};

    function corDelta(cell) {{
      const v = cell.getValue();
      if (v == null) return;
      if (v > 0) cell.getElement().classList.add("delta-pos");
      else if (v < 0) cell.getElement().classList.add("delta-neg");
      else cell.getElement().classList.add("delta-neu");
    }}

    function badgeIndexador(cell) {{
      const v = cell.getValue() || "";
      const cls = v.includes("IPCA") ? "badge-ipca"
        : v.includes("DI+") ? "badge-di"
        : v.includes("% DI") || v.includes("%DI") ? "badge-cdi"
        : "badge-pre";
      return `<span class="badge ${{cls}}">${{v}}</span>`;
    }}

    function renderLiquidez(cell) {{
      if (cell.getValue()) return '<span class="badge badge-baixa-liq">Baixa Liq.</span>';
      return "";
    }}

    // Tabela
    const tabela = new Tabulator("#tabela-debentures", {{
      data: DADOS,
      layout: "fitDataStretch",
      pagination: "local",
      paginationSize: 50,
      paginationSizeSelector: [25, 50, 100, 250],
      movableColumns: true,
      initialSort: [{{column: "emissor", dir: "asc"}}],
      columns: [
        {{title: "Código", field: "codigo", width: 100, frozen: true}},
        {{title: "Emissor", field: "emissor", width: 160}},
        {{title: "Setor", field: "setor", width: 120}},
        {{title: "Indexador", field: "indexador", width: 80, formatter: badgeIndexador}},
        {{title: "Vencimento", field: "vencimento", width: 95}},
        {{title: "Taxa (%)", field: "taxa", width: 80, formatter: (c) => fmtNum(c.getValue(), 4), hozAlign: "right"}},
        {{title: "Spread", field: "spread", width: 80, formatter: (c) => fmtNum(c.getValue(), 4), hozAlign: "right"}},
        {{title: "Duration", field: "duration", width: 80, formatter: (c) => fmtNum(c.getValue(), 2), hozAlign: "right"}},
        {{title: "PU", field: "pu", width: 100, formatter: (c) => fmtNum(c.getValue(), 4), hozAlign: "right"}},
        {{title: "ΔTaxa D-1 (bps)", field: "delta_taxa_d1_bps", width: 110, formatter: (c) => {{corDelta(c); return fmtBps(c.getValue());}}, hozAlign: "right"}},
        {{title: "ΔSpread D-1 (bps)", field: "delta_spread_d1_bps", width: 120, formatter: (c) => {{corDelta(c); return fmtBps(c.getValue());}}, hozAlign: "right"}},
        {{title: "ΔSpread 5d (bps)", field: "delta_spread_5d_bps", width: 110, formatter: (c) => {{corDelta(c); return fmtBps(c.getValue());}}, hozAlign: "right"}},
        {{title: "ΔSpread 21d (bps)", field: "delta_spread_21d_bps", width: 115, formatter: (c) => {{corDelta(c); return fmtBps(c.getValue());}}, hozAlign: "right"}},
        {{title: "ΔSpread 63d (bps)", field: "delta_spread_63d_bps", width: 115, formatter: (c) => {{corDelta(c); return fmtBps(c.getValue());}}, hozAlign: "right"}},
        {{title: "Negócios", field: "negocios", width: 80, hozAlign: "right"}},
        {{title: "Liquidez", field: "baixa_liquidez", width: 90, formatter: renderLiquidez, hozAlign: "center"}},
      ],
    }});

    // Estatísticas dinâmicas
    function atualizarStats() {{
      const visiveis = tabela.getData("active");
      document.getElementById("stat-hoje").textContent = visiveis.length;
      document.getElementById("stat-delta").textContent =
        visiveis.filter(d => d.delta_spread_d1_bps != null).length;
      document.getElementById("stat-liq").textContent =
        visiveis.filter(d => d.baixa_liquidez).length;
    }}

    tabela.on("dataFiltered", atualizarStats);
    tabela.on("tableBuilt", () => {{
      atualizarStats();
      renderDestaques();
      popularFiltroSetor();
    }});

    // Destaques
    function renderDestaques() {{
      const comDelta = DADOS.filter(d => d.delta_spread_d1_bps != null);
      const abertura = [...comDelta].sort((a,b) => b.delta_spread_d1_bps - a.delta_spread_d1_bps).slice(0,10);
      const fechamento = [...comDelta].sort((a,b) => a.delta_spread_d1_bps - b.delta_spread_d1_bps).slice(0,10);

      function renderItem(d) {{
        const delta = d.delta_spread_d1_bps;
        const cls = delta > 0 ? "delta-pos" : delta < 0 ? "delta-neg" : "delta-neu";
        return `<div class="destaque-item">
          <span class="cod">${{d.codigo}}</span>
          <span class="emissor">${{d.emissor}}</span>
          <span class="${{cls}}">${{delta >= 0 ? "+" : ""}}${{delta.toFixed(1)}} bps</span>
        </div>`;
      }}

      document.getElementById("top-abertura").innerHTML = abertura.map(renderItem).join("") || "<em>Sem dados</em>";
      document.getElementById("top-fechamento").innerHTML = fechamento.map(renderItem).join("") || "<em>Sem dados</em>";
    }}

    // Filtros
    function popularFiltroSetor() {{
      const setores = [...new Set(DADOS.map(d => d.setor).filter(Boolean))].sort();
      const sel = document.getElementById("filtro-setor");
      setores.forEach(s => {{
        const opt = document.createElement("option");
        opt.value = s; opt.textContent = s;
        sel.appendChild(opt);
      }});
    }}

    function aplicarFiltros() {{
      const filtros = [];
      const idx = document.getElementById("filtro-indexador").value;
      const setor = document.getElementById("filtro-setor").value;
      const durMin = parseFloat(document.getElementById("dur-min").value) || null;
      const durMax = parseFloat(document.getElementById("dur-max").value) || null;
      const soLiq = document.getElementById("filtro-liq").checked;

      if (idx) filtros.push({{field: "indexador", type: "=", value: idx}});
      if (setor) filtros.push({{field: "setor", type: "=", value: setor}});
      if (durMin != null) filtros.push({{field: "duration", type: ">=", value: durMin}});
      if (durMax != null) filtros.push({{field: "duration", type: "<=", value: durMax}});
      if (soLiq) filtros.push({{field: "baixa_liquidez", type: "=", value: true}});

      tabela.setFilter(filtros);
    }}

    function limparFiltros() {{
      document.getElementById("filtro-indexador").value = "";
      document.getElementById("filtro-setor").value = "";
      document.getElementById("dur-min").value = "";
      document.getElementById("dur-max").value = "";
      document.getElementById("filtro-liq").checked = false;
      tabela.clearFilter();
    }}

    ["filtro-indexador","filtro-setor","dur-min","dur-max","filtro-liq"]
      .forEach(id => document.getElementById(id).addEventListener("change", aplicarFiltros));
  </script>
</body>
</html>
"""

    INDEX_HTML.write_text(html, encoding="utf-8")
    print(f"Dashboard gerado: {INDEX_HTML} ({INDEX_HTML.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
