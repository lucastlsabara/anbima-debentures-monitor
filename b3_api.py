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

import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import requests

from _http_utils import request_with_retry
from b3_calendar import is_b3_business_day, today_brt


# Versao do schema do `meta` embutido em data/b3_trades*/<date>.json.
# v1: ausente (formato pre-cache check).
# v2: presente com lastUpdateDate + totalRecords + firstPageBytes +
#     fetchedAt + schemaVersion. Cache check ativo.
CACHE_SCHEMA_VERSION = 2


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


def extract_total_records(envelope: dict) -> int | None:
    """Extrai o total de registros do envelope B3.

    A B3 nao expoe 'totalRecords' nem 'totalRows' no top-level do envelope.
    Em vez disso retorna 'table.pageCount' que e o numero de PAGINAS dado o
    page_size da requisicao.
    """
    if not isinstance(envelope, dict):
        return None
    table = envelope.get("table")
    if not isinstance(table, dict):
        return None
    page_count = table.get("pageCount")
    if isinstance(page_count, int):
        return page_count
    return None


def compute_first_page_bytes(envelope: dict) -> int:
    """Tamanho (em chars) do envelope da primeira pagina apos serializacao
    deterministica via json.dumps(..., sort_keys=True, ensure_ascii=False).

    Terceiro indicador do cache check: sensivel a QUALQUER mudanca de
    conteudo no envelope (incluindo timestamps internos, lista de campos,
    valores). Funciona em conjunto com lastUpdateDate e totalRecords
    para detectar republicacoes da B3 que mudam dados mas nao mexem em
    lastUpdateDate (motivo da falha do PR #145).
    """
    return len(json.dumps(envelope, sort_keys=True, ensure_ascii=False))


def ping_first_page(table: str, date_iso: str) -> dict:
    """Ping leve: POST /<table>/<date>/<date>/1/1 (page_size=1).

    Usado para o cache check: response.lastUpdateDate eh o timestamp da
    B3, response.table.pageCount com page_size=1 eh o total de registros
    e compute_first_page_bytes(response) eh o terceiro indicador.
    Mesma cadeia de erros do post_page (B3UnavailableError em 403/404,
    SandboxBlockedError em 403 sandbox, HTTPError em 5xx persistente).
    """
    return post_page(table, date_iso, 1, 1)


def extract_ping_indicators(envelope: dict) -> dict:
    """Extrai os 3 indicadores do envelope da primeira pagina (ping@1).

    Retorna dict com lastUpdateDate (str|None), totalRecords (int),
    firstPageBytes (int). Tipos sao normalizados (totalRecords sempre
    int, firstPageBytes sempre int) para garantir comparacao exata
    com o meta local apos round-trip via JSON.
    """
    table = envelope.get("table") or {}
    return {
        "lastUpdateDate": envelope.get("lastUpdateDate"),
        "totalRecords": int(table.get("pageCount") or 0),
        "firstPageBytes": int(compute_first_page_bytes(envelope)),
    }


