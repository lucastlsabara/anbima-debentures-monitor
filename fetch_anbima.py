"""Baixa os arquivos diários da ANBIMA: mercado secundário de debêntures + ETTJ NTN-B.

Saída intermediária (em raw/):
  - raw/db<YYMMDD>.txt          : arquivo bruto de debêntures (ISO-8859-1)
  - raw/ettj_<YYYY-MM-DD>.csv   : arquivo bruto da curva (ISO-8859-1)

Saída estruturada (parsed.json):
  {
    "data_referencia": "YYYY-MM-DD",
    "debentures": [ { codigo, emissor, vencimento, indice, taxa_indicativa,
                      duration_dias, pu, referencia_ntnb, ... } ],
    "ettj_ipca": [ { vertice_du: int, taxa: float } ]
  }
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).parent
RAW_DIR = ROOT / "raw"

DB_URL = "https://www.anbima.com.br/informacoes/merc-sec-debentures/arqs/db{yymmdd}.txt"
ETTJ_URL = "https://www.anbima.com.br/informacoes/est-termo/CZ-down.asp?Dt_Ref={dd}/{mm}/{yyyy}&saida=csv"

DB_HEADER = [
    "codigo", "nome", "vencimento", "indice", "taxa_compra", "taxa_venda",
    "taxa_indicativa", "desvio_padrao", "intervalo_min", "intervalo_max",
    "pu", "pct_pu_par", "duration_dias", "pct_reune", "referencia_ntnb",
]


def _br_float(s: str) -> float | None:
    s = (s or "").strip()
    if s in ("", "--", "N/D"):
        return None
    return float(s.replace(".", "").replace(",", "."))


def _br_int(s: str) -> int | None:
    s = (s or "").strip().replace(".", "")
    if not s:
        return None
    return int(s)


def _iso(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _br_date(s: str) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    return datetime.strptime(s, "%d/%m/%Y").date().isoformat()


def fetch_db(target: date) -> str:
    yymmdd = target.strftime("%y%m%d")
    url = DB_URL.format(yymmdd=yymmdd)
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    text = r.content.decode("latin-1")
    out = RAW_DIR / f"db{yymmdd}.txt"
    out.write_text(text, encoding="utf-8")
    return text


def fetch_ettj(target: date) -> str:
    url = ETTJ_URL.format(dd=f"{target.day:02d}", mm=f"{target.month:02d}", yyyy=target.year)
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    text = r.content.decode("latin-1")
    out = RAW_DIR / f"ettj_{_iso(target)}.csv"
    out.write_text(text, encoding="utf-8")
    return text


def parse_db(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        if not line or "@" not in line:
            continue
        parts = line.split("@")
        if parts[0].strip() == "Código" or parts[0].startswith("ANBIMA"):
            continue
        if len(parts) < len(DB_HEADER):
            parts = parts + [""] * (len(DB_HEADER) - len(parts))
        rec = dict(zip(DB_HEADER, parts))
        # Normalize
        try:
            rec["vencimento"] = _br_date(rec["vencimento"])
        except ValueError:
            continue
        rec["taxa_compra"] = _br_float(rec["taxa_compra"])
        rec["taxa_venda"] = _br_float(rec["taxa_venda"])
        rec["taxa_indicativa"] = _br_float(rec["taxa_indicativa"])
        rec["desvio_padrao"] = _br_float(rec["desvio_padrao"])
        rec["intervalo_min"] = _br_float(rec["intervalo_min"])
        rec["intervalo_max"] = _br_float(rec["intervalo_max"])
        rec["pu"] = _br_float(rec["pu"])
        rec["pct_pu_par"] = _br_float(rec["pct_pu_par"])
        rec["duration_dias"] = _br_float(rec["duration_dias"])
        rec["pct_reune"] = _br_float(rec["pct_reune"])
        try:
            rec["referencia_ntnb"] = _br_date(rec["referencia_ntnb"])
        except ValueError:
            rec["referencia_ntnb"] = None
        rec["emissor"] = rec.pop("nome").strip()
        rec["codigo"] = rec["codigo"].strip()
        rec["indice"] = rec["indice"].strip()
        rows.append(rec)
    return rows


def parse_ettj(text: str) -> tuple[str, list[dict]]:
    """Extrai a tabela 'ETTJ Inflação Implicita (IPCA)' da resposta CZ-down.

    Retorna (data_referencia_iso, [ {vertice_du, taxa_ipca, taxa_pref, inflacao_implicita} ]).
    """
    lines = [ln.rstrip("\r") for ln in text.splitlines()]
    data_ref = None
    if lines and ";" in lines[0]:
        first = lines[0].split(";")[0].strip()
        try:
            data_ref = datetime.strptime(first, "%d/%m/%Y").date().isoformat()
        except ValueError:
            data_ref = None
    # Localiza início da tabela ETTJ Inflação Implícita
    start = None
    for i, ln in enumerate(lines):
        if "ETTJ Infla" in ln:
            start = i
            break
    if start is None:
        raise RuntimeError("Tabela 'ETTJ Inflação Implícita' não encontrada no CSV ANBIMA")
    # Cabeçalho está em start+1, dados a partir de start+2 até linha em branco
    rows = []
    for ln in lines[start + 2:]:
        if not ln.strip():
            break
        cells = [c.strip() for c in ln.split(";")]
        if len(cells) < 2:
            continue
        v = _br_int(cells[0])
        ipca = _br_float(cells[1])
        if v is None or ipca is None:
            continue
        pref = _br_float(cells[2]) if len(cells) > 2 else None
        infl = _br_float(cells[3]) if len(cells) > 3 else None
        rows.append({
            "vertice_du": v,
            "taxa_ipca": ipca,
            "taxa_pref": pref,
            "inflacao_implicita": infl,
        })
    return data_ref, rows


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Coleta ANBIMA (debêntures + ETTJ NTN-B).")
    p.add_argument("--date", required=True, help="Data de referência (YYYY-MM-DD).")
    p.add_argument("--out", default="parsed.json", help="Saída JSON consolidada.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    target = datetime.strptime(args.date, "%Y-%m-%d").date()
    RAW_DIR.mkdir(exist_ok=True)

    print(f"[fetch] db de {target.isoformat()}", file=sys.stderr)
    db_text = fetch_db(target)
    debentures = parse_db(db_text)
    print(f"[fetch]   -> {len(debentures)} papéis", file=sys.stderr)

    print(f"[fetch] ETTJ de {target.isoformat()}", file=sys.stderr)
    ettj_text = fetch_ettj(target)
    ettj_date, ettj_rows = parse_ettj(ettj_text)
    print(f"[fetch]   -> {len(ettj_rows)} vértices (data publicada: {ettj_date})", file=sys.stderr)

    if ettj_date and ettj_date != target.isoformat():
        print(
            f"[warn ] ETTJ retornou data {ettj_date} (esperado {target.isoformat()})",
            file=sys.stderr,
        )

    out = {
        "data_referencia": target.isoformat(),
        "ettj_data_publicada": ettj_date,
        "debentures": debentures,
        "ettj_ipca": ettj_rows,
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[fetch] -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
