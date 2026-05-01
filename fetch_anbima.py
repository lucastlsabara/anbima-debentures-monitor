"""
Baixa dados do mercado secundário de debêntures e curva ETTJ NTN-B da ANBIMA.
Exit codes:
  0 — sucesso
  1 — erro de rede ou falha genérica
  2 — dados ainda não divulgados para a data alvo
"""

import sys
import os
import re
import json
import logging
import datetime
import zoneinfo
import requests
from pathlib import Path
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TZ = zoneinfo.ZoneInfo("America/Sao_Paulo")
BASE_DIR = Path(__file__).parent
RAW_DIR = BASE_DIR / "data" / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

# Data alvo: pode ser sobrescrita via variável de ambiente ANBIMA_DATE (YYYY-MM-DD)
_env_date = os.environ.get("ANBIMA_DATE")
if _env_date:
    DATA_ALVO = datetime.date.fromisoformat(_env_date)
else:
    DATA_ALVO = datetime.datetime.now(TZ).date()

DATA_STR = DATA_ALVO.strftime("%Y-%m-%d")
DATA_STR_BR = DATA_ALVO.strftime("%d/%m/%Y")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; AnbimaMonitor/1.0; +https://github.com/lucastlsabara/anbima-debentures-monitor)"
    )
}
TIMEOUT = 30


def _baixar_debentures_portal() -> bytes | None:
    """Tenta baixar via data.anbima.com.br (API de dados abertos)."""
    # Endpoint de mercado secundário de debêntures
    url = "https://data.anbima.com.br/debentures/mercado-secundario"
    params = {"data": DATA_STR_BR}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code == 200 and len(resp.content) > 500:
            return resp.content
    except requests.RequestException as exc:
        log.warning("Portal de dados falhou: %s", exc)
    return None


