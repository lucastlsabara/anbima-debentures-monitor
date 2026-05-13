# Plano de migracao ANBIMA: Routine (claude.ai) -> GitHub Actions

Este documento descreve o plano de migracao do pipeline ANBIMA que hoje roda
em uma Routine no `claude.ai` (`trig_01HLZMAUHzPjeKPytm6YfnUd`) para um
workflow `daily_update.yml` no GitHub Actions, usando exclusivamente URLs
publicas da ANBIMA (sem API autenticada, sem browser automation).

## 1. Estado atual

A Routine no claude.ai dispara as 23h BRT e executa, dentro da sandbox do
claude.ai, o pipeline:

```bash
for d in HOJE D-1 D-2 D-3 D-4; do
  python3 fetch_anbima.py    --date "$d" || true
  python3 compute_spreads.py --date "$d" || true
done
python3 build_dashboard.py
git add data/ history/ data.json index.html
git commit -m "data: anbima update ..."
git push
```

Janela de catch-up de 5 dias corridos (hoje + 4 anteriores) absorve dias em
que a ANBIMA atrasou a publicacao ou a execucao da Routine pulou. Idempotente:
`history/<YYYY-MM-DD>.json` e' sobrescrito com o mesmo conteudo deterministico.

O workflow B3 (`b3_trades.yml`) ja' roda no GitHub Actions desde o PR #80,
porque a sandbox da Routine bloqueia `arquivos.b3.com.br`. Esta migracao
unifica tudo em GitHub Actions, eliminando a Routine.

## 2. Inventario de fontes ANBIMA

Tres URLs publicas (`anbima.com.br`, sem auth) cobrem 100% da captura:

| Fonte | URL publica | Funcao em `fetch_anbima.py` | Arquivo intermediario |
|---|---|---|---|
| Debentures (mercado secundario) | `https://www.anbima.com.br/informacoes/merc-sec-debentures/arqs/db<YYMMDD>.txt` | `fetch_db` | `raw/db<YYMMDD>.txt` |
| Estrutura a termo (ETTJ IPCA + Pre) | `https://www.anbima.com.br/informacoes/est-termo/CZ-down.asp?Dt_Ref=DD/MM/YYYY&saida=csv` | `fetch_ettj` | `raw/ettj_<YYYY-MM-DD>.csv` |
| Mercado secundario de titulos publicos | `https://www.anbima.com.br/informacoes/merc-sec/arqs/ms<YYMMDD>.txt` | `fetch_titulos_publicos` | `raw/ms<YYMMDD>.txt` |

Disponibilidade publica: ultimos 5 dias uteis gratis -- bate com a janela de
catch-up. Encoding: latin-1 nos tres arquivos. Separador: `@` (db/ms) e `;`
(ETTJ CSV).

### Versao XLS (mencionada no system prompt, NAO usada)

A ANBIMA tambem publica `https://www.anbima.com.br/informacoes/merc-sec-debentures/arqs/d<YY><mmm><DD>.xls`
(mmm = mes pt-BR 3 letras). E' o mesmo dado em formato planilha; a pipeline
sempre usou a versao TXT, que e' mais leve e parseavel sem dependencia de
openpyxl/xlrd. Mantemos TXT.

## 3. Artefatos gerados (consumidos pelo dashboard)

A coluna "Fonte" abaixo indica qual URL ANBIMA alimenta cada arquivo:

