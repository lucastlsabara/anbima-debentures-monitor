"""Baixa os indices ANBIMA (IDA e IMA) para os ultimos 5 dias uteis B3.

Indices cobertos:
  - IDA - Indice de Debentures ANBIMA (Quadro Resumo): IDA-DI, IDA-IPCA,
    IDA-IPCA Infraestrutura, IDA-IPCA ex-Infraestrutura, IDA-GERAL.
  - IMA - Indices de Mercado ANBIMA (TOTAIS): IRF-M 1, IRF-M 1+, IRF-M,
    IMA-B 5, IMA-B 5+, IMA-B, IMA-S, IMA-GERAL-EX-C, IMA-GERAL.

Saida: data/anbima_indices/<YYYY-MM-DD>.json (um arquivo por dia util).

Fontes (validadas empiricamente):
  - IDA: POST https://www.anbima.com.br/informacoes/ida/IDA_result.asp
    com body urlencoded; resposta vem como HTML (apesar de saida=csv).
  - IMA dia mais recente: GET https://www.anbima.com.br/informacoes/ima/
    arqs/ima_completo.txt (formato delimitado por '@').
  - IMA dias anteriores: POST https://www.anbima.com.br/informacoes/ima/
    IMA-geral-down.asp com Titulo_1='quadro-resumo' (retorna CSV ';').

Limitacao: o portal publico so disponibiliza os ultimos ~5 du B3. Historico
maior requer ANBIMA Feed (credenciais, fora de escopo).

Politica de escrita: nunca sobrescreve <date>.json existente, exceto com
--force. Assim cada execucao acumula novos dias sem retrabalhar passado.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests

from _http_utils import request_with_retry
from b3_api import parse_br_float, parse_br_float_optional
from b3_calendar import is_b3_business_day, today_brt

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "data" / "anbima_indices"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

IDA_URL = "https://www.anbima.com.br/informacoes/ida/IDA_result.asp"
IMA_COMPLETO_URL = "https://www.anbima.com.br/informacoes/ima/arqs/ima_completo.txt"
IMA_DOWN_URL = "https://www.anbima.com.br/informacoes/ima/IMA-geral-down.asp"

IDA_TITLE_MARKER = "IDA - Indice de Debentures ANBIMA - Quadro Resumo"
IDA_ERROR_MARKER = "Ocorreu um erro na leitura do arquivo XML"

IDA_COLUMNS = [
    "subindice",
    "data",
    "numero_indice",
    "variacao_diaria_pct",
    "variacao_mensal_pct",
    "variacao_anual_pct",
    "variacao_12m_pct",
    "variacao_24m_pct",
    "duration_du",
    "peso_pct",
    "valor_mercado_milhoes",
]

# Schema canonico do IMA. Ambas as fontes (TXT direto e CSV via download)
# preservam a mesma ordem; a unica diferenca observada e o swap entre
# Duration e Peso, normalizado na hora do parse.
IMA_COLUMNS = [
    "data",
    "subindice",
    "numero_indice",
    "variacao_diaria_pct",
    "variacao_mensal_pct",
    "variacao_anual_pct",
    "variacao_12m_pct",
    "variacao_24m_pct",
    "duration_du",
    "peso_geral_pct",
    "carteira_mercado_milhares",
    "numero_operacoes",
    "quant_negociada_milhares",
    "valor_negociado_milhares",
    "pmr",
    "convexidade",
    "yield",
    "redemption_yield",
]


# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------

def _http_post(url: str, data: dict) -> requests.Response:
    return request_with_retry(
        "POST",
        url,
        data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )


def _http_get(url: str) -> requests.Response:
    return request_with_retry("GET", url, headers={"User-Agent": USER_AGENT})


# ----------------------------------------------------------------------------
# IDA: parse HTML via html.parser (stdlib)
# ----------------------------------------------------------------------------

class _IDATableParser(HTMLParser):
    """Coleta linhas de TODAS as tabelas do HTML para identificar a tabela
    'IDA - Quadro Resumo' pelo titulo na 1a celula da 1a linha.

    O HTML do IDA_result.asp e malformado em varios sentidos:
      - tabela alvo esta aninhada em tabelas de layout (precisa stack);
      - 3 das 5 data rows nao abrem <tr> antes dos <td> (so fecham </tr>
        no final). Pra tolerar isso, acumulamos celulas no nivel da tabela
        e flush'amos como nova linha em cada </tr> (ou novo <tr>).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # Stack de tabelas; para cada tabela mantemos:
        #   - rows: linhas ja fechadas
        #   - pending: celulas acumuladas desde o ultimo flush
        self._tbl_stack: list[dict] = []
        # Stack de buffers de celula (None = nao estamos dentro de td/th).
        self._cell_stack: list[list[str] | None] = []
        self.tables: list[list[list[str]]] = []

    def _flush_row(self) -> None:
        if not self._tbl_stack:
            return
        tbl = self._tbl_stack[-1]
        if tbl["pending"]:
            tbl["rows"].append(tbl["pending"])
            tbl["pending"] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "table":
            self._tbl_stack.append({"rows": [], "pending": []})
        elif t == "tr":
            # tolerancia: fecha row pendente antes de iniciar nova.
            self._flush_row()
        elif t in ("td", "th") and self._tbl_stack:
            self._cell_stack.append([])

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in ("td", "th") and self._cell_stack:
            buf = self._cell_stack.pop()
            if buf is not None and self._tbl_stack:
                self._tbl_stack[-1]["pending"].append("".join(buf).strip())
        elif t == "tr":
            self._flush_row()
        elif t == "table" and self._tbl_stack:
            self._flush_row()
            tbl = self._tbl_stack.pop()
            self.tables.append(tbl["rows"])

    def handle_data(self, data):
        if self._cell_stack and self._cell_stack[-1] is not None:
            self._cell_stack[-1].append(data)

    def finalize(self) -> list[list[str]]:
        """Identifica a tabela alvo pelo titulo na 1a celula da 1a linha."""
        marker_norm = _strip_accents(IDA_TITLE_MARKER).lower()
        for table in self.tables:
            if not table or not table[0]:
                continue
            first_cell = table[0][0] if table[0] else ""
            if marker_norm in _strip_accents(first_cell).lower():
                return [r for r in table[2:] if any(c.strip() for c in r)]
        return []


