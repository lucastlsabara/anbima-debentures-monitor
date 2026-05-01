"""
Calcula spreads, variações e métricas de liquidez para debêntures.
Salva snapshot diário em /history/<data>.json e consolida em /data.json.
Exit codes:
  0 — sucesso
  1 — erro
"""

import sys
import os
import json
import logging
import datetime
import zoneinfo
import glob
import numpy as np
from pathlib import Path
from scipy.interpolate import CubicSpline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TZ = zoneinfo.ZoneInfo("America/Sao_Paulo")
BASE_DIR = Path(__file__).parent
RAW_DIR = BASE_DIR / "data" / "raw"
HISTORY_DIR = BASE_DIR / "history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

_env_date = os.environ.get("ANBIMA_DATE")
if _env_date:
    DATA_ALVO = datetime.date.fromisoformat(_env_date)
else:
    DATA_ALVO = datetime.datetime.now(TZ).date()

DATA_STR = DATA_ALVO.strftime("%Y-%m-%d")
DATA_STR_BR = DATA_ALVO.strftime("%d/%m/%Y")

DIAS_UTEIS_SEMANA = 5
DIAS_UTEIS_MES = 21
DIAS_UTEIS_TRIMESTRE = 63


def _ler_ettj() -> dict[float, float]:
    """Lê curva ETTJ NTN-B e retorna dict {duration_anos: taxa_%aa}."""
    arq = RAW_DIR / f"ettj_{DATA_STR}.txt"
    if not arq.exists():
        log.warning("ETTJ não encontrada para %s", DATA_STR)
        return {}

    curva = {}
    for linha in arq.read_text(encoding="utf-8").splitlines():
        partes = linha.replace(",", ".").split(";")
        if len(partes) < 2:
            continue
        try:
            vertice = float(partes[0])
            taxa = float(partes[1])
            curva[vertice] = taxa
        except ValueError:
            continue

    log.info("ETTJ carregada: %d vértices", len(curva))
    return curva


def _interpolar_ntnb(curva: dict[float, float], duration: float) -> float | None:
    """Interpola taxa NTN-B para uma duration específica via cubic spline."""
    if len(curva) < 3:
        return None

    xs = sorted(curva.keys())
    ys = [curva[x] for x in xs]

    # Extrapolação plana nas bordas
    if duration <= xs[0]:
        return ys[0]
    if duration >= xs[-1]:
        return ys[-1]

    cs = CubicSpline(xs, ys, bc_type="not-a-knot")
    return float(cs(duration))


def _ntnb_mais_proximo(curva: dict[float, float], duration: float) -> tuple[float, float] | None:
    """Retorna (vertice, taxa) do vértice NTN-B mais próximo da duration."""
    if not curva:
        return None
    vertice = min(curva.keys(), key=lambda v: abs(v - duration))
    return vertice, curva[vertice]


def _ler_debentures() -> list[dict]:
    """Lê arquivo de debêntures do dia e retorna lista de registros."""
    # Tentar extensões conhecidas
    for ext in [".txt", ".xls", ".xlsx", ".csv"]:
        arq = RAW_DIR / f"debentures_{DATA_STR}{ext}"
        if arq.exists():
            return _parsear_arquivo(arq, ext)

    log.error("Arquivo de debêntures não encontrado para %s", DATA_STR)
    return []


def _parsear_arquivo(arq: Path, ext: str) -> list[dict]:
    """Parseia arquivo de debêntures em formato TXT ou Excel."""
    registros = []

    if ext in (".xls", ".xlsx"):
        try:
            import pandas as pd
            df = pd.read_excel(arq, header=None)
            return _df_para_registros(df)
        except Exception as exc:
            log.error("Erro ao ler Excel: %s", exc)
            return []

    # TXT/CSV — separador pode ser ';', ',' ou TAB
    texto = arq.read_text(encoding="utf-8", errors="replace")
    linhas = [l for l in texto.splitlines() if l.strip()]

    if not linhas:
        return []

    # Detectar separador
    sep = ";"
    if linhas[0].count(";") < linhas[0].count(","):
        sep = ","
    elif "\t" in linhas[0]:
        sep = "\t"

    cabecalho = [c.strip().lower() for c in linhas[0].split(sep)]

    # Mapeamento de nomes de colunas ANBIMA → campos internos
    mapa = {
        "código": "codigo",
        "codigo": "codigo",
        "emissor": "emissor",
        "nome": "emissor",
        "setor": "setor",
        "indexador": "indexador",
        "vencimento": "vencimento",
        "data vencimento": "vencimento",
        "taxa indicativa": "taxa",
        "taxa": "taxa",
        "duration": "duration",
        "du": "duration",
        "pu": "pu",
        "negócios": "negocios",
        "negocios": "negocios",
        "volume": "volume",
    }

    idx_map = {}
    for i, col in enumerate(cabecalho):
        for chave, campo in mapa.items():
            if chave in col:
                idx_map[campo] = i
                break

    for linha in linhas[1:]:
        partes = [p.strip() for p in linha.split(sep)]
        if len(partes) < 3:
            continue

        def _get(campo, default=None):
            i = idx_map.get(campo)
            if i is None or i >= len(partes):
                return default
            return partes[i] or default

        def _float(campo, default=None):
            val = _get(campo)
            if val is None:
                return default
            try:
                return float(val.replace(",", "."))
            except (ValueError, AttributeError):
                return default

        codigo = _get("codigo", "")
        if not codigo or not codigo.strip():
            continue

        registros.append({
            "codigo": codigo,
            "emissor": _get("emissor", ""),
            "setor": _get("setor", ""),
            "indexador": _get("indexador", ""),
            "vencimento": _get("vencimento", ""),
            "taxa": _float("taxa"),
            "duration": _float("duration"),
            "pu": _float("pu"),
            "negocios": _float("negocios", 0),
            "volume": _float("volume", 0),
        })

    log.info("Arquivo parseado: %d debêntures", len(registros))
    return registros


