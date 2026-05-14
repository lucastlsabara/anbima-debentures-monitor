"""Helpers compartilhados para a API publica de tabelas da B3.

Endpoint padrao:
  POST {HTTP_BASE_URL}/{table}/{ini}/{fim}/{page}/{page_size}

`table` muda entre datasets (Trade trade-by-trade, ConsolidatedRecords
consolidados). Body sempre `{}`.

Convencao de erros: 403/404 viram B3UnavailableError (FDS/feriado/dia
ainda nao publicado — caller pula). 403 com header `x-deny-reason` vira
SandboxBlockedError (bloqueio de allowlist do ambiente, nao da B3 —
caller deve propagar).
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import requests

from _http_utils import request_with_retry
from b3_calendar import is_b3_business_day, today_brt


HTTP_BASE_URL = "https://arquivos.b3.com.br/bdi/table"

B3_HEADERS = {
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

DEFAULT_MAX_RETRIES = 3


class B3UnavailableError(Exception):
    """B3 retornou 403/404: dia em FDS/feriado/ainda nao publicado."""


class SandboxBlockedError(Exception):
    """Resposta 403 com header x-deny-reason: bloqueio de allowlist do
    ambiente (sandbox), nao da B3. Falha real — nao silenciar como
    'indisponivel'."""


def post_page(
    table: str,
    date_iso: str,
    page: int,
    page_size: int,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """POST {HTTP_BASE_URL}/{table}/{date}/{date}/{page}/{page_size}.

    Toda chamada HTTP passa por `request_with_retry` (timeout default
    (15, 120)s + retry exponencial 2/4/8s em Timeout/ConnectionError/5xx).
    403 com x-deny-reason -> SandboxBlockedError; 403/404 ->
    B3UnavailableError; 5xx persistente -> requests.HTTPError propagado.
    """
    url = (
        f"{HTTP_BASE_URL}/{table}/{date_iso}/{date_iso}/{page}/{page_size}"
    )
    try:
        r = request_with_retry(
            "POST",
            url,
            headers=B3_HEADERS,
            json={},
            max_attempts=max_retries,
        )
    except requests.HTTPError as exc:
        resp = exc.response
        if resp is not None:
            deny_reason = resp.headers.get("x-deny-reason")
            if resp.status_code == 403 and deny_reason:
                raise SandboxBlockedError(
                    f"HTTP 403 bloqueado pela sandbox (x-deny-reason={deny_reason}): "
                    f"host arquivos.b3.com.br fora da allowlist. Rode este script "
                    f"em ambiente com egress livre (ex: GitHub Actions)."
                ) from exc
            if resp.status_code in (403, 404):
                raise B3UnavailableError(f"HTTP {resp.status_code}") from exc
        raise
    return r.json()


def last_n_business_days(n: int, today: date | None = None) -> list[date]:
    """N ultimos dias uteis B3, ordem ascendente.

    Inclui `today` se for dia util B3 (padrao ANBIMA: [HOJE, D-1, ...]);
    caso contrario (FDS/feriado), retorna N dias uteis estritamente anteriores.
    """
    if today is None:
        today = today_brt()
    out: list[date] = []
    d = today if is_b3_business_day(today) else today - timedelta(days=1)
    while len(out) < n:
        if is_b3_business_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def refresh_recent_days(
    fetch_day_fn: Callable[[str], dict],
    n: int,
    *,
    label: str = "",
    sleep_between_days: float = 1.0,
) -> int:
    """Forca versao fresca dos N dias uteis B3 mais recentes.

    Tenta todos os dias antes de retornar. Exit code != 0 se QUALQUER dia
    falhar (falha de rede apos retry; 403/404 nao conta como falha).
    `fetch_day_fn(date_iso)` deve devolver dict com chave `status` em
    {'updated', 'unavailable'}; pode levantar exceptions para sinalizar
    falha real (caller registra e segue).
    """
    days = last_n_business_days(n)
    header = f"Refresh{(' ' + label) if label else ''} dos ultimos {len(days)} dias uteis B3"
    print(f"{header}: {days[0]} a {days[-1]}")

    updated = 0
    unavailable = 0
    failed = 0

    for i, d in enumerate(days):
        date_iso = d.isoformat()
        try:
            result = fetch_day_fn(date_iso)
            if result["status"] == "updated":
                updated += 1
            else:
                unavailable += 1
        except Exception as exc:
            print(
                f"[{date_iso}] FALHA apos retries: {exc} - "
                f"mantendo arquivo existente (se houver)"
            )
            failed += 1
        if i < len(days) - 1:
            time.sleep(sleep_between_days)

    print()
    print(
        f"Resumo: {updated} atualizado(s), {unavailable} indisponivel(eis) "
        f"(FDS/feriado/nao publicado), {failed} falha(s)"
    )

    return 0 if failed == 0 else 1
