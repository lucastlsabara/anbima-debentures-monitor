# anbima-debentures-monitor

Dashboard diário do mercado secundário de debêntures (ANBIMA), atualizado
automaticamente via Routines do Claude Code.

## Pipeline (3 scripts, sem backend)

```
fetch_anbima.py     → baixa db<YYMMDD>.txt + ETTJ + ms<YYMMDD>.txt
                      (taxas indicativas de NTN-B/LTN/NTN-F por vencimento exato)
                      gera parsed.json
compute_spreads.py  → IPCA+: método oficial ANBIMA (lookup EXATO da Referência
                      NTN-B no ms<YYMMDD>.txt). DI+ (= CDI+ na ANBIMA): a taxa
                      indicativa publicada já é o spread aditivo sobre o DI/CDI,
                      então spread_pp = taxa_indicativa (spread_metodo =
                      "indexador_aditivo", benchmark = "CDI"). %DI/PRE/IGP-M+
                      ficam sem spread em pp. Sem interpolação.
                      Grava data.json + history/<YYYY-MM-DD>.json.
build_dashboard.py  → pré-agrega history/ por indexador em data/*.json,
                      emite index.html (single file, seletor global de
                      indexador no header, hash routing).
```

## Metodologia oficial de spread

Fórmula (composição em base 252 d.u., consistente com a capitalização da taxa):

```
spread_pp = ((1 + taxa_indicativa / 100) / (1 + taxa_NTNB_referência / 100) − 1) * 100
```

A NTN-B de referência vem da coluna **Referência NTN-B** do `db<YYMMDD>.txt`
(vencimento exato, ex.: `15/05/2030`) e sua taxa vem do arquivo oficial:

```
https://www.anbima.com.br/informacoes/merc-sec/arqs/ms<YYMMDD>.txt
```

Não há interpolação. Se a referência divulgada não constar do arquivo do
dia, o papel recebe `spread_metodo = "sem_referencia"` e `spread_pp = null`.
Para Prefixados, ANBIMA não publica referência LTN/NTN-F no `db.txt`, então
caem em `sem_referencia`.

Para **DI+** (= CDI+ na ANBIMA — mesmo grupo), a taxa indicativa publicada
já é, por construção do indexador, o spread aditivo sobre o DI/CDI; portanto
`spread_pp = taxa_indicativa`, `spread_metodo = "indexador_aditivo"` e
`benchmark_titulo = "CDI"`. Não há cálculo — é leitura direta. **%DI** continua
sem spread em pp (`nao_aplicavel`), pois o indexador é multiplicativo.

A variação D-1 (`delta_spread_bps`) é subtração simples entre `spread_pp`
de hoje e de D-1 — é variação de spread, não spread em si.

`compute_spreads.py` também computa, **apenas como diagnóstico**, o spread
pelo método antigo (cubic spline na ETTJ, com a mesma fórmula composta)
e expõe em `data.diagnostico_metodo` a magnitude da diferença
`oficial − legado` — isolando o efeito de "lookup vs spline".

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

## Pipeline de coleta (automação externa)

Trigger: **Routine do Claude Code, 23h BRT diariamente**. A configuração da
routine vive fora do repositório (no painel do Claude Code do operador), mas
o que ela executa está versionado abaixo:

Janela de catch-up: **[hoje, D-1, D-2, D-3, D-4]** — cinco datas, hoje + 4
anteriores (em dias corridos, sem pular fim de semana). A janela larga
absorve dias em que a ANBIMA atrasou a publicação ou a execução pulou.

Para cada data `X` na janela, executa:

```bash
python3 fetch_anbima.py     --date X
python3 compute_spreads.py  --date X
```

Após processar todas as datas, regenera o dashboard:

```bash
python3 build_dashboard.py
```

Tratamento de erro por data:

| Cenário | Comportamento |
|---|---|
| ANBIMA ainda não publicou (`db<YYMMDD>.txt` 404) | `fetch_anbima.py` sai com exit 2; routine pula a data |
| ms.txt 404 (títulos públicos) | warn-and-skip; `titpub_status='404'` no snapshot, `titpub_rows=[]` (debêntures + ETTJ continuam) |
| ms.txt com Data Referência interna divergente da target | warn; `titpub_status='data_divergente'`, rows parseadas normalmente |
| HTTP 5xx ou erro de rede | raise (routine acusa falha → retry/alerta) |
| Sábado / domingo / feriado | ANBIMA retorna 404; mesmo path do "ainda não publicou" |

Re-rodar é idempotente: `history/<YYYY-MM-DD>.json` é sobrescrito com o mesmo
conteúdo determinístico, então catch-up de 5 dias sobre snapshots já bons
não corrompe nada (apenas re-grava).

## Como rodar manualmente