| Arquivo | Gerador | Fonte primaria |
|---|---|---|
| `data.json` | `compute_spreads.py` | db + ETTJ + ms |
| `history/<YYYY-MM-DD>.json` | `compute_spreads.py` | db + ETTJ + ms |
| `data/manifest.json` | `build_dashboard.py` | history/ |
| `data/overview.json` | `build_dashboard.py` | history/ |
| `data/curves_history.json` | `build_dashboard.py` | history/ (ETTJ) |
| `data/heatmap_history.json` | `build_dashboard.py` | history/ |
| `data/movements.json` | `build_dashboard.py` | history/ |
| `data/titpub_history.json` | `build_dashboard.py` | history/ (ms) |
| `data/dispersion/_index.json` | `build_dashboard.py` | history/ |
| `data/dispersion/<date>.json` | `build_dashboard.py` | history/ |
| `index.html` | `build_dashboard.py` | data/*.json |

Os arquivos em `data/b3_trades/` e `data/b3_trades_consolidated/` NAO derivam
de ANBIMA -- sao alimentados pelo workflow `b3_trades.yml`, fora do escopo
desta migracao.

## 4. Mapeamento de bloqueios (Plano A: apenas info publica)

Nenhum bloqueio. As tres URLs ANBIMA usadas pelo pipeline atual ja' sao
publicas e acessiveis sem autenticacao a partir do egress do GitHub Actions.
Confirmado por execucao manual em 2026-05-13 contra `2026-05-12`:

```
[fetch] db de 2026-05-12
[fetch]   -> 1291 papeis
[fetch] ETTJ de 2026-05-12
[fetch]   -> 67 vertices (data publicada: 2026-05-12)
[fetch] titulos publicos de 2026-05-12
[fetch]   -> LFT=17, LTN=12, NTN-B=15, NTN-C=1, NTN-F=6 | titpub_rows=51 | status=ok
```

Estrutura e contagens batem com `history/2026-05-11.json` (1291 papeis, mesmas
chaves, `titpub_status=ok`).

Resultado: **plano A executavel sem bloqueios**. Browser automation, fallback
sintetico e API autenticada permanecem fora do escopo.

## 5. `fetch_anbima.py`

O script atende todos os requisitos do plano A:

- requests apenas (sem playwright/selenium)
- uma funcao por fonte (`fetch_db`, `fetch_ettj`, `fetch_titulos_publicos`)
- parsers dedicados (`parse_db`, `parse_ettj`, `parse_ettj_pre`,
  `parse_titulos_publicos`, `parse_titpub_rows`) que retornam estruturas
  consumidas direto por `compute_spreads.py`/`build_dashboard.py`
- User-Agent identificavel (`anbima-debentures-monitor/1.0`) com fallback
  para UA Mozilla/Chrome em caso de 4xx (nao-404) ou 5xx persistente
  (adicionado na fase 2; algumas vezes a ANBIMA bloqueia UAs nao-browser)
- Retry exponencial 4x (2s/4s/8s/16s) por UA -- mais conservador que o
  minimo pedido (3x 2/4/8); mantido como esta'
- Falha barulhento: HTTP 5xx/timeout -> retry com UA Mozilla, se falhar
  raise (exit 1); HTTP 4xx exceto 404 -> retry com UA Mozilla, se falhar
  raise; 404 do db -> exit 2 ("ainda nao publicado", caller skipa).
  404 do ms.txt marca `titpub_status='404'` e segue.

## 6. Workflow `daily_update.yml` (producao)

Localizacao: `.github/workflows/daily_update.yml`.

Caracteristicas:
- Trigger: `cron: '0 2 * * *'` (02:00 UTC todos os dias = 23:00 BRT) +
  `workflow_dispatch`. Roda em fds/feriado tambem -- ANBIMA responde 404 e
  o workflow trata como skip (idem Routine).
- Job: `anbima-daily-update`, `runs-on: ubuntu-latest`, `timeout-minutes: 15`.
- Steps:
  1. Checkout com `persist-credentials: true` (workflow commita).
  2. Setup Python 3.11 com cache de pip.
  3. `pip install -r requirements.txt`.
  4. Resolve datas alvo via `scripts/list_target_dates.py` (5 dias uteis B3
     contando HOJE, pulando fds/Sexta-Feira Santa/Carnaval/Corpus Christi/
     25-jan/20-nov/demais feriados nacionais).
  5. Para cada data:
     - Idempotencia: `history/<D>.json` existente -> skip sem refetch.
     - `fetch_anbima.py` -> exit 2 (404) = skip; exit != 0 = falha real.
     - `compute_spreads.py` se fetch OK; contabiliza como snapshot NOVO.
  6. Se `new_count > 0`: `build_dashboard.py`, depois commit & push:
     - `git pull --rebase origin main` (race com `b3_trades.yml`); rebase
       falhando -> exit 1 (sem mascarar conflito).
     - `git add data/ history/ data.json index.html`.
     - Commit message: `feat: ANBIMA snapshot <data1>[, <data2>, ...] (run via daily_update.yml)`.
  7. Se `new_count == 0`: pula build/commit, loga "sem dados novos".
  8. Step summary + upload de artifacts (debug, retencao 7 dias).

`permissions: contents: write`. Race com `b3_trades.yml` mitigada pelo
`git pull --rebase origin main` antes do push.

## 7. Transicao em 3 fases

### Fase 1 -- Validar (PR #125 + #126, MERGED)
- `daily_update.yml` em modo PoC (nao commita, gera artifact).
- Validado em paralelo com a Routine durante 2 dias; artifacts batendo com
  os commits da Routine. Bug de exit code sob `set -e` corrigido em #126.

### Fase 2 -- Promover a producao (esta PR, MERGED)
Promove `daily_update.yml` para substituir 100% a Routine. Mudancas:
- `permissions: contents: write`; `persist-credentials: true`.
- Cron `'0 2 * * *'` (todos os dias, nao so seg-sex).
- Janela em dias uteis B3 via `scripts/list_target_dates.py` (em vez de
  dias corridos via bash inline).
- Idempotencia por `history/<D>.json` (skip sem refetch).
- Step de commit & push com `git pull --rebase` antes; commit message
  estruturada listando as datas capturadas.
- `fetch_anbima.py`: fallback UA Mozilla/Chrome quando UA identificavel
  falha com 4xx (nao-404) ou 5xx persistente.

### Fase 3 -- Desativar Routine (manual, fora do repo)
- Lucas desativa a Routine `trig_01HLZMAUHzPjeKPytm6YfnUd` no painel
  `claude.ai/code/routines`.
- Atualizar README.md trocando "Trigger: Routine no Claude.ai" por
  "Trigger: GitHub Actions (.github/workflows/daily_update.yml)".

## 8. Regras invioladas

- Nenhum dado sintetico. Se ANBIMA quebrar, workflow falha barulhento
  (exit code != 0 em qualquer data com erro nao-404).
- `b3_trades.yml` NAO foi tocado.
- `compute_spreads.py`, `build_dashboard.py` e schema dos JSONs intactos.
- Routine no claude.ai NAO foi desativada (Lucas faz na fase 3).
