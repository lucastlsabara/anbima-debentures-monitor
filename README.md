# anbima-debentures-monitor

Dashboard diário do mercado secundário de debêntures (ANBIMA), atualizado
automaticamente via Routines do Claude Code.

## Pipeline (3 scripts, sem backend)

```
fetch_anbima.py     → baixa db<YYMMDD>.txt + ETTJ + ms<YYMMDD>.txt
                      (taxas indicativas de NTN-B/LTN/NTN-F por vencimento exato)
                      gera parsed.json
compute_spreads.py  → método oficial ANBIMA: para cada IPCA+ faz lookup
                      EXATO da Referência NTN-B no ms<YYMMDD>.txt;
                      spread = taxa_indicativa − taxa_NTNB_referencia.
                      DI+/%DI/PRE/IGP-M+ ficam sem spread (ANBIMA não publica
                      referência LTN/NTN-F no db.txt). Sem interpolação.
                      Grava data.json + history/<YYYY-MM-DD>.json.
build_dashboard.py  → pré-agrega history/ por indexador em data/*.json,
                      emite index.html (single file, seletor global de
                      indexador no header, hash routing).
```

## Metodologia oficial de spread

`spread = taxa_indicativa_debenture − taxa_NTNB_referência`, onde a NTN-B de
referência vem da coluna **Referência NTN-B** do `db<YYMMDD>.txt` (vencimento
exato, ex.: `15/05/2030`) e sua taxa vem do arquivo oficial:

```
https://www.anbima.com.br/informacoes/merc-sec/arqs/ms<YYMMDD>.txt
```

Não há interpolação. Se a referência divulgada não constar do arquivo do
dia, o papel recebe `spread_metodo = "sem_referencia"` e `spread_pp = null`.
Para Prefixados, ANBIMA não publica referência LTN/NTN-F no `db.txt`, então
caem em `sem_referencia`. DI+ e %DI exibem apenas a taxa publicada
(`spread_metodo = "nao_aplicavel"`), sem cálculo de spread.

`compute_spreads.py` também computa, **apenas como diagnóstico**, o spread
pelo método antigo (cubic spline na ETTJ) e expõe em
`data.diagnostico_metodo` a magnitude da diferença `oficial − legado` —
útil para validar a migração.

## Dashboard

Site estático puro: HTML único + Plotly + Tabulator + Flatpickr via CDN.
Toda a pesada agregação acontece em build time (Python). O frontend só faz
fetch lazy de JSONs prontos por aba.

### Tabs (hash routing)

| Hash | Tab | Conteúdo |
|---|---|---|
| `#/visao` | Visão Geral | 4 KPIs filtrados pelo seletor global + curvas overlay (T, T-1, T-5, T-21, T-63) + spread por indexador + histograma de spreads + top 10 movers |
| `#/curvas` | Curvas Históricas | Comparativo Hoje vs 30d / 90d / customizado + heatmap temporal NTN-B |
| `#/dispersao` | Dispersão | Scatter duration × spread (filtra pelo indexador global), cores por setor, slider de data, toggle "minha cobertura" |
| `#/movimentacoes` | Movimentações | Tabulator com Δ D-1 / D-5 / D-21 (bps), benchmark + vencimento de referência + spread oficial, filtros por setor / cobertura / indexador |
| `#/heatmap` | Heatmap Setor × Duration | Grid setor × bucket-duration, troca o slice por indexador via seletor global, toggle spread atual / Δ7d / Δ30d |

### Pré-agregação (`data/`)

Tudo é gerado por `python3 build_dashboard.py` lendo `history/`:

| Arquivo | Conteúdo | Tamanho típico (1 snapshot) |
|---|---|---|
| `data/manifest.json` | datas disponíveis, setores, buckets | < 1 KB |
| `data/overview.json` | KPIs + curvas overlay + histograma + top movers | ~2-5 KB |
| `data/curves_history.json` | matriz `dates × vértices_du` da ETTJ | ~1 KB · escala linear |
| `data/heatmap_history.json` | grid setor × bucket (atual + Δ7d + Δ30d) | < 1 KB |
| `data/movements.json` | tabela completa do dia + Δ D-1/D-5/D-21 | ~220 KB |
| `data/dispersion/_index.json` | datas com snapshot disponível | < 1 KB |
| `data/dispersion/<date>.json` | papéis IPCA+ daquele dia (codigo, setor, dur, taxa, spread) | ~80 KB/dia |

Garantia de design: cada arquivo individual fica < 5 MB mesmo com 252 dias
de histórico. Dispersion e overview escalam por número de dias (lazy load
por data); curves e heatmap usam matriz compacta.

### Mapeamento setorial

`sectors.py` classifica por (1) substring no nome do emissor, (2) prefixo de
4 letras do código. Default: `Outros`. Setores: Energia Elétrica, Petróleo &
Gás, Saneamento, Saúde, Logística & Concessões, Telecom, Mineração &
Siderurgia, Bancos & Financeiro, Varejo, Imobiliário, Agro & Alimentos,
Industrial, Outros.

Cobertura sell-side (badge azul na tabela, toggle no scatter):
Aegea, BRK, Iguá, Hapvida, Kora, DASA, Viveo, Oncoclínicas, CSN, Prio,
Brava, Origem.

## Como rodar

```bash
pip install -r requirements.txt

# 1. coleta + parse (1 dia)
python3 fetch_anbima.py --date 2026-04-30

# 2. spread + delta D-1 + history
python3 compute_spreads.py

# 3. agregação completa + HTML
python3 build_dashboard.py

# servir local
python3 -m http.server 8000
# abrir http://localhost:8000/index.html
```

## Convenção

- **252 dias úteis = 1 ano** (convenção ANBIMA da ETTJ)
- **Spread oficial** = lookup exato da Referência NTN-B (sem spline)
- Spread sempre em **pp** (pontos percentuais), Δ sempre em **bps**
- Estagnado = ≥ 5 dias úteis sem mudança em `taxa_indicativa`
