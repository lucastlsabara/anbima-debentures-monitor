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

## 5. `fetch_anbima.py` (sem mudancas nesta PR)

O script atual ja' atende todos os requisitos do plano A:

- requests apenas (sem playwright/selenium)
- uma funcao por fonte (`fetch_db`, `fetch_ettj`, `fetch_titulos_publicos`)
- parsers dedicados (`parse_db`, `parse_ettj`, `parse_ettj_pre`,
  `parse_titulos_publicos`, `parse_titpub_rows`) que retornam estruturas
  consumidas direto por `compute_spreads.py`/`build_dashboard.py`
- User-Agent identificavel (`anbima-debentures-monitor/1.0`)
- Retry exponencial 4x (2s/4s/8s/16s) -- mais conservador que o minimo
  pedido (3x 2/4/8); mantido como esta'
- Falha barulhento: HTTP 5xx/timeout -> raise; HTTP 4xx (exceto 404 do db
  que sai com exit 2 = "ainda nao publicado") -> raise. 404 do ms.txt
  marca `titpub_status='404'` e segue.

Por isso esta PR NAO refatora o fetcher; apenas adiciona o workflow + esta
documentacao.

## 6. Workflow `daily_update.yml` (PoC nesta PR)

Localizacao: `.github/workflows/daily_update.yml`.

Caracteristicas:
- Trigger: `cron: '0 2 * * 2-6'` (02:00 UTC ter-sab = 23:00 BRT seg-sex,
  mesmo cron do `b3_trades.yml`) + `workflow_dispatch`.
- Job: `validate-anbima-pipeline`, `runs-on: ubuntu-latest`,
  `timeout-minutes: 15`.
- Steps:
  1. Checkout (sem credenciais persistidas -- nao precisa, nao commita)
  2. Setup Python 3.11 com cache de pip
  3. `pip install -r requirements.txt`
  4. Resolve datas alvo (default: hoje BRT + D-1..D-4; override por input)
  5. Para cada data: `fetch_anbima.py` -> exit 2 = skip; `compute_spreads.py`
  6. `build_dashboard.py`
  7. Imprime diff vs main + tamanho dos JSONs no GitHub Step Summary
  8. Upload do diretorio gerado como artifact (retencao 7 dias)
- **NAO commita nada**. Modo PoC, exclusivamente para validar paridade vs
  Routine durante a fase 1 da transicao.

Roda em paralelo a Routine e ao `b3_trades.yml`. Como nao escreve no repo,
nao colide com o `git pull --rebase` do workflow B3.

## 7. Transicao em 3 fases

### Fase 1 -- Validar (esta PR)
- Mergear `daily_update.yml` como PoC (nao commita).
- Deixar rodar 5-10 dias em paralelo com a Routine.
- Inspecionar artifacts: comparar `history/<data>.json` e `data/*.json`
  gerados pelo PoC com os commitados pela Routine. Devem bater
  (deterministico).
- Inspecionar Step Summary: `git status --porcelain` deve mostrar apenas
  arquivos esperados (`history/<hoje>.json` novo, `data/*.json` regenerados,
  `index.html` regenerado).

### Fase 2 -- Promover a producao (PR separado)
Depois de N execucoes da fase 1 batendo com a Routine:
- Adicionar step de commit & push ao `daily_update.yml`, espelhando o
  padrao do `b3_trades.yml`:
  - `git config user.name 'github-actions[bot]'`
  - `git add data/ history/ data.json index.html`
  - `git pull --rebase origin main` (evita race com `b3_trades.yml`)
  - `git push`
- Trocar `permissions: contents: read` para `contents: write` e remover
  `persist-credentials: false` do checkout.
- Manter Routine ativa em paralelo por +3 dias para confirmar que o commit
  do workflow nao quebra nada.

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
