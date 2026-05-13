# anbima-debentures-monitor

Dashboard diário do mercado secundário de debêntures (ANBIMA), atualizado
automaticamente via GitHub Actions. Dois workflows agendados commitam em
`main` todos os dias e o GitHub Pages re-deploya o site sozinho:

- [`.github/workflows/daily_update.yml`](.github/workflows/daily_update.yml)
  — pipeline ANBIMA (debêntures + ETTJ + títulos públicos → spreads →
  dashboard). Cron `0 2 * * *` (02:00 UTC = 23h BRT, **todos os dias**
  inclusive fim de semana e feriado — quando a ANBIMA não publica, o
  script sai 404 e o workflow termina limpo).
- [`.github/workflows/b3_trades.yml`](.github/workflows/b3_trades.yml)
  — pipeline B3 trades (trade-by-trade + consolidated). Cron `0 2 * * 2-6`
  (UTC) = 23h BRT seg-sex. Separado do ANBIMA porque o endpoint
  `arquivos.b3.com.br` exige tratamento próprio.

Acompanhar execuções em
[Actions](https://github.com/lucastlsabara/anbima-debentures-monitor/actions).

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
| `data/titpub_history.json` | série histórica de taxas indicativas de NTN-B/LTN/NTN-F (ms.txt) | ~50 KB |
| `data/b3_trades/<date>.json` | trades B3 trade-by-trade do dia (DEB/CRA/CRI/CFF/COE) | ~1-3 MB/dia |
| `data/b3_trades/manifest.json` | índice de datas + totais (n_trades, vol_brl) | < 5 KB |
| `data/b3_trades_consolidated/<date>.json` | consolidados B3 (pmp, vol total, min/max) por instrumento × dia | ~200-500 KB/dia |
| `data/b3_trades_consolidated/manifest.json` | índice de datas + total_rows | < 2 KB |

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

## Automação ANBIMA (`daily_update.yml`)

Workflow: [`.github/workflows/daily_update.yml`](.github/workflows/daily_update.yml).

- **Cron**: `0 2 * * *` (UTC) = **23h BRT todos os dias** (inclusive
  fim de semana e feriado — quando ANBIMA não publica, sai 404 e o
  workflow termina limpo).
- **Janela de catch-up**: **5 dias úteis B3** mais recentes (HOJE BRT se
  útil + D-1..D-4 úteis), calculada via `python-holidays`. A janela larga
  absorve dias em que a ANBIMA atrasou a publicação ou a execução pulou.
- **Para cada data `X` na janela**:
  ```bash
  python3 fetch_anbima.py     --date X
  python3 compute_spreads.py  --date X
  ```
- **Após processar todas as datas**: roda `python3 build_dashboard.py` e
  commita `data/`, `history/`, `index.html` em `main` (`git pull --rebase
  --autostash origin main` antes do push, pra evitar race com o workflow B3).
- **Re-rodar é idempotente**: `history/<YYYY-MM-DD>.json` é sobrescrito com
  conteúdo determinístico; catch-up sobre snapshots já bons não corrompe nada.

Tratamento de erro por data:

| Cenário | Comportamento |
|---|---|
| ANBIMA ainda não publicou (`db<YYMMDD>.txt` 404) | `fetch_anbima.py` sai com exit 2; o workflow captura e pula a data |
| ms.txt 404 (títulos públicos) | warn-and-skip; `titpub_status='404'` no snapshot, `titpub_rows=[]` (debêntures + ETTJ continuam) |
| ms.txt com Data Referência interna divergente da target | warn; `titpub_status='data_divergente'`, rows parseadas normalmente |
| HTTP 5xx ou erro de rede | raise (job marca failure → notificação do GitHub) |
| Sábado / domingo / feriado | ANBIMA retorna 404; mesmo path do "ainda não publicou" |

### Disparar manualmente (`workflow_dispatch`)

1. Abrir [Actions › daily_update](https://github.com/lucastlsabara/anbima-debentures-monitor/actions/workflows/daily_update.yml).
2. Clicar em **Run workflow**.
3. Input opcional `target_date` (formato `YYYY-MM-DD`):
   - **Vazio** → HOJE BRT + catch-up dos 5 dias úteis B3 (mesmo comportamento do cron).
   - **Data preenchida** → roda fetch + compute apenas naquela data e regenera o dashboard.

Acompanhar runs em
[Actions](https://github.com/lucastlsabara/anbima-debentures-monitor/actions/workflows/daily_update.yml).
Cada run tem logs por step e expõe artifacts (`fetch-debug-*.zip`) para
inspeção do payload bruto da ANBIMA quando algo dá errado.

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

## Aba Trades

Pipeline independente do core ANBIMA. Coleta trades de renda fixa (DEB / CRA /
CRI / CFF / COE) do endpoint público da B3 em **duas granularidades** e exibe
na aba **Trades** do dashboard:

| Fonte | Script | Endpoint B3 | Granularidade |
|---|---|---|---|
| Negócio a negócio | `fetch_b3_trades.py` | `/bdi/table/Trade/...` | 1 linha = 1 operação (qtd, PU, taxa, hora, ISIN, contraparte) |
| Negócios consolidados | `fetch_b3_trades_consolidated.py` | `/bdi/table/{TableName}/...` | 1 linha = 1 instrumento × dia (vol total, preço médio, min/max) |

```bash
# 1. backfill inicial (dias úteis entre 2026-04-24 e hoje, idempotente)
python3 backfill_b3_trades.py
python3 backfill_b3_trades_consolidated.py

# 1a. range customizado
python3 backfill_b3_trades.py              2026-04-24 2026-05-01
python3 backfill_b3_trades_consolidated.py 2026-04-24 2026-05-01

# 2. refresh forçado dos 5 dias úteis B3 mais recentes (delete + write).
#    Cada execução zera e regrava esses 5 dias para capturar correções
#    retroativas (cancelamento de trades, ajuste de PU no Boletim Diário).
#    Histórico antigo (>5 dias úteis) NUNCA é tocado.
python3 fetch_b3_trades.py
python3 fetch_b3_trades_consolidated.py

# 2a. modo unitário (mesmo padrão delete+write, 1 dia)
python3 fetch_b3_trades.py              2026-05-08
python3 fetch_b3_trades_consolidated.py 2026-05-08

# 3. se ajustar a lógica em sectors.py, reaplica a classificação
#    em todos os snapshots já gravados (idempotente, sem rede)
python3 recompute_sectors.py
```

Saídas:

- `data/b3_trades/` — JSON colunar minificado por dia útil (`{YYYY-MM-DD}.json`)
  + `manifest.json` com índice de datas, totais e filenames.
- `data/b3_trades_consolidated/` — mesmo formato (1 JSON por dia útil +
  `manifest.json`), schema fixo da tabela `ConsolidatedRecords` da B3
  (17 colunas snake_case: `data_negocio`, `codigo_if`, `instrumento`,
  `emissor`, `volume_total`, `grupo` etc.).

A aba carrega o manifest no boot, faz fetch lazy dos dias do range escolhido
e cacheia em memória durante a sessão.

Setor das debêntures (`instrument == "DEB"`) é resolvido via
`sectors.classify(ticker, issuer)` no trade-by-trade; para os demais
instrumentos o setor é literalmente `"Outros"`. O consolidated não aplica
enriquecimento setorial nesta fase. Mudanças em `sectors.py` só refletem
nos snapshots existentes após rodar `recompute_sectors.py`.

### Padrão delete + write (refresh forçado)

Cada execução de `fetch_b3_trades.py` / `fetch_b3_trades_consolidated.py`
sem argumentos:

1. Para cada um dos 5 dias úteis B3 da janela (HOJE se útil + D-1..D-4):
   - Chama a API B3 (com retry exponencial 3x).
   - Em caso de sucesso confirmado: **deleta** o arquivo existente em
     `data/b3_trades[_consolidated]/<data>.json` e **escreve** a versão
     nova (atômico via `.tmp` + rename).
   - Em caso de falha de rede/timeout/5xx após retries: **não toca** no
     arquivo existente (preserva versão anterior) e contabiliza falha.
   - 403/404 (FDS/feriado/dia ainda não publicado): pula sem alterar arquivo.
2. Tenta todos os dias antes de retornar.
3. **Exit code != 0 se qualquer dia falhar** (após esgotar retries). 403/404
   não conta como falha.

Garantia: **nunca** geramos dados sintéticos ou fallback. Se a B3 não
responde, o arquivo existente fica intacto. O manifest é atualizado
incrementalmente (apenas entradas dos dias da janela são sobrescritas;
histórico completo é preservado).

## Aba Pesquisa de Trades

Aba comparativa que reúne os tickers selecionados em um único chart de
VWAP (line) + volume R$ por bucket (stacked bar), com cor por **emissor**
(tickers do mesmo emissor compartilham cor). Reusa os trades de
`data/b3_trades/`. Quando todos os tickers ANBIMA de um emissor estão
selecionados, um chip agregador do emissor aparece antes dos chips de
ticker — clicar no X remove todos de uma vez.

Filtros disponíveis:

- **Fonte**: `Trade-by-trade` (`data/b3_trades/`) ou `Consolidated`
  (`data/b3_trades_consolidated/`). O consolidated agrega por instrumento ×
  dia (PMP, volume total, min/max) — útil quando o chart trade-by-trade vira
  ruído. Toggle preserva os tickers selecionados ao trocar.
- **Grupo**: `INTRAGRUPO` vs `-` (não-intragrupo). Filtro disponível apenas
  na fonte consolidated, onde a coluna `grupo` da B3 marca operações
  intragrupo (entre instituições do mesmo conglomerado). Default exibe os
  dois grupos.

**Invariante de datas**: o seletor garante `Data Anterior <= Data Atual`. Se
o usuário escolher data anterior posterior à data atual, o componente
empurra a data atual pra frente automaticamente.

## Automação B3 (`b3_trades.yml`)

Workflow separado de `daily_update.yml` porque o endpoint
`arquivos.b3.com.br` exige tratamento próprio (retry/backoff específicos,
janela seg-sex apenas) e mantemos os dois jobs isolados pra falha de um
não derrubar o outro.

Workflow: [`.github/workflows/b3_trades.yml`](.github/workflows/b3_trades.yml).

- **Cron**: `0 2 * * 2-6` (UTC) = 23h BRT seg-sex, alinhado à janela ANBIMA
- **Modo padrão (schedule)**: roda `fetch_b3_trades.py` +
  `fetch_b3_trades_consolidated.py`, depois `build_dashboard.py`, commit em main
- **Modo manual (workflow_dispatch)**: aceita `mode=fetch_day` ou
  `mode=backfill` com `start_date` / `end_date`. Disparar em
  [Actions › b3_trades](https://github.com/lucastlsabara/anbima-debentures-monitor/actions/workflows/b3_trades.yml)
- **Janela**: 5 dias úteis B3 mais recentes (HOJE se útil + D-1..D-4)
- **Padrão delete + write atômico**: cada execução zera e regrava esses 5
  dias para capturar correções retroativas; histórico antigo nunca é tocado
- **Falha de rede após retries**: preserva arquivo existente (não gera dado
  sintético); exit code != 0 sinaliza a falha
- **Race com `daily_update.yml`**: workflow faz `git pull --rebase --autostash
  origin main` antes do push (ambos commitam em main na mesma janela das 23h BRT)

### Retenção

- `data/b3_trades/*.json` e `data/b3_trades_consolidated/*.json` mantêm
  apenas os **60 dias úteis B3 mais recentes** no `main` (rolling). A poda
  acontece a cada execução do workflow via `scripts/podar_historico.py`,
  após os fetches e antes do build/commit.
- O histórico completo (estado anterior à política) vive na branch
  **`historical-data`**, criada como backup imutável: não recebe builds
  futuros, só retém snapshots brutos. Use-a para análises retroativas
  longas.
- `data/history/`, `data/dispersion/`, `data/overview.json`,
  `data/movements.json` etc. **não são podados** — são leves o suficiente
  para retenção total e dependem de série histórica para gráficos.

## Dependências

- **Python 3.11+** (versão fixada em `setup-python` nos dois workflows)
- Pacotes em [`requirements.txt`](requirements.txt):
  - `numpy`, `scipy`, `pandas` — parsing + álgebra da ETTJ + agregações
  - `requests` — captura HTTP da ANBIMA + B3
  - `holidays` — calendário B3 (feriados nacionais + B3-específicos) para
    a janela de 5 dias úteis usada em ambos os workflows
  - `pytz` — fuso BRT para `today_brt()`

```bash
pip install -r requirements.txt
```

## Convenção

- **252 dias úteis = 1 ano** (convenção ANBIMA da ETTJ)
- **Spread oficial** = lookup exato da Referência NTN-B (sem spline)
- Spread sempre em **pp** (pontos percentuais), Δ sempre em **bps**
- Estagnado = ≥ 5 dias úteis sem mudança em `taxa_indicativa`
