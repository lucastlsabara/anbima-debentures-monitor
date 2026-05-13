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
import time
from datetime import date, datetime
from pathlib import Path

import requests

from b3_calendar import resolve_default_date

ROOT = Path(__file__).parent
RAW_DIR = ROOT / "raw"

DB_URL = "https://www.anbima.com.br/informacoes/merc-sec-debentures/arqs/db{yymmdd}.txt"
ETTJ_URL = "https://www.anbima.com.br/informacoes/est-termo/CZ-down.asp?Dt_Ref={dd}/{mm}/{yyyy}&saida=csv"
# Mercado secundário de títulos públicos: tabela por título + vencimento exato
# (fonte oficial das taxas de NTN-B/LTN/NTN-F usadas como benchmark de debêntures).
TITPUB_URL = "https://www.anbima.com.br/informacoes/merc-sec/arqs/ms{yymmdd}.txt"

# User-Agent identificável: ANBIMA pode bloquear requests genéricos do
# python-requests. Mantemos o contato para cumprir polidez de scraper.
USER_AGENT = (
    "anbima-debentures-monitor/1.0 "
    "(+https://github.com/lucastlsabara/anbima-debentures-monitor)"
)
# UA "navegador" usado APENAS como fallback de ultima tentativa quando o UA
# identificavel resulta em 4xx/5xx persistente. ANBIMA ocasionalmente bloqueia
# UAs nao-browser; tentar uma vez mais com UA Chrome antes de declarar falha.
MOZILLA_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 30
# Retry exponencial só para falhas transitórias de rede (timeout, conexão,
# 5xx). 404 nunca repete — é resposta legítima da ANBIMA quando o arquivo
# do dia ainda não foi publicado, e o caller já trata como warn-and-skip.
HTTP_RETRY_ATTEMPTS = 4
HTTP_RETRY_BACKOFF_SEC = (2, 4, 8, 16)


