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

## 7. Transicao em fases

> **Status (mai/2026)**: fases 1, 2 e 4 MERGED. Fase 3 (desativar Routine no
> claude.ai) ainda eh acao manual fora do repo. A producao hoje roda 100%
> pelo workflow `anbima_b3_probe.yml` (Fase 4); `daily_update.yml` e
> `b3_trades.yml` foram rebaixados para backup manual (`workflow_dispatch`
> apenas, sem cron). Fase 4.1 (PR atual) adicionou idempotencia diaria por
> marker `data/.probe_state.json` -- ver secao 9 abaixo.

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

### Fase 4 -- Probe horario unificado (ANBIMA + B3, PR #133, MERGED)

Problema motivador: cron fixo 23h BRT em `daily_update.yml` e `b3_trades.yml`.
Quando a ANBIMA atrasa publicacao (pos-feriado, dia comprido), o workflow
roda e captura parcial ou zero -- e so tenta de novo 24h depois. Sem retry
inteligente intra-dia.

Solucao: novo workflow `.github/workflows/anbima_b3_probe.yml` que cicla
de hora em hora 19h-05h BRT (cron `'0 0-8,22-23 * * *'` UTC, depois
ajustado em duas variantes seg-sex / ter-sab para nao disparar em fds;
ver Fase 4.2 para o cron atual de 3 slots fixos) e so importa quando
TODOS os 5 arquivos do dia estao publicados:

1. ANBIMA Debentures (`db<YYMMDD>.txt`)
2. ANBIMA ETTJ (`CZ-down.asp?Dt_Ref=...`)
3. ANBIMA TPF (`ms<YYMMDD>.txt`)
4. B3 Trade (POST `/bdi/table/Trade/<date>/<date>/1/1`)
5. B3 ConsolidatedRecords (POST `/bdi/table/ConsolidatedRecords/<date>/<date>/1/1`)

Fluxo por execucao:
- Determina dia alvo: se hora BRT >= 19h, alvo = HOJE; senao alvo = ONTEM.
- Guard B3: se nao for dia util B3, sai 0 sem nada.
- Probe via `scripts/probe_files.py --date <alvo>`. Exit 0 se todos OK;
  exit 2 se algum faltando (workflow segue verde, proximo cron tenta de novo);
  exit 1 em erro real (rede, sandbox, etc).
- Se todos OK: roda gap-fill ANBIMA (`scripts/list_target_dates.py` ->
  loop `fetch_anbima.py` skip se `history/<D>.json` ja existe), overwrite
  B3 (`fetch_b3_trades.py` + `fetch_b3_trades_consolidated.py`),
  `podar_historico.py`, `build_dashboard.py`, commit + push.

Workflows antigos:
- `daily_update.yml`: cron removido, vira backup manual (workflow_dispatch).
- `b3_trades.yml`: cron removido, vira backup manual (workflow_dispatch).

Recuperacao de dias atipicos: dia D que falha (ANBIMA atrasou alem das 05h
BRT do dia D+1) eh recuperado automaticamente no ciclo do dia D+1 porque
o gap-fill ANBIMA processa `[D+1, D, D-1, D-2, D-3]` e a janela 5du do B3
sobrescreve. Sem manual.

Reuso de codigo: zero alteracao em `fetch_anbima.py`, `fetch_b3_trades.py`,
`fetch_b3_trades_consolidated.py`, `compute_spreads.py`, `build_dashboard.py`,
`b3_calendar.py`, `scripts/list_target_dates.py`, `scripts/podar_historico.py`.
Novidades: `scripts/probe_files.py` (HEAD + GET range para ANBIMA, POST
page=1 size=1 para B3) e o workflow `anbima_b3_probe.yml`.

### Fase 4.1 -- Idempotencia diaria por marker (PR atual)

Problema motivador: apos um run bem-sucedido as 19h BRT, os runs das 20h,
21h, ..., 05h continuavam executando todo o pipeline (probe HTTP + B3
overwrite + build), gastando Actions minutes e batendo nas APIs sem
necessidade. Probe nao detecta mudanca de versao do arquivo, entao o
overwrite ate refazia o mesmo trabalho.

Solucao: marker `data/.probe_state.json` versionado em git com schema:

```json
{
  "last_successful_date": "YYYY-MM-DD",
  "last_success_ts": "YYYY-MM-DDTHH:MM:SS-03:00"
}
```

