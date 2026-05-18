"""Baixa benchmarks de mercado (CDI e IPCA) via BCB SGS e gera numero
indice diario acumulado base 1000.

Fontes:
  - CDI: https://api.bcb.gov.br/dados/serie/bcdata.sgs.12/dados?formato=json
    Serie 12 (CDI diario, % a.d.); ~252 pontos por ano.
  - IPCA: https://api.bcb.gov.br/dados/serie/bcdata.sgs.433/dados?formato=json
    Serie 433 (IPCA mensal, % a.m.); 1 ponto por mes (DD=01).

Saida: data/benchmarks/<SLUG>.json com schema enxuto:
  {
    "indice": "CDI" | "IPCA",
    "familia": "Benchmark",
    "slug": "CDI" | "IPCA",
    "fetched_at": "...",
    "columns": ["data", "numero_indice"],
    "rows": [["YYYY-MM-DD", float], ...]
  }

REGRA MATEMATICA: todo calculo eh COMPOSTO. Nunca aditivo. Composicao
exponencial em todas as operacoes (acumular, distribuir entre dias uteis,
combinar segmentos). Ver detalhes em IPCA abaixo.

CDI: produto direto dos (1 + valor[i]/100), com acc[0] = 1000.

IPCA: construir num indice DIARIO (apenas dias uteis) tal que a mesma
formula simples (num_fim/num_ini - 1) que o frontend usa pros outros
indices tambem funcione pra IPCA, mesmo em ranges arbitrarios que cortam
mes parcial.

Como: pra cada mes (com IPCA divulgado pelo BCB), distribui o IPCA do mes
igualmente entre os DIAS UTEIS via composicao exponencial:
    fator_diario = (1 + ipca_mes/100) ^ (1 / DU_mes)
Cada dia util do mes recebe esse fator multiplicativamente. Composicao
exata: produto dos fatores diarios = (1 + ipca_mes/100). Pra mes em curso
(ainda sem IPCA divulgado), usa proxy = ultimo IPCA mensal divulgado,
distribuindo entre os dias uteis ATE hoje, mas a base do expoente eh DU
do mes corrente (nao do mes proxy).

Self-test (--self-test): 5 cenarios cobrindo CDI direto, IPCA fechado,
IPCA parcial (exponencial != linear), IPCA mes em curso com proxy, e
sanity matematico 12m (composicao exponencial preserva o composto
mensal).
"""

from __future__ import annotations

import argparse
import json
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from _http_utils import request_with_retry
from b3_calendar import is_b3_business_day, today_brt

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "data" / "benchmarks"

SOURCES: dict[str, str] = {
    "CDI": "https://api.bcb.gov.br/dados/serie/bcdata.sgs.12/dados?formato=json",
    "IPCA": "https://api.bcb.gov.br/dados/serie/bcdata.sgs.433/dados?formato=json",
}

FAMILIA = "Benchmark"

_OUT_COLUMNS = ["data", "numero_indice"]

# BCB SGS faz content-negotiation e devolve 406 Not Acceptable quando o
# cliente nao parece um browser (User-Agent default do python-requests ou
# UA "tecnico"). Reproduzivel em runner US-east do GitHub Actions; nao
# reproduz local. Por isso usamos UA realista de browser + Accept amplo.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, */*",
}

# Corte da serie diaria do IPCA: anteriormente o acc base 1000 era ancorado
# no primeiro ponto BCB (1980-01), o que faz o num indice explodir
# (~9.5e17 em 2026) por causa da hiperinflacao BR 1980-1994. Ancoramos em
# 2000-01-01 (pos-Plano Real, pos-estabilizacao) pra manter o num indice
# em ordem de grandeza utilizavel.
IPCA_SERIES_START = date(2000, 1, 1)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _http_get_json(url: str) -> list[dict]:
    r = request_with_retry("GET", url, headers=HEADERS)
    return r.json()


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_br_date(s: str) -> date:
    """Converte 'DD/MM/YYYY' (formato BCB) para datetime.date."""
    d, m, y = s.split("/")
    return date(int(y), int(m), int(d))


def _parse_bcb_pair(item: dict) -> tuple[date, float]:
    return _parse_br_date(item["data"]), float(item["valor"])


# ---------------------------------------------------------------------------
# CDI: numero indice diario por composicao multiplicativa
# ---------------------------------------------------------------------------