def _strip_accents(s: str) -> str:
    import unicodedata
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def parse_ida_html(html: str) -> dict:
    """HTML do IDA_result.asp -> dict com columns/rows/status.

    Status:
      - 'unavailable' (reason='non_business_day') se IDA_ERROR_MARKER presente.
      - 'unavailable' (reason='parse_error') se tabela alvo nao for encontrada
        ou tiver header inesperado.
      - 'ok' caso 5 linhas (sub-indices IDA) sejam extraidas com sucesso.
    """
    if IDA_ERROR_MARKER in html:
        # ANBIMA usa o mesmo erro pra (a) fds/feriado e (b) dia util ainda
        # nao publicado. Caller (`fetch_ida_qr`) decide o reason exato.
        return {
            "status": "unavailable",
            "reason": "non_business_day",
            "columns": IDA_COLUMNS,
            "rows": [],
        }
    parser = _IDATableParser()
    parser.feed(html)
    raw_rows = parser.finalize()
    if not raw_rows:
        return {
            "status": "unavailable",
            "reason": "parse_error",
            "columns": IDA_COLUMNS,
            "rows": [],
        }
    rows: list[list] = []
    for r in raw_rows:
        if len(r) != 11:
            continue
        subindice = r[0].strip()
        data_str = r[1].strip()
        try:
            numero_indice = parse_br_float(r[2])
            var_d = parse_br_float(r[3])
            var_m = parse_br_float(r[4])
            var_a = parse_br_float(r[5])
            var_12 = parse_br_float(r[6])
            var_24 = parse_br_float(r[7])
            duration = parse_br_float_optional(r[8])
            peso = parse_br_float(r[9])
            valor = parse_br_float(r[10])
        except ValueError:
            continue
        rows.append([
            subindice, data_str, numero_indice, var_d, var_m, var_a,
            var_12, var_24, duration, peso, valor,
        ])
    if not rows:
        return {
            "status": "unavailable",
            "reason": "parse_error",
            "columns": IDA_COLUMNS,
            "rows": [],
        }
    return {
        "status": "ok",
        "reason": "",
        "columns": IDA_COLUMNS,
        "rows": rows,
    }


