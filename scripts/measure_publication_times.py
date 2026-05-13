#!/usr/bin/env python3
"""Mede horario de publicacao dos 5 arquivos diarios (ANBIMA + B3) nos ultimos
5 dias uteis B3.

Para cada (data alvo, arquivo), tenta capturar o horario em que o arquivo foi
disponibilizado pelo servidor:

  1. HEAD (ANBIMA): le header `Last-Modified`.
  2. GET com Range bytes=0-1023 (fallback ANBIMA): le header `Last-Modified`.
  3. POST (B3): le header `Last-Modified` da resposta; se nao houver, procura
     no payload JSON por campos como `lastUpdate`, `dataAtualizacao`,
     `lastUpdateDate`, `updateDate`.

Saida (stdout):
  - Tabela markdown ordenada por data (mais recente primeiro), uma linha por
    (data, arquivo), com colunas Data | Arquivo | Last-Modified (raw) | Horario BRT.
  - Sumario por arquivo: mediana e horario mais tardio observado (em BRT).

Uso:
    python3 scripts/measure_publication_times.py
    python3 scripts/measure_publication_times.py --date 2026-05-13

Script puramente de leitura -- nao altera estado do repo.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import requests

# Permite importar modulos do raiz do repo quando rodado direto.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from b3_calendar import is_b3_business_day, today_brt  # noqa: E402

# Reusamos a mesma UA fallback do fetch_anbima.py: a ANBIMA bloqueia UAs
# nao-browser, entao em probes pontuais usamos direto a UA Mozilla.
MOZILLA_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 30
HTTP_RETRY_BACKOFF_SEC = (2, 4, 8, 16)
HTTP_RETRY_ATTEMPTS = 4

DB_URL = "https://www.anbima.com.br/informacoes/merc-sec-debentures/arqs/db{yymmdd}.txt"
TITPUB_URL = "https://www.anbima.com.br/informacoes/merc-sec/arqs/ms{yymmdd}.txt"
ETTJ_URL = "https://www.anbima.com.br/informacoes/est-termo/CZ-down.asp?Dt_Ref={dd}/{mm}/{yyyy}&saida=csv"
B3_BASE_URL = "https://arquivos.b3.com.br/bdi/table"

BRT = ZoneInfo("America/Sao_Paulo")
UTC = ZoneInfo("UTC")

# Campos comuns em payloads B3/ANBIMA que carregam timestamp de publicacao.
B3_TIMESTAMP_KEYS = (
    "lastUpdate",
    "lastUpdateDate",
    "updateDate",
    "dataAtualizacao",
    "dataReferencia",
    "dateReference",
    "lastModified",
)


def _last_n_business_days(today: date, n: int) -> list[date]:
    """N ultimos dias uteis B3 estritamente anteriores a `today`, mais recente primeiro."""
    out: list[date] = []
    d = today - timedelta(days=1)
    while len(out) < n:
        if is_b3_business_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return out


def _parse_http_date(raw: str) -> datetime | None:
    """Parse Last-Modified (RFC 7231) -> datetime tz-aware. None se invalido."""
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    # parsedate_to_datetime pode retornar naive (sem tz) se faltar tz no header;
    # nesses casos, assumimos UTC (convencao HTTP).
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _to_brt_str(dt: datetime | None) -> str:
    if dt is None:
        return "N/A"
    return dt.astimezone(BRT).strftime("%Y-%m-%d %H:%M:%S %Z")


def _http_with_retry(
    fn: Callable[[], requests.Response],
    label: str,
) -> requests.Response | None:
    """Executa `fn()` com retry exponencial em falhas transitorias. Retorna a
    Response ainda que com status >= 400 (caller decide). Devolve None se
    estourar retries com erro de rede.
    """
    last_exc: Exception | None = None
    for attempt in range(HTTP_RETRY_ATTEMPTS):
        try:
            return fn()
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            if attempt < HTTP_RETRY_ATTEMPTS - 1:
                wait = HTTP_RETRY_BACKOFF_SEC[attempt]
                print(
                    f"[medir] {label}: {type(exc).__name__}; "
                    f"retry em {wait}s (tentativa {attempt + 2}/{HTTP_RETRY_ATTEMPTS})",
                    file=sys.stderr,
                )
                time.sleep(wait)
    print(f"[medir] {label}: falha apos retries ({last_exc})", file=sys.stderr)
    return None


def _measure_anbima_url(url: str, label: str) -> tuple[str, datetime | None, str]:
    """HEAD + fallback GET Range. Retorna (raw_last_modified, dt_parsed, detalhe)."""
    r = _http_with_retry(
        lambda: requests.head(
            url,
            headers={"User-Agent": MOZILLA_UA},
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        ),
        f"HEAD {label}",
    )
    if r is not None and r.status_code < 400:
        raw = r.headers.get("Last-Modified", "")
        if raw:
            return raw, _parse_http_date(raw), f"HEAD HTTP {r.status_code}"
        # HEAD OK mas sem header -- tenta GET range pra ver se vem la.
    head_detail = f"HEAD HTTP {r.status_code}" if r is not None else "HEAD falhou"

    r2 = _http_with_retry(
        lambda: requests.get(
            url,
            headers={"User-Agent": MOZILLA_UA, "Range": "bytes=0-1023"},
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        ),
        f"GET range {label}",
    )
    if r2 is None:
        return "", None, f"{head_detail}; GET range falhou"
    if r2.status_code >= 400:
        return "", None, f"{head_detail}; GET range HTTP {r2.status_code}"
    raw = r2.headers.get("Last-Modified", "")
    if raw:
        return raw, _parse_http_date(raw), f"GET range HTTP {r2.status_code}"
    return "", None, f"{head_detail}; GET range HTTP {r2.status_code} sem Last-Modified"


def measure_anbima_debentures(target: date) -> tuple[str, datetime | None, str]:
    yymmdd = target.strftime("%y%m%d")
    return _measure_anbima_url(DB_URL.format(yymmdd=yymmdd), "Debentures")


def measure_anbima_tpf(target: date) -> tuple[str, datetime | None, str]:
    yymmdd = target.strftime("%y%m%d")
    return _measure_anbima_url(TITPUB_URL.format(yymmdd=yymmdd), "TPF")


def measure_anbima_ettj(target: date) -> tuple[str, datetime | None, str]:
    """ETTJ usa endpoint .asp dinamico. HEAD tipicamente nao retorna
    Last-Modified util; tentamos HEAD primeiro, depois GET full.
    """
    url = ETTJ_URL.format(
        dd=target.strftime("%d"),
        mm=target.strftime("%m"),
        yyyy=target.strftime("%Y"),
    )
    r = _http_with_retry(
        lambda: requests.head(
            url,
            headers={"User-Agent": MOZILLA_UA},
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        ),
        "HEAD ETTJ",
    )
    if r is not None and r.status_code < 400:
        raw = r.headers.get("Last-Modified", "")
        if raw:
            return raw, _parse_http_date(raw), f"HEAD HTTP {r.status_code}"
    head_detail = f"HEAD HTTP {r.status_code}" if r is not None else "HEAD falhou"

    r2 = _http_with_retry(
        lambda: requests.get(
            url,
            headers={"User-Agent": MOZILLA_UA},
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        ),
        "GET ETTJ",
    )
    if r2 is None:
        return "", None, f"{head_detail}; GET falhou"
    if r2.status_code >= 400:
        return "", None, f"{head_detail}; GET HTTP {r2.status_code}"
    raw = r2.headers.get("Last-Modified", "")
    if raw:
        return raw, _parse_http_date(raw), f"GET HTTP {r2.status_code}"
    return "", None, f"{head_detail}; GET HTTP {r2.status_code} sem Last-Modified"


def _find_timestamp_in_payload(obj, depth: int = 0):
    """Procura recursivamente (profundidade limitada) por chaves de timestamp
    em dict/list aninhados. Retorna (chave_encontrada, valor) ou (None, None).
    """
    if depth > 4:
        return None, None
    if isinstance(obj, dict):
        for k in B3_TIMESTAMP_KEYS:
            if k in obj and isinstance(obj[k], str) and obj[k]:
                return k, obj[k]
        for v in obj.values():
            found = _find_timestamp_in_payload(v, depth + 1)
            if found[0] is not None:
                return found
    elif isinstance(obj, list):
        for v in obj[:5]:  # nao precisa varrer lista inteira
            found = _find_timestamp_in_payload(v, depth + 1)
            if found[0] is not None:
                return found
    return None, None


def _parse_iso_loose(raw: str) -> datetime | None:
    """Aceita ISO 8601 com ou sem 'Z'; se naive, assume BRT (B3 publica em
    horario local). Retorna tz-aware ou None.
    """
    if not raw:
        return None
    s = raw.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BRT)
    return dt


def measure_b3_table(table: str, target: date) -> tuple[str, datetime | None, str]:
    """POST {B3_BASE_URL}/{table}/{date}/{date}/1/1 body {}.

    Captura Last-Modified do header; se ausente, varre o JSON em busca de
    campos de timestamp conhecidos.
    """
    date_iso = target.isoformat()
    url = f"{B3_BASE_URL}/{table}/{date_iso}/{date_iso}/1/1"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Origin": "https://arquivos.b3.com.br",
        "Referer": "https://arquivos.b3.com.br/",
        "User-Agent": MOZILLA_UA,
    }
    r = _http_with_retry(
        lambda: requests.post(url, headers=headers, json={}, timeout=HTTP_TIMEOUT),
        f"POST B3 {table}",
    )
    if r is None:
        return "", None, "POST falhou"
    if r.status_code >= 400:
        return "", None, f"POST HTTP {r.status_code}"
    raw = r.headers.get("Last-Modified", "")
    dt = _parse_http_date(raw)
    if dt is not None:
        return raw, dt, f"POST HTTP {r.status_code} (header Last-Modified)"

    # Sem Last-Modified: tenta extrair do payload JSON.
    try:
        payload = r.json()
    except ValueError:
        return "", None, f"POST HTTP {r.status_code} sem Last-Modified, body nao-JSON"
    key, val = _find_timestamp_in_payload(payload)
    if key and val:
        dt = _parse_iso_loose(val)
        return val, dt, f"POST HTTP {r.status_code} (payload.{key})"
    return "", None, f"POST HTTP {r.status_code} sem Last-Modified nem timestamp no payload"


ARQUIVOS: list[tuple[str, Callable[[date], tuple[str, datetime | None, str]]]] = [
    ("ANBIMA Debentures", measure_anbima_debentures),
    ("ANBIMA TPF", measure_anbima_tpf),
    ("ANBIMA ETTJ", measure_anbima_ettj),
    ("B3 Trade", lambda d: measure_b3_table("Trade", d)),
    ("B3 ConsolidatedRecords", lambda d: measure_b3_table("ConsolidatedRecords", d)),
]


def _median_time_brt(dts: list[datetime]) -> str:
    """Mediana do horario do dia (hh:mm:ss) entre os dts (em BRT)."""
    if not dts:
        return "N/A"
    seconds = []
    for dt in dts:
        local = dt.astimezone(BRT)
        seconds.append(local.hour * 3600 + local.minute * 60 + local.second)
    med = int(statistics.median(seconds))
    h, rem = divmod(med, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d} BRT"


def _latest_time_brt(dts: list[datetime]) -> str:
    """Horario mais tardio do dia observado (hh:mm:ss BRT) + data ISO completa."""
    if not dts:
        return "N/A"
    best = None
    best_secs = -1
    for dt in dts:
        local = dt.astimezone(BRT)
        secs = local.hour * 3600 + local.minute * 60 + local.second
        if secs > best_secs:
            best_secs = secs
            best = local
    assert best is not None
    return best.strftime("%Y-%m-%d %H:%M:%S %Z")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--date",
        default=None,
        help="YYYY-MM-DD usado como HOJE (default: hoje em America/Sao_Paulo).",
    )
    p.add_argument(
        "--n",
        type=int,
        default=5,
        help="Quantos ultimos dias uteis B3 medir (default: 5).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.date:
        today = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        today = today_brt()

    datas = _last_n_business_days(today, args.n)
    print(
        f"[medir] hoje={today.isoformat()} | medindo {len(datas)} dia(s) uteis B3: "
        + ", ".join(d.isoformat() for d in datas),
        file=sys.stderr,
    )

    # results: lista de dicts {data, arquivo, raw, dt, detalhe}
    results: list[dict] = []
    for d in datas:
        for label, fn in ARQUIVOS:
            print(f"[medir] {d.isoformat()} | {label}", file=sys.stderr)
            try:
                raw, dt, detalhe = fn(d)
            except Exception as exc:  # noqa: BLE001
                raw, dt, detalhe = "", None, f"erro inesperado: {exc}"
                print(f"[medir]   ERRO: {exc}", file=sys.stderr)
            results.append({
                "data": d,
                "arquivo": label,
                "raw": raw,
                "dt": dt,
                "detalhe": detalhe,
            })

    # Tabela markdown ordenada por data (mais recente primeiro) e ordem fixa de arquivos.
    print()
    print("## Horarios de publicacao (ultimos {} dias uteis B3)".format(len(datas)))
    print()
    print("| Data | Arquivo | Last-Modified (raw) | Horario BRT | Detalhe |")
    print("|---|---|---|---|---|")
    ordem_arq = [lbl for lbl, _ in ARQUIVOS]
    for d in datas:  # ja em ordem decrescente
        for label in ordem_arq:
            rec = next(
                r for r in results
                if r["data"] == d and r["arquivo"] == label
            )
            raw = rec["raw"] or "N/A"
            brt = _to_brt_str(rec["dt"])
            detalhe = rec["detalhe"]
            print(f"| {d.isoformat()} | {label} | {raw} | {brt} | {detalhe} |")

    # Sumario por arquivo.
    print()
    print("## Sumario por arquivo")
    print()
    print("| Arquivo | Amostras | Mediana (hora do dia BRT) | Mais tardio observado |")
    print("|---|---|---|---|")
    for label in ordem_arq:
        dts = [r["dt"] for r in results if r["arquivo"] == label and r["dt"] is not None]
        amostras = len(dts)
        med = _median_time_brt(dts)
        tardio = _latest_time_brt(dts)
        print(f"| {label} | {amostras}/{len(datas)} | {med} | {tardio} |")

    return 0


if __name__ == "__main__":
    sys.exit(main())
