# Cleanup Audit 2026-05-15

Auditoria geral pós-PR #154. Critério: aplicar apenas mudancas com
0 referencias residuais, reversiveis via `git revert`, sem afetar
comportamento observavel (site GH Pages, fetchers, schema de dados,
workflows funcionais). Tudo o que gerou qualquer duvida ficou
documentado abaixo como `[DOCUMENTADO]` em vez de aplicado.

Branch: `claude/cleanup-geral-2026-05-15`.
Base: `main` em `ee8de28` (post PR #154).

---

## Resumo executivo

| Categoria | Aplicado | Documentado | Revertido |
|---|---|---|---|
| A. Python morto | 3 imports + 0 funcoes | 0 | 0 |
| B. JS/HTML morto | 1 funcao (`trd_fetchDay`) | varias (vide abaixo) | 0 |
| C. Workflows | 0 | varias (limites de seguranca) | 0 |
| D. Arquivos orfaos | 2 docs movidas para archive | 3 | 0 |
| E. Branches remotas | 0 (push --delete retorna 403) | 100 listadas | 0 |
| F. PRs orfaos | 0 | 0 (nenhum open >14 dias) | 0 |
| G. Workflow runs | 0 | comando sugerido | 0 |
| H. Dependencias Python | 0 (out of scope) | inventario | 0 |
| I. CSS/estilos | 0 | 6 classes documentadas | 0 |
| J. Configs | 0 (out of scope) | inventario | 0 |

**Validacoes finais (apos todas as mudancas):**

- `python -c 'import b3_api, fetch_b3_trades, fetch_b3_trades_consolidated,
  fetch_anbima, build_dashboard, compute_spreads, sectors'` -> **OK**
- `python fetch_b3_trades.py --self-test` -> **OK (6/6 cenarios)**
- `python fetch_b3_trades_consolidated.py --self-test` -> **OK (6/6 cenarios)**
- `python -c 'import yaml; yaml.safe_load(...anbima_b3_probe.yml);
  ...b3_trades_intraday.yml'` -> **OK**
- `python build_dashboard.py` -> **OK (14 snapshot(s), index.html 193.1 KB)**

---

## A. Codigo Python morto

### Aplicado

Rodado `ruff check --select F401,F841 --fix` em todos os `.py` do raiz
e de `scripts/`. 3 fixes:

- [APLICADO] `fetch_b3_trades.py:64` — removido `from b3_api import
  CACHE_SCHEMA_VERSION` (importado, nunca usado).
- [APLICADO] `fetch_b3_trades.py:67` — removido `from b3_api import
  extract_ping_indicators` (importado, nunca usado).
- [APLICADO] `fetch_b3_trades_consolidated.py:64` — removido `from b3_api
  import CACHE_SCHEMA_VERSION` (importado, nunca usado).

Nenhuma variavel `F841` (atribuida e nao lida) encontrada.

### Investigado, nada removido

- **Funcoes orfaos**: rodado grep amplo (`<nome>` com word boundary)
  em todos os `.py`/`*.yml`/`*.html`/`*.md`. Todas as funcoes top-level
  definidas em `_http_utils.py`, `b3_api.py`, `b3_calendar.py`,
  `compute_spreads.py`, `build_dashboard.py`, `fetch_anbima.py`,
  `fetch_b3_trades.py`, `fetch_b3_trades_consolidated.py`, `sectors.py`
  tem pelo menos 1 chamada externa ou sao expostas como API publica
  (chamadas via workflows). **0 funcoes orfaos no Python.**
- **Branches `if X:` mortos**: nenhuma constante dead (`if True/False:`)
  obvia encontrada.
- **Constantes nao usadas no topo**: nenhuma encontrada.
- **Codigo comentado (>5 linhas seguidas)**: nenhuma sequencia desse
  porte encontrada.

### Scripts em raiz

- `backfill_b3_trades.py` e `backfill_b3_trades_consolidated.py`:
  **NAO movidos**. Sao referenciados pelo workflow
  `.github/workflows/b3_trades.yml` (`workflow_dispatch` manual, modo
  `backfill`) — linhas 117-124. Tambem aparecem no README.md (`#
  Backfill manual`).
- `recompute_sectors.py`: nao referenciado por nenhum workflow, mas
  documentado no README.md:376 como ferramenta manual quando taxonomia
  de setores muda. **NAO mexido** (utilitario manual, escopo conservador).
- Scripts em `scripts/` (`list_target_dates.py`, `podar_historico.py`,
  `probe_files.py`, `oneshot_backfill_prefixados.py`): todos referenciados
  por `anbima_b3_probe.yml` ou `daily_update.yml` ou doc. **OK.**

---

## B. Codigo JS / HTML morto (`index.template.html`)

### Aplicado

- [APLICADO] `index.template.html:3641` — removida `async function
  trd_fetchDay(entry)` (14 linhas). Grep amplo em todo o repo
  (`grep -rn "trd_fetchDay" --include="*.html" --include="*.py"
  --include="*.js" --include="*.md" --include="*.yml"`) retornou
  apenas a propria definicao em `index.template.html` e em `index.html`
  (que e gerado de template). Funcao era duplicada de `tr2_fetchTradeDay`
  (linha 4207), que continua sendo o consumer real do `TRD_DAY_CACHE`.
  Resto do prefixo `trd_*` (formatadores, helpers de bucket,
  `_trd_buildTradesTable`, etc) **continua sendo usado** pela aba
  Pesquisa de Trades unificada (PR #150) — verificado um a um com
  grep word-boundary.

### Investigado, nada removido

- **Outras funcoes JS**: cada funcao `trd_*`/`tr2_*` declarada em
  `index.template.html` foi verificada via `grep -c "\b<fn>\b"`.
  Todas com count >= 2 (definicao + pelo menos 1 chamada). `trd_fetchDay`
  foi a unica com count = 1.
- **Hash routes orfaos**: nao foi identificado nenhum `window.location.hash`
  apontando para handler removido.
- **IDs HTML referenciados sem elemento**: nao auditado item-a-item
  (escopo conservador — falso positivo aqui pode quebrar UI). Documentado
  como possivel acao futura.
- **Bibliotecas `<script src=...>` orfaos**: somente `plotly` e
  `tabulator` sao carregados. Ambos usados. **OK.**

---

## C. Workflows GitHub Actions

### Inventario

| Arquivo | Status | Trigger |
|---|---|---|
| `.github/workflows/anbima_b3_probe.yml` | **ATIVO** | `schedule` (3 slots noturnos `0,3,6 UTC * * 2-6` = 21h/00h/03h BRT) + `workflow_dispatch` |
| `.github/workflows/b3_trades_intraday.yml` | **ATIVO** | `schedule` (intraday 10h-19h BRT) + `workflow_dispatch` |
| `.github/workflows/b3_trades.yml` | **ATIVO (fallback manual)** | `workflow_dispatch` apenas (cron removido em PR #80) |
| `.github/workflows/daily_update.yml` | **ATIVO (fallback manual)** | `workflow_dispatch` apenas |

Nenhum YAML orfao. Todos sao acionaveis.

### Documentado, nao mexido

- **Permissoes**: nao auditadas / nao modificadas. O usuario pediu para
  *apenas listar*. Inventario:
  - `anbima_b3_probe.yml`: `permissions: contents: write`
  - `b3_trades_intraday.yml`: `permissions: contents: write`
  - `b3_trades.yml`: `permissions: contents: write`
  - `daily_update.yml`: `permissions: contents: write`
  Nenhum `write-all`. Todos no minimo necessario para `git push`.
- **Env vars orfaos / outputs orfaos**: nao auditado linha-a-linha
  (escopo conservador — qualquer remocao em workflow funcional pode
  quebrar pipeline noturno). Documentado como possivel acao futura
  caso o usuario priorize.
- **Triggers `on:`**: nenhum trigger declarado e nao usado. Workflows
  com `workflow_dispatch` so sao acionados manualmente — uso esperado.

---

## D. Arquivos orfaos no repo

### Aplicado

- [APLICADO] `docs/branch-cleanup.md` -> `docs/archive/branch-cleanup.md`.
  Snapshot do cleanup de PR #118 (D12 do audit anterior). Lista de 85
  branches que ja foram deletadas em maio/2026. Mantido como historico.
- [APLICADO] `docs/audit-report.md` -> `docs/archive/audit-report.md`.
  Datado de 2026-05-12. Lista B7 (`legacy_curve`) como pendencia —
  ja resolvido em PR #116. Lista RD3, D12 — ja resolvidos. Mantido
  como historico.

### Documentado, nao mexido

- [DOCUMENTADO] `docs/anbima-migration-plan.md` — descreve plano de
  migracao Routine claude.ai -> GitHub Actions ja executado (PRs #125,
  #127). Mantido em `docs/` por enquanto, pois contem documentacao
  arquitetural detalhada (>400 linhas) que pode servir de referencia.
  Sugestao: mover para `docs/archive/` em proxima rodada se a aquela
  pasta virar dominante.
- [DOCUMENTADO] `data.json` no raiz — output de `compute_spreads.py`,
  commitado pelos workflows `daily_update.yml` e `anbima_b3_probe.yml`
  (vide `daily_update.yml:190`, `anbima_b3_probe.yml:205`). Embora
  o frontend nao consuma diretamente (so consome `data/overview.json`
  etc), o arquivo eh produzido pelo pipeline e o workflow gravita ao
  redor dele. **NAO mexido** (hard limit #6 — logica funcional dos
  workflows).
- [DOCUMENTADO] `data/.probe_state.json` — verificado: nao existe.
  Nao reapareceu. **Nenhuma acao necessaria.**
- [DOCUMENTADO] README.md — relido inteiro. Linhas 5-6, 154-160, 326-329
  ja mencionam "3 slots noturnos por dia util B3 — 21h, 00h, 03h BRT",
  consistente com `anbima_b3_probe.yml` linha 47 (`0 0,3,6 * * 2-6`)
  pos PR #154. Linhas 173-174 reconhecem que o marker de idempotencia
  foi removido. **Nenhuma referencia a "Todos Trades" como aba separada**
  (PR #150 unificou). README esta atualizado. **NAO mexido.**
- [DOCUMENTADO] Nenhum arquivo de fixture / one-shot esquecido no raiz.

---

## E. Branches no GitHub remote

### Tentativa de delecao

Foram identificadas **115 branches `claude/*`** cujos PRs estao
fechados e mergeados em `main`. O usuario autorizou deletar ate 100
por execucao. As 15 mais recentes (PRs #135-154 com `merged_at` >
2026-05-13) ficam preservadas como rede de seguranca.

**Resultado real:** `git push origin --delete <branch>` retornou
HTTP 403 para todas as tentativas. O token de credencial do
ambiente remoto **nao tem permissao para deletar refs** (so
push de commits / PRs). **0 branches deletadas.**

### Lista de branches que deveriam ser deletadas (100)

Para o usuario rodar localmente:

```bash
# Lista completa (100 branches, todas com PR mergeado em main):
for br in claude/dashboard-overhaul claude/eloquent-brahmagupta-bIwAP \
  claude/restore-setor-and-disp-filter claude/visao-geral-redesign \
  claude/audit-and-fixes claude/fix-dispersao-render \
  claude/fix-disp-anterior-scrub claude/revert-disp-anterior-scrub \
  claude/header-date-format claude/curva-pre-and-multi-data \
  claude/filters-search-aggregate claude/polish-and-validate \
  claude/dispersao-highlight-white claude/titulos-publicos-tab \
  claude/equiparate-titpub-pipeline claude/polish-tabs-and-exclusions \
  claude/unify-titpub-curvas claude/fix-titpub-sort \
  claude/fix-titpub-sort-v2 claude/fix-titpub-sort-v3 \
  claude/titpub-grid-2x2 claude/reorder-securities-columns-JstHh \
  claude/website-audit-review-EpjzN \
  claude/multi-tweaks-titpub-vencimento-format \
  claude/heatmap-delta-paperwise-and-emissor-base \
  claude/aba-debentures-incentivadas claude/incentivadas-isolamento \
  claude/revert-debentures-tab-jmne0 claude/incentivada-coluna-papeis \
  claude/add-incentivada-column-FVN4b claude/eloquent-brahmagupta-5ETQj \
  claude/revert-header-filter-cV2ab claude/heatmap-melhorias-e-data-br \
  claude/change-spread-suffix-2t26k \
  claude/waterfall-stacked-e-graficos-visao-geral \
  claude/cards-kpi-cores-graficos-waterfalls \
  claude/level-to-level-cores-folga-waterfall2 claude/await-spec-fW9ud \
  claude/sync-date-pickers claude/fix-bidirectional-date-sync \
  claude/composicao-emissores-heatmap \
  claude/composicao-tickers-filtro-hibrido \
  claude/composicao-sync-visual-selecionar-todos \
  claude/eloquent-brahmagupta-oKVn2 claude/aba-trades-b3-v0 \
  claude/fix-setor-trades-b3 claude/gha-backfill-b3-trades \
  claude/remove-gha-schedule-b3 claude/filtros-trades-b3 \
  claude/trades-b3-chart-evolucao-taxa claude/trades-b3-picker-busca \
  claude/fix-evol-chart-axis-outliers claude/aba-trades-multi-indexador \
  claude/fix-tr2-build-indexer-map-b3-registry \
  claude/fix-b3-instruments-filter-by-trades \
  claude/fetch-b3-trades-always-refresh-5-days \
  claude/fix-fetch-b3-trades-include-today \
  claude/trades-tab-ux-empty-state-search-grouping \
  claude/trades-tabs-auto-refresh-multi-category-axis-format \
  claude/trades-tab-volume-only-with-filtered-table \
  claude/charts-no-weekend-gaps-and-unified-trades-chart \
  claude/trades-tables-sort-filter-cancelled-and-trades-b3-redesign \
  claude/move-trades-b3-summary-to-visao-geral-deb-only \
  claude/topn20-rename-tabs-and-card-interval \
  claude/remove-indexer-base-and-reorder-tabs \
  claude/rename-trades-tab-and-vg-top20-decimals-and-dod-variation \
  claude/footer-source-add-b3 \
  claude/tp-cards-deb-papeis-and-pesquisa-trades-fixes \
  claude/visao-geral-variation-1day-and-tr2-issuer-persist \
  claude/rewrite-fetch-b3-trades-and-add-consolidated \
  claude/pesquisa-trades-fonte-and-grupo-selectbox \
  claude/debug-b3-fetch-403-cwmOh \
  claude/fix-b3-consolidated-table-name \
  claude/fix-consolidated-bug-mSHGk \
  claude/fix-trade-search-columns-RH6rB \
  claude/rename-date-labels-NLoua claude/daily-update \
  claude/code-review-cleanup-qa3MW claude/limpeza-backend-trivial \
  claude/extrair-b3-api claude/pin-requirements \
  claude/workflow-cache-pip-rebase claude/readme-update \
  claude/remover-legado-spreads claude/retencao-60du \
  claude/delete-branches-mergeadas \
  claude/fix-numeric-filter-and-top20-decimals \
  claude/fix-numeric-filter-nsMDj \
  claude/merge-features-rebuild-dashboard-SBMQI \
  claude/fix-volume-chart-DhNxo claude/fix-volume-chart-oZUr6 \
  claude/fix-daily-volume-chart-eK0Qr claude/fix-daily-update-set-e \
  claude/anbima-phase-2-migration-OhQTf claude/fix-git-pull-autostash \
  claude/update-readme-github-actions-kjNg5 \
  claude/trades-filters-scatter-datepicker \
  claude/pause-animation-tab-switch-76WaK \
  claude/unified-availability-probe-m0Odr \
  claude/idempotency-obsolescence-audit-Ct1m6 ; do
  git push origin --delete "$br"
done
```

15 branches `claude/*` adicionais com PR mergeado tambem podem ser
deletadas, mas ficam reservadas como rede de seguranca:

```
claude/fix-cron-datepicker-PZ9Kf            (PR #135)
claude/cleanup-measure-publication-tooling  (PR #138)
claude/trades-chart-skip-weekends-w3Oi6     (PR #139)
claude/b3-resilience-1051                   (PR #141)
claude/b3-cache-check-7399                  (PR #143)
claude/cron-3-slots-timeout-3h-VS0G         (PR #144)
claude/fix-cache-pagecount-4585             (PR #145)
claude/fix-intraday-concurrency-a1b2        (PR #146)
claude/fix-stash-conflict-manifest-ZUxgf    (PR #147)
claude/intraday-window-10h-18h-WdG63        (PR #148)
claude/unify-trade-tabs-e31f6               (PR #150)
claude/update-overview-data-source-ZtVLM    (PR #151)
claude/remove-b3-cache-checks-r5g4s         (PR #152)
claude/cron-canonico-1-slot-03h-brt-PtgxQ   (PR #153)
claude/add-b3-cache-check-TBsBH             (PR #154)
```

Branches preservadas:

- `main` — default
- `historical-data` — branch separada, possui dados historicos.

Branches sem PR associado: **nenhuma encontrada** (todas as 115
`claude/*` tem PR fechado e mergeado).

PRs `open`: **nenhum**. Page 1 e page 2 da API mostram 154 PRs, todos
fechados.

---

## F. Pull Requests no GitHub

`mcp__github__list_pull_requests state=all sort=updated desc` (paginas
1 e 2, total 154 PRs):

- **Open**: 0
- **Draft sem update >30 dias**: 0
- **Aguardando entrada >14 dias**: 0

Sem PRs orfaos. **Nada a sugerir.**

---

## G. Workflow runs antigos

Nao executado nesta rodada (requer `gh run list` que nao esta
disponivel neste ambiente — `gh` CLI esta restrito ao MCP GitHub).

Comando sugerido para o usuario rodar localmente:

```bash
# Contagem por workflow nos ultimos 30 dias, separados por conclusion
gh run list --limit 1000 --json workflowName,conclusion,createdAt \
  | jq -r '.[] | "\(.workflowName)\t\(.conclusion)"' \
  | sort | uniq -c | sort -rn
```

Para limpeza posterior (manual, com auditoria):

```bash
# Deletar runs com mais de 30 dias e conclusion = failure ou cancelled
gh run list --limit 1000 --json databaseId,conclusion,createdAt,workflowName \
  | jq -r '.[] | select(.conclusion=="failure" or .conclusion=="cancelled") | .databaseId' \
  | xargs -I{} gh run delete {}
```

**NAO aplicado nesta PR** — polui o log de auditoria do GitHub.

---

## H. Dependencias Python (`requirements.txt`)

```
numpy==2.4.4
scipy==1.17.1
pandas==3.0.3
requests==2.33.1
holidays>=0.50
pytz>=2024.1
```

### Inventario (so listar)

- `numpy` — usado em `compute_spreads.py:65`. **OK.**
- `scipy` — usado em `compute_spreads.py:66` (`scipy.interpolate.CubicSpline`).
  **OK.**
- `pandas` — **nao importado em nenhum `.py`** do repo. Pode ser
  dependencia transitiva exigida por `scipy` ou por scripts antigos.
  *Sugerido* remover, mas **fora de escopo** desta PR.
- `requests` — usado em `_http_utils.py:20`, `b3_api.py:23`,
  `fetch_anbima.py:24`. **OK.**
- `holidays` — usado em `b3_calendar.py:17`. **OK.**
- `pytz` — usado em `b3_calendar.py:18`. **OK.**

Nenhum pacote importado sem estar listado.

---

## I. CSS / estilos

### Documentado, nao removido

Classes CSS declaradas em `<style>` de `index.template.html` mas sem
referencia fora da style sheet:

- `.badge.tp-LTN`, `.badge.tp-LFT`, `.badge.tp-NTNB`, `.badge.tp-NTNF`,
  `.badge.tp-NTNC` (linhas 144-148) — `.badge` + sufixo de tipo de titulo
  publico (LTN/LFT/NTN-B/NTN-F/NTN-C). Sem `class="badge tp-XXX"`
  encontrado em HTML/JS. **Possivelmente preparadas para uso futuro
  no painel TP, ou herdadas de versao anterior.**

NAO removidas — sao 5 linhas de CSS especifico que custam ~250 bytes
e ja resolvem o caso de re-introducao de badges. Conservador.

Classes que parecem orfaos mas **nao sao**:

- `.tabulator-cell`, `.tabulator-header`, `.tabulator-row`,
  `.tabulator-row-even` — aplicadas pela biblioteca Tabulator em
  runtime, nao no source. **Mantidas.**

---

## J. Configuracoes

### `.gitignore`

Padrao Python boilerplate (gerado pelo template GitHub). Linhas finais
sao especificas do projeto:

```
raw/
parsed.json
```

Ambos sao intermediarios usados por `fetch_anbima.py` (raw HTTP) e
`compute_spreads.py` (parsed). **OK, mantido.**

Sem regras claramente orfaos.

### `.github/dependabot.yml`

**Nao existe.** Sugestao (fora de escopo): adicionar dependabot para
monitorar `requirements.txt` e `actions/*` versoes pinadas.

### `.claude/settings.json`

```json
{ "sandbox": { "network": { "allowedDomains": [...ANBIMA + debentures...] } },
  "permissions": { "allow": [ WebFetch para esses dominios ] } }
```

Allowlist consistente com o uso real do `fetch_anbima.py`. **OK.**

---

## Hard limits respeitados

- [x] `data/anbima/*`, `data/b3_trades/*`, `data/b3_trades_consolidated/*`,
  `data/overview.json` — **nao tocados** (nem alterados, nem auditados).
- [x] `history/*` — **nao tocado**.
- [x] `fetch_anbima.py`, `fetch_b3_trades.py`, `fetch_b3_trades_consolidated.py`
  — apenas imports F401 removidos (`ruff --fix`). Nenhuma alteracao de
  logica funcional. Self-tests 6/6 OK em ambos os scripts pos-fix.
- [x] `build_dashboard.py`, `compute_spreads.py`, `sectors.py`, `b3_api.py`,
  `b3_calendar.py`, `_http_utils.py` — **nao tocados**.
- [x] Schema de JSONs em `data/` — nao alterado.
- [x] Logica funcional dos workflows — nao alterada. Cron/steps/env vars
  inalterados.
- [x] `requirements.txt` — apenas listado. Nao mexido.
- [x] `permissions:` dos workflows — apenas listados. Nao mexidos.
- [x] Dependencias entre arquivos — nao reordenadas.
- [x] PRs abertos — **nenhum existia**. Nada fechado.

---

## Reversibilidade

Toda a PR pode ser revertida via `git revert <merge-sha>` sem
colaterais:

- Os 3 imports removidos podem voltar e nao afetam nada (eram
  unused).
- A funcao `trd_fetchDay` pode ser restaurada (14 linhas) e nao
  afeta nada (ja era orfa).
- Os 2 docs em `docs/archive/` voltam pra `docs/`.
- `index.html` regenerado se o `build_dashboard.py` rodar.

Branches remotas: **nada foi deletado nesta PR**, entao nao ha o que
reverter nesse front. Lista preservada para acao manual do usuario.
