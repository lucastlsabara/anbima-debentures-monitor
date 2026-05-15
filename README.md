# anbima-debentures-monitor

Dashboard diário do mercado secundário de debêntures (ANBIMA + B3),
atualizado automaticamente via GitHub Actions. **Dois workflows
complementares**: o **canônico** roda 1 slot por dia útil B3 — às
03h BRT da madrugada seguinte, processando o dia útil anterior —,
espera todos os 5 arquivos do dia ficarem disponíveis e importa
ANBIMA + B3 num só commit (sempre re-baixa os 5 du B3 para pegar
republicações dentro do mesmo dia útil, sem cache check); o **intraday**
roda a cada 30min durante o pregão (10h-19h BRT seg-sex) e atualiza só o
Trade-by-Trade da B3 do dia para que o site reflita os negócios em
tempo quase real. O GitHub Pages re-deploya o site sozinho a cada push:

- [`.github/workflows/anbima_b3_probe.yml`](.github/workflows/anbima_b3_probe.yml)
  — **canônico (Fase 4)**. Cron `0 6 * * 2-6` (06 UTC ter-sáb = 03h BRT
  ter-sáb). 1 slot por dia útil, processa o dia útil anterior. Determina
  dia_alvo (HOJE BRT se hora ≥ 19, senão ONTEM BRT — às 03h cai em ONTEM),
  guard B3 (fds/feriado → exit 0), probe HTTP dos 5 arquivos (ANBIMA
  Debentures + ETTJ + TPF + B3 Trade + B3 ConsolidatedRecords); se todos
  OK: gap-fill ANBIMA + overwrite B3 (sempre fetch full dos 5 du) +
  rebuild dashboard + commit & push. **Não há marker de idempotência
  diária nem cache check B3** — o pipeline completo roda para que
  republicações da B3 dentro do mesmo dia útil (até as 03h BRT) sejam
  capturadas.
- [`.github/workflows/b3_trades_intraday.yml`](.github/workflows/b3_trades_intraday.yml)
  — **refresh intraday do Trade-by-Trade da B3 (Fase 5)**.
  **Complementar** ao canônico, não substitui. Cron `0,30 13-21 * * 1-5`
  + `0 22 * * 1-5` (UTC) = a cada 30min entre 10h-19h BRT seg-sex (19
  disparos/dia útil máx). Também disponível via "Run workflow" no
  GitHub Actions UI (sem inputs — usa HOJE BRT automaticamente). A
  cada slot: guard B3 (fds/feriado → exit 0), fetch SOMENTE
  Trade-by-Trade do dia (`fetch_b3_trades.py <HOJE>`), rebuild
  dashboard e commit & push com prefixo `chore: B3 intraday refresh ...`.
  **Não toca** ANBIMA, ETTJ, TPF, nem ConsolidatedRecords — esses
  ficam no canônico. Concurrency group
  `anbima-b3-pipeline` compartilhado com o canônico evita push
  simultâneo se janelas se sobreporem.
- [`.github/workflows/daily_update.yml`](.github/workflows/daily_update.yml)
  e [`.github/workflows/b3_trades.yml`](.github/workflows/b3_trades.yml)
  — **backup manual** (apenas `workflow_dispatch`, sem cron). Use só
  para reprocessar um dia específico se o probe canônico falhar.

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

## Automação canônica (`anbima_b3_probe.yml`)

Workflow: [`.github/workflows/anbima_b3_probe.yml`](.github/workflows/anbima_b3_probe.yml).

- **Cron**: `0 6 * * 2-6` (UTC) = **1 slot por dia útil B3**: 03h BRT
  (06h UTC) ter-sáb, madrugada após cada dia útil BRT. Cada execução
  processa o dia útil anterior. Day-of-week 2-6 elimina triggers de
  dom/seg UTC, que mapeariam ref dates de fim de semana. Guard B3
  filtra feriados B3 em runtime.
- **Determinação do `target_date`**: HOJE BRT se hora ≥ 19, senão ONTEM
  BRT. Como o slot dispara às 03h BRT (hora=3 < 19), `target_date`
  cai sempre em ONTEM — o último dia útil B3.