Step `Check already-completed` no workflow le o marker antes do probe:
- Se `last_successful_date == target_date`: encerra verde sem rodar o
  resto (`steps.idempotencia.outputs.already_done=true` propaga para
  todos os steps subsequentes via `if:`).
- Se nao bate, marker ausente, ou JSON invalido: prossegue normal.

Apos podar historico (e antes de detectar mudancas), o step
`Atualiza marker de idempotencia` reescreve `data/.probe_state.json` com
o dia alvo atual + timestamp BRT. O proprio marker entra como diff e
garante commit (pelo menos do marker) sempre que o pipeline completar.

`workflow_dispatch` ganha input `force` (boolean) que ignora o marker --
util para reprocessar manualmente um dia atipico.

Importante: marker eh por **dia_alvo**, nao por timestamp absoluto. Se o
dia_alvo mudar entre runs (ex: 05h BRT vira ONTEM e 19h BRT vira HOJE),
o marker nao bloqueia o novo ciclo. A recuperacao retroativa do
gap-fill ANBIMA + overwrite janela B3 continua intocada.

### Fase 4.2 -- Migracao probe horario -> probe 3 slots fixos (PR atual)

Problema motivador: o cron original cobria 11 triggers por dia util B3
(19h, 20h, 21h, 22h, 23h, 00h, 01h, 02h, 03h, 04h, 05h BRT). Apos uma
execucao bem-sucedida (tipicamente 21h-23h, quando ANBIMA fecha
publicacao), os ~9 slots restantes apenas leem o marker e encerram em
~37s -- ainda assim cada um consome Actions minutes, polui o log de
operacoes e dispara webhooks/notificacoes ruido.

Solucao: trocar o cron para 3 horarios fixos por dia util B3:
- 21h BRT (00h UTC do dia seguinte) -- primeira tentativa, alvo = HOJE
- 23h BRT (02h UTC do dia seguinte) -- retry se 21h falhou
- 05h BRT (08h UTC do dia seguinte) -- ultima tentativa do ciclo,
  alvo = ONTEM (= mesma ref date dos slots das 21h/23h da vespera)

Cron novo: `'0 0,2,8 * * 2-6'` (UTC). Day-of-week 2-6 (ter-sab UTC)
garante que a ref date BRT cai sempre em seg-sex BRT, eliminando
triggers de dom/seg UTC que iriam para fim de semana e seriam barrados
pelo guard de qualquer jeito.

Mapeamento (BRT = UTC-3):
- Slot 00 UTC dia X (ter-sab) -> 21h BRT dia X-1 -> ref date BRT = X-1 (HOJE)
- Slot 02 UTC dia X (ter-sab) -> 23h BRT dia X-1 -> ref date BRT = X-1 (HOJE)
- Slot 08 UTC dia X (ter-sab) -> 05h BRT dia X   -> ref date BRT = X-1 (ONTEM)

Para todos os 3 slots, ref date BRT = X-1 (UTC). Pra essa ref ser
seg-sex BRT, X (UTC) precisa ser ter-sab (2-6). OK.

Motivacao quantitativa:
- Actions minutes: de ate 11 triggers/dia util para 3 (reducao ~73%).
- Triggers ruidosos: ~9 no-ops/dia util eliminados.
- Log de operacoes: 3 execucoes/dia util tornam o historico de Actions
  imediatamente legivel (ANBIMA publica entre 19h-22h BRT na maioria
  dos dias; 21h captura o caso comum, 23h cobre dia comprido, 05h cobre
  publicacao tardia ja na madrugada).

Garantias preservadas:
- Mesma probabilidade de sucesso eventual: o slot das 05h cobre o caso
  extremo em que ANBIMA so publica depois das 23h BRT. Se mesmo o 05h
  falhar, o dia eh descartado e capturado retroativamente no ciclo do
  proximo dia util B3 pelo gap-fill ANBIMA (`scripts/list_target_dates.py`)
  + overwrite janela 5 du B3 (comportamento inalterado).
- Marker `data/.probe_state.json` continua barrando re-execucoes do
  mesmo dia_alvo (sucesso as 21h -> 23h e 05h encerram em ~37s).