def build_cdi_series(pairs: list[tuple[date, float]]) -> list[list]:
    """Constroi serie [iso_date, acc] base 1000 a partir das taxas diarias CDI.

    pairs: lista de (data, valor_pct_a_d), ordem ASC.
    """
    series: list[list] = []
    acc = 1000.0
    for d, v in pairs:
        acc *= (1.0 + v / 100.0)
        series.append([d.isoformat(), acc])
    return series


# ---------------------------------------------------------------------------
# IPCA: numero indice diario por composicao exponencial intra-mes
# ---------------------------------------------------------------------------

def _business_days_of_month(y: int, m: int) -> list[date]:
    """Todos os dias uteis B3 dentro do mes (y, m), em ordem ASC."""
    d = date(y, m, 1)
    out: list[date] = []
    while d.month == m and d.year == y:
        if is_b3_business_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _next_month(y: int, m: int) -> tuple[int, int]:
    if m == 12:
        return (y + 1, 1)
    return (y, m + 1)


def build_ipca_series(
    monthly_pairs: list[tuple[date, float]],
    today: date,
    *,
    start: date | None = None,
) -> list[list]:
    """Constroi serie diaria [iso_date, acc] do numero indice IPCA.

    monthly_pairs: pontos do BCB SGS 433 em ordem ASC (data_mes_ref, ipca_pct).
        data_mes_ref tipicamente DD=01 do mes referencia.
    today: data limite (inclusive). O ultimo mes vai ate today.
    start: data de inicio da serie diaria (default IPCA_SERIES_START =
        2000-01-01). Meses anteriores a `start` sao ignorados; acc base
        1000 eh ancorado no primeiro dia util da janela serializada.

    Logica:
      - Pra cada mes (do primeiro divulgado ate o mes corrente, inclusive):
          * Determina IPCA do mes: do BCB se divulgado, caso contrario proxy
            (ultimo IPCA divulgado).
          * DU = numero de dias uteis do MES INTEIRO (mesmo se for o mes
            corrente em curso). Garante que o expoente bate.
          * fator_diario = (1 + ipca_pct/100) ^ (1/DU).
          * Aplica fator multiplicativamente em CADA dia util do mes (ate
            today, se for o mes corrente). Cada aplicacao gera um ponto da
            serie [iso_date, acc].

    acc inicial = 1000.0 (anchor virtual antes do primeiro dia util da
    janela). NAO acumulamos desde 1980 (BCB) pra evitar que o num indice
    estoure ordens de grandeza por causa da hiperinflacao 1980-1994.
    """
    if not monthly_pairs:
        return []

    if start is None:
        start = IPCA_SERIES_START

    by_ym = {(d.year, d.month): v for d, v in monthly_pairs}
    last_known = monthly_pairs[-1][1]

    first_ym = (monthly_pairs[0][0].year, monthly_pairs[0][0].month)
    start_ym = (start.year, start.month)
    y, m = max(first_ym, start_ym)
    end_y, end_m = today.year, today.month

    series: list[list] = []
    acc = 1000.0

    while (y, m) <= (end_y, end_m):
        all_bds = _business_days_of_month(y, m)
        du = len(all_bds)
        if du == 0:
            y, m = _next_month(y, m)
            continue

        ipca_pct = by_ym.get((y, m))
        if ipca_pct is None:
            ipca_pct = last_known

        # Pra mes corrente, recorta os dias uteis ate today.
        if (y, m) == (end_y, end_m):
            bds = [d for d in all_bds if d <= today]
        else:
            bds = all_bds

        fator = (1.0 + ipca_pct / 100.0) ** (1.0 / du)
        for d in bds:
            acc *= fator
            series.append([d.isoformat(), acc])

        y, m = _next_month(y, m)

    return series


# ---------------------------------------------------------------------------
# Orquestracao
# ---------------------------------------------------------------------------

def _write_payload(slug: str, indice_label: str, rows: list[list]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "indice": indice_label,
        "familia": FAMILIA,
        "slug": slug,
        "fetched_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "columns": _OUT_COLUMNS,
        "rows": rows,
    }
    (OUT_DIR / f"{slug}.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def fetch_cdi() -> int:
    data = _http_get_json(SOURCES["CDI"])
    pairs = [_parse_bcb_pair(item) for item in data]
    pairs.sort(key=lambda p: p[0])
    series = build_cdi_series(pairs)
    _write_payload("CDI", "CDI", series)
    return len(series)


def fetch_ipca(today: date | None = None) -> int:
    today = today or today_brt()
    data = _http_get_json(SOURCES["IPCA"])
    pairs = [_parse_bcb_pair(item) for item in data]
    pairs.sort(key=lambda p: p[0])
    series = build_ipca_series(pairs, today)
    _write_payload("IPCA", "IPCA", series)
    return len(series)


def run_all() -> int:
    n_fail = 0
    try:
        n = fetch_cdi()
        print(f"[bench] CDI ok ({n} pontos)", file=sys.stderr)
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"[bench] ::error:: CDI: {e}", file=sys.stderr)
        n_fail += 1
    try:
        n = fetch_ipca()
        print(f"[bench] IPCA ok ({n} pontos)", file=sys.stderr)
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"[bench] ::error:: IPCA: {e}", file=sys.stderr)
        n_fail += 1
    return 1 if n_fail else 0


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------

