#!/usr/bin/env python3
"""Estudo empírico: lag entre release de resultado (PDF, IPE) e ITR estruturado.

Complementa docs/estudo-cvm-timing-divulgacao.md. Precisa de acesso de rede a
dados.cvm.gov.br (bloqueado em alguns ambientes gerenciados — rode local).

Mede duas coisas:

1. --head-only: cadência de regeneração dos ZIPs anuais do Portal Dados
   Abertos (Last-Modified/Content-Length de ITR + DFP + IPE do ano). Rodar
   1x/dia por alguns dias revela a cadência real (a documentada é semanal).

2. Estudo completo: baixa ipe_cia_aberta_<ANO>.zip + itr_cia_aberta_<ANO>.zip,
   casa por CNPJ x trimestre o release (categoria IPE "Dados
   Econômico-Financeiros", 1ª entrega) com o ITR (1ª versão, DT_RECEB) e
   imprime a distribuição do delta em dias:
       delta < 0  → release protocolado ANTES do ITR
       delta == 0 → mesmo dia (padrão esperado)
       delta > 0  → ITR protocolado antes do release

Uso:
    python3 scripts/estudo_cvm_timing.py --ano 2026
    python3 scripts/estudo_cvm_timing.py --ano 2026 --head-only
    python3 scripts/estudo_cvm_timing.py --ano 2026 --empresa AEGEA --empresa SABESP
    python3 scripts/estudo_cvm_timing.py --ano 2026 --out /tmp/lag_detalhe.csv
"""

from __future__ import annotations

import argparse
import io
import sys
import tempfile
import unicodedata
import zipfile
from pathlib import Path

import pandas as pd
import requests

BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC"
ZIPS = {
    "ITR": BASE + "/ITR/DADOS/itr_cia_aberta_{ano}.zip",
    "DFP": BASE + "/DFP/DADOS/dfp_cia_aberta_{ano}.zip",
    "IPE": BASE + "/IPE/DADOS/ipe_cia_aberta_{ano}.zip",
}
HEADERS = {"User-Agent": "anbima-debentures-monitor/estudo-cvm-timing"}
TIMEOUT = 120


def _norm(s: str) -> str:
    """minúsculo + sem acento, para matching robusto de colunas/categorias."""
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def _col(df: pd.DataFrame, *candidatos: str) -> str:
    """Acha coluna por nome normalizado (schemas variam entre anos)."""
    normmap = {_norm(c): c for c in df.columns}
    for cand in candidatos:
        if _norm(cand) in normmap:
            return normmap[_norm(cand)]
    raise KeyError(f"nenhuma coluna {candidatos} em {list(df.columns)}")


def _parse_dates(serie: pd.Series) -> pd.Series:
    """ISO por padrão; se a maioria falhar, tenta DD/MM/YYYY."""
    iso = pd.to_datetime(serie, errors="coerce", format="mixed")
    if iso.isna().mean() > 0.5:
        iso = pd.to_datetime(serie, errors="coerce", dayfirst=True)
    return iso.dt.normalize()


def head_only(ano: int) -> None:
    print(f"Cadência dos ZIPs do Portal Dados Abertos — ano {ano}")
    print("(rode diariamente e compare Last-Modified para medir a cadência real)\n")
    for nome, tpl in ZIPS.items():
        url = tpl.format(ano=ano)
        try:
            r = requests.head(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
            lm = r.headers.get("Last-Modified", "?")
            cl = r.headers.get("Content-Length", "?")
            print(f"  {nome:4s} HTTP {r.status_code}  Last-Modified: {lm}  bytes: {cl}")
        except requests.RequestException as exc:
            print(f"  {nome:4s} ERRO: {exc}")


def baixar(url: str, cache_dir: Path, refresh: bool) -> Path:
    destino = cache_dir / url.rsplit("/", 1)[-1]
    if destino.exists() and not refresh:
        print(f"  cache: {destino}")
        return destino
    print(f"  baixando {url} ...")
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT * 5)
    r.raise_for_status()
    destino.write_bytes(r.content)
    return destino


def ler_csv_do_zip(zip_path: Path, nome_contem: str) -> pd.DataFrame:
    """Lê o CSV cujo nome contém `nome_contem` (case-insensitive)."""
    with zipfile.ZipFile(zip_path) as zf:
        candidatos = [n for n in zf.namelist() if nome_contem.lower() in n.lower()]
        if not candidatos:
            raise FileNotFoundError(f"nenhum '{nome_contem}' em {zip_path.name}: {zf.namelist()}")
        # o CSV "capa" é o de nome mais curto (sem sufixo _BPA_con etc.)
        alvo = min(candidatos, key=len)
        with zf.open(alvo) as fh:
            return pd.read_csv(
                io.TextIOWrapper(fh, encoding="latin-1"), sep=";", dtype=str
            )