def fetch_ida_qr(date_iso: str) -> dict:
    """POST IDA_result.asp para `date_iso` (YYYY-MM-DD) e parseia."""
    dt = datetime.strptime(date_iso, "%Y-%m-%d").date()
    body = {
        "tipo": "qr",
        "indice": "GERAL",
        "escolha": "1",
        "Idioma": "PT",
        "saida": "csv",
        "DataIniM": f"{dt.month:02d}/{dt.year}",
        "DataIniD": f"{dt.day:02d}/{dt.month:02d}/{dt.year}",
    }
    try:
        resp = _http_post(IDA_URL, body)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        return {
            "status": "unavailable",
            "reason": f"http_error_{status}" if status else "http_error",
            "columns": IDA_COLUMNS,
            "rows": [],
        }
    except (requests.ConnectionError, requests.Timeout):
        return {
            "status": "unavailable",
            "reason": "http_error",
            "columns": IDA_COLUMNS,
            "rows": [],
        }
    html = resp.content.decode("iso-8859-1", errors="replace")
    parsed = parse_ida_html(html)
    # Refina o reason quando o caller tem a data: se for dia util B3, ainda
    # nao publicado; senao, fds/feriado.
    if parsed["status"] == "unavailable" and parsed["reason"] == "non_business_day":
        if is_b3_business_day(dt):
            parsed["reason"] = "not_yet_published"
    return parsed


# ----------------------------------------------------------------------------
# IMA: parse do arquivo TXT '@'-delimited
# ----------------------------------------------------------------------------

def _f(s: str) -> float | None:
    """parse numero BR; '--' ou '' -> None."""
    s = (s or "").strip()
    if not s or s == "--" or s == "-":
        return None
    return parse_br_float_optional(s)


def parse_ima_completo_txt(txt: str) -> dict:
    """Parseia ima_completo.txt.

    Layout:
      0@... (identificacao, ignorado)
      1@TOTAIS (marker)
      1@<headers> (data ref, indice, numero indice, ...)
      1@<data row> x 9 sub-indices

    Tudo apos a primeira linha '2@...' (composicao de carteira detalhada) e
    ignorado nessa primeira versao.
    """
    lines = txt.splitlines()
    totais_section = False
    data_rows: list[list[str]] = []
    ref_date_iso: str | None = None
    for ln in lines:
        if ln.startswith("2@"):
            break
        if not ln.startswith("1@"):
            continue
        payload = ln[2:]
        if payload.strip().upper() == "TOTAIS":
            totais_section = True
            header_seen = False
            continue
        if not totais_section:
            continue
        parts = payload.split("@")
        if not header_seen:
            header_seen = True
            continue
        if len(parts) < 18:
            continue
        data_rows.append(parts)
    if not data_rows:
        return {
            "status": "unavailable",
            "reason": "parse_error",
            "columns": IMA_COLUMNS,
            "rows": [],
        }
    # Layout TXT:
    #   [0]=Data, [1]=INDICE, [2]=Numero Indice, [3]=Var Diaria, [4]=Var Mensal,
    #   [5]=Var Anual, [6]=Var 12m, [7]=Var 24m, [8]=Duration, [9]=Peso,
    #   [10]=Carteira Mercado, [11]=N Operacoes, [12]=Qtd Negoc, [13]=Val Negoc,
    #   [14]=PMR, [15]=Convexidade, [16]=Yield, [17]=Redemption Yield.
    rows: list[list] = []
    for r in data_rows:
        try:
            data_str = r[0].strip()
            rows.append([
                data_str, r[1].strip(),
                _f(r[2]), _f(r[3]), _f(r[4]), _f(r[5]), _f(r[6]), _f(r[7]),
                _f(r[8]), _f(r[9]), _f(r[10]), _f(r[11]), _f(r[12]), _f(r[13]),
                _f(r[14]), _f(r[15]), _f(r[16]), _f(r[17]),
            ])
            # captura primeira data util pra retorno
            if not ref_date_iso and "/" in data_str:
                d, m, y = data_str.split("/")
                ref_date_iso = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
        except (IndexError, ValueError):
            continue
    if not rows:
        return {
            "status": "unavailable",
            "reason": "parse_error",
            "columns": IMA_COLUMNS,
            "rows": [],
        }
    return {
        "status": "ok",
        "reason": "",
        "source": "ima_completo.txt",
        "columns": IMA_COLUMNS,
        "rows": rows,
        "_ref_date_iso": ref_date_iso,
    }