- Guard B3 continua ativo: feriados B3 em dia util seg-sex passam pelo
  cron (que so olha day-of-week, nao calendario B3) e sao cortados em
  runtime com exit 0.
- `workflow_dispatch` com `force=true` ou `target_date` preservado.
- Zero alteracao em scripts Python (probe_files.py, fetch_anbima.py,
  fetch_b3_*.py, compute_spreads.py, build_dashboard.py,
  list_target_dates.py, b3_calendar.py).

### Fase 5 -- Refresh intraday Trade-by-Trade (PR atual)

Motivacao: a B3 publica Trade-by-Trade a cada 15min durante o pregao,
mas o canonico das 21h/23h/05h fica preso aguardando os 5 arquivos do
dia (em particular `ConsolidatedRecords`, que so sai ao fim do
pregao). Resultado: o site nao reflete trades intraday ate o ciclo
noturno.

Solucao: novo workflow `.github/workflows/b3_trades_intraday.yml`,
paralelo ao canonico, que captura SOMENTE Trade-by-Trade a cada 30min
durante o pregao. Reusa `fetch_b3_trades.py <data>` em modo unitario
(zero alteracao no script). Apos o fetch, detecta mudanca via
`git status --porcelain` (cobre arquivo novo + modificado), roda
`build_dashboard.py`, faz commit com prefixo
`chore: B3 intraday refresh ...` e push.

Janela: 10h-19h BRT seg-sex (19 disparos/dia util max). Cron UTC em 2
linhas para cravar fim em 19h:
- `0,30 13-21 * * 1-5` (UTC) = 18 slots, 10:00 ate 18:30 BRT.
- `0 22 * * 1-5` (UTC) = 1 slot adicional em 19:00 BRT.

Por que 2 linhas: GitHub Actions cron nao suporta limitar hora de fim
parcial. Se usasse `0,30 13-22 * * 1-5` incluiria 19:30 BRT (fora da
janela pedida). Por que dia-da-semana 1-5: cada slot UTC mapeia para o
MESMO dia da semana BRT (BRT esta atras de UTC, entao 13h UTC seg =
10h BRT seg; 22h UTC sex = 19h BRT sex — sem virada de dia da semana).

Convivencia com canonico:
- Janelas nao se sobrepoem (gap de 2h entre 19:00 BRT intraday e
  21:00 BRT canonico).
- Concurrency group `anbima-b3-pipeline` adicionado a AMBOS os
  workflows (intraday + canonico) como safety net: se algum dia
  janelas se sobreporem (mudanca futura de cron, retry manual etc),
  fila de execucao evita dois push concorrentes em main.

Comportamento em D+1: canonico das 21h sobrescreve trade-by-trade do
dia com versao final (igual ou marginalmente maior, ja que B3 fecha o
arquivo no fim do pregao) e adiciona ConsolidatedRecords + ANBIMA +
ETTJ + TPF.

Restricoes invioladas:
- NAO toca `data/.probe_state.json` (marker pertence ao canonico).
- NAO baixa ANBIMA, ETTJ, TPF nem ConsolidatedRecords.
- NAO poda historico (`data/b3_trades/` antigos, `data/dispersion/`,
  `history/`) — funcao do canonico via `scripts/podar_historico.py`.
- NAO gera dados sinteticos. Falha de rede / 5xx -> workflow falha
  barulhento (`fetch_b3_trades.py` ja sai nao-zero). 403/404
  (FDS/feriado/dia ainda nao publicado) eh pulado em silencio pelo
  proprio script, preservando o arquivo existente.

Limitacao explicita: o intraday SO atualiza
`data/b3_trades/<HOJE>.json` + rebuild do dashboard. Setor x Prazo,
Heatmap, Dispersao, Visao Geral, Titulos Publicos continuam
refletindo o ultimo dia importado pelo canonico ate o proximo ciclo
dele.

Zero alteracao em scripts Python (`fetch_b3_trades.py`,
`build_dashboard.py`, `b3_calendar.py`).

## 8. Regras invioladas

- Nenhum dado sintetico. Se ANBIMA quebrar, workflow falha barulhento
  (exit code != 0 em qualquer data com erro nao-404).
- `b3_trades.yml` NAO foi tocado.
- `compute_spreads.py`, `build_dashboard.py` e schema dos JSONs intactos.
- Routine no claude.ai NAO foi desativada (Lucas faz na fase 3).
