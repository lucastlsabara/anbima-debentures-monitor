"""Fetch de NEGOCIOS CONSOLIDADOS de renda fixa da B3.

Diferenca de fonte:
  - fetch_b3_trades.py        -> negocio a negocio (trade-by-trade), endpoint
                                 /bdi/table/Trade. 1 linha = 1 operacao.
  - fetch_b3_trades_consolidated.py (este) -> negocios consolidados por
                                 instrumento/dia (vol total, preco medio, etc).

Endpoint publico (mesmo padrao do trade-by-trade):
  POST https://arquivos.b3.com.br/bdi/table/{TABLE_NAME}/{ini}/{fim}/{page}/{page_size}
Headers: Content-Type: application/json
Body: {}

IMPORTANTE — table name (probing automatico):
  O nome canonico da tabela /bdi/table/ para "Negocios Consolidados de Renda
  Fixa" nao foi confirmado em documentacao publica acessivel. PR #102 chutou
  `TradeInformationConsolidated`, que ate retorna 200 da B3 mas com rows
  vazios — provavelmente cobre listed/equities, nao renda fixa.

  Comportamento atual:
    - Se `B3_CONSOLIDATED_TABLE` (env) ou `--table` (CLI) for setado nao-vazio,
      usa APENAS esse nome (sem fallback). Util para teste via workflow_dispatch.
    - Caso contrario, probing automatico: tenta CANDIDATE_TABLES em ordem;
      primeira que retornar rows>0 vence e e' usada para o dia.
    - Se TODOS candidatos retornarem 0 rows (ou 403/404) para um dia, dia e'
      marcado como `unavailable`.

  Quando algum candidato comprovadamente funcionar em prod, simplificar:
  remover lista e travar default no nome correto.

Comportamento (analogo a fetch_b3_trades.py):
  - Janela: 5 dias uteis B3 mais recentes (HOJE se dia util + D-1..D-4).
  - Para cada dia com sucesso na API: DELETA arquivo existente + ESCREVE
    nova versao (atomico via .tmp + rename).
  - Falha de rede/timeout/5xx apos retry: preserva arquivo existente.
  - 403/404: pula (FDS/feriado/nao publicado), preserva arquivo.
  - Exit != 0 se qualquer dia falhar OU se TODOS os dias forem unavailable
    (sinal de tabela errada — falha barulhento, sem mascarar como FDS).
  - NUNCA gera dados sinteticos/fallback.

Persistencia: data/b3_trades_consolidated/{YYYY-MM-DD}.json + manifest.json.

Schema do JSON salvo:
  {
    "date": "YYYY-MM-DD",
    "fetched_at": ISO8601 UTC,
    "last_b3_update": ISO8601 da B3 (lastUpdateDate),
    "table_name": "<nome usado>",
    "columns": [...]  // colunas reportadas pela B3, se houver
    "rows": [[...], ...]  // values bruto retornado pela B3 (passthrough)
    "stats": { "n_rows": int, "n_pages_fetched": int }
  }

Por nao termos schema confirmado, NAO inferimos colunas — persistimos como
retornado. Build_dashboard.py / consumidores posteriores adaptam quando o
schema for confirmado.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from b3_calendar import is_b3_business_day, today_brt


# Candidatos pra probing automatico (1o que retornar rows>0 vence). Ordem
# baseada em heuristica do que se parece com "consolidated de renda fixa";
# inclui variantes en + pt-BR porque B3 mistura nomenclatura nas tabelas.
# Quando algum funcionar em prod, simplificar: travar `DEFAULT_TABLE_NAME` e
# remover lista.
CANDIDATE_TABLES: list[str] = [
    "TradeInformationConsolidatedAfterHours",
    "FixedIncomeTradesConsolidated",
    "FixedIncomeTradeInformationConsolidated",
    "DebenturesTradeInformationConsolidated",
    "TradeRegisterConsolidated",
    "TradesConsolidatedRegister",
    "ConsolidatedTradeInformation",
    "TradeInformationConsolidatedFixedIncome",
    "TradeInformationConsolidated",  # original PR #102 — 200 mas sem rows
    # Variantes pt-BR (B3 mistura idiomas em algumas tabelas):
    "NegocioConsolidado",
    "NegociosConsolidados",
    "NegociosConsolidadosRendaFixa",
    "ConsolidadoRendaFixa",
    "ConsolidadoNegocios",
]

# Override explicito (env ou --table): se setado nao-vazio, desativa probing
# e usa SOMENTE esse nome. Util pra teste via workflow_dispatch.
_ENV_OVERRIDE = os.environ.get("B3_CONSOLIDATED_TABLE", "").strip() or None

# Sentinel: passar None pra fetch_day/refresh = "use probing default";
# passar string explicita = "use somente esse nome, sem probing".
DEFAULT_TABLE_NAME: str | None = _ENV_OVERRIDE
B3_URL_TEMPLATE = (
    "https://arquivos.b3.com.br/bdi/table/{table}/{ini}/{fim}/{page}/{page_size}"
)
PAGE_SIZE = 500
TIMEOUT = 60
MAX_RETRIES = 3
SLEEP_BETWEEN_PAGES = 0.2
SLEEP_BETWEEN_DAYS = 1.0
REFRESH_WINDOW_DAYS = 5

REQUEST_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Origin": "https://arquivos.b3.com.br",
    "Referer": "https://arquivos.b3.com.br/",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

DATA_DIR = Path(__file__).parent / "data" / "b3_trades_consolidated"
MANIFEST_PATH = DATA_DIR / "manifest.json"


class B3UnavailableError(Exception):
    """B3 retornou 403/404: dia em FDS/feriado/ainda nao publicado."""


class SandboxBlockedError(Exception):
    """Resposta 403 com header x-deny-reason: bloqueio de allowlist do
    ambiente (sandbox), nao da B3. Falha real — nao silenciar como
    'indisponivel'."""


def _post_page(table: str, date_iso: str, page: int) -> dict:
    url = B3_URL_TEMPLATE.format(
        table=table, ini=date_iso, fim=date_iso, page=page, page_size=PAGE_SIZE
    )
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(url, headers=REQUEST_HEADERS, json={}, timeout=TIMEOUT)
            deny_reason = r.headers.get("x-deny-reason")
            if r.status_code == 403 and deny_reason:
                raise SandboxBlockedError(
                    f"HTTP 403 bloqueado pela sandbox (x-deny-reason={deny_reason}): "
                    f"host arquivos.b3.com.br fora da allowlist. Rode este script "
                    f"em ambiente com egress livre (ex: GitHub Actions)."
                )
            if r.status_code in (403, 404):
                raise B3UnavailableError(f"HTTP {r.status_code}")
            if r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except (B3UnavailableError, SandboxBlockedError):
            raise
        except (requests.Timeout, requests.HTTPError, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt == MAX_RETRIES - 1:
                break
            backoff = 2 ** attempt
            print(f"  [retry {attempt + 1}/{MAX_RETRIES}] {exc}; aguardando {backoff}s")
            time.sleep(backoff)
    raise RuntimeError(f"Falha apos {MAX_RETRIES} tentativas: {last_exc}")


def _extract_columns(payload: dict) -> list:
    """Tenta extrair lista de colunas do payload B3 (varia entre tabelas).

    Retorna lista vazia se nao encontrar — rows ainda sao persistidos brutos.
    """
    table = payload.get("table") or {}
    cols = table.get("columns") or payload.get("columns") or []
    if isinstance(cols, list):
        return cols
    return []


def _fetch_one_table(date_iso: str, table: str) -> tuple[list, list, str | None, int] | None:
    """Busca todas as paginas de uma tabela pra um dia.

    Retorna (rows, columns, last_b3_update, page_count) em caso de sucesso,
    ou None se a B3 respondeu 403/404 (tabela inexistente / dia nao publicado).

    Levanta RuntimeError em falha de rede pos-retries.
    Levanta SandboxBlockedError se sandbox bloqueou o host.
    """
    try:
        first = _post_page(table, date_iso, 1)
    except B3UnavailableError:
        return None

    tbl = first.get("table") or {}
    page_count = int(tbl.get("pageCount") or 0)
    last_b3_update = first.get("lastUpdateDate")
    columns = _extract_columns(first)

    rows: list = []
    rows.extend(tbl.get("values") or [])

    for page in range(2, page_count + 1):
        time.sleep(SLEEP_BETWEEN_PAGES)
        resp = _post_page(table, date_iso, page)
        rows.extend(((resp.get("table") or {}).get("values")) or [])

    return rows, columns, last_b3_update, max(page_count, 1)


def _candidates_for(table: str | None) -> list[str]:
    """Define ordem de tentativa.

    - Se `table` for nome explicito (string nao-vazia), usa SOMENTE ele.
    - Se None: retorna CANDIDATE_TABLES inteiro pra probing.
    """
    if table:
        return [table]
    return list(CANDIDATE_TABLES)


def fetch_day(date_iso: str, table: str | None = DEFAULT_TABLE_NAME) -> dict:
    """Busca consolidated de UM dia (delete+write atomico).

    Probing: tenta candidatos em ordem (ou somente `table` se override).
    1o candidato com rows>0 vence, persiste, retorna status='updated'.
    Se todos retornarem 0 rows ou 403/404, retorna status='unavailable'
    (arquivo existente preservado).

    Retorna dict com `status` em {'updated', 'unavailable'}.
    Levanta RuntimeError em falha de rede pos-retries — chamador trata
    sem alterar arquivo (delete so ocorre apos sucesso confirmado).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{date_iso}.json"

    candidates = _candidates_for(table)
    tried: list[str] = []
    last_zero_meta: tuple[str, list, str | None, int] | None = None

    for cand in candidates:
        tried.append(cand)
        result = _fetch_one_table(date_iso, cand)
        if result is None:
            print(f"[{date_iso}] tabela={cand} -> 403/404 (inexistente ou nao publicada)")
            continue
        rows, columns, last_b3_update, page_count = result
        if not rows:
            print(f"[{date_iso}] tabela={cand} -> 200 mas 0 rows")
            # Guarda metadata caso TODOS candidatos retornem 0 rows: ainda
            # assim persistimos algo (com o ultimo tentado) pra deixar visivel
            # qual nome foi probado por ultimo. status fica 'unavailable'.
            last_zero_meta = (cand, columns, last_b3_update, page_count)
            continue

        # Encontrou tabela com rows. Persiste e retorna.
        payload = {
            "date": date_iso,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "last_b3_update": last_b3_update,
            "table_name": cand,
            "columns": columns,
            "rows": rows,
            "stats": {
                "n_rows": len(rows),
                "n_pages_fetched": page_count,
            },
        }

        tmp_path = out_path.parent / f"{date_iso}.json.tmp"
        tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        if out_path.exists():
            out_path.unlink()
        tmp_path.replace(out_path)
        size_mb = out_path.stat().st_size / (1024 * 1024)

        print(
            f"[{date_iso}] OK tabela={cand}: {len(rows):,} linha(s), "
            f"{size_mb:.2f} MB (tentadas: {tried})".replace(",", ".")
        )

        _update_manifest(date_iso, len(rows), out_path.name)
        return {
            "date": date_iso,
            "status": "updated",
            "n_rows": len(rows),
            "table_name": cand,
            "tried": tried,
        }

    # Nenhum candidato deu rows>0. Nao sobrescreve arquivo existente — so
    # reporta unavailable. Refresh_recent_days agrega e decide exit code.
    print(
        f"[{date_iso}] UNAVAILABLE: nenhum candidato retornou rows. "
        f"Tentados: {tried}"
    )
    return {
        "date": date_iso,
        "status": "unavailable",
        "n_rows": 0,
        "tried": tried,
        "last_zero_table": (last_zero_meta[0] if last_zero_meta else None),
    }