def fetch_ima_qr_latest() -> dict:
    """GET ima_completo.txt e parseia. Retorna dict com `_ref_date_iso`."""
    try:
        resp = _http_get(IMA_COMPLETO_URL)
    except (requests.HTTPError, requests.ConnectionError, requests.Timeout):
        return {
            "status": "unavailable",
            "reason": "http_error",
            "source": "ima_completo.txt",
            "columns": IMA_COLUMNS,
            "rows": [],
        }
    txt = resp.content.decode("iso-8859-1", errors="replace")
    return parse_ima_completo_txt(txt)


# ----------------------------------------------------------------------------
# IMA: historico via IMA-geral-down.asp (CSV ';')
# ----------------------------------------------------------------------------

def parse_ima_csv(csv_text: str) -> dict:
    """Parseia CSV de IMA-geral-down.asp.

    Layout (1 linha titulo + 1 linha header + 9 data rows):
      'TOTAIS - IMA-GERAL'
      'Indice;Data de Referencia;Numero Indice;Var Diaria(%);Var Mensal(%);
       Var Anual(%);Var 12m(%);Var 24m(%);Peso(%);Duration(d.u.);
       Carteira a Mercado(R$ mil);N Operacoes *;Qtd Negoc(1.000 titulos) *;
       Val Negoc(R$ mil) *;PMR;Convexidade;Yield;Redemption Yield'
    """
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    if len(lines) < 3:
        return {
            "status": "unavailable",
            "reason": "parse_error",
            "source": "ima_geral_down",
            "columns": IMA_COLUMNS,
            "rows": [],
        }
    data_lines = lines[2:]
    rows: list[list] = []
    for ln in data_lines:
        parts = ln.split(";")
        if len(parts) < 18:
            continue
        try:
            subindice = parts[0].strip()
            data_str = parts[1].strip()
            # CSV: [...8]=Peso, [9]=Duration. Swap pra encaixar no schema
            # canonico (Duration, Peso).
            rows.append([
                data_str, subindice,
                _f(parts[2]), _f(parts[3]), _f(parts[4]), _f(parts[5]),
                _f(parts[6]), _f(parts[7]),
                _f(parts[9]),  # duration
                _f(parts[8]),  # peso
                _f(parts[10]), _f(parts[11]), _f(parts[12]), _f(parts[13]),
                _f(parts[14]), _f(parts[15]), _f(parts[16]), _f(parts[17]),
            ])
        except (IndexError, ValueError):
            continue
    if not rows:
        return {
            "status": "unavailable",
            "reason": "parse_error",
            "source": "ima_geral_down",
            "columns": IMA_COLUMNS,
            "rows": [],
        }
    return {
        "status": "ok",
        "reason": "",
        "source": "ima_geral_down",
        "columns": IMA_COLUMNS,
        "rows": rows,
    }