def _df_para_registros(df) -> list[dict]:
    """Converte DataFrame Excel para lista de registros."""
    import pandas as pd

    # Encontrar linha de cabeçalho
    for i, row in df.iterrows():
        if any(str(v).lower() in ["código", "codigo", "emissor"] for v in row):
            df.columns = [str(v).strip().lower() for v in row]
            df = df.iloc[i + 1:].reset_index(drop=True)
            break

    registros = []
    for _, row in df.iterrows():
        try:
            registros.append({
                "codigo": str(row.get("código", row.get("codigo", ""))).strip(),
                "emissor": str(row.get("emissor", row.get("nome", ""))).strip(),
                "setor": str(row.get("setor", "")).strip(),
                "indexador": str(row.get("indexador", "")).strip(),
                "vencimento": str(row.get("vencimento", row.get("data vencimento", ""))).strip(),
                "taxa": pd.to_numeric(row.get("taxa indicativa", row.get("taxa")), errors="coerce"),
                "duration": pd.to_numeric(row.get("duration", row.get("du")), errors="coerce"),
                "pu": pd.to_numeric(row.get("pu"), errors="coerce"),
                "negocios": pd.to_numeric(row.get("negócios", row.get("negocios", 0)), errors="coerce"),
                "volume": pd.to_numeric(row.get("volume", 0), errors="coerce"),
            })
        except Exception:
            continue
    return registros


def _classificar_indexador(indexador: str) -> str:
    """Normaliza o indexador para categorias padrão."""
    idx = str(indexador).upper().strip()
    if "IPCA" in idx:
        return "IPCA+"
    elif "% DI" in idx or "%DI" in idx or "CDI" in idx and "+" not in idx:
        return "% DI"
    elif "DI+" in idx or "CDI+" in idx or "+ DI" in idx:
        return "DI+"
    elif "PRE" in idx or "PRÉ" in idx or "FIXED" in idx:
        return "Pré"
    return indexador


def _carregar_historico() -> list[dict]:
    """Carrega lista de snapshots históricos ordenados por data."""
    arquivos = sorted(HISTORY_DIR.glob("*.json"))
    snapshots = []
    for arq in arquivos:
        try:
            dados = json.loads(arq.read_text(encoding="utf-8"))
            if isinstance(dados, list):
                snapshots.append({"data": arq.stem, "papeis": {p["codigo"]: p for p in dados}})
        except Exception:
            continue
    return snapshots


def _dias_uteis_anteriores(snapshots: list[dict], n: int) -> dict | None:
    """Retorna snapshot de N dias úteis atrás (ou None)."""
    historico_recente = [s for s in snapshots if s["data"] < DATA_STR]
    if len(historico_recente) < n:
        return None
    return historico_recente[-n]


def _calcular_flag_liquidez(codigo: str, snapshots: list[dict]) -> bool:
    """Retorna True se o papel não teve variação de taxa nos últimos 5 dias úteis."""
    recentes = [s for s in snapshots if s["data"] < DATA_STR][-DIAS_UTEIS_SEMANA:]
    if len(recentes) < DIAS_UTEIS_SEMANA:
        return False

    taxas = set()
    for snap in recentes:
        papel = snap["papeis"].get(codigo)
        if papel and papel.get("taxa") is not None:
            taxas.add(papel["taxa"])

    return len(taxas) <= 1