class _SelfTest(unittest.TestCase):
    TOL = 1e-9

    def test_cdi_composicao_multiplicativa(self):
        # 5 dias uteis com taxas conhecidas. Validar que
        # acc[fim]/acc[ini] - 1 == produto((1+v_i/100)) - 1.
        taxas_pct = [0.05, 0.04, 0.05, 0.06, 0.05]
        # Datas arbitrarias (5 dias seguidos uteis em maio/2026).
        datas = [date(2026, 5, 11) + timedelta(days=i) for i in range(5)]
        pairs = list(zip(datas, taxas_pct))
        series = build_cdi_series(pairs)
        self.assertEqual(len(series), 5)
        # acc[-1]/1000 deveria ser produto((1+v/100)).
        prod = 1.0
        for v in taxas_pct:
            prod *= (1.0 + v / 100.0)
        rent_observado = series[-1][1] / 1000.0
        self.assertAlmostEqual(rent_observado, prod, delta=self.TOL)

    def test_ipca_mes_fechado_composicao_exata(self):
        # IPCA = 0.5%, mes com 20 DU. Forcar DU=20 construindo um mes
        # ficticio: ao inves de iterar o calendario real (que tem DU
        # variavel), chamamos build_ipca_series com um par mensal e
        # inspecionamos os fatores. Verificamos:
        #   (a) acc[bday_20]/acc_anchor == 1.005 (composicao exata)
        #   (b) todos os fatores iguais e fator^DU == 1.005
        # Usamos um mes real com 20 DU como cenario (jul/2026 tem 23 DU
        # tipicamente, mai/2026 ~21 — basta acharmos um). Verificamos o
        # invariante composicao independente do mes escolhido: produto
        # dos fatores aplicados == 1+ipca_mes.
        monthly = [(date(2026, 4, 1), 0.5)]
        today = date(2026, 4, 30)
        series = build_ipca_series(monthly, today)
        self.assertGreater(len(series), 0)
        # acc final / 1000 (anchor) == 1.005, exata.
        self.assertAlmostEqual(series[-1][1] / 1000.0, 1.005, delta=self.TOL)
        # Fatores diarios identicos (todos os deltas log iguais).
        import math
        deltas = [math.log(series[0][1] / 1000.0)] + [
            math.log(series[i][1] / series[i - 1][1])
            for i in range(1, len(series))
        ]
        for d in deltas[1:]:
            self.assertAlmostEqual(d, deltas[0], delta=self.TOL)
        # fator^DU == 1.005 (exato).
        import math as _m
        fator = _m.exp(deltas[0])
        du = len(series)
        self.assertAlmostEqual(fator ** du, 1.005, delta=self.TOL)

    def test_ipca_mes_parcial_exponencial_nao_linear(self):
        # IPCA mensal = 1.0%. Range cobrindo metade dos dias uteis do mes.
        # Validar (acc[fim]/acc[ini]) - 1 == (1.01)^(K/DU) - 1, com K dias
        # uteis cobertos. Tambem checar que NAO bate com interpolacao
        # linear (1.0% * K/DU = 0.5%).
        monthly = [(date(2026, 6, 1), 1.0)]
        today = date(2026, 6, 30)
        series = build_ipca_series(monthly, today)
        du = len(series)
        self.assertGreater(du, 5)
        # Pegar ini = primeiro dia util, fim = ~metade do mes (K dias
        # uteis depois -> K aplicacoes de fator).
        k = du // 2
        ini = series[0][1]   # acc apos 1 aplicacao
        fim = series[k][1]   # acc apos (k+1) aplicacoes -> diferenca = k fatores
        rent_obs = fim / ini - 1.0
        rent_exp_exponencial = (1.01) ** (k / du) - 1.0
        rent_errada_linear = 0.01 * (k / du)
        self.assertAlmostEqual(rent_obs, rent_exp_exponencial, delta=1e-9)
        # E nao bate com linear (a menos que 1.01^x ~ 1 + 0.01x, que
        # difere pelo termo quadratico).
        self.assertNotAlmostEqual(rent_obs, rent_errada_linear, delta=1e-6)

    def test_ipca_mes_em_curso_proxy(self):
        # Ultimo IPCA divulgado = 0.7% (mes referencia abr/2026).
        # today = 15/mai/2026 (mes em curso, sem IPCA de mai divulgado).
        # Verificar que a serie cobre X dias uteis de mai com fator
        # (1.007)^(1/DU_mai_inteiro), e que acc cresce
        # exponencialmente conforme esse proxy.
        monthly = [(date(2026, 4, 1), 0.7)]
        today = date(2026, 5, 15)
        series = build_ipca_series(monthly, today)
        # Separa pontos por mes.
        abr = [r for r in series if r[0].startswith("2026-04-")]
        mai = [r for r in series if r[0].startswith("2026-05-")]
        self.assertGreater(len(abr), 0)
        self.assertGreater(len(mai), 0)
        # Mes corrente usa proxy do ultimo divulgado (0.7%) com DU_mai_full.
        du_mai_full = len(_business_days_of_month(2026, 5))
        # acc[primeiro_dia_util_mai] / acc[ultimo_dia_util_abr]
        # = (1.007)^(1/DU_mai_full).
        ratio_first = mai[0][1] / abr[-1][1]
        fator_esperado = (1.007) ** (1.0 / du_mai_full)
        self.assertAlmostEqual(ratio_first, fator_esperado, delta=1e-12)
        # Mais geralmente: acc[K-esimo_du_mai] = acc[ultimo_du_abr] * fator^K.
        for k in range(1, len(mai) + 1):
            self.assertAlmostEqual(
                mai[k - 1][1] / abr[-1][1],
                fator_esperado ** k,
                delta=1e-9,
            )

    def test_ipca_12m_engenharia_reversa(self):
        # 14 meses sequenciais com IPCAs variados. Compara acc[ultimo_du_mes_K]
        # / acc[ultimo_du_mes_J] (= acumulado fechado J..K) contra
        # produto((1+ipca_mes_i)) dos meses correspondentes.
        #
        # acc[ultimo_du_mes_idx_i] = 1000 * produto((1+v[0..i])).
        # Razao entre month_idx J e K (J<K): produto((1+v[J+1..K])).
        # Escolhemos J=1, K=13 -> 12 fatores (v[2..13]).
        valores = [0.50, 0.30, 0.45, 0.60, 0.25, 0.40, 0.35, 0.55, 0.20,
                   0.45, 0.30, 0.50, 0.40, 0.35]
        monthly: list[tuple[date, float]] = []
        y, m = 2025, 3
        for v in valores:
            monthly.append((date(y, m, 1), v))
            y, m = _next_month(y, m)
        # `today` = ultimo dia do mes 14 (todos meses sao "fechados").
        last_y, last_m = monthly[-1][0].year, monthly[-1][0].month
        ny, nm = _next_month(last_y, last_m)
        today = date(ny, nm, 1) - timedelta(days=1)
        series = build_ipca_series(monthly, today)
        # Mapeia (y, m) -> indice do ultimo dia util na serie.
        idx_by_ym: dict[tuple[int, int], int] = {}
        for i, (iso, _) in enumerate(series):
            yy, mm, _dd = iso.split("-")
            idx_by_ym[(int(yy), int(mm))] = i
        ym_start = (monthly[1][0].year, monthly[1][0].month)
        ym_end = (monthly[13][0].year, monthly[13][0].month)
        acc_ini = series[idx_by_ym[ym_start]][1]
        acc_fim = series[idx_by_ym[ym_end]][1]
        rent_via_indice = acc_fim / acc_ini - 1.0
        prod = 1.0
        for v in valores[2:14]:  # 12 fatores
            prod *= (1.0 + v / 100.0)
        self.assertAlmostEqual(rent_via_indice, prod - 1.0, delta=1e-9)


def run_self_test() -> int:
    suite = unittest.TestLoader().loadTestsFromTestCase(_SelfTest)
    res = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if res.wasSuccessful() else 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Coleta CDI e IPCA via BCB SGS e gera num indice diario.",
    )
    p.add_argument(
        "--self-test", action="store_true",
        help="Roda 5 cenarios in-process (composicao exponencial vs linear).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        return run_self_test()
    return run_all()


if __name__ == "__main__":
    sys.exit(main())