def fetch_ima_qr_historical(date_iso: str) -> dict:
    """POST IMA-geral-down.asp pra dia historico (dentro da janela 5 du B3)."""
    dt = datetime.strptime(date_iso, "%Y-%m-%d").date()
    body = {
        "Titulo_1": "quadro-resumo",
        "Titulo_2": "quadro-resumo",
        "Tipo": "1",
        "DataRef": f"{dt.day:02d}/{dt.month:02d}/{dt.year}",
        "Pai": "ima",
        "escolha": "1",
        "Idioma": "PT",
        "indice": "ima-geral",
        "consulta": "1",
        "Dt_Ref": f"{dt.day:02d}/{dt.month:02d}/{dt.year}",
        "Dt_Ref_Ver": f"{dt.year}{dt.month:02d}{dt.day:02d}",
        "saida": "csv",
    }
    try:
        resp = _http_post(IMA_DOWN_URL, body)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        return {
            "status": "unavailable",
            "reason": f"http_error_{status}" if status else "http_error",
            "source": "ima_geral_down",
            "columns": IMA_COLUMNS,
            "rows": [],
        }
    except (requests.ConnectionError, requests.Timeout):
        return {
            "status": "unavailable",
            "reason": "http_error",
            "source": "ima_geral_down",
            "columns": IMA_COLUMNS,
            "rows": [],
        }
    text = resp.content.decode("iso-8859-1", errors="replace")
    # Se o servidor devolveu HTML (form completo) em vez de CSV, e sinal de
    # data invalida / nao disponivel. Heuristica: HTML comeca com '<' ou
    # contem '<html'.
    stripped = text.lstrip()
    if stripped.startswith("<") or "<html" in stripped[:200].lower():
        return {
            "status": "unavailable",
            "reason": "non_business_day_or_unavailable",
            "source": "ima_geral_down",
            "columns": IMA_COLUMNS,
            "rows": [],
        }
    return parse_ima_csv(text)


# ----------------------------------------------------------------------------
# Orquestracao: ultimos 5 dias uteis B3
# ----------------------------------------------------------------------------

def last_n_business_days(today: date, n: int) -> list[date]:
    """N ultimos dias uteis B3 estritamente <= today, em ordem decrescente."""
    out: list[date] = []
    d = today
    while len(out) < n:
        if is_b3_business_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return out


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def build_day_payload(date_iso: str, *, use_ima_latest: bool) -> dict:
    """Monta payload completo (IDA + IMA) para um dia."""
    ida = fetch_ida_qr(date_iso)
    if use_ima_latest:
        ima = fetch_ima_qr_latest()
        ref = ima.pop("_ref_date_iso", None)
        # Sanity check: se ima_completo.txt aponta pra outra data, usa fallback.
        if ima.get("status") == "ok" and ref and ref != date_iso:
            print(
                f"[ima] ima_completo.txt aponta {ref}, mas alvo e {date_iso} -- "
                f"caindo no fallback historico",
                file=sys.stderr,
            )
            ima = fetch_ima_qr_historical(date_iso)
    else:
        ima = fetch_ima_qr_historical(date_iso)
    return {
        "date": date_iso,
        "fetched_at": _now_iso_utc(),
        "ida": ida,
        "ima": ima,
    }