def main() -> int:
    log.info("Calculando spreads para %s", DATA_STR_BR)

    curva_ettj = _ler_ettj()
    debentures = _ler_debentures()

    if not debentures:
        log.error("Nenhuma debênture encontrada para processar")
        return 1

    snapshots = _carregar_historico()
    snap_d1 = _dias_uteis_anteriores(snapshots, 1)
    snap_5d = _dias_uteis_anteriores(snapshots, DIAS_UTEIS_SEMANA)
    snap_21d = _dias_uteis_anteriores(snapshots, DIAS_UTEIS_MES)
    snap_63d = _dias_uteis_anteriores(snapshots, DIAS_UTEIS_TRIMESTRE)

    resultado = []

    for deb in debentures:
        codigo = deb.get("codigo", "").strip()
        if not codigo:
            continue

        taxa = deb.get("taxa")
        duration = deb.get("duration")
        indexador_raw = deb.get("indexador", "")
        indexador = _classificar_indexador(indexador_raw)

        # Calcular spread conforme indexador
        spread = None
        spread_ntnb_proximo = None
        vertice_ref = None

        if indexador == "IPCA+" and taxa is not None and duration is not None:
            taxa_ntnb = _interpolar_ntnb(curva_ettj, duration)
            if taxa_ntnb is not None:
                spread = round(taxa - taxa_ntnb, 4)

            ref = _ntnb_mais_proximo(curva_ettj, duration)
            if ref:
                vertice_ref, taxa_ref = ref
                spread_ntnb_proximo = round(taxa - taxa_ref, 4) if taxa is not None else None

        elif indexador in ("DI+", "% DI") and taxa is not None:
            spread = taxa  # mantém original

        elif indexador == "Pré" and taxa is not None:
            spread = taxa  # placeholder — LTN/NTN-F não disponível

        # Variações
        def _delta_taxa(snap):
            if snap is None or taxa is None:
                return None
            ant = snap["papeis"].get(codigo)
            if ant and ant.get("taxa") is not None:
                return round((taxa - ant["taxa"]) * 100, 2)  # bps
            return None

        def _delta_spread(snap):
            if snap is None or spread is None:
                return None
            ant = snap["papeis"].get(codigo)
            if ant and ant.get("spread") is not None:
                return round((spread - ant["spread"]) * 100, 2)  # bps
            return None

        flag_liquidez = _calcular_flag_liquidez(codigo, snapshots)

        registro = {
            "codigo": codigo,
            "emissor": deb.get("emissor", ""),
            "setor": deb.get("setor", ""),
            "indexador": indexador,
            "vencimento": deb.get("vencimento", ""),
            "taxa": taxa,
            "duration": duration,
            "pu": deb.get("pu"),
            "negocios": deb.get("negocios", 0),
            "volume": deb.get("volume", 0),
            "spread": spread,
            "spread_ntnb_proximo": spread_ntnb_proximo,
            "vertice_ref_anos": vertice_ref,
            "delta_taxa_d1_bps": _delta_taxa(snap_d1),
            "delta_spread_d1_bps": _delta_spread(snap_d1),
            "delta_spread_5d_bps": _delta_spread(snap_5d),
            "delta_spread_21d_bps": _delta_spread(snap_21d),
            "delta_spread_63d_bps": _delta_spread(snap_63d),
            "baixa_liquidez": flag_liquidez,
            "data": DATA_STR,
        }
        resultado.append(registro)

    # Snapshot do dia
    arq_snapshot = HISTORY_DIR / f"{DATA_STR}.json"
    arq_snapshot.write_text(
        json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("Snapshot salvo: %s (%d papéis)", arq_snapshot, len(resultado))

    # Curva ETTJ para o data.json
    curva_lista = [{"vertice_anos": k, "taxa": v} for k, v in sorted(curva_ettj.items())]

    # Consolidar data.json
    dados_consolidados = {
        "data": DATA_STR,
        "timestamp": datetime.datetime.now(TZ).isoformat(),
        "total_papeis": len(resultado),
        "papeis_baixa_liquidez": sum(1 for r in resultado if r["baixa_liquidez"]),
        "curva_ettj": curva_lista,
        "debentures": resultado,
    }

    (BASE_DIR / "data.json").write_text(
        json.dumps(dados_consolidados, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("data.json consolidado com %d papéis", len(resultado))

    # Resumo para o PR
    com_delta = [r for r in resultado if r["delta_spread_d1_bps"] is not None]
    top_abertura = sorted(com_delta, key=lambda r: r["delta_spread_d1_bps"] or 0, reverse=True)[:5]
    top_fechamento = sorted(com_delta, key=lambda r: r["delta_spread_d1_bps"] or 0)[:5]

    print("=== RESUMO DO DIA ===")
    print(f"Total de papéis: {len(resultado)}")
    print(f"Papéis com baixa liquidez: {dados_consolidados['papeis_baixa_liquidez']}")
    print("\nTop 5 Maiores Aberturas de Spread (bps):")
    for r in top_abertura:
        print(f"  {r['codigo']} ({r['emissor']}): +{r['delta_spread_d1_bps']:.1f} bps")
    print("\nTop 5 Maiores Fechamentos de Spread (bps):")
    for r in top_fechamento:
        print(f"  {r['codigo']} ({r['emissor']}): {r['delta_spread_d1_bps']:.1f} bps")

    return 0


if __name__ == "__main__":
    sys.exit(main())