def read_local_cache_meta(out_path: Path) -> tuple[dict | None, str | None]:
    """Le o objeto `meta` do JSON local em <out_path>.

    Retorna (meta_dict, None) quando OK ou (None, motivo) caso
    contrario, com motivo em:
      - 'metadata_ausente': arquivo nao existe, nao tem chave 'meta',
        schemaVersion < CACHE_SCHEMA_VERSION ou faltam campos exigidos.
      - 'json_corrompido': falha de parse JSON ou erro de IO.

    Em ambos os casos o caller deve tratar como cache-miss (refetch).
    """
    if not out_path.exists():
        return None, "metadata_ausente"
    try:
        local = json.loads(out_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, "json_corrompido"
    if not isinstance(local, dict):
        return None, "metadata_ausente"
    meta = local.get("meta")
    if not isinstance(meta, dict):
        return None, "metadata_ausente"
    try:
        schema_v = int(meta.get("schemaVersion") or 0)
    except (TypeError, ValueError):
        return None, "metadata_ausente"
    if schema_v < CACHE_SCHEMA_VERSION:
        return None, "metadata_ausente"
    required = ("lastUpdateDate", "totalRecords", "firstPageBytes")
    if any(k not in meta for k in required):
        return None, "metadata_ausente"
    return meta, None


def compare_cache_indicators(remote: dict, local_meta: dict) -> str | None:
    """Compara os 3 indicadores remoto x local (comparacao de tipos exata).

    Retorna None quando TODOS batem (cache-hit). Caso contrario retorna
    o motivo da divergencia: 'lastUpdate_changed' |
    'totalRecords_changed' | 'firstPageBytes_changed' (ordem importa:
    primeiro motivo encontrado eh retornado, para logging consistente).
    """
    if remote.get("lastUpdateDate") != local_meta.get("lastUpdateDate"):
        return "lastUpdate_changed"
    if remote.get("totalRecords") != local_meta.get("totalRecords"):
        return "totalRecords_changed"
    if remote.get("firstPageBytes") != local_meta.get("firstPageBytes"):
        return "firstPageBytes_changed"
    return None


def check_cache(
    table: str, date_iso: str, out_path: Path
) -> tuple[bool, str, dict | None]:
    """Executa o cache check completo para um dia.

    Retorna (cache_hit, motivo, ping_envelope) onde:
      - cache_hit=True: 3 indicadores bateram. Caller deve PULAR fetch
        full. ping_envelope eh o envelope que serviu de prova
        (pode ser usado pelo caller para re-gravar o meta se quiser).
      - cache_hit=False: divergiu ou metadata ausente. Caller deve
        FAZER fetch full. ping_envelope eh o envelope do ping (pode
        ser reaproveitado para popular o meta apos o fetch); pode ser
        None se o ping falhou (caller deve tentar o fetch full mesmo
        assim e re-fazer um ping ao final para gravar o meta).

    Levanta B3UnavailableError (403/404 do ping) - caller trata como
    indisponivel sem retornar (h)it nem (m)iss. Levanta
    SandboxBlockedError se a sandbox bloquear (propagar).

    Motivos possiveis (no log e no return):
      cache_hit | metadata_ausente | json_corrompido | ping_falhou
      | lastUpdate_changed | totalRecords_changed | firstPageBytes_changed
    """
    # 1. Ping leve. Falhas de rede transientes (apos retry interno do
    #    post_page) viram cache-miss explicito — NUNCA cache-hit otimista
    #    (regra para nao introduzir falso positivo como no PR #145).
    try:
        ping = ping_first_page(table, date_iso)
    except (B3UnavailableError, SandboxBlockedError):
        raise
    except Exception as exc:
        print(
            f"[cache-miss] Dia {date_iso}: motivo=ping_falhou ({exc}) "
            f"-> fetch completo"
        )
        return False, "ping_falhou", None

    remote = extract_ping_indicators(ping)

    # 2. Le meta local. Schema antigo / ausente / corrompido -> cache-miss.
    local_meta, missing = read_local_cache_meta(out_path)
    if local_meta is None:
        print(
            f"[cache-miss] Dia {date_iso}: motivo={missing} -> fetch completo"
        )
        return False, missing, ping

    # 3. Compara os 3 indicadores (tipo exato).
    motivo = compare_cache_indicators(remote, local_meta)
    if motivo is None:
        print(
            f"[cache-hit] Dia {date_iso}: "
            f"lastUpdate={remote['lastUpdateDate']} "
            f"totalRecords={remote['totalRecords']} "
            f"firstPageBytes={remote['firstPageBytes']} (skip)"
        )
        return True, "cache_hit", ping

    field = {
        "lastUpdate_changed": "lastUpdateDate",
        "totalRecords_changed": "totalRecords",
        "firstPageBytes_changed": "firstPageBytes",
    }[motivo]
    print(
        f"[cache-miss] Dia {date_iso}: motivo={motivo} "
        f"(local={local_meta.get(field)!r} remote={remote.get(field)!r}) "
        f"-> fetch completo"
    )
    return False, motivo, ping


def build_meta_from_ping(envelope: dict, fetched_at_iso: str) -> dict:
    """Monta o objeto `meta` a partir do envelope do ping@1 + timestamp."""
    ind = extract_ping_indicators(envelope)
    return {
        "lastUpdateDate": ind["lastUpdateDate"],
        "totalRecords": ind["totalRecords"],
        "firstPageBytes": ind["firstPageBytes"],
        "fetchedAt": fetched_at_iso,
        "schemaVersion": CACHE_SCHEMA_VERSION,
    }


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
    summary_path: Path | str | None = None,
) -> int:
    """Forca versao fresca dos N dias uteis B3 mais recentes.

    Tenta todos os dias antes de retornar. Exit code != 0 se QUALQUER dia
    falhar (falha de rede apos retry; 403/404 nao conta como falha).
    `fetch_day_fn(date_iso)` deve devolver dict com chave `status` em
    {'updated', 'unavailable', 'cache_hit'} e (opcional) `cache_motivo`
    com o motivo do cache-miss; pode levantar exceptions para sinalizar
    falha real (caller registra e segue).

    Se `summary_path` for fornecido, grava resumo (hits/misses/motivos)
    em arquivo simples key=value, para consumo pelo workflow YAML.
    """
    days = last_n_business_days(n)
    header = f"Refresh{(' ' + label) if label else ''} dos ultimos {len(days)} dias uteis B3"
    print(f"{header}: {days[0]} a {days[-1]}")

    updated = 0
    unavailable = 0
    failed = 0
    cache_hits = 0
    cache_misses = 0
    miss_reasons: list[str] = []

    for i, d in enumerate(days):
        date_iso = d.isoformat()
        try:
            result = fetch_day_fn(date_iso)
            status = result["status"]
            motivo = result.get("cache_motivo")
            if status == "cache_hit":
                cache_hits += 1
            elif status == "updated":
                updated += 1
                cache_misses += 1
                if motivo and motivo != "cache_hit":
                    miss_reasons.append(motivo)
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
        f"Resumo: {cache_hits} cache-hit(s), {updated} atualizado(s), "
        f"{unavailable} indisponivel(eis) (FDS/feriado/nao publicado), "
        f"{failed} falha(s)"
    )
    if miss_reasons:
        print(f"Motivos de cache-miss: {','.join(miss_reasons)}")

    if summary_path is not None:
        try:
            Path(summary_path).write_text(
                (
                    f"cache_hits={cache_hits}\n"
                    f"cache_misses={cache_misses}\n"
                    f"miss_reasons={','.join(miss_reasons)}\n"
                    f"unavailable={unavailable}\n"
                    f"failed={failed}\n"
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"[summary] AVISO: nao foi possivel gravar {summary_path}: {exc}")

    return 0 if failed == 0 else 1