def _update_manifest(date_iso: str, n_rows: int, filename: str) -> None:
    if MANIFEST_PATH.exists():
        try:
            manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
    else:
        manifest = {}

    dates = manifest.get("dates") or []
    by_date = {entry["date"]: entry for entry in dates}
    by_date[date_iso] = {
        "date": date_iso,
        "n_rows": n_rows,
        "filename": filename,
    }
    dates_sorted = sorted(by_date.values(), key=lambda e: e["date"])

    manifest = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "earliest_date": dates_sorted[0]["date"] if dates_sorted else None,
        "latest_date": dates_sorted[-1]["date"] if dates_sorted else None,
        "dates": dates_sorted,
        "total_rows": sum(e["n_rows"] for e in dates_sorted),
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, separators=(",", ":")), encoding="utf-8"
    )


def _last_n_b3_business_days(n: int, today: date | None = None) -> list[date]:
    if today is None:
        today = today_brt()
    out: list[date] = []
    d = today if is_b3_business_day(today) else today - timedelta(days=1)
    while len(out) < n:
        if is_b3_business_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def refresh_recent_days(n: int = REFRESH_WINDOW_DAYS, table: str | None = DEFAULT_TABLE_NAME) -> int:
    days = _last_n_b3_business_days(n)
    candidates_preview = _candidates_for(table)
    print(
        f"Refresh consolidated dos ultimos {len(days)} dias uteis B3: "
        f"{days[0]} a {days[-1]} (candidatos={candidates_preview})"
    )

    updated = 0
    unavailable = 0
    failed = 0
    winning_tables: set[str] = set()

    for i, d in enumerate(days):
        date_iso = d.isoformat()
        try:
            result = fetch_day(date_iso, table=table)
            if result["status"] == "updated":
                updated += 1
                if result.get("table_name"):
                    winning_tables.add(result["table_name"])
            else:
                unavailable += 1
        except Exception as exc:
            print(
                f"[{date_iso}] FALHA apos retries: {exc} - "
                f"mantendo arquivo existente (se houver)"
            )
            failed += 1
        if i < len(days) - 1:
            time.sleep(SLEEP_BETWEEN_DAYS)

    print()
    print(
        f"Resumo: {updated} atualizado(s), {unavailable} indisponivel(eis) "
        f"(FDS/feriado/nao publicado/tabela errada), {failed} falha(s)"
    )
    if winning_tables:
        print(f"Tabela(s) vencedora(s): {sorted(winning_tables)}")

    # Falha barulhento se nenhuma tabela retornou rows na janela inteira.
    # Distingue "rede falhou" (failed>0) de "tabela errada" (updated=0
    # e failed=0) — ambos retornam exit 1, mas a msg diferencia.
    if updated == 0 and failed == 0 and unavailable > 0:
        print(
            "ERRO: nenhum candidato retornou rows em NENHUM dia da janela. "
            "Provavel causa: nome de tabela errado. Disparar workflow_dispatch "
            "com input `consolidated_table_name` para testar nomes individuais."
        )
        return 1
    return 0 if failed == 0 else 1


def _parse_argv(argv: list[str]) -> tuple[str | None, str | None]:
    """Retorna (date_iso_or_None, table_name_or_None).

    table_name=None significa "use probing automatico". Se --table for
    passado nao-vazio (ou env B3_CONSOLIDATED_TABLE setada), override fica.

    Aceita:
      python fetch_b3_trades_consolidated.py
      python fetch_b3_trades_consolidated.py YYYY-MM-DD
      python fetch_b3_trades_consolidated.py --table Nome
      python fetch_b3_trades_consolidated.py YYYY-MM-DD --table Nome
    """
    table: str | None = DEFAULT_TABLE_NAME
    date_iso: str | None = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--table" and i + 1 < len(argv):
            cli_val = argv[i + 1].strip()
            table = cli_val or None
            i += 2
            continue
        if date_iso is None:
            date_iso = a
            i += 1
            continue
        raise SystemExit(f"argumento inesperado: {a}")
    return date_iso, table


def main(argv: list[str]) -> int:
    date_iso, table = _parse_argv(argv)
    if date_iso is not None:
        date.fromisoformat(date_iso)
        try:
            fetch_day(date_iso, table=table)
        except Exception as exc:
            print(f"FALHA: {exc}")
            return 1
        return 0
    return refresh_recent_days(table=table)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