def fetch_window(*, force: bool = False, today: date | None = None) -> dict:
    """Pega os 5 du B3 mais recentes; salva 1 JSON por dia em OUT_DIR.

    Retorna dict com contagens: {written, skipped, ok_ida, ok_ima, failed}.
    """
    today = today or today_brt()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dates = last_n_business_days(today, 5)  # ordem decrescente: D0, D-1, ...
    latest = dates[0]
    counts = {"written": 0, "skipped": 0, "ok_ida": 0, "ok_ima": 0, "failed": 0}
    for d in dates:
        date_iso = d.isoformat()
        out_path = OUT_DIR / f"{date_iso}.json"
        if out_path.exists() and not force:
            print(f"[skip] {date_iso}.json ja existe", file=sys.stderr)
            counts["skipped"] += 1
            continue
        use_latest = (d == latest)
        print(f"[fetch] {date_iso} (ima_latest={use_latest})", file=sys.stderr)
        try:
            payload = build_day_payload(date_iso, use_ima_latest=use_latest)
        except Exception as exc:
            print(f"[err] {date_iso}: {type(exc).__name__}: {exc}", file=sys.stderr)
            counts["failed"] += 1
            continue
        if payload["ida"].get("status") == "ok":
            counts["ok_ida"] += 1
        if payload["ima"].get("status") == "ok":
            counts["ok_ima"] += 1
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        counts["written"] += 1
        print(
            f"[write] {out_path.name} (ida={payload['ida']['status']}, "
            f"ima={payload['ima']['status']})",
            file=sys.stderr,
        )
    return counts


# ----------------------------------------------------------------------------
# Self-test
# ----------------------------------------------------------------------------

_IDA_FIXTURE = """<html><body>
<center><table border=1>
  <tr><td colspan=11><b>IDA - Indice de Debentures ANBIMA - Quadro Resumo</b></td></tr>
  <tr>
    <td><b>Indices</b></td><td><b>Data</b></td><td><b>Numero Indice</b></td>
    <td><b>Var Diaria</b></td><td><b>Var Mes</b></td><td><b>Var Ano</b></td>
    <td><b>Var 12m</b></td><td><b>Var 24m</b></td><td><b>Duration</b></td>
    <td><b>Peso</b></td><td><b>Valor</b></td>
  </tr>
  <tr><td>IDA - DI</td><td>14/05/2026</td><td>4.682,223102</td><td>0,1144</td>
      <td>0,9656</td><td>4,8971</td><td>14,8293</td><td>30,2732</td>
      <td>701</td><td>51,79</td><td>395.499,59</td></tr>
  <tr><td>IDA - IPCA Infraestrutura</td><td>14/05/2026</td><td>1.000,000000</td>
      <td>0,1000</td><td>0,2000</td><td>0,3000</td><td>0,4000</td><td>0,5000</td>
      <td>500</td><td>10,00</td><td>50.000,00</td></tr>
  <tr><td>IDA - IPCA ex-Infraestrutura</td><td>14/05/2026</td><td>2.000,000000</td>
      <td>0,2000</td><td>0,3000</td><td>0,4000</td><td>0,5000</td><td>0,6000</td>
      <td>600</td><td>20,00</td><td>100.000,00</td></tr>
  <tr><td>IDA - IPCA</td><td>14/05/2026</td><td>4.629,411772</td><td>0,2940</td>
      <td>0,5000</td><td>5,0000</td><td>15,0000</td><td>30,0000</td>
      <td>800</td><td>46,87</td><td>357.918,48</td></tr>
  <tr><td>IDA - GERAL</td><td>14/05/2026</td><td>4.533,259289</td><td>0,1968</td>
      <td>0,7000</td><td>4,9000</td><td>14,9000</td><td>30,1000</td>
      <td>750</td><td>100,00</td><td>763.627,88</td></tr>
</table></center>
</body></html>"""

