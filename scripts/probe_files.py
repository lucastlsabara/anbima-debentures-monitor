#!/usr/bin/env python3
"""Probe horario de disponibilidade dos 5 arquivos diarios (ANBIMA + B3).

Verifica se TODOS os 5 arquivos do dia alvo ja foram publicados:
  1. ANBIMA Debentures   (db<YYMMDD>.txt)
  2. ANBIMA ETTJ         (CZ-down.asp?Dt_Ref=DD/MM/YYYY)
  3. ANBIMA TPF          (ms<YYMMDD>.txt)
  4. B3 Trade            (POST /bdi/table/Trade/<date>/<date>/1/1)
  5. B3 ConsolidatedRecords (POST /bdi/table/ConsolidatedRecords/<date>/<date>/1/1)

Uso:
    python3 scripts/probe_files.py --date YYYY-MM-DD

Saida:
    - Log detalhado por arquivo (stdout, pt-BR)
    - Linha final: '[probe] N/5 disponivel(eis) para <date>'
    - Se --github-output for passado: escreve all_available=true|false e
      available_count=N nesse arquivo (uso em GitHub Actions: --github-output
      "$GITHUB_OUTPUT").

Exit codes:
    0  -> todos os 5 disponiveis
    2  -> pelo menos 1 faltando
    1  -> erro real (rede, parse, etc) que impede decisao
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from b3_api import B3UnavailableError, SandboxBlockedError, post_page  # noqa: E402

DB_URL = "https://www.anbima.com.br/informacoes/merc-sec-debentures/arqs/db{yymmdd}.txt"
ETTJ_URL = "https://www.anbima.com.br/informacoes/est-termo/CZ-down.asp?Dt_Ref={dd}/{mm}/{yyyy}&saida=csv"
TITPUB_URL = "https://www.anbima.com.br/informacoes/merc-sec/arqs/ms{yymmdd}.txt"

# UA "navegador" -- ANBIMA bloqueia UAs nao-browser. Usamos direto no probe
# (pratico, ja que e check rapido e nao expoe contato de scraper formal).
MOZILLA_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 20

# Mensagens que a ANBIMA retorna no body quando o arquivo nao existe ainda.
# (ETTJ retorna pagina HTML/CSV vazio em vez de 404 quando data nao publicada.)
ETTJ_ABSENT_MARKERS = (
    "nao existem informacoes",
    "não existem informações",
    "informacoes nao disponiveis",
    "informações não disponíveis",
)


def _probe_anbima_fixed_url(label: str, url: str) -> tuple[bool, str]:
    """HEAD -> fallback GET range. Retorna (disponivel, detalhe)."""
    try:
        r = requests.head(
            url,
            headers={"User-Agent": MOZILLA_UA},
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        return False, f"erro de rede no HEAD: {exc}"

    if r.status_code == 404:
        return False, "HTTP 404 (nao publicado)"
    if r.status_code >= 400:
        return False, f"HTTP {r.status_code}"

    content_length = r.headers.get("Content-Length")
    if content_length is not None:
        try:
            n = int(content_length)
        except ValueError:
            n = -1
        if n > 0:
            return True, f"HTTP 200, Content-Length={n}"
        if n == 0:
            return False, "HTTP 200 mas Content-Length=0"

    try:
        r2 = requests.get(
            url,
            headers={"User-Agent": MOZILLA_UA, "Range": "bytes=0-1023"},
            timeout=HTTP_TIMEOUT,
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        return False, f"erro de rede no GET range: {exc}"

    if r2.status_code in (200, 206):
        n = len(r2.content)
        if n > 0:
            return True, f"HTTP {r2.status_code}, bytes recebidos={n}"
        return False, f"HTTP {r2.status_code} mas body vazio"
    return False, f"GET range HTTP {r2.status_code}"


def probe_anbima_debentures(date_iso: str) -> tuple[bool, str]:
    d = datetime.strptime(date_iso, "%Y-%m-%d").date()
    yymmdd = d.strftime("%y%m%d")
    return _probe_anbima_fixed_url("Debentures", DB_URL.format(yymmdd=yymmdd))


def probe_anbima_tpf(date_iso: str) -> tuple[bool, str]:
    d = datetime.strptime(date_iso, "%Y-%m-%d").date()
    yymmdd = d.strftime("%y%m%d")
    return _probe_anbima_fixed_url("TPF", TITPUB_URL.format(yymmdd=yymmdd))


def probe_anbima_ettj(date_iso: str) -> tuple[bool, str]:
    """ETTJ usa endpoint dinamico (.asp) -- HEAD nao confiavel; faz GET."""
    d = datetime.strptime(date_iso, "%Y-%m-%d").date()
    url = ETTJ_URL.format(dd=d.strftime("%d"), mm=d.strftime("%m"), yyyy=d.strftime("%Y"))
    try:
        r = requests.get(
            url,
            headers={"User-Agent": MOZILLA_UA},
            timeout=HTTP_TIMEOUT,
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        return False, f"erro de rede: {exc}"

    if r.status_code >= 400:
        return False, f"HTTP {r.status_code}"

    body = (r.text or "").strip()
    if not body:
        return False, "HTTP 200 mas body vazio"

    body_low = body.lower()
    for marker in ETTJ_ABSENT_MARKERS:
        if marker in body_low:
            return False, f"body sinaliza ausencia ('{marker}')"

    # CSV valido tem cabecalho com 'Vertice' / 'Coeficiente' em alguma forma e
    # pelo menos uma linha numerica. Heuristica: minimo de 200 bytes e contem
    # ';' (separador CSV ANBIMA).
    if len(body) < 200 or ";" not in body:
        return False, f"body suspeito (len={len(body)}, sem ';' ou muito curto)"

    return True, f"HTTP 200, body len={len(body)}"


def probe_b3_table(table: str, date_iso: str) -> tuple[bool, str]:
    """POST 1 pagina/1 registro; disponivel se retornar pelo menos 1 row."""
    try:
        resp = post_page(table, date_iso, page=1, page_size=1)
    except B3UnavailableError as exc:
        return False, f"B3 indisponivel ({exc})"
    except SandboxBlockedError as exc:
        # Bloqueio de allowlist -- erro real, nao 'nao publicado'.
        raise RuntimeError(f"sandbox bloqueou B3 ({exc})") from exc
    except Exception as exc:
        return False, f"erro inesperado: {exc}"

    values = ((resp.get("table") or {}).get("values")) or []
    n = len(values)
    if n > 0:
        return True, f"HTTP 200, {n} row(s) na pagina 1"
    return False, "HTTP 200 mas zero registros"


PROBES = [
    ("ANBIMA Debentures", probe_anbima_debentures),
    ("ANBIMA ETTJ", probe_anbima_ettj),
    ("ANBIMA TPF", probe_anbima_tpf),
    ("B3 Trade", lambda d: probe_b3_table("Trade", d)),
    ("B3 ConsolidatedRecords", lambda d: probe_b3_table("ConsolidatedRecords", d)),
]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", required=True, help="YYYY-MM-DD (data alvo)")
    p.add_argument(
        "--github-output",
        default=None,
        help="Arquivo do $GITHUB_OUTPUT para escrever all_available e available_count.",
    )
    args = p.parse_args()

    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print(f"[probe] ERRO: data invalida: {args.date}", file=sys.stderr)
        return 1

    print(f"[probe] checando disponibilidade dos 5 arquivos para {args.date}")
    results: list[tuple[str, bool, str]] = []
    for label, fn in PROBES:
        try:
            ok, detail = fn(args.date)
        except RuntimeError as exc:
            print(f"[probe] ERRO real em '{label}': {exc}", file=sys.stderr)
            return 1
        status = "OK" if ok else "FALTANDO"
        print(f"  [{status}] {label}: {detail}")
        results.append((label, ok, detail))

    available = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    all_available = available == total

    resumo_partes = [
        f"{label} {'OK' if ok else 'FALTANDO'}" for label, ok, _ in results
    ]
    print(
        f"[probe] {available} de {total} arquivos disponiveis para "
        f"{args.date}: " + ", ".join(resumo_partes)
    )

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"all_available={'true' if all_available else 'false'}\n")
            fh.write(f"available_count={available}\n")
            fh.write(f"total_count={total}\n")
            faltando = [label for label, ok, _ in results if not ok]
            if faltando:
                fh.write(f"missing={', '.join(faltando)}\n")
            else:
                fh.write("missing=\n")

    return 0 if all_available else 2


if __name__ == "__main__":
    sys.exit(main())
