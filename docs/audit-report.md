# Auditoria geral do anbima-debentures-monitor

Data: 2026-05-12.
Branch: `claude/code-review-cleanup-qa3MW`.
Escopo: relatório só. Nenhum código foi alterado. PRs de fix vêm depois.

---

## Sumário executivo

O projeto está estruturalmente saudável: pipeline ANBIMA (compute_spreads + build_dashboard) e pipeline B3 (fetch + consolidated) estão consistentes com a doc, sem TODO/FIXME pendentes, sem dados sintéticos e sem hardcoded credentials. O backlog real é higiene, não bug:

- **Duplicação clara** entre `fetch_b3_trades.py` e `fetch_b3_trades_consolidated.py` (~120 linhas idênticas: headers, classes de erro, retry, manifest, janela de 5 dias). Cabe um módulo `b3_api.py`.
- **Inconsistência sutil de retry**: backoff do consolidated é `2 ** (attempt+1)` (2/4/8s) e o do trade-by-trade é `2 ** attempt` (1/2/4s). Padronizar.
- **Código diagnóstico vivo mas sem leitor**: `spread_pp_legado` e `diagnostico_metodo.diff_vs_legado_bps` em `compute_spreads.py` (lines 154-156, 215-224, 380-386) são gerados todo dia, persistidos em `history/*.json` e propagados pra `manifest.json`, mas **nenhuma referência no frontend**. Decidir manter como métrica interna ou retirar (PR backend cleanup).
- **Imports não usados isolados**: `from dataclasses import dataclass` em `fetch_anbima.py:22`, `import re` redundante dentro de funções em `build_dashboard.py:164, 690`.
- **Frontend (`index.template.html`, 4998 linhas)** está OK: zero `onclick=` inline, só 1 uso de `window.*` (hashchange listener legítimo), 7 `console.error` em paths de erro reais, sem CSS classes obviamente órfãs. Não há sinais de refactor leve fácil — frontend é frágil e está estável depois dos PRs #103/#106/#107/#108.
- **Storage / data**: 122 MB em `data/` hoje, dominado por `data/b3_trades/` (69 MB, 11 dias) e `data/b3_trades_consolidated/` (44 MB, 5 dias). Projeção a 252 dias úteis: ≈ 1,5 GB no main se nada for podado. **Esta é a maior decisão pendente do relatório.**
- **77 branches `claude/*` no GitHub**, todas de PRs já mergeados ou abandonadas. Limpeza rotineira não-destrutiva (PRs orfãos não há — só 1 draft, #63).
- **README está atualizado e fiel à arquitetura atual.** Workflow B3 está OK, com timeout-minutes: 30 e permissions: contents:write mínimo. Só falta cache de pip e a action de setup-python ainda em v5 (atual).

Funcionalidades ANBIMA, Trades B3, Pesquisa de Trades, Curvas, Dispersão, Heatmap e Títulos Públicos NÃO são afetadas por nenhum item deste relatório. Todos os fixes propostos preservam comportamento.

---

## Frente 1 — Backend Python

Arquivos: `b3_calendar.py` (83 linhas), `fetch_anbima.py` (509), `compute_spreads.py` (440), `build_dashboard.py` (768), `fetch_b3_trades.py` (343), `fetch_b3_trades_consolidated.py` (315), `backfill_b3_trades.py` (78), `backfill_b3_trades_consolidated.py` (105), `recompute_sectors.py` (79), `sectors.py` (348).

### Achados

| # | Arquivo:linha | Achado | Severidade | Ação |
|---|---|---|---|---|
| B1 | `fetch_anbima.py:22` | `from dataclasses import dataclass` importado e nunca usado | baixa | remover |
| B2 | `build_dashboard.py:164` | `import re` dentro de `_clean_emissor`. Subir pro topo | baixa | mover import |
| B3 | `build_dashboard.py:690` | `import re` dentro de `write_html`. Idem | baixa | mover import |
| B4 | `build_dashboard.py:161-165` | `_clean_emissor` reescrito; `sectors.clean_emissor` faz exatamente a mesma coisa (regex `\s*\(\*+\)\s*`) | baixa | usar `sectors.clean_emissor` direto, deletar a cópia |
| B5 | `fetch_b3_trades.py:115` vs `fetch_b3_trades_consolidated.py:140` | Backoff inconsistente: `2 ** attempt` (1,2,4s) vs `2 ** (attempt+1)` (2,4,8s) | média | padronizar pra 2,4,8s (consolidated tem o padrão correto, alinha com `fetch_anbima.HTTP_RETRY_BACKOFF_SEC`) |
| B6 | `fetch_b3_trades.py` ↔ `fetch_b3_trades_consolidated.py` | Duplicação extensa: `REQUEST_HEADERS`, `B3UnavailableError`, `SandboxBlockedError`, `_post_page`, `_last_n_b3_business_days`, `_update_manifest`, `refresh_recent_days` (mesma estrutura). ~120 linhas em comum | média | extrair `b3_api.py` com `B3_HEADERS`, classes de erro, `post_page(table, date, page, size)`, `last_n_business_days(n)`, `update_manifest(path, entry)` |
| B7 | `compute_spreads.py:154-156, 215-224, 380-386` | `legacy_curve` / `spread_pp_legado` / `diff_vs_legado_bps` gerados sempre, gravados em `data.json` + `history/*.json` + `manifest.json`. Frontend NÃO LÊ nada disso (`grep diagnostico_metodo index.template.html` → zero) | média | **DECISÃO PENDENTE 1**: manter (métrica interna pra log)? ou remover e simplificar `compute_spreads`? |
| B8 | `build_dashboard.py:60-94 `_backfill_prefixados()`` | Backfill de spread Prefixado para snapshots antigos (pré-item-12). Continua útil só enquanto houver dias velhos em `history/`. Hoje 100% dos snapshots já foram gerados por `compute_spreads` novo. Verificar e potencialmente remover | baixa | **DECISÃO PENDENTE 2**: rodar `_backfill_prefixados` em todos os snaps existentes uma única vez via script, persistir, depois deletar a função |
| B9 | `fetch_anbima.py:73` | Expressão `r.raise_for_status() if r.status_code >= 400 else None` — funciona mas é uso de expressão ternária como statement, atípico | baixa | trocar por `if r.status_code >= 400: r.raise_for_status()` |
| B10 | `backfill_b3_trades.py` ↔ `backfill_b3_trades_consolidated.py` | Mesma estrutura `_iter_business_days`, mesmo loop de skipped/fetched/failed. consolidated tem `_parse_argv` com --start/--end; trade-by-trade não | baixa | uniformizar parsing de args; OK manter dois arquivos finos por explicit dispatch no workflow |
| B11 | `backfill_b3_trades.py:21` | `DEFAULT_START = date(2026, 4, 24)` hardcoded (primeiro dia do dashboard) | baixa | OK manter como sentinela, comentar no arquivo |
| B12 | `backfill_b3_trades_consolidated.py:60-62` | Importa `fetch_b3_trades_consolidated._last_n_b3_business_days` (underscore-private). Quebra contrato | baixa | exportar como público em `b3_api.py` (ver B6) |
| B13 | `requirements.txt` | Usa `>=` em todas as deps (numpy>=1.26, scipy>=1.11, pandas>=2.0, requests>=2.31, holidays>=0.50, pytz>=2024.1). Sem pin. CI/web sessions podem pegar versão major nova e quebrar | média | pinar com `==` ou `~=` pelo menos as críticas (requests, scipy). Workflow B3 não tem cache de pip nem pip-tools |
| B14 | `b3_calendar.py:42-53` | Feriados B3 calculados dinamicamente via `python-holidays` + Easter manual. Cobre 2026/2027 corretamente. Aniversário de SP (25/jan) hardcoded — OK. Sexta-feira Santa coberta por `python-holidays` BR. Carnaval e Corpus Christi calculados localmente. Não usa o `B3` extension de `python-holidays` (`country_holidays("BR", subdiv=None)` retorna nacional só) | baixa | OK manter; eventual upgrade para `holidays>=0.50` que tem `holidays.financial_holidays("B3")`. **DECISÃO PENDENTE 3**: trocar pra `financial_holidays("B3")`? Atualmente custom é mais explícito |
| B15 | `compute_spreads.py:65-71` | Imports: `argparse, json, sys, datetime, Path, numpy, scipy.CubicSpline, b3_calendar.resolve_default_date`. Tudo usado | OK | — |
| B16 | `sectors.py:1-348` | Heurística enorme de regex + prefixos. Tudo usado por `classify()` que é chamado em `fetch_b3_trades._row_from_array`, `build_dashboard._enrich`, `recompute_sectors._recompute_file`. Sem dead patterns óbvios. `clean_emissor` exportado mas só `build_dashboard` reescreve a função (ver B4) | OK | — |
| B17 | `recompute_sectors.py` | Util de manutenção, idempotente, sem rede. Pouco usado mas critica enquanto `sectors.py` muda | OK | manter |
| B18 | print() vs logging | TODOS os scripts usam `print(...)` com `file=sys.stderr` (fetch_anbima e compute_spreads) ou stdout (fetch_b3_trades). Sem `logging`. Workflow B3 lê stdout direto | baixa | OK como está. Migrar pra `logging` muda formato e não traz beneficio óbvio (não há filtros nem múltiplos handlers) |
| B19 | Estilo string | 100% f-strings já. Nenhum `.format()` ou `%` legado | OK | — |
| B20 | Tratamento de erro | Padrão consistente: cada fetch tem retry exponencial; 404 é warn-and-skip explícito; rede/timeout/5xx vira raise pós-retries; nada engole exceção em silêncio | OK | — |
| B21 | Funções >80 linhas | `compute_spreads.main` (≈150 linhas, mas estrutura linear bem comentada), `build_dashboard.build_overview` (≈80 linhas), `_post_page` em ambos os fetch (≈30 linhas). Nada gritante | OK | não refatorar sem aprovação |

### Resumo Frente 1

Cleanup com ROI bom (B1-B4, B5, B9): ≈30 minutos, ganho de legibilidade.
Cleanup com risco (B6, B7, B8, B13): cada um merece PR isolado.

---

## Frente 2 — Frontend (`index.template.html` + `build_dashboard.py` rendering)

`index.template.html`: 4998 linhas, 188 KB. 134 funções JS top-level. CDN: tabulator 6.2.5, plotly 2.35.2, flatpickr 4.6.13.

### Achados

| # | Local | Achado | Severidade | Ação |
|---|---|---|---|---|
| F1 | template:7-22 | CDNs com versões fixas: tabulator-tables@6.2.5, plotly.js-dist-min@2.35.2, flatpickr@4.6.13 | OK | versões fixas é o jeito certo. Não vale upgrade especulativo |
| F2 | template (geral) | Zero `onclick="..."` inline. Todos os handlers via `addEventListener` ou Tabulator/Plotly callbacks | OK | — |
| F3 | template:955 | Único `window.*` é `window.addEventListener("hashchange", ...)` — hash routing legítimo | OK | — |
| F4 | template:7 acessos a `console` | 7 ocorrências, **todas** em catch blocks (`console.error(err)`). Nenhum debug esquecido | OK | — |
| F5 | template (geral) | Nenhum `<!-- comentário com código HTML -->` esquecido. Nenhum `// debug`. Nenhum `// TODO/FIXME` no JS | OK | — |
| F6 | template:1378-1396, 4486-4530 | `vg_*` e `tr2_*` namespaces consistentes (prefixos por feature). Padrão de nomenclatura sólido | OK | — |
| F7 | build_dashboard.py:60-94 | `_backfill_prefixados` aplica spread Prefixado on-the-fly antes de servir. Frontend está cego pra essa transformação — depende totalmente do build. Ver B8 | média | ver B8 |
| F8 | build_dashboard.py:686-698 | `write_html` faz find-replace de `const BUILD_VERSION = "..."` via regex no template. Funciona porque o template tem placeholder exato; é deterministico | OK | — |
| F9 | build_dashboard.py:668-683 | `_build_version` é SHA-256 do conteúdo de todos os JSONs em `data/` (ordenado por path). Idempotente se inputs não mudam. Bom design pra cache-busting | OK | — |
| F10 | build_dashboard.py `main()` | Build inteiro lê todo `history/*.json` em memória (11 snapshots ≈ 30 MB total atualmente). Pra 252 snaps seria ≈ 700 MB. Não é problema agora mas vira problema | baixa | sinalizar — **DECISÃO PENDENTE 4** |
| F11 | template (CSS inline) | CSS variables (`--bg-card`, `--text-1`, etc) já em uso. Cores hardcoded inline residuais em poucos pontos (estilo de `style="color:#..."`) — não vale a pena rastrear sem feature dependente | OK | — |
| F12 | template (event listeners) | Nenhum `addEventListener` evidente duplicado no boot. Cada `setupTimeline`, `tr2_initPicker`, etc é chamado de `_bootstrap()` único (linha 4984) | OK | — |
| F13 | build_dashboard.py:114-121 | `_write_json` escreve diretamente sem `.tmp` + rename. Diferente do padrão atômico de `fetch_b3_trades`. Crash no meio do build deixa JSON corrompido | baixa | adicionar `.tmp` + rename. ROI baixo: build é rápido e o git fica como recovery |

### Resumo Frente 2

Frontend está limpo. Não há lixo de versão anterior visível. Os PRs #103/#106/#107/#108 ficaram bem aplicados sem deixar dead code visível.

---

## Frente 3 — Routine no Claude.ai (read-only)

**Não tenho acesso ao prompt da routine `trig_01HLZMAUHzPjeKPytm6YfnUd`.** A inspeção é puramente baseada no README e em código.

### Achados

| # | Achado | Severidade | Ação |
|---|---|---|---|
| R1 | README:104-139 documenta a janela de catch-up `[hoje, D-1, D-2, D-3, D-4]` em **dias corridos**. Em fim de semana ou feriado isso vira 1-2 dias úteis efetivos. Funciona com o tratamento de 404 da ANBIMA | OK | — |
| R2 | README:106 diz "Routine do Claude Code". O nome canônico hoje é Routine no Claude.ai (Claude Code é o CLI). Pequeno drift de terminologia | baixa | atualizar README "Routine no Claude.ai" |
| R3 | Workflow b3_trades.yml roda `0 2 * * 2-6` (23h BRT seg-sex). Routine ANBIMA roda 23h BRT diário. Conflito de horário possível em commit race (B3 e ANBIMA pushing no mesmo minuto). Mitigação atual: branchs separados? Não — push direto a `main`. Verificar | média | **DECISÃO PENDENTE 5**: como evitar push collision? Cron do GH Actions em 02:05 UTC? |
| R4 | Conectores Gmail/Calendar/Drive da Routine — **sem visibilidade**. README não menciona | baixa | **DECISÃO PENDENTE 6**: Lucas valida se a routine usa esses conectores. Se não, desativar pra reduzir superfície |
| R5 | Routine v2 sugerida | — | ver seção dedicada abaixo |

### Routine Instructions v2 (sugestão, NÃO aplicada)

```text
Coleta diária ANBIMA — pipeline DEB + ETTJ + TitPub.

Quando: 23h BRT, todo dia.

Para cada data X em [hoje, hoje-1, hoje-2, hoje-3, hoje-4] (dias corridos):
  1. python3 fetch_anbima.py --date X
     - exit 0  → seguir
     - exit 2  → ANBIMA ainda não publicou X; PULAR esta data
     - exit 1 ou outro  → ABORTAR rotina, sem commit
  2. python3 compute_spreads.py --date X
     - exit != 0 → ABORTAR rotina, sem commit

Depois do loop, se PELO MENOS UM dia teve sucesso:
  3. python3 build_dashboard.py
     - exit != 0 → log e ABORTAR sem commit
  4. git add data/ history/ index.html data.json
  5. git commit -m "feat: ANBIMA snapshot {data_mais_recente_processada}"
  6. git pull --rebase origin main && git push

Se nenhum dia teve sucesso, encerrar silenciosamente (FDS/feriado).

NUNCA gerar dados sintéticos. Nunca commitar parsed.json ou raw/.
NUNCA tocar em data/b3_trades/ ou data/b3_trades_consolidated/
(coletados por workflow GitHub Actions separado).
```

Comparar com a routine atual e ajustar manualmente no painel do Claude.ai. **DECISÃO PENDENTE 7**: confirmar se a routine atual já faz git pull --rebase antes do push (evita race com workflow B3).

---

## Frente 4 — Inventário de `data/` e `history/`

### Distribuição de bytes (122 MB total em `data/`)

| Diretório/arquivo | Tamanho | Conteúdo |
|---|---|---|
| `data/b3_trades/` | **69 MB** | 11 dias × ~6.5 MB/dia |
| `data/b3_trades_consolidated/` | **44 MB** | 5 dias × ~9 MB/dia |
| `data/movements.json` | 5.4 MB | tabela completa por data (build) |
| `data/dispersion/` | 1.7 MB | 11 dias × ~150 KB |
| `data/heatmap_history.json` | 1.5 MB | matriz setor × bucket |
| `data/overview.json` | 1.2 MB | KPIs + curvas + top movers |
| `data/titpub_history.json` | 108 KB | títulos públicos por data |
| `data/curves_history.json` | 16 KB | matriz ETTJ |
| `data/manifest.json` | 4 KB | índice |
| `history/` | 13 MB | 11 snapshots ANBIMA × ~1.2 MB |
| `data.json` (root) | 1.2 MB | output do compute_spreads (último dia) |
| `index.html` | 188 KB | dashboard renderizado |
| `index.template.html` | 188 KB | fonte |
| **Total raw em data/** | **122 MB** | 11 dias |

### Top 5 maiores arquivos

| Arquivo | Tamanho |
|---|---|
| `data/b3_trades_consolidated/2026-05-05.json` | 14 MB |
| `data/b3_trades_consolidated/2026-05-06.json` | 11 MB |
| `data/b3_trades_consolidated/2026-05-08.json` | 9.9 MB |
| `data/b3_trades_consolidated/2026-05-07.json` | 8.9 MB |
| `data/b3_trades/2026-04-28.json` | 8.2 MB |

### Top 5 diretórios por nº de arquivos

| Diretório | Arquivos |
|---|---|
| `data/b3_trades/` | 12 (11 dias + manifest) |
| `data/dispersion/` | 12 (11 dias + _index) |
| `history/` | 11 |
| `data/b3_trades_consolidated/` | 6 (5 dias + manifest) |
| `data/` (top-level) | 9 jsons |

### Manifests vs filesystem

- `data/manifest.json`: 11 datas listadas (2026-04-24 a 2026-05-11). `history/` tem 11 arquivos. **Alinhado.**
- `data/b3_trades/manifest.json`: 11 datas. `data/b3_trades/*.json` tem 11 dias. **Alinhado.**
- `data/b3_trades_consolidated/manifest.json`: **5 datas (2026-05-05 a 2026-05-11)**. `data/b3_trades_consolidated/*.json` tem 5 dias. **Alinhado mas defasado** vs `data/b3_trades/` (que tem 11 dias começando em 2026-04-24).
- `data/dispersion/_index.json`: 11 datas. Arquivos: 11. **Alinhado.**

### Projeção de crescimento (ritmo atual: ~13 MB/dia útil em `data/`)

Considerando 21 dias úteis/mês:
- 3 meses: **~820 MB**
- 6 meses: **~1.6 GB**
- 12 meses: **~3.3 GB**

Cenário acima é com 100% retenção. GH free repo limita ~1 GB cumulativo recomendado.

### Achados de dados

| # | Achado | Severidade | Ação |
|---|---|---|---|
| D1 | `data/b3_trades_consolidated/` começa em 2026-05-05; trade-by-trade começa em 2026-04-24. Gap de ~7 dias úteis no consolidated | média | rodar backfill consolidated 2026-04-24 → 2026-05-04, OU aceitar que aba só funciona desde 05-05 |
| D2 | `data/b3_trades_consolidated/2026-05-11.json` com apenas 703 KB / 3140 linhas (vs ~10 MB / 50k linhas normal). Provavelmente publicação parcial da B3 — domingo? não, era segunda. **Verificar se a B3 reprocessou no dia seguinte** | média | **DECISÃO PENDENTE 8**: Lucas confere se 2026-05-11 está completo na B3 hoje. Workflow refresh forçado já corrigiria automaticamente |
| D3 | Não há arquivos com extensão errada (`.bak`, `.tmp`, `.old`, `~`) | OK | — |
| D4 | Não há `parsed.json` ou `raw/` versionados (gitignore correto). Verificado | OK | — |
| D5 | Nenhum diretório vazio em `data/` | OK | — |
| D6 | Política de retenção: não há. **Tudo cresce sem teto** | alta | **DECISÃO PENDENTE 9 (a principal)**: ver bloco abaixo |

### DECISÃO PENDENTE 9 — Política de retenção

Opções:

A) **Retenção no main, full history em branch separado** (recomendado).
   - Manter últimos 60 dias úteis em `data/b3_trades/`, `data/b3_trades_consolidated/`, `data/dispersion/`, `history/` no main.
   - Manter `data/overview.json`, `data/movements.json`, `data/heatmap_history.json`, `data/curves_history.json`, `data/titpub_history.json` (agregações leves, todas <6 MB) com retenção completa.
   - Em paralelo, branch `historical-data` sem build, só com arquivos diários completos.
   - `build_dashboard.py` continua gerando agregações com base no que existe em `history/` — perde-se profundidade pra trás de 60 du, ganha-se peso constante.

B) **Comprimir on-disk**: gzip dos JSONs de `b3_trades/` e `b3_trades_consolidated/`. Frontend descomprime via `DecompressionStream` (suportado em Chrome/Safari/Firefox modernos). Reduz ~70% em texto JSON. Custo: complexidade no frontend e na pipeline.

C) **Mover B3 trades pra release assets** (gh release upload). Mantém apenas links versionados; repo principal só com agregações leves.

D) **Aceitar o crescimento** e migrar pra Git LFS quando passar de 1 GB.

Minha recomendação: A, com cutoff de 60 du. Reverter é trivial via branch.

---

## Frente 5 — Infraestrutura + Docs

### `.github/workflows/b3_trades.yml`

| # | Achado | Severidade | Ação |
|---|---|---|---|
| I1 | `permissions: contents: write` — mínimo necessário | OK | — |
| I2 | `timeout-minutes: 30` — adequado (consolidated demora ~24 min) | OK | — |
| I3 | `actions/checkout@v4` e `actions/setup-python@v5` — versões atuais | OK | — |
| I4 | Sem cache de pip. Cada run reinstala deps inteiras (5-8 dependências, mas inclui scipy=~30s) | baixa | adicionar `cache: 'pip'` no setup-python@v5 (vai pra `~/.cache/pip` automaticamente). Diff: 4 linhas |
| I5 | `git push` simples no final. Em race com a Routine ANBIMA (que pode estar dando push exatamente nas 23h BRT) → push rejected | média | adicionar `git pull --rebase origin main` antes do push (linha única). Ou aceitar retry manual — workflow tem `workflow_dispatch` |
| I6 | Validate backfill inputs faz double-check de YYYY-MM-DD; OK. Inputs do mode=backfill são opcionais no schema mas o step verifica via shell | OK | — |
| I7 | Sem matrix strategy. Workflow rodando duas etapas seriais (trade-by-trade depois consolidated). Tempo total: ~24+5 = ~29 min. Borderline contra timeout-minutes:30 | média | **DECISÃO PENDENTE 10**: paralelizar em dois jobs (+5 min total se algum reprocessa). Não vale ainda |
| I8 | Mensagem de commit: `data: b3 trades update ${DATE_TAG} (workflow ${MODE})` — consistente com histórico | OK | — |

### `.gitignore`

| # | Achado |
|---|---|
| G1 | `__pycache__/`, `*.py[codz]`, `.DS_Store` — tudo coberto |
| G2 | `raw/` e `parsed.json` corretamente ignorados |
| G3 | Gitignore default do Python (gerado pelo gh) tem ~150 linhas de coisas que esse projeto nunca terá (pipenv, poetry, Sage, mkdocs, Streamlit, Marimo, etc) — não machuca |

### README

| # | Achado |
|---|---|
| RD1 | Está atualizado: descreve pipeline ANBIMA + B3 corretamente; menciona Routine às 23h BRT; documenta delete+write atomic dos fetchers; cita PRs #102, #103, #105, #106 implicitamente via texto |
| RD2 | "Routine do Claude Code" → ler "Routine no Claude.ai" (drift de terminologia) |
| RD3 | Seção "Aba Pesquisa de Trades" (linha 262) menciona feature mas não os filtros Fonte/Grupo (PR #103) nem a invariante Data Anterior <= Data Atual (PR #108) |
| RD4 | Bloco de "Pré-agregação" lista os JSONs gerados em `data/` mas omite `titpub_history.json` da tabela (linha 78) e `dispersion/_index.json` aparece mas não `data/b3_trades*` |
| RD5 | Não cita o workflow `.github/workflows/b3_trades.yml` por nome — vale acrescentar uma seção curta "Automação B3" |

### Outras configs

- `pyproject.toml`: **não existe**. OK pra projeto de scripts.
- `setup.py` / `Procfile` / `Makefile`: **não existem**. OK.
- `CLAUDE.md` ou `AGENTS.md`: **não existem**. README serve.
- `.claude/settings.json`: configurações de sandbox + permissões pra WebFetch em domínios ANBIMA. Bem feito.

### Issues + PRs

- Open issues: **0**.
- Open PRs: **1 draft (#63)** — "revert: remove coluna Incentivada da aba Papéis (PRs #60 e #61)". Está aberto desde 2026-05-08 (4 dias). **DECISÃO PENDENTE 11**: mergear ou fechar como abandoned? O conteúdo do PR é reverter feature retirada — pode estar obsoleto.

---

## Frente 6 — Lixo a deletar

### Lixo local (workspace atual)

Verificado: **nenhum** arquivo em qualquer dessas categorias:
- `__pycache__/`, `*.pyc`, `*.pyo` — nenhum versionado nem local
- `.DS_Store`, `Thumbs.db` — nenhum
- `*.bak`, `*.old`, `*.tmp`, `*.swp` — nenhum
- `*~`, `*.orig` — nenhum
- `node_modules/` — não aplica
- `.vscode/`, `.idea/` — não versionado

Estado já está limpo. Nada pra deletar no FS local.

### Branches no GitHub — total 77 (incl. main)

Todas as branches `claude/*` listadas correspondem a PRs já mergeados em `main` (verificado por amostragem: `claude/charts-no-weekend-gaps-and-unified-trades-chart` é o PR #93 já mergeado; `claude/footer-source-add-b3` é #99; `claude/rewrite-fetch-b3-trades-and-add-consolidated` é #102; etc).

Exceções e candidatas a tratamento especial:

| Branch | Razão pra reter? |
|---|---|
| `main` | mantém |
| `claude/code-review-cleanup-qa3MW` (current) | mantém — em uso |
| `claude/revert-incentivada-column-1G4Mc` | PR #63 draft aberto — **DECISÃO PENDENTE 11** |
| `claude/audit-and-fixes` | nome genérico; sem PR correspondente óbvio — verificar antes de deletar |
| `claude/website-audit-review-EpjzN` | idem |
| `claude/await-spec-fW9ud` | idem |
| `claude/eloquent-brahmagupta-{5ETQj,bIwAP,oKVn2}` | placeholders Anthropic-gerados — deletar |
| Demais 70 branches | **deletar todas** (PRs mergeados) |

**Ação**: PR separado `claude/delete-branches-mergeadas` com lista explícita por nome.

---

## Top 10 ações recomendadas (com ROI)

| # | Ação | Esforço | Risco | ROI |
|---|---|---|---|---|
| 1 | Limpeza backend trivial (B1, B2, B3, B4, B9) | 15 min | baixíssimo | alto: legibilidade |
| 2 | Extrair `b3_api.py` (B6) | 1 h | médio (mover código entre arquivos) | alto: ~120 linhas dedup |
| 3 | Padronizar backoff B5 pra (2,4,8s) | 5 min | baixíssimo | médio |
| 4 | Adicionar `cache: 'pip'` ao workflow (I4) | 5 min | baixíssimo | médio: -30s por run |
| 5 | Adicionar `git pull --rebase` antes do push no workflow (I5) | 5 min | baixo | alto: evita falha por race |
| 6 | Backfill consolidated 2026-04-24 → 2026-05-04 (D1) | 1 run de workflow | baixo | médio: completude da aba |
| 7 | Pin versões em `requirements.txt` (B13) | 10 min | baixo | médio: reproducibility |
| 8 | Atualizar README (RD2-5) | 20 min | nenhum | baixo: doc fresca |
| 9 | Decidir destino de `spread_pp_legado` (B7) | discussão + 30 min se remover | médio (toca compute_spreads) | médio |
| 10 | Política de retenção (D6, decisão A/B/C/D) | discussão; implementação 1-2 h | alto se sair pela tangente; baixo se opção A | **muito alto** a longo prazo |

---

## DECISÕES PENDENTES (para Lucas responder)

1. **B7**: Manter `spread_pp_legado` + `diagnostico_metodo.diff_vs_legado_bps` em `compute_spreads.py` (útil pra log/auditoria), ou remover (já que frontend nunca lê)?
2. **B8**: Rodar `_backfill_prefixados` uma vez sobre `history/`, persistir o resultado, e remover a função do build (one-shot retroativo); ou manter como salvaguarda?
3. **B14**: Trocar feriados manuais por `holidays.financial_holidays("B3")`? (Mais robusto; libera o `_easter_sunday` interno.)
4. **F10**: Build segura todo o `history/` em memória. Vale paginar/streaming quando o histórico passar de N dias? Qual N?
5. **R3**: Como evitar push collision entre Routine ANBIMA (23:00 BRT) e workflow B3 (02:00 UTC = 23:00 BRT no horário de verão; 22:00 BRT fora dele — janelas se sobrepõem)? Sugestão: `git pull --rebase` em ambos.
6. **R4**: Conectores Gmail/Calendar/Drive da Routine — algum é usado de verdade? Se não, desativar.
7. **R7**: Routine atual faz `git pull --rebase` antes do push? Se não, fazer ajuste manual.
8. **D2**: `data/b3_trades_consolidated/2026-05-11.json` está com 3140 linhas (vs ~50k normal). Conferir se a B3 publicou o arquivo completo (próximo refresh do workflow corrige automático).
9. **D6**: Qual política de retenção? A (60 du no main + branch full), B (gzip), C (release assets), ou D (LFS depois)?
10. **I7**: Paralelizar o workflow em dois jobs (trade-by-trade + consolidated)? Hoje fica em ~29 min, perto do timeout de 30. Por enquanto sobra folga.
11. **PR #63**: Mergeable ou fechar como abandoned?
12. **Branches stale**: posso fazer um PR `claude/delete-branches-mergeadas` com lista por nome (~70 branches)? Ou preferes deletar tudo só após confirmação caso-a-caso?

---

## Próximos PRs sugeridos (após decisões)

Não criados nesta sessão. Cada um vira PR isolado:

- `claude/limpeza-backend` — B1, B2, B3, B4, B5, B9
- `claude/extrair-b3-api` — B6, B12 (módulo `b3_api.py`)
- `claude/pin-requirements` — B13
- `claude/workflow-cache-pip-rebase` — I4, I5
- `claude/readme-update` — RD2, RD3, RD4, RD5
- `claude/remover-legado-spreads` — B7 (condicional à decisão 1)
- `claude/retencao-dados` — D6 (condicional à decisão 9)
- `claude/delete-branches-mergeadas` — limpeza GitHub (condicional à decisão 12)

Sem regressões funcionais previstas em nenhum desses.