- **Idempotência B3: não aplicável.** O pipeline executa
  incondicionalmente. Não há marker `data/.probe_state.json`, não há
  cache check (lastUpdate / pageCount) nos fetchers B3, não há
  verificação de "arquivo local já existe". Os 5 dias úteis B3 da
  janela são sempre re-baixados via `fetch_b3_trades.py` +
  `fetch_b3_trades_consolidated.py`. **Trade-off**: republicações da
  B3 publicadas APÓS as 03h BRT (raro) só são capturadas no próximo
  dia útil, via a janela de gap-fill 5 du; em troca, ficamos com 1
  execução/dia útil (~3-5 min de Actions minutes) no horário em que
  a B3 já publicou as versões finais do dia útil anterior.
- **Idempotência ANBIMA: natural.** `history/<D>.json` é definitivo
  uma vez gravado, então o import ANBIMA pula a data se o arquivo já
  existe (gap-fill).
- **Probe HTTP**: [`scripts/probe_files.py`](scripts/probe_files.py)
  verifica disponibilidade dos 5 arquivos do dia (ANBIMA Debentures +
  ETTJ + TPF + B3 Trade + B3 ConsolidatedRecords). Se todos OK → import.
  Se algum faltando → exit 0 sem commitar (próximo slot tenta de novo).
  **Não é cache check**: é pre-flight de publicação no lado remoto.
- **Import** (só se probe all OK):
  - **ANBIMA** (gap-fill 5 dias úteis B3): para cada data, pula se
    `history/<D>.json` já existe; senão `fetch_anbima.py` + `compute_spreads.py`.
  - **B3** (overwrite janela 5 dias úteis B3): `fetch_b3_trades.py` +
    `fetch_b3_trades_consolidated.py` apagam e reescrevem os 5 arquivos
    mais recentes incondicionalmente, capturando correções retroativas.
  - **Retenção 60 du B3** em `data/b3_trades*/` via `scripts/podar_historico.py`.
  - `build_dashboard.py` regenera `data/*.json` + `index.html`.
  - `git pull --rebase --autostash origin main` + commit + push.
- **Recuperação retroativa**: se o slot do dia D falha (ex.: arquivos
  ainda não publicados pela B3 às 03h BRT), o dia D é descartado e
  recuperado automaticamente no ciclo do próximo dia útil B3 via
  gap-fill ANBIMA (`scripts/list_target_dates.py` retorna
  `[D+1, D, D-1, D-2, D-3]`) e a janela 5du do B3 sobrescreve. Zero
  ação manual.

Tratamento de erro por data:

| Cenário | Comportamento |
|---|---|
| ANBIMA ainda não publicou (`db<YYMMDD>.txt` 404) | probe responde "faltando", workflow encerra verde sem commit; próximo slot tenta de novo |
| ms.txt 404 (títulos públicos) | warn-and-skip; `titpub_status='404'` no snapshot, `titpub_rows=[]` (debêntures + ETTJ continuam) |
| ms.txt com Data Referência interna divergente da target | warn; `titpub_status='data_divergente'`, rows parseadas normalmente |
| HTTP 5xx ou erro de rede | raise (job marca failure → notificação do GitHub) |
| Sábado / domingo / feriado | guard B3 derruba o run; ANBIMA retornaria 404 do mesmo jeito |

### Disparar manualmente (`workflow_dispatch`)