_IMA_FIXTURE = "\n".join([
    "0@ANBIMA",
    "1@TOTAIS",
    "1@Data de Referencia@INDICE@Numero Indice@Variacao Diaria(%)@Variacao Mensal(%)@Variacao Anual(%)@Variacao 12m(%)@Variacao 24m(%)@Duration(d.u.)@Peso(Geral)(%)@Carteira a Mercado(R$ mil)@Numero de Operacoes *@Quant. Negociada(1.000 titulos) *@Valor Negociado(R$ mil) *@PMR@Convexidade@Yield@Redemption Yield",
    "1@15/05/2026@IRF-M 1@20024,11548000@0,0363@0,5009@4,8340@14,4689@27,3112@106@6,19@498144508@--@--@--@155,745251562956@0,528510777990758@14,2010314677408@14,0827758360893",
    "1@15/05/2026@IRF-M@22275,06752000@-0,3051@-0,5184@3,0971@12,8415@21,9755@601@21,97@1769557324@--@--@--@933,403636388319@9,36504920857652@14,2495083447029@14,311967699464",
    "1@15/05/2026@IMA-GERAL@10028,64567200@-0,1424@0,0779@4,5259@13,2929@23,6089@519@100,00@8053994604@--@--@--@928,80258749941@21,0916331799229@--@--",
    "2@CARTEIRA@stuff_ignored",
])

_IDA_NON_BUSINESS_FIXTURE = (
    "<html><body>Ocorreu um erro na leitura do arquivo XML</body></html>"
)


def run_self_test() -> int:
    failures = 0

    # Caso 1: parse IDA HTML
    print("[test] IDA HTML parse...", file=sys.stderr)
    out = parse_ida_html(_IDA_FIXTURE)
    if out["status"] != "ok":
        print(f"  FAIL: status={out['status']} reason={out.get('reason')}",
              file=sys.stderr)
        failures += 1
    elif len(out["rows"]) != 5:
        print(f"  FAIL: expected 5 rows, got {len(out['rows'])}", file=sys.stderr)
        failures += 1
    else:
        # Verifica IDA-DI: numero_indice ~ 4682.22
        di = out["rows"][0]
        if di[0] != "IDA - DI" or abs(di[2] - 4682.223102) > 0.001:
            print(f"  FAIL: IDA-DI inesperado: {di}", file=sys.stderr)
            failures += 1
        else:
            print("  OK", file=sys.stderr)

    # Caso 2: parse IMA TXT
    print("[test] IMA TXT parse...", file=sys.stderr)
    out = parse_ima_completo_txt(_IMA_FIXTURE)
    if out["status"] != "ok":
        print(f"  FAIL: status={out['status']}", file=sys.stderr)
        failures += 1
    elif len(out["rows"]) != 3:
        print(f"  FAIL: expected 3 rows, got {len(out['rows'])}", file=sys.stderr)
        failures += 1
    else:
        # Verifica IMA-GERAL
        ima_geral = out["rows"][-1]
        if ima_geral[1] != "IMA-GERAL" or abs(ima_geral[2] - 10028.645672) > 0.01:
            print(f"  FAIL: IMA-GERAL inesperado: {ima_geral}", file=sys.stderr)
            failures += 1
        else:
            print("  OK", file=sys.stderr)

    # Caso 3: IDA dia nao util
    print("[test] IDA non-business day...", file=sys.stderr)
    out = parse_ida_html(_IDA_NON_BUSINESS_FIXTURE)
    if out["status"] != "unavailable" or out["reason"] != "non_business_day":
        print(f"  FAIL: {out}", file=sys.stderr)
        failures += 1
    else:
        print("  OK", file=sys.stderr)

    print(f"\n[self-test] {failures} falha(s)", file=sys.stderr)
    return 0 if failures == 0 else 1


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--force", action="store_true",
                   help="sobrescreve arquivos ja existentes em data/anbima_indices/")
    p.add_argument("--date", default=None,
                   help="data alvo YYYY-MM-DD (default: hoje BRT). Janela de 5 du "
                        "B3 termina nessa data.")
    p.add_argument("--self-test", action="store_true",
                   help="roda parsers contra fixtures e sai")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        return run_self_test()
    today = (datetime.strptime(args.date, "%Y-%m-%d").date()
             if args.date else today_brt())
    counts = fetch_window(force=args.force, today=today)
    print(
        f"[done] escritos={counts['written']} pulados={counts['skipped']} "
        f"ok_ida={counts['ok_ida']} ok_ima={counts['ok_ima']} "
        f"falhas={counts['failed']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