def _http_get_once(url: str, user_agent: str) -> requests.Response:
    """GET com retry exponencial em falhas transitorias, UA configuravel.

    Erros que disparam retry: requests.ConnectionError, requests.Timeout,
    HTTPError com status 5xx ou 429. HTTPError 4xx (exceto 429) e repassado
    direto -- caller decide (404 = warn-and-skip, demais = raise).
    """
    last_exc: Exception | None = None
    for attempt in range(HTTP_RETRY_ATTEMPTS):
        try:
            r = requests.get(
                url,
                timeout=HTTP_TIMEOUT,
                headers={"User-Agent": user_agent},
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
        else:
            if r.status_code < 400 or r.status_code in (404,):
                # Sucesso ou 404 (resposta legítima a propagar para o caller).
                if r.status_code >= 400:
                    r.raise_for_status()
                return r
            if r.status_code in (429,) or r.status_code >= 500:
                last_exc = requests.HTTPError(
                    f"HTTP {r.status_code} em {url}", response=r,
                )
            else:
                # Outros 4xx (401, 403, etc.) — não tem o que tentar de novo.
                r.raise_for_status()
        if attempt < HTTP_RETRY_ATTEMPTS - 1:
            wait = HTTP_RETRY_BACKOFF_SEC[attempt]
            print(
                f"[fetch] {url}: {type(last_exc).__name__}; "
                f"retry em {wait}s (tentativa {attempt + 2}/{HTTP_RETRY_ATTEMPTS})",
                file=sys.stderr,
            )
            time.sleep(wait)
    raise last_exc if last_exc else RuntimeError(f"falha inesperada em {url}")


def _http_get(url: str) -> requests.Response:
    """GET com UA identificavel; se falhar com 4xx/5xx (exceto 404), tenta
    UMA vez com UA Mozilla/Chrome antes de propagar o erro.

    404 propaga sem fallback -- e resposta legitima da ANBIMA quando o arquivo
    do dia ainda nao foi publicado, e o caller trata via exit 2 (warn-and-skip).
    Demais falhas (4xx nao-404, 5xx persistente, timeout, ConnectionError):
    fallback UA Mozilla; se mesmo assim falhar, propaga (exit 1 no caller).
    """
    try:
        return _http_get_once(url, USER_AGENT)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            raise
        print(
            f"[fetch] {url}: falha com UA padrao ({e}); "
            f"retry final com UA Mozilla/Chrome",
            file=sys.stderr,
        )
        return _http_get_once(url, MOZILLA_UA)
    except (requests.ConnectionError, requests.Timeout) as e:
        print(
            f"[fetch] {url}: falha de rede ({type(e).__name__}); "
            f"retry final com UA Mozilla/Chrome",
            file=sys.stderr,
        )
        return _http_get_once(url, MOZILLA_UA)

DB_HEADER = [
    "codigo", "nome", "vencimento", "indice", "taxa_compra", "taxa_venda",
    "taxa_indicativa", "desvio_padrao", "intervalo_min", "intervalo_max",
    "pu", "pct_pu_par", "duration_dias", "pct_reune", "referencia_ntnb",
]

# Layout do ms<YYMMDD>.txt (mercado secundário de títulos públicos federais),
# separador '@'. Mantido como constante (paralelo a DB_HEADER) para tornar a
# extração resiliente a mudanças de posição de colunas.
MS_HEADER = [
    "tipo", "data_referencia", "cod_selic", "data_base_emissao",
    "data_vencimento", "taxa_compra", "taxa_venda", "taxa_indicativa", "pu",
    "desvio_padrao", "intervalo_inf_d0", "intervalo_sup_d0",
    "intervalo_inf_d1", "intervalo_sup_d1", "criterio",
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
    r = _http_get(url)
    text = r.content.decode("latin-1")
    out = RAW_DIR / f"db{yymmdd}.txt"
    out.write_text(text, encoding="utf-8")
    return text


def fetch_ettj(target: date) -> str:
    url = ETTJ_URL.format(dd=f"{target.day:02d}", mm=f"{target.month:02d}", yyyy=target.year)
    r = _http_get(url)
    text = r.content.decode("latin-1")
    out = RAW_DIR / f"ettj_{_iso(target)}.csv"
    out.write_text(text, encoding="utf-8")
    return text


def fetch_titulos_publicos(target: date) -> str:
    yymmdd = target.strftime("%y%m%d")
    url = TITPUB_URL.format(yymmdd=yymmdd)
    r = _http_get(url)
    text = r.content.decode("latin-1")
    out = RAW_DIR / f"ms{yymmdd}.txt"
    out.write_text(text, encoding="utf-8")
    return text


def _ms_records(text: str):
    """Itera linhas válidas do ms.txt como dict alinhado a MS_HEADER.

    Linhas curtas recebem padding com '' (mesma estratégia de parse_db),
    para não descartar registros com campos opcionais vazios.
    """
    for ln in text.splitlines():
        if not ln or "@" not in ln:
            continue
        parts = [p.strip() for p in ln.split("@")]
        tipo = parts[0]
        if tipo in ("Titulo", "") or tipo.startswith("ANBIMA"):
            continue
        if len(parts) < len(MS_HEADER):
            parts = parts + [""] * (len(MS_HEADER) - len(parts))
        yield dict(zip(MS_HEADER, parts))


def parse_titulos_publicos(text: str) -> dict[str, dict[str, float]]:
    """Tabela {Titulo: {vencimento_iso: taxa_indicativa}}.

    Layout do arquivo: cabeçalho separado por '@', linhas de dados começando
    com o nome do título (LTN/NTN-B/NTN-F/NTN-C/LFT). Datas em YYYYMMDD.
    """
    out: dict[str, dict[str, float]] = {}
    for rec in _ms_records(text):
        venc_iso = _yyyymmdd_to_iso(rec["data_vencimento"])
        if not venc_iso:
            continue
        taxa = _br_float(rec["taxa_indicativa"])
        if taxa is None:
            continue
        out.setdefault(rec["tipo"], {})[venc_iso] = taxa
    return out


def _yyyymmdd_to_iso(s: str) -> str | None:
    s = (s or "").strip()
    if len(s) != 8 or not s.isdigit():
        return None
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def parse_titpub_rows(
    text: str, data_referencia: date,
) -> tuple[list[dict], str | None]:
    """Parser detalhado do arquivo ANBIMA ms<YYMMDD>.txt (mercado secundário
    de títulos públicos federais), com schema alinhado ao do dashboard.

    Cabeçalho real (separador '@'), conforme arquivo emitido pela ANBIMA:
        Titulo @ Data Referencia @ Codigo SELIC @ Data Base/Emissao @
        Data Vencimento @ Tx. Compra @ Tx. Venda @ Tx. Indicativas @ PU @
        Desvio padrao @ Interv. Ind. Inf. (D0) @ Interv. Ind. Sup. (D0) @
        Interv. Ind. Inf. (D+1) @ Interv. Ind. Sup. (D+1) @ Criterio

    Datas vêm em YYYYMMDD; números em formato BR (vírgula decimal). Campos
    vazios viram None. Ignora cabeçalho ('ANBIMA…', linha vazia, 'Titulo…').

    Duration: distância simples (dias corridos) entre data_referencia e
    vencimento. É APROXIMAÇÃO — para NTN-B/NTN-F com cupom semestral a
    duration de Macaulay seria menor; usar somente como eixo no scatter da
    aba 'Títulos Públicos', não para cálculo de risco.

    Retorna (rows, internal_data_iso) — internal_data_iso é a Data Referencia
    extraída do próprio arquivo (parts[1]). Caller compara com a target para
    detectar divergência (ANBIMA reservindo arquivo de outra data no mesmo URL).
    None se nenhuma linha tiver data válida ou se houver mais de uma data
    distinta no arquivo (caso anômalo).
    """
    rows: list[dict] = []
    internal_dates: set[str] = set()
    for rec in _ms_records(text):
        d_ref_iso = _yyyymmdd_to_iso(rec["data_referencia"])
        if d_ref_iso:
            internal_dates.add(d_ref_iso)
        venc_iso = _yyyymmdd_to_iso(rec["data_vencimento"])
        if not venc_iso:
            continue
        try:
            venc_date = datetime.strptime(venc_iso, "%Y-%m-%d").date()
        except ValueError:
            continue
        dur_dias = (venc_date - data_referencia).days
        # Títulos já vencidos eventualmente aparecem no arquivo da ANBIMA
        # (linhas residuais de dias após o vencimento). Duration negativa
        # quebra o eixo dos gráficos e gera linhas inúteis nas tabelas.
        if dur_dias < 0:
            continue
        rows.append({
            "tipo": rec["tipo"],
            "vencimento": venc_iso,
            "cod_selic": rec["cod_selic"],
            "taxa_compra": _br_float(rec["taxa_compra"]),
            "taxa_venda": _br_float(rec["taxa_venda"]),
            "taxa_indicativa": _br_float(rec["taxa_indicativa"]),
            "pu": _br_float(rec["pu"]),
            "desvio_padrao": _br_float(rec["desvio_padrao"]),
            "intervalo_min": _br_float(rec["intervalo_inf_d0"]),
            "intervalo_max": _br_float(rec["intervalo_sup_d0"]),
            "duration_dias": dur_dias,
            "duration_anos": round(dur_dias / 365.25, 3),
        })
    internal_iso = next(iter(internal_dates)) if len(internal_dates) == 1 else None
    return rows, internal_iso


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


def parse_ettj_pre(text: str) -> list[dict] | None:
    """Extrai a tabela 'ETTJ Pre' (LTN/NTN-F) da resposta CZ-down.

    A ANBIMA emite no MESMO CSV duas tabelas distintas para Pré:
    "ETTJ Pre" (taxa nominal pré, vértices DU × taxa) e
    "ETTJ Inflação Implícita (IPCA)" (que repete `taxa_pref` numa coluna).
    Esta função procura especificamente o cabeçalho "ETTJ Pre" / "ETTJ Pré";
    se não achar, retorna None — o caller faz fallback para a coluna pref da
    tabela de inflação implícita.

    Retorna [{vertice_du: int, taxa_pre: float}, ...].
    """
    lines = [ln.rstrip("\r") for ln in text.splitlines()]
    start = None
    for i, ln in enumerate(lines):
        head = ln.strip().upper()
        # Casa "ETTJ PRE", "ETTJ PRÉ", "ETTJ PREFIXADA", mas nunca "ETTJ INFLA".
        if head.startswith("ETTJ PRE") and "INFLA" not in head:
            start = i
            break
    if start is None:
        return None
    rows: list[dict] = []
    for ln in lines[start + 2:]:
        if not ln.strip():
            break
        cells = [c.strip() for c in ln.split(";")]
        if len(cells) < 2:
            continue
        v = _br_int(cells[0])
        taxa = _br_float(cells[1])
        if v is None or taxa is None:
            continue
        rows.append({"vertice_du": v, "taxa_pre": taxa})
    return rows or None


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
    p.add_argument(
        "--date",
        default=None,
        help="Data de referência YYYY-MM-DD. Default: hoje (BRT).",
    )
    p.add_argument("--out", default="parsed.json", help="Saída JSON consolidada.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target = resolve_default_date()
        print(f"[fetch] --date não informado; usando default {target.isoformat()}", file=sys.stderr)
    RAW_DIR.mkdir(exist_ok=True)

    print(f"[fetch] db de {target.isoformat()}", file=sys.stderr)
    try:
        db_text = fetch_db(target)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            print(
                f"[fetch] db de {target.isoformat()} ainda não publicado pela ANBIMA "
                f"(404); pulando sem falhar.",
                file=sys.stderr,
            )
            return 2
        raise
    debentures = parse_db(db_text)
    print(f"[fetch]   -> {len(debentures)} papéis", file=sys.stderr)

    print(f"[fetch] ETTJ de {target.isoformat()}", file=sys.stderr)
    try:
        ettj_text = fetch_ettj(target)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            print(
                f"[fetch] ETTJ de {target.isoformat()} ainda não publicada pela ANBIMA "
                f"(404); pulando sem falhar.",
                file=sys.stderr,
            )
            return 2
        raise
    ettj_date, ettj_rows = parse_ettj(ettj_text)
    print(f"[fetch]   -> {len(ettj_rows)} vértices (data publicada: {ettj_date})", file=sys.stderr)

    # ETTJ Pré dedicada (item 12). Graceful fallback: se ANBIMA não emitiu a
    # tabela "ETTJ Pre" no CSV, prefixados ficarão sem referência exclusiva e
    # cairão no método "ettj_pre_interp" via taxa_pref da tabela IPCA.
    ettj_pre_rows = parse_ettj_pre(ettj_text)
    if ettj_pre_rows is None:
        print("[warn ] tabela 'ETTJ Pre' não encontrada no CSV ANBIMA; "
              "spreads de prefixados usarão a coluna taxa_pref da tabela "
              "Inflação Implícita.", file=sys.stderr)
    else:
        print(f"[fetch]   -> ETTJ Pre: {len(ettj_pre_rows)} vértices", file=sys.stderr)

    if ettj_date and ettj_date != target.isoformat():
        print(
            f"[warn ] ETTJ retornou data {ettj_date} (esperado {target.isoformat()})",
            file=sys.stderr,
        )

    print(f"[fetch] títulos públicos de {target.isoformat()}", file=sys.stderr)
    titpub: dict[str, dict[str, float]] = {}
    titpub_rows: list[dict] = []
    titpub_status = "ok"
    try:
        tp_text = fetch_titulos_publicos(target)
    except requests.HTTPError as e:
        # Espelha fetch_db: 404 = warn-and-skip (ANBIMA não publicou ainda);
        # 5xx ou outros HTTPError viram raise (acionam retry/exit não-zero).
        # ms.txt é opcional para a pipeline (debêntures/ETTJ continuam), por
        # isso aqui não retornamos exit 2 como em fetch_db; apenas marcamos
        # titpub_status='404' e seguimos sem titpub_rows.
        if e.response is not None and e.response.status_code == 404:
            print(
                f"[warn ] títulos públicos de {target.isoformat()} ainda não "
                f"publicados pela ANBIMA (404); seguindo sem referência exata.",
                file=sys.stderr,
            )
            titpub_status = "404"
        else:
            raise
    else:
        titpub = parse_titulos_publicos(tp_text)
        titpub_rows, internal_iso = parse_titpub_rows(tp_text, target)
        if internal_iso and internal_iso != target.isoformat():
            print(
                f"[warn ] ms.txt: Data Referência interna ({internal_iso}) "
                f"diverge da target ({target.isoformat()}); "
                f"marcando titpub_status='data_divergente'.",
                file=sys.stderr,
            )
            titpub_status = "data_divergente"
        sizes = ", ".join(f"{k}={len(v)}" for k, v in sorted(titpub.items()))
        print(
            f"[fetch]   -> {sizes} | titpub_rows={len(titpub_rows)} | "
            f"status={titpub_status}",
            file=sys.stderr,
        )

    out = {
        "data_referencia": target.isoformat(),
        "ettj_data_publicada": ettj_date,
        "debentures": debentures,
        "ettj_ipca": ettj_rows,
        "ettj_pre": ettj_pre_rows,
        "titulos_publicos": titpub,
        "titpub_rows": titpub_rows,
        "titpub_status": titpub_status,
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[fetch] -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