1. Abrir [Actions › ANBIMA + B3 Probe & Import](https://github.com/lucastlsabara/anbima-debentures-monitor/actions/workflows/anbima_b3_probe.yml).
2. Clicar em **Run workflow**.
3. Input opcional:
   - `target_date` (formato `YYYY-MM-DD`): vazio = derivar conforme
     horário BRT atual. Preenchido = força aquele dia. Como cada slot
     já executa o pipeline completo (sem marker de idempotência),
     reprocessar um dia atípico só requer disparar `workflow_dispatch`
     com o `target_date` desejado.

## Automação intraday Trade-by-Trade (`b3_trades_intraday.yml`)

Workflow: [`.github/workflows/b3_trades_intraday.yml`](.github/workflows/b3_trades_intraday.yml).

**Complementar** ao canônico — não substitui. Resolve a lacuna em que o
canônico das 03h BRT fica preso esperando os 5 arquivos (em particular
ConsolidatedRecords, que só sai ao fim do dia) antes de importar
qualquer coisa, deixando o site sem trades intraday até a madrugada.

- **Cron**: duas linhas UTC para cobrir 10h-19h BRT seg-sex.
  - `0,30 13-21 * * 1-5` (UTC) = 18 slots de 10:00 até 18:30 BRT (cada 30min).
  - `0 22 * * 1-5` (UTC) = 1 slot adicional em 19:00 BRT (fim de pregão).
  - Total: 19 disparos/dia útil máximo. Cron em 2 linhas porque GitHub
    Actions não suporta delimitar hora de fim parcial; `0,30 13-22 * * 1-5`
    incluiria 19:30 BRT (fora da janela). Day-of-week 1-5 = seg-sex UTC:
    como a janela toda fica em um mesmo dia da semana BRT/UTC (13h-22h UTC
    = 10h-19h BRT), não há virada de dia.
- **Disparo manual**: "Run workflow" na aba Actions → `b3_trades_intraday.yml`.
  Sem inputs — usa HOJE BRT automaticamente.
- **Steps**: guard B3 em HOJE BRT (fds/feriado → exit 0); roda
  `fetch_b3_trades.py <HOJE>` (modo unitário, **só Trade-by-Trade**);
  detecta mudança em `data/b3_trades/<HOJE>.json` via
  `git status --porcelain` (cobre arquivo novo + modificado); se mudou,
  roda `build_dashboard.py`, faz `git pull --rebase --autostash origin
  main`, `git add data/ index.html` e commit & push com mensagem
  `chore: B3 intraday refresh <YYYY-MM-DD> <HH:MM BRT>`.
- **Restrições invioladas**:
  - Não baixa ANBIMA (`db.txt`/ETTJ/`ms.txt`) nem ConsolidatedRecords.
  - Não poda histórico (`data/b3_trades/` antigos, `data/dispersion/`,
    `history/`) — função do canônico via `scripts/podar_historico.py`.
  - Não gera dados sintéticos. Falha de rede / 5xx → `fetch_b3_trades.py`
    sai não-zero e o workflow falha barulhento (notificação do GitHub).
    403/404 (FDS/feriado/dia ainda não publicado) é pulado em silêncio
    pelo próprio script, preservando o arquivo existente.
- **Concurrency**: grupo `anbima-b3-pipeline`, `cancel-in-progress: false`.
  O mesmo grupo está em `anbima_b3_probe.yml` como safety net — se algum
  dia janelas se sobreporem, evita dois push concorrentes em main.
- **Convivência com canônico**: janelas não se sobrepõem (gap de 2h entre
  19:00 BRT intraday e 21:00 BRT canônico). Em D+1, cada slot do canônico
  sobrescreve o trade-by-trade do dia com a versão atual da B3 (igual ou
  marginalmente maior) e adiciona ConsolidatedRecords + ANBIMA + ETTJ + TPF.
- **Limitação explícita**: o intraday SÓ atualiza
  `data/b3_trades/<HOJE>.json` + rebuild do dashboard. Setor × Prazo,
  Heatmap, Dispersão, Visão Geral e Títulos Públicos continuam refletindo
  o último dia importado pelo canônico até o próximo ciclo dele.
- Commits do intraday usam prefixo `chore: B3 intraday refresh ...` para
  diferenciar do canônico (`feat: ANBIMA + B3 snapshot ...`) no `git log`.

### Workflows backup manuais

[`daily_update.yml`](.github/workflows/daily_update.yml) e
[`b3_trades.yml`](.github/workflows/b3_trades.yml) continuam disponíveis
mas **sem cron** (apenas `workflow_dispatch`). Use só para fallback se o
probe estiver com problema ou para reprocessar dia/range específico fora
da janela do probe.

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
(opcional). Quando omitido, usam **hoje (BRT)**. A pipeline canônica
(`anbima_b3_probe.yml`) roda 1 slot por dia útil B3 às 03h BRT (06h UTC),
processando o dia útil anterior, e só importa quando o probe confirma
que todos os 5 arquivos do dia foram publicados.
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

- **Python 3.11+** (versão fixada em `setup-python` nos workflows)
- Pacotes em [`requirements.txt`](requirements.txt):
  - `numpy`, `scipy`, `pandas` — parsing + álgebra da ETTJ + agregações
  - `requests` — captura HTTP da ANBIMA + B3
  - `holidays` — calendário B3 (feriados nacionais + B3-específicos) para
    a janela de 5 dias úteis usada pelo probe canônico
  - `pytz` — fuso BRT para `today_brt()`

```bash
pip install -r requirements.txt
```

## Convenção

- **252 dias úteis = 1 ano** (convenção ANBIMA da ETTJ)
- **Spread oficial** = lookup exato da Referência NTN-B (sem spline)
- Spread sempre em **pp** (pontos percentuais), Δ sempre em **bps**
- Estagnado = ≥ 5 dias úteis sem mudança em `taxa_indicativa`
