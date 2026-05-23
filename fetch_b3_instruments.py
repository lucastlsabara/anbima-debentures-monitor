"""Fetch do cadastro publico de instrumentos da B3 (Renda Fixa Balcao).

Fonte: endpoint OFICIAL de export CSV da B3 (mesmo dominio e padrao do
fetch_b3_trades.py / fetch_b3_trades_consolidated.py).

  POST https://arquivos.b3.com.br/bdi/table/export/csv?lang=pt-BR
  Body: {"Name":"InstrumentRegistration","Date":<dia>,"FinalDate":<dia>,
         "ClientId":"","Filters":{}}
  Response: text/csv (utf-8 com BOM, separator ';', 3 linhas de
  preambulo/glossario antes do header tabular). ~292.818 linhas em
  2026-05-21, ~30 MB. Sem autenticacao.

Motivacao: a aba Trades B3 do dashboard nao mostra indexador hoje porque
a unica fonte com indexador (movements.json) cobre so ~35% dos tickers
(DEB ANBIMA). Este cadastro cobre 99,4% dos tickers negociados, com
indexador + vencimento + %indexador + taxa adicional + etc. Este PR so
adiciona o fetcher e o step no workflow; build_dashboard e UI sao
tocados em PR futuro.

Saida: data/b3_instruments.json (sobrescrito a cada execucao -- o
cadastro B3 eh cumulativo, papeis vencidos continuam la com vencimento
passado, entao nao precisa de historico diario).

Esquema (padrao columns+rows pra economizar espaco, igual b3_trades):
  {
    "updated_at": "<iso8601 UTC>",
    "source": "B3 InstrumentRegistration",
    "schema_version": 1,
    "date": "<YYYY-MM-DD>",
    "total": <n_rows>,
    "columns": [17 nomes],
    "rows": [[...17 valores...], ...]
  }

Tipagem: tudo string EXCETO incentivada/esforco_restrito (bool),
num_emissao/qtd_emitida (int|None), pu_emissao (float|None). Os 17
campos sao preservados sem filtro -- Lucas pode usar campos extras
(qtd_emitida, pu_emissao, etc) depois.

CLI: `python fetch_b3_instruments.py [YYYY-MM-DD]` (sem argumento usa
ultimo dia util B3).
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from _http_utils import request_with_retry
from b3_calendar import is_b3_business_day, last_b3_business_day, today_brt


TABLE_NAME = "InstrumentRegistration"
EXPORT_URL = "https://arquivos.b3.com.br/bdi/table/export/csv"

# UA Mozilla + headers identicos ao b3_api.B3_HEADERS (a B3 bloqueia
# requests genericos do python-requests). Duplicado aqui em vez de
# importar de b3_api para nao acoplar este fetcher ao modulo de cache
# check do trade/consolidated -- este endpoint nao tem ping@1, eh um
# unico dump por data.
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

OUT_PATH = Path(__file__).parent / "data" / "b3_instruments.json"

# Ordem canonica das colunas no JSON de saida (snake_case). Indices
# batem com a ordem das colunas no CSV B3 (Codigo IF, Codigo ISIN,
# Emissor, Instrumento financeiro, Incentivada, Numero de serie,
# Numero de emissao, Indexador, Percentual indexador, Taxa adicional,
# Base calculo, Vencimento, Quantidade emitida, Preco unitario de
# emissao, Esforco restrito, Tipo de emissao, Indicador oferta).
COLUMNS = [
    "codigo_if",
    "isin",
    "emissor",
    "instrumento",
    "incentivada",
    "num_serie",
    "num_emissao",
    "indexador",
    "pct_indexador",
    "taxa_adicional",
    "base_calculo",
    "vencimento",
    "qtd_emitida",
    "pu_emissao",
    "esforco_restrito",
    "tipo_emissao",
    "indicador_oferta",
]

# Nomes no header do CSV B3 (sem acento + lower + so alfanumerico),
# na mesma ordem de COLUMNS.
_CSV_HEADER_KEYS = [
    "codigoif",
    "codigoisin",
    "emissor",
    "instrumentofinanceiro",
    "incentivada",
    "numerodeserie",
    "numerodeemissao",
    "indexador",
    "percentualindexador",
    "taxaadicional",
    "basecalculo",
    "vencimento",
    "quantidadeemitida",
    "precounitariodeemissao",
    "esforcorestrito",
    "tipodeemissao",
    "indicadoroferta",
]


def _norm_col(s: str) -> str:
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", s or "")
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return "".join(c for c in stripped.lower() if c.isalnum())


def _parse_bool(s: str) -> bool | None:
    """'True'/'False' (ou variantes) -> bool. '-'/'' -> None."""
    v = (s or "").strip().lower()
    if v in ("true", "verdadeiro", "sim", "1"):
        return True
    if v in ("false", "falso", "nao", "não", "0"):
        return False
    return None


def _parse_int_opt(s: str) -> int | None:
    """'1.234' ou '1234' -> 1234; '-' ou '' -> None.

    O CSV B3 ja vem com numeros inteiros sem separador na maioria das
    linhas, mas tolera separador de milhar BR ('.') por seguranca.
    """
    v = (s or "").strip()
    if not v or v == "-":
        return None
    v = v.replace(".", "")
    try:
        return int(v)
    except ValueError:
        return None


def _parse_float_opt(s: str) -> float | None:
    """'1.234,56' -> 1234.56; '-' ou '' -> None."""
    v = (s or "").strip()
    if not v or v == "-":
        return None
    try:
        return float(v.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def fetch_csv(date_iso: str) -> str:
    """POST no endpoint de export -> texto CSV completo (utf-8).

    Sem tratamento especial pra 403/404: o caller (main) loga e retorna
    1, e o workflow tem `continue-on-error` no step de instruments.
    """
    body = {
        "Name": TABLE_NAME,
        "Date": date_iso,
        "FinalDate": date_iso,
        "ClientId": "",
        "Filters": {},
    }
    # Read timeout maior que o default (120s): ~30 MB de CSV pode
    # passar dos 2min em horarios de pico.
    r = request_with_retry(
        "POST",
        EXPORT_URL,
        params={"lang": "pt-BR"},
        headers=B3_HEADERS,
        json=body,
        timeout=(15, 300),
    )
    return r.content.decode("utf-8-sig")


def parse_csv(csv_text: str) -> tuple[list[int], list[list[str]]]:
    """Parse CSV InstrumentRegistration: pula preambulo, le header, retorna rows.

    Retorna (col_indices, data_rows):
      - col_indices: lista de 17 indices na ordem de COLUMNS (mapeia
        posicao no CSV B3). Levanta RuntimeError se faltar coluna.
      - data_rows: lista de listas de strings, ainda em formato BR.
    """
    lines = csv_text.splitlines()
    header_idx = None
    for i, ln in enumerate(lines):
        if ";" not in ln:
            continue
        normalized = _norm_col(ln)
        # Header tem "codigoif" + "codigoisin" + "emissor" -- assinatura
        # robusta contra mudanca no numero de linhas de preambulo.
        if "codigoif" in normalized and "codigoisin" in normalized:
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError(
            "Header CSV InstrumentRegistration nao encontrado "
            "(linha com 'Codigo IF' + 'Codigo ISIN' + ';')"
        )
    reader = csv.reader(
        io.StringIO("\n".join(lines[header_idx:])), delimiter=";"
    )
    rows = list(reader)
    if not rows:
        return [], []
    header = rows[0]
    header_index = {_norm_col(h): i for i, h in enumerate(header)}
    missing = [k for k in _CSV_HEADER_KEYS if k not in header_index]
    if missing:
        raise RuntimeError(
            f"Colunas ausentes no header CSV B3: {missing}; "
            f"header recebido: {header}"
        )
    col_indices = [header_index[k] for k in _CSV_HEADER_KEYS]
    data_rows = [r for r in rows[1:] if any(c.strip() for c in r)]
    return col_indices, data_rows


def _convert_row(raw: list[str], col_indices: list[int]) -> list:
    """Aplica tipagem (bool/int/float/string) preservando os 17 campos."""
    def cell(idx: int) -> str:
        i = col_indices[idx]
        if 0 <= i < len(raw):
            return raw[i] or ""
        return ""

    return [
        cell(0).strip(),                # codigo_if
        cell(1).strip(),                # isin
        cell(2).strip(),                # emissor
        cell(3).strip(),                # instrumento
        _parse_bool(cell(4)),           # incentivada
        cell(5).strip(),                # num_serie
        _parse_int_opt(cell(6)),        # num_emissao
        cell(7).strip(),                # indexador
        cell(8).strip(),                # pct_indexador
        cell(9).strip(),                # taxa_adicional
        cell(10).strip(),               # base_calculo
        cell(11).strip(),               # vencimento
        _parse_int_opt(cell(12)),       # qtd_emitida
        _parse_float_opt(cell(13)),     # pu_emissao
        _parse_bool(cell(14)),          # esforco_restrito
        cell(15).strip(),               # tipo_emissao
        cell(16).strip(),               # indicador_oferta
    ]


def _write_atomic(payload: dict) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = OUT_PATH.parent / f"{OUT_PATH.name}.tmp"
    tmp_path.write_text(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp_path, OUT_PATH)


def _emit_summary(total: int, by_instrumento: dict[str, int],
                  by_indexador: dict[str, int], date_iso: str) -> None:
    """Loga summary no stdout + GITHUB_STEP_SUMMARY (se disponivel)."""
    top_inst = sorted(by_instrumento.items(), key=lambda kv: -kv[1])[:10]
    top_idx = sorted(by_indexador.items(), key=lambda kv: -kv[1])[:5]

    lines = [
        f"[b3_instruments] data={date_iso} total={total:,}".replace(",", "."),
        "  por instrumento:",
    ]
    for nome, n in top_inst:
        lines.append(f"    {nome or '(vazio)':<10s} {n:>8,}".replace(",", "."))
    lines.append("  top 5 indexadores:")
    for nome, n in top_idx:
        lines.append(f"    {nome or '(vazio)':<25s} {n:>8,}".replace(",", "."))
    for ln in lines:
        print(ln)

    gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_summary:
        md = ["## B3 InstrumentRegistration", "",
              f"- **Data:** `{date_iso}`",
              f"- **Total de instrumentos:** {total:,}".replace(",", "."),
              "", "### Top instrumentos", "| Instrumento | N |", "|---|---:|"]
        for nome, n in top_inst:
            md.append(f"| {nome or '(vazio)'} | {n:,} |".replace(",", "."))
        md += ["", "### Top 5 indexadores", "| Indexador | N |", "|---|---:|"]
        for nome, n in top_idx:
            md.append(f"| {nome or '(vazio)'} | {n:,} |".replace(",", "."))
        md.append("")
        with open(gh_summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(md))


def fetch_and_save(date_iso: str) -> int:
    """Pipeline completo: fetch + parse + tipagem + write atomico."""
    print(f"[b3_instruments] fetching {TABLE_NAME} for {date_iso}...")
    csv_text = fetch_csv(date_iso)
    print(
        f"[b3_instruments] received {len(csv_text):,} bytes; parsing..."
        .replace(",", ".")
    )
    col_indices, raw_rows = parse_csv(csv_text)
    print(f"[b3_instruments] {len(raw_rows):,} raw rows; converting types..."
          .replace(",", "."))

    rows: list[list] = []
    by_instrumento: dict[str, int] = {}
    by_indexador: dict[str, int] = {}
    for raw in raw_rows:
        row = _convert_row(raw, col_indices)
        rows.append(row)
        inst = row[3] or ""
        by_instrumento[inst] = by_instrumento.get(inst, 0) + 1
        idx_name = row[7] or ""
        by_indexador[idx_name] = by_indexador.get(idx_name, 0) + 1

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": f"B3 {TABLE_NAME}",
        "schema_version": 1,
        "date": date_iso,
        "total": len(rows),
        "columns": COLUMNS,
        "rows": rows,
    }
    _write_atomic(payload)
    size_mb = OUT_PATH.stat().st_size / (1024 * 1024)
    print(
        f"[b3_instruments] OK: {len(rows):,} linhas, {size_mb:.2f} MB "
        f"-> {OUT_PATH}".replace(",", ".")
    )
    _emit_summary(len(rows), by_instrumento, by_indexador, date_iso)
    return 0


def _resolve_default_date() -> str:
    """Dia util B3 mais recente (HOJE se for du, senao ultimo anterior)."""
    today = today_brt()
    if is_b3_business_day(today):
        return today.isoformat()
    return last_b3_business_day(today).isoformat()


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    if args:
        date_iso = args[0]
        date.fromisoformat(date_iso)  # valida formato
    else:
        date_iso = _resolve_default_date()

    try:
        return fetch_and_save(date_iso)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        print(
            f"[b3_instruments] FALHA HTTP {status}: {exc}; "
            f"mantendo {OUT_PATH.name} anterior (se existir)",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(
            f"[b3_instruments] FALHA: {type(exc).__name__}: {exc}; "
            f"mantendo {OUT_PATH.name} anterior (se existir)",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
