# Cleanup de branches stale — D12 do audit-report

Inventario das 87 branches remotas em `origin` no momento desta PR
(`main` + `historical-data` + 85 `claude/*`).

## A deletar (85)

Todas as branches abaixo correspondem a PRs **mergeados em `main`** (via
squash, por isso `git branch --merged` nao detecta). Foram identificadas
via cruzamento entre `gh api branches` e `PRs closed com merged_at != null`.

```
claude/aba-debentures-incentivadas
claude/aba-trades-b3-v0
claude/aba-trades-multi-indexador
claude/add-incentivada-column-FVN4b
claude/audit-and-fixes
claude/await-spec-fW9ud
claude/cards-kpi-cores-graficos-waterfalls
claude/change-spread-suffix-2t26k
claude/charts-no-weekend-gaps-and-unified-trades-chart
claude/code-review-cleanup-qa3MW
claude/composicao-emissores-heatmap
claude/composicao-sync-visual-selecionar-todos
claude/composicao-tickers-filtro-hibrido
claude/curva-pre-and-multi-data
claude/daily-update
claude/dashboard-overhaul
claude/debug-b3-fetch-403-cwmOh
claude/dispersao-highlight-white
claude/eloquent-brahmagupta-5ETQj
claude/eloquent-brahmagupta-bIwAP
claude/eloquent-brahmagupta-oKVn2
claude/equiparate-titpub-pipeline
claude/extrair-b3-api
claude/fetch-b3-trades-always-refresh-5-days
claude/filters-search-aggregate
claude/filtros-trades-b3
claude/fix-b3-consolidated-table-name
claude/fix-b3-instruments-filter-by-trades
claude/fix-bidirectional-date-sync
claude/fix-consolidated-bug-mSHGk
claude/fix-disp-anterior-scrub
claude/fix-dispersao-render
claude/fix-evol-chart-axis-outliers
claude/fix-fetch-b3-trades-include-today
claude/fix-setor-trades-b3
claude/fix-titpub-sort
claude/fix-titpub-sort-v2
claude/fix-titpub-sort-v3
claude/fix-tr2-build-indexer-map-b3-registry
claude/fix-trade-search-columns-RH6rB
claude/footer-source-add-b3
claude/gha-backfill-b3-trades
claude/header-date-format
claude/heatmap-delta-paperwise-and-emissor-base
claude/heatmap-melhorias-e-data-br
claude/incentivada-coluna-papeis
claude/incentivadas-isolamento
claude/level-to-level-cores-folga-waterfall2
claude/limpeza-backend-trivial
claude/move-trades-b3-summary-to-visao-geral-deb-only
claude/multi-tweaks-titpub-vencimento-format
claude/pesquisa-trades-fonte-and-grupo-selectbox
claude/pin-requirements
claude/polish-and-validate
claude/polish-tabs-and-exclusions
claude/readme-update
claude/remove-gha-schedule-b3
claude/remove-indexer-base-and-reorder-tabs
claude/remover-legado-spreads
claude/rename-date-labels-NLoua
claude/rename-trades-tab-and-vg-top20-decimals-and-dod-variation
claude/reorder-securities-columns-JstHh
claude/restore-setor-and-disp-filter
claude/retencao-60du
claude/revert-debentures-tab-jmne0
claude/revert-disp-anterior-scrub
claude/revert-header-filter-cV2ab
claude/rewrite-fetch-b3-trades-and-add-consolidated
claude/sync-date-pickers
claude/titpub-grid-2x2
claude/titulos-publicos-tab
claude/topn20-rename-tabs-and-card-interval
claude/tp-cards-deb-papeis-and-pesquisa-trades-fixes
claude/trades-b3-chart-evolucao-taxa
claude/trades-b3-picker-busca
claude/trades-tab-ux-empty-state-search-grouping
claude/trades-tab-volume-only-with-filtered-table
claude/trades-tables-sort-filter-cancelled-and-trades-b3-redesign
claude/trades-tabs-auto-refresh-multi-category-axis-format
claude/unify-titpub-curvas
claude/visao-geral-redesign
claude/visao-geral-variation-1day-and-tr2-issuer-persist
claude/waterfall-stacked-e-graficos-visao-geral
claude/website-audit-review-EpjzN
claude/workflow-cache-pip-rebase
```

## A preservar

| Branch | Motivo |
|---|---|
| `main` | branch principal |
| `historical-data` | backup imutavel do estado pre-retencao 60 du (PR #117) |
| `claude/revert-incentivada-column-1G4Mc` | PR #63 ainda aberto (draft) |
| `claude/process-audit-report-kFYVh` | branch operacional desta sessao |
| `claude/delete-branches-mergeadas` | branch desta PR (sera auto-deletada no merge via squash) |

## Branches closed-not-merged (NAO podem ser deletadas via esta automacao)

Estas tem `closed_at` mas `merged_at = null` (PR foi fechado sem merge,
geralmente abandonado/superseded). Nao estao na lista de delecao por
seguranca — eventual codigo experimental pode estar la:

- `claude/nice-tesla-SrIOn`
- `claude/update-debentures-routine-EeYWr`
- `claude/optimistic-archimedes-kbah6`

(Ja nao aparecem em `gh api branches` — provavelmente o usuario ja
deletou. Lista mantida pra paper-trail.)

## Execucao

A delecao em si **nao foi feita por esta PR** porque o sandbox de
execucao bloqueia delete de refs remotas (`git push origin --delete`
retorna HTTP 403). Lucas pode rodar localmente:

```bash
# bulk delete
while read b; do
  git push origin --delete "$b"
done < <(grep '^claude/' docs/branch-cleanup.md | grep -v Preservar)

# ou via gh
while read b; do
  gh api -X DELETE "repos/lucastlsabara/anbima-debentures-monitor/git/refs/heads/$b"
done < <(grep '^claude/' docs/branch-cleanup.md)
```
