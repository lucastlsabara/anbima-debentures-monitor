"""HTTP helper compartilhado: timeout + retry exponencial.

Motivacao: requests.get/requests.post sem `timeout=` esperam infinitamente
se o servidor para de responder a meio de uma resposta (B3 ja travou
fetch_b3_trades_consolidated.py por 26min em 2026-05-13). Aqui injetamos
timeout default (15, 120) -- (connect, read) -- e retry com backoff
exponencial em falhas transitorias.

Reuso: b3_api.post_page (B3 Trade + ConsolidatedRecords) e
fetch_anbima._http_get_once (ANBIMA Debentures + ETTJ + TPF) chamam este
helper. Mantem regras especificas (B3UnavailableError, fallback Mozilla)
nas camadas de cima.
"""

from __future__ import annotations

import sys
import time

import requests


DEFAULT_CONNECT_TIMEOUT = 15  # segundos -- handshake TCP/TLS
DEFAULT_READ_TIMEOUT = 120    # segundos por request (paginas grandes da B3)
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE = 2      # backoff: 2s, 4s, 8s


def request_with_retry(
    method: str,
    url: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base: int = DEFAULT_BACKOFF_BASE,
    **kwargs,
) -> requests.Response:
    """requests.request com timeout default e retry exponencial.

    Comportamento:
      - Se `timeout` nao estiver em kwargs, usa (15, 120) (connect, read).
      - 2xx: response.raise_for_status() (no-op) e return.
      - 4xx: re-raise HTTPError sem retry (caller inspeciona
        exc.response.status_code para distinguir 403/404/etc).
      - 5xx: retry com sleep backoff_base ** (attempt+1) (2s, 4s, 8s).
      - Timeout/ConnectionError: retry com backoff.
      - Apos esgotar max_attempts: re-raise a ultima excecao.

    Logging em stderr a cada retry com classe da excecao e tempo de espera.
    """
    kwargs.setdefault("timeout", (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT))
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            response = requests.request(method, url, **kwargs)
            if 500 <= response.status_code < 600:
                raise requests.HTTPError(
                    f"HTTP {response.status_code} em {url}",
                    response=response,
                )
            response.raise_for_status()
            return response
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 400 <= status < 500:
                raise
            last_exc = exc
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
        if attempt < max_attempts - 1:
            wait = backoff_base ** (attempt + 1)
            print(
                f"[http-retry] {method} {url}: {type(last_exc).__name__}; "
                f"aguardando {wait}s (tentativa {attempt + 2}/{max_attempts})",
                file=sys.stderr,
            )
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc
