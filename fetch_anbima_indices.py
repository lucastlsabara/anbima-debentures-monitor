"""Baixa os indices ANBIMA (familias IDA e IMA) do S3 publico da ANBIMA Data.

Fonte: bucket S3 publico (CDN AWS), URL fixa por slug:
    https://s3-data-prd-use1-precos.s3.us-east-1.amazonaws.com
        /arquivos/indices-historico/<SLUG>-HISTORICO.xls

Sao 17 slugs (5 IDA + 12 IMA). O "XLS" e' na verdade um XLSX (OOXML),
entao pandas.read_excel via engine `openpyxl`. Sem auth, sem cookies,
sem rate limit conhecido (CDN S3 publico).

Schema (colunas reais conforme XLS empirico em 2026-05):
  - IDA  (9 cols):  Indice | Data de Referencia | Numero Indice |
                    Variacao Diaria (%) | Variacao no Mes (%) |
                    Variacao no Ano (%) | Variacao 12 Meses (%) |
                    Variacao 24 Meses (%) | Duration (d.u.)
  - IMA (10 cols):  idem + coluna extra PMR (no fim)

Saida (1 arquivo por indice): data/anbima_indices/<SLUG>.json com schema:
  {
    "indice": "IDA - Geral",
    "familia": "IDA",
    "slug": "IDAGERAL",
    "fetched_at": "2026-05-17T...",
    "last_modified": "Fri, 15 May 2026 ... GMT",
    "columns": [
        "data", "numero_indice", "variacao_diaria_pct",
        "variacao_mensal_pct", "variacao_anual_pct",
        "variacao_12m_pct", "variacao_24m_pct",
        "duration_du", "pmr"
    ],
    "rows": [ ["2009-01-05", 1000.0, ..., null] ]
  }

Cache: HEAD request -> compara Last-Modified com o ja salvo; iguais
pula. --force ignora cache.

Self-test (--self-test): roda 3 cenarios in-process com fixtures locais
(parse 5-linha + cache hit + IDA9/IMA10).
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import pandas as pd
import requests

from _http_utils import request_with_retry

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "data" / "anbima_indices"

BASE_URL = (
    "https://s3-data-prd-use1-precos.s3.us-east-1.amazonaws.com"
    "/arquivos/indices-historico"
)

INDICES: dict[str, list[str]] = {
    "IDA": [
        "IDAGERAL",
        "IDADI",
        "IDAIPCA",
        "IDAIPCAINFRAESTRUTURA",
        "IDAIPCAEXINFRAESTRUTURA",
    ],
    "IMA": [
        "IMAGERAL",
        "IMAGERALEXC",
        "IMAB",
        "IMAB5",
        "IMAB5MAIS",
        "IMAS",
        "IRFM",
        "IRFM1",
        "IRFM1MAIS",
        "IRFMP2",
        "IRFMP3",
        "IMAB5P2",
    ],
}

# Mapeia rotulos do XLS (com acentos/parenteses) para snake_case curto
# usado no JSON. Mesma ordem das colunas em todas as familias.
_COL_MAP = {
    "Data de Referência": "data",
    "Número Índice": "numero_indice",
    "Variação Diária (%)": "variacao_diaria_pct",
    "Variação no Mês (%)": "variacao_mensal_pct",
    "Variação no Ano (%)": "variacao_anual_pct",
    "Variação 12 Meses (%)": "variacao_12m_pct",
    "Variação 24 Meses (%)": "variacao_24m_pct",
    "Duration (d.u.)": "duration_du",
    "PMR": "pmr",
}

# Schema canonico de saida -- IDA preenche `pmr` com None.
_OUT_COLUMNS = [
    "data",
    "numero_indice",
    "variacao_diaria_pct",
    "variacao_mensal_pct",
    "variacao_anual_pct",
    "variacao_12m_pct",
    "variacao_24m_pct",
    "duration_du",
    "pmr",
]

USER_AGENT = (
    "anbima-debentures-monitor/1.0 "
    "(+https://github.com/lucastlsabara/anbima-debentures-monitor)"
)


def _url_for(slug: str) -> str:
    return f"{BASE_URL}/{slug}-HISTORICO.xls"


def _familia_for(slug: str) -> str:
    for fam, slugs in INDICES.items():
        if slug in slugs:
            return fam
    raise ValueError(f"slug desconhecido: {slug}")


def _http_head(url: str) -> requests.Response:
    return request_with_retry("HEAD", url, headers={"User-Agent": USER_AGENT})


def _http_get(url: str) -> requests.Response:
    return request_with_retry("GET", url, headers={"User-Agent": USER_AGENT})


def parse_xls_to_records(xls_bytes: bytes) -> tuple[str, list[list]]:
    """Le XLS/XLSX da ANBIMA e devolve (nome_indice, rows).

    `nome_indice` vem da coluna "Indice" da primeira linha (e' constante
    no arquivo, ex: "IDA - Geral" / "IMA-B"). `rows` segue _OUT_COLUMNS:
    [data_iso, numero_indice, var_diaria_pct, ..., duration_du, pmr] --
    pmr fica None para IDA (9 colunas).
    """
    df = pd.read_excel(io.BytesIO(xls_bytes), engine="openpyxl")
    if df.empty:
        return "", []
    # "Indice" e' a primeira coluna no XLS.
    nome_indice = str(df.iloc[0, 0]).strip()

    rows: list[list] = []
    has_pmr = "PMR" in df.columns
    for _, r in df.iterrows():
        data_val = r["Data de Referência"]
        if pd.isna(data_val):
            continue
        if isinstance(data_val, datetime):
            data_iso = data_val.date().isoformat()
        else:
            # Fallback raro: string '2009-01-05' ou similar.
            data_iso = str(data_val)[:10]

        def _f(col):
            v = r.get(col)
            if v is None or pd.isna(v):
                return None
            return float(v)

        def _i(col):
            v = r.get(col)
            if v is None or pd.isna(v):
                return None
            return int(v)

        rows.append([
            data_iso,
            _f("Número Índice"),
            _f("Variação Diária (%)"),
            _f("Variação no Mês (%)"),
            _f("Variação no Ano (%)"),
            _f("Variação 12 Meses (%)"),
            _f("Variação 24 Meses (%)"),
            _i("Duration (d.u.)"),
            _f("PMR") if has_pmr else None,
        ])
    return nome_indice, rows


def _load_meta(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def fetch_slug(slug: str, *, force: bool = False) -> tuple[str, int]:
    """Baixa (se necessario) o indice `slug` e salva o JSON.

    Retorna (status, n_rows). status: "hit" | "new" | "updated".
    """
    url = _url_for(slug)
    familia = _familia_for(slug)
    out_path = OUT_DIR / f"{slug}.json"
    cached = _load_meta(out_path)

    if not force and cached is not None:
        try:
            head = _http_head(url)
        except requests.RequestException as e:
            print(
                f"[indices] HEAD {slug} falhou ({e}); refazendo GET",
                file=sys.stderr,
            )
        else:
            lm_remote = head.headers.get("Last-Modified")
            lm_cached = cached.get("last_modified")
            if lm_remote and lm_cached and lm_remote == lm_cached:
                return ("hit", len(cached.get("rows") or []))

    resp = _http_get(url)
    last_modified = resp.headers.get("Last-Modified") or ""
    nome_indice, rows = parse_xls_to_records(resp.content)
    payload = {
        "indice": nome_indice,
        "familia": familia,
        "slug": slug,
        "fetched_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "last_modified": last_modified,
        "columns": _OUT_COLUMNS,
        "rows": rows,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    status = "new" if cached is None else "updated"
    return (status, len(rows))


def run_all(*, force: bool = False) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    n_hit = n_new = n_upd = n_fail = 0
    for familia, slugs in INDICES.items():
        for slug in slugs:
            print(f"[indices] {familia} {slug}", file=sys.stderr)
            try:
                status, n_rows = fetch_slug(slug, force=force)
            except requests.RequestException as e:
                print(f"[indices] ::error:: {slug}: {e}", file=sys.stderr)
                n_fail += 1
                continue
            print(
                f"[indices]   -> {status} ({n_rows} linhas)",
                file=sys.stderr,
            )
            if status == "hit":
                n_hit += 1
            elif status == "new":
                n_new += 1
            else:
                n_upd += 1
    print(
        f"[indices] resumo: hit={n_hit} new={n_new} updated={n_upd} "
        f"falhas={n_fail}",
        file=sys.stderr,
    )
    return 1 if n_fail else 0


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------

def _make_fake_xls_bytes(*, with_pmr: bool) -> bytes:
    """Gera um XLSX em memoria com 5 linhas no schema ANBIMA.

    `with_pmr=False` simula familia IDA (9 colunas); `with_pmr=True`
    simula familia IMA (10 colunas).
    """
    cols = [
        "Índice", "Data de Referência", "Número Índice",
        "Variação Diária (%)", "Variação no Mês (%)",
        "Variação no Ano (%)", "Variação 12 Meses (%)",
        "Variação 24 Meses (%)", "Duration (d.u.)",
    ]
    if with_pmr:
        cols.append("PMR")
    data = []
    for i in range(5):
        row = [
            "IMA-B" if with_pmr else "IDA - Geral",
            datetime(2026, 5, 10 + i),
            1000.0 + i,
            0.01 * i, 0.05 * i, 0.10 * i, 1.0 + 0.1 * i, 2.0 + 0.1 * i,
            1000 + i,
        ]
        if with_pmr:
            row.append(500.0 + i)
        data.append(row)
    df = pd.DataFrame(data, columns=cols)
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


class _SelfTest(unittest.TestCase):

    def test_parse_xls_5_linhas_ida(self):
        b = _make_fake_xls_bytes(with_pmr=False)
        nome, rows = parse_xls_to_records(b)
        self.assertEqual(nome, "IDA - Geral")
        self.assertEqual(len(rows), 5)
        # cada row tem 9 campos (pmr fica None para IDA).
        for r in rows:
            self.assertEqual(len(r), 9)
            self.assertIsNone(r[8])

    def test_parse_xls_5_linhas_ima(self):
        b = _make_fake_xls_bytes(with_pmr=True)
        nome, rows = parse_xls_to_records(b)
        self.assertEqual(nome, "IMA-B")
        self.assertEqual(len(rows), 5)
        # IMA tem pmr nao-None.
        for r in rows:
            self.assertEqual(len(r), 9)
            self.assertIsNotNone(r[8])

    def test_cache_hit_nao_re_baixa(self):
        # Cenario: Last-Modified do HEAD == do cache; fetch_slug retorna
        # "hit" sem chamar _http_get.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            global OUT_DIR
            old = OUT_DIR
            OUT_DIR = Path(td)
            try:
                slug = "IDAGERAL"
                (OUT_DIR / f"{slug}.json").write_text(
                    json.dumps({
                        "indice": "IDA - Geral", "familia": "IDA",
                        "slug": slug,
                        "fetched_at": "2026-05-10T00:00:00Z",
                        "last_modified": "Fri, 15 May 2026 11:58:02 GMT",
                        "columns": _OUT_COLUMNS,
                        "rows": [["2026-05-14", 1.0, 0.0, 0.0, 0.0,
                                  0.0, 0.0, 1000, None]],
                    }),
                    encoding="utf-8",
                )
                fake_head = mock.Mock()
                fake_head.headers = {
                    "Last-Modified": "Fri, 15 May 2026 11:58:02 GMT",
                }
                with mock.patch(f"{__name__}._http_head",
                                return_value=fake_head) as h_mock, \
                     mock.patch(f"{__name__}._http_get") as g_mock:
                    status, n = fetch_slug(slug, force=False)
                self.assertEqual(status, "hit")
                self.assertEqual(n, 1)
                h_mock.assert_called_once()
                g_mock.assert_not_called()
            finally:
                OUT_DIR = old


def run_self_test() -> int:
    suite = unittest.TestLoader().loadTestsFromTestCase(_SelfTest)
    res = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if res.wasSuccessful() else 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Coleta indices ANBIMA (IDA + IMA) do S3 publico.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Ignora cache (Last-Modified) e refaz todos os GETs.",
    )
    p.add_argument(
        "--self-test", action="store_true",
        help="Executa cenarios in-process (parse / cache / IDA+IMA).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        return run_self_test()
    return run_all(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