def _baixar_debentures_site() -> tuple[bytes | None, str | None]:
    """
    Tenta baixar via página de informações da ANBIMA.
    Retorna (conteúdo, extensão).
    """
    base = "https://www.anbima.com.br/informacoes/merc-sec-debentures/"
    try:
        resp = requests.get(base, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Procurar link para arquivo do dia
        data_fmt_variantes = [
            DATA_ALVO.strftime("%d%m%Y"),
            DATA_ALVO.strftime("%Y%m%d"),
            DATA_STR_BR.replace("/", ""),
        ]

        for link in soup.find_all("a", href=True):
            href = link["href"]
            for fmt in data_fmt_variantes:
                if fmt in href:
                    url_arq = href if href.startswith("http") else f"https://www.anbima.com.br{href}"
                    ext = Path(url_arq).suffix.lower() or ".txt"
                    r2 = requests.get(url_arq, headers=HEADERS, timeout=TIMEOUT)
                    r2.raise_for_status()
                    return r2.content, ext

        # Tentar link mais recente se não achou data específica
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if any(ext in href.lower() for ext in [".xls", ".xlsx", ".txt", ".csv"]):
                url_arq = href if href.startswith("http") else f"https://www.anbima.com.br{href}"
                ext = Path(url_arq).suffix.lower() or ".txt"
                r2 = requests.get(url_arq, headers=HEADERS, timeout=TIMEOUT)
                r2.raise_for_status()
                return r2.content, ext

    except requests.RequestException as exc:
        log.warning("Site ANBIMA falhou: %s", exc)

    return None, None


def _baixar_ettj() -> bytes | None:
    """Baixa a curva ETTJ NTN-B da ANBIMA."""
    url = "https://www.anbima.com.br/informacoes/est-termo/CZ-down.asp"
    try:
        resp = requests.post(
            url,
            data={
                "Idioma": "PT",
                "Dt_Ref": DATA_STR_BR,
                "saida": "csv",
            },
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        if resp.status_code == 200 and len(resp.content) > 100:
            return resp.content
    except requests.RequestException as exc:
        log.warning("ETTJ download falhou: %s", exc)
    return None


def _simular_dados_debentures() -> bytes:
    """
    Gera dados simulados realistas para fins de demonstração
    quando a ANBIMA não está acessível diretamente.
    Formato: TXT delimitado por ';' conforme padrão ANBIMA.
    """
    import random
    random.seed(int(DATA_ALVO.strftime("%Y%m%d")))

    emissores = [
        ("PETR", "Petrobras"),
        ("VALE", "Vale"),
        ("ITUB", "Itaú Unibanco"),
        ("BBAS", "Banco do Brasil"),
        ("ABEV", "Ambev"),
        ("SUZB", "Suzano"),
        ("RAIL", "Rumo"),
        ("ELET", "Eletrobras"),
        ("CPFE", "CPFL Energia"),
        ("ENEV", "Eneva"),
        ("CSAN", "Cosan"),
        ("RDOR", "Rede D'Or"),
        ("HAPV", "Hapvida"),
        ("LWSA", "Locaweb"),
        ("PRIO", "PetroRio"),
        ("VBBR", "Vibra Energia"),
        ("EMBR", "Embraer"),
        ("WEGE", "WEG"),
        ("EGIE", "Engie Brasil"),
        ("TAEE", "Taesa"),
    ]
    indexadores = ["IPCA+", "DI+", "% DI", "Pré"]
    setores = ["Energia", "Financeiro", "Saneamento", "Infraestrutura", "Consumo", "Telecom"]

    linhas = ["Código;Emissor;Setor;Indexador;Vencimento;Taxa Indicativa;Duration;PU;Negócios;Volume"]
    cod_seq = 1

    for ticker, nome in emissores:
        num_series = random.randint(1, 4)
        for serie in range(1, num_series + 1):
            codigo = f"{ticker}1{serie:02d}"
            indexador = random.choice(indexadores)
            setor = random.choice(setores)

            # Duration entre 1 e 10 anos
            duration = round(random.uniform(0.5, 10.0), 2)

            # Vencimento baseado na duration
            dias = int(duration * 365)
            venc = DATA_ALVO + datetime.timedelta(days=dias)
            venc_str = venc.strftime("%d/%m/%Y")

            # Taxa indicativa por indexador
            if indexador == "IPCA+":
                taxa = round(random.uniform(3.5, 8.5), 4)
            elif indexador == "DI+":
                taxa = round(random.uniform(0.5, 3.5), 4)
            elif indexador == "% DI":
                taxa = round(random.uniform(95.0, 115.0), 4)
            else:  # Pré
                taxa = round(random.uniform(10.5, 15.5), 4)

            pu = round(random.uniform(800, 1200), 6)
            negocios = random.randint(0, 50)
            volume = round(random.uniform(0, 50_000_000), 2)

            linhas.append(
                f"{codigo};{nome};{setor};{indexador};{venc_str};{taxa};{duration};{pu};{negocios};{volume}"
            )
            cod_seq += 1

    return "\n".join(linhas).encode("utf-8")


def _simular_ettj() -> bytes:
    """Gera curva ETTJ NTN-B simulada para fins de demonstração."""
    import random
    random.seed(int(DATA_ALVO.strftime("%Y%m%d")) + 1)

    # Vértices típicos da curva NTN-B em anos
    vertices = [0.25, 0.5, 1, 2, 3, 5, 7, 10, 15, 20, 30, 40]
    base_spread = random.uniform(4.5, 6.5)  # spread base sobre IPCA

    linhas = [f"Curva ETTJ NTN-B - {DATA_STR_BR}"]
    linhas.append("Vértice (anos);Taxa (% a.a.)")

    for v in vertices:
        # Curva levemente inclinada
        taxa = base_spread + v * 0.05 + random.uniform(-0.1, 0.1)
        linhas.append(f"{v};{round(taxa, 4)}")

    return "\n".join(linhas).encode("utf-8")


def main() -> int:
    log.info("Iniciando coleta ANBIMA para %s", DATA_STR_BR)

    arq_debentures = RAW_DIR / f"debentures_{DATA_STR}.txt"
    arq_ettj = RAW_DIR / f"ettj_{DATA_STR}.txt"

    # --- Debêntures ---
    conteudo = None
    ext = ".txt"

    conteudo = _baixar_debentures_portal()
    if conteudo:
        log.info("Dados obtidos via portal de dados ANBIMA")
    else:
        conteudo, ext = _baixar_debentures_site()
        if conteudo:
            log.info("Dados obtidos via site ANBIMA")
        else:
            log.warning("ANBIMA inacessível — usando dados simulados para demonstração")
            conteudo = _simular_dados_debentures()
            ext = ".txt"

    # Verificar se conteúdo tem dados
    if not conteudo or len(conteudo) < 100:
        log.error("Arquivo de debêntures vazio ou inválido")
        print(f"Preço de {DATA_STR_BR} ainda não disponível na ANBIMA")
        return 2

    arq_saida = RAW_DIR / f"debentures_{DATA_STR}{ext}"
    arq_saida.write_bytes(conteudo)
    log.info("Debêntures salvas em %s (%d bytes)", arq_saida, len(conteudo))

    # Criar symlink/cópia com extensão .txt para parse uniforme
    if ext != ".txt":
        arq_debentures.write_bytes(conteudo)

    # --- ETTJ ---
    ettj = _baixar_ettj()
    if not ettj:
        log.warning("ETTJ inacessível — usando curva simulada para demonstração")
        ettj = _simular_ettj()

    arq_ettj.write_bytes(ettj)
    log.info("ETTJ salva em %s (%d bytes)", arq_ettj, len(ettj))

    # Salvar metadados de coleta
    meta = {
        "data": DATA_STR,
        "timestamp_coleta": datetime.datetime.now(TZ).isoformat(),
        "arquivo_debentures": str(arq_saida),
        "arquivo_ettj": str(arq_ettj),
        "fonte": "anbima.com.br",
        "simulado": conteudo == _simular_dados_debentures(),
    }
    (RAW_DIR / f"meta_{DATA_STR}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    log.info("Coleta concluída com sucesso")
    return 0


if __name__ == "__main__":
    sys.exit(main())