```bash
pip install -r requirements.txt

# 1. coleta + parse (1 dia)
python3 fetch_anbima.py                    # default: hoje (BRT)
python3 fetch_anbima.py --date 2026-04-30  # data específica

# 2. spread + delta D-1 + history
python3 compute_spreads.py                 # default: mesmo do fetch
python3 compute_spreads.py --date 2026-04-30

# 3. agregação completa + HTML
python3 build_dashboard.py

# 4. catch-up manual de N dias (ex.: últimos 5 corridos)
for d in 2026-05-03 2026-05-04 2026-05-05 2026-05-06 2026-05-07; do
  python3 fetch_anbima.py    --date "$d" || true   # tolera 404
  python3 compute_spreads.py --date "$d" || true
done
python3 build_dashboard.py

# servir local
python3 -m http.server 8000
# abrir http://localhost:8000/index.html
```

### Default de `--date`

Tanto `fetch_anbima.py` quanto `compute_spreads.py` aceitam `--date YYYY-MM-DD`
(opcional). Quando omitido, usam **hoje (BRT)**. A pipeline é agendada para
rodar às 23h BRT — horário em que a ANBIMA já publicou os arquivos do dia.
Em sábado, domingo ou feriado, hoje continua sendo o próprio dia: a ANBIMA
retorna 404 e o script termina com exit code 2 (comportamento esperado).
O calendário B3 (`b3_calendar.py`) continua disponível via
`last_b3_business_day(today_brt())` para uso explícito quando se quer pular
fim de semana / feriado: dias úteis B3 = seg-sex que não sejam feriado
nacional brasileiro nem feriado B3-específico (Carnaval seg+ter,
Sexta-feira Santa, Corpus Christi, aniversário de SP 25/jan, Consciência
Negra 20/nov).

Re-rodar a pipeline para uma data já capturada é idempotente: o
`history/<YYYY-MM-DD>.json` é sobrescrito com o mesmo conteúdo determinístico.

## Aba Trades B3

Pipeline independente do core ANBIMA. Coleta trades de renda fixa (DEB / CRA /
CRI / CFF / COE) do endpoint público da B3 e exibe na aba **Trades B3** do
dashboard.

```bash
# 1. backfill inicial (dias úteis entre 2026-04-24 e hoje, idempotente)
python3 backfill_b3_trades.py

# 1a. range customizado
python3 backfill_b3_trades.py 2026-04-24 2026-05-01

# 2. fetch incremental de 1 dia (último dia útil B3 por padrão)
python3 fetch_b3_trades.py
python3 fetch_b3_trades.py 2026-05-08

# 3. se ajustar a lógica em sectors.py, reaplica a classificação
#    em todos os snapshots já gravados (idempotente, sem rede)
python3 recompute_sectors.py
```

Saídas em `data/b3_trades/`: um JSON colunar minificado por dia útil
(`{YYYY-MM-DD}.json`) + `manifest.json` com índice de datas, totais e
filenames. A aba carrega o manifest no boot, faz fetch lazy dos dias do
range escolhido e cacheia em memória durante a sessão.

Setor das debêntures (`instrument == "DEB"`) é resolvido via
`sectors.classify(ticker, issuer)`; para os demais instrumentos o setor é
literalmente `"Outros"`. Mudanças em `sectors.py` só refletem nos snapshots
existentes após rodar `recompute_sectors.py`.

## Aba Trades (multi-indexador)

Aba comparativa que separa os tickers selecionados em **CDI** vs **IPCA** e
mostra, para cada grupo, VWAP por bucket (line) e volume R$ por bucket
(stacked bar). Reusa os trades de `data/b3_trades/` e adiciona um segundo
pipeline para resolver o indexador de cada ticker:

```bash
# B3 InstrumentRegistration: cobre o universo completo (DEB/CRA/CRI/...).
# Roda diariamente como parte da routine ANBIMA Debentures Diário.
python3 fetch_b3_instruments.py                    # default: último dia útil
python3 fetch_b3_instruments.py 2026-05-08         # data específica
```

Saída em `data/b3_instruments/{YYYY-MM-DD}.json` (formato colunar
minificado) + `manifest.json`. Idempotente — pula dia já existente. O
mapping `ticker → indexador` é a união de:
1. `history/<data>.json` (ANBIMA, prioritário, cobre debêntures via
   `indexador_grupo`)
2. `data/b3_instruments/<data>.json` (B3, fallback para CRA/CRI/CFF/COE
   via campo `indexer`)

Tickers Pré-fixados / Outros aparecem como chips, mas não são plotados
nos charts CDI/IPCA — o aviso sutil do card identifica quantos foram
ocultados.

## Convenção

- **252 dias úteis = 1 ano** (convenção ANBIMA da ETTJ)
- **Spread oficial** = lookup exato da Referência NTN-B (sem spline)
- Spread sempre em **pp** (pontos percentuais), Δ sempre em **bps**
- Estagnado = ≥ 5 dias úteis sem mudança em `taxa_indicativa`