def estudo(ano: int, cache_dir: Path, refresh: bool, filtros_empresa: list[str], out: str | None) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    ipe_zip = baixar(ZIPS["IPE"].format(ano=ano), cache_dir, refresh)
    itr_zip = baixar(ZIPS["ITR"].format(ano=ano), cache_dir, refresh)

    # ---- IPE: releases de resultado (PDF) --------------------------------
    ipe = ler_csv_do_zip(ipe_zip, f"ipe_cia_aberta_{ano}")
    c_cnpj = _col(ipe, "CNPJ_Companhia", "CNPJ_CIA")
    c_nome = _col(ipe, "Nome_Companhia", "DENOM_CIA")
    c_cat = _col(ipe, "Categoria")
    c_ref = _col(ipe, "Data_Referencia", "DT_REFER")
    c_ent = _col(ipe, "Data_Entrega", "DT_ENTREGA")

    ipe["_cnpj"] = ipe[c_cnpj].str.replace(r"\D", "", regex=True)
    releases = ipe[ipe[c_cat].map(_norm).str.contains("economico-financeiro", na=False)].copy()
    print(f"\nIPE {ano}: {len(ipe)} docs, {len(releases)} na categoria Dados Econômico-Financeiros")
    releases["_ref"] = _parse_dates(releases[c_ref])
    releases["_entrega"] = _parse_dates(releases[c_ent])
    releases["_tri"] = releases["_ref"].dt.to_period("Q")
    # 1ª entrega de release por empresa x trimestre
    rel = (
        releases.dropna(subset=["_ref", "_entrega"])
        .sort_values("_entrega")
        .groupby(["_cnpj", "_tri"], as_index=False)
        .agg(nome=(c_nome, "first"), release=("_entrega", "first"))
    )

    # ---- ITR: capa com data de protocolo ---------------------------------
    itr = ler_csv_do_zip(itr_zip, f"itr_cia_aberta_{ano}")
    i_cnpj = _col(itr, "CNPJ_CIA", "CNPJ_Companhia")
    i_ref = _col(itr, "DT_REFER", "Data_Referencia")
    i_receb = _col(itr, "DT_RECEB", "Data_Entrega")
    i_ver = _col(itr, "VERSAO", "Versao")

    itr["_cnpj"] = itr[i_cnpj].str.replace(r"\D", "", regex=True)
    itr["_ref"] = _parse_dates(itr[i_ref])
    itr["_receb"] = _parse_dates(itr[i_receb])
    itr["_ver"] = pd.to_numeric(itr[i_ver], errors="coerce")
    itr["_tri"] = itr["_ref"].dt.to_period("Q")
    primeira = (
        itr.dropna(subset=["_ref", "_receb"])
        .sort_values("_ver")
        .groupby(["_cnpj", "_tri"], as_index=False)
        .agg(itr_receb=("_receb", "first"))
    )
    print(f"ITR {ano}: {len(itr)} entregas, {len(primeira)} pares empresa x trimestre (1ª versão)")

    # ---- cruzamento -------------------------------------------------------
    m = rel.merge(primeira, on=["_cnpj", "_tri"], how="inner")
    m["delta_dias"] = (m["release"] - m["itr_receb"]).dt.days
    if filtros_empresa:
        padrao = "|".join(_norm(f) for f in filtros_empresa)
        m = m[m["nome"].map(_norm).str.contains(padrao, na=False)]

    if m.empty:
        print("\nSem pares release x ITR para os filtros dados.")
        return

    d = m["delta_dias"]
    print(f"\n=== Lag release (PDF) vs ITR estruturado — {ano} ===")
    print(f"pares empresa x trimestre com release E ITR : {len(m)}")
    print(f"mesmo dia                                    : {(d == 0).mean():6.1%}")
    print(f"|delta| <= 1 dia                             : {(d.abs() <= 1).mean():6.1%}")
    print(f"release ANTES do ITR por > 1 dia             : {(d < -1).mean():6.1%}")
    print(f"ITR antes do release por > 1 dia             : {(d > 1).mean():6.1%}")
    print(f"mediana / p10 / p90 (dias)                   : "
          f"{d.median():.0f} / {d.quantile(0.1):.0f} / {d.quantile(0.9):.0f}")

    extremos = m.reindex(m["delta_dias"].abs().sort_values(ascending=False).index).head(10)
    print("\nMaiores descolamentos (delta<0 = release antes do ITR):")
    for _, row in extremos.iterrows():
        print(f"  {row['delta_dias']:+5.0f}d  {row['nome'][:45]:45s} {row['_tri']}")

    # emissores SEM release (só ITR) — relevante para carteira de debêntures
    so_itr = primeira.merge(rel[["_cnpj", "_tri"]], on=["_cnpj", "_tri"], how="left", indicator=True)
    pct = (so_itr["_merge"] == "left_only").mean()
    print(f"\nPares com ITR mas SEM release na categoria Dados Econômico-Financeiros: {pct:.1%}")

    if out:
        m.drop(columns=["_cnpj"]).to_csv(out, index=False)
        print(f"\nDetalhe salvo em {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ano", type=int, required=True, help="ano dos ZIPs (ex.: 2026)")
    ap.add_argument("--head-only", action="store_true", help="só Last-Modified dos ZIPs")
    ap.add_argument("--empresa", action="append", default=[], help="filtro por substring do nome (repetível)")
    ap.add_argument("--cache-dir", default=str(Path(tempfile.gettempdir()) / "cvm_estudo"))
    ap.add_argument("--refresh", action="store_true", help="ignora cache local dos ZIPs")
    ap.add_argument("--out", help="CSV de saída com o detalhe por empresa x trimestre")
    args = ap.parse_args()

    if args.head_only:
        head_only(args.ano)
        return 0
    estudo(args.ano, Path(args.cache_dir), args.refresh, args.empresa, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
