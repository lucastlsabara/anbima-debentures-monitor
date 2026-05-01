"""Mapeamento heurístico de papel ANBIMA -> setor + flag de cobertura.

Classificação é feita por (1) prefixo do código da debênture (4 letras),
(2) palavras-chave no nome do emissor. Default: "Outros".

Lista de "minha cobertura" segue carteira sell-side: Aegea, BRK, Iguá,
Hapvida, Kora, DASA, Viveo, Oncoclínicas, CSN, Prio, Brava, Origem.
"""

from __future__ import annotations

import re

# ----------------------------------------------------------------------------
# Setores
# ----------------------------------------------------------------------------

SECTORS = [
    "Energia Elétrica",
    "Petróleo & Gás",
    "Saneamento",
    "Saúde",
    "Logística & Concessões",
    "Telecom",
    "Mineração & Siderurgia",
    "Bancos & Financeiro",
    "Varejo",
    "Imobiliário",
    "Agro & Alimentos",
    "Industrial",
    "Outros",
]

# Substring no nome do emissor (UPPER) -> setor.
# Ordem importa: a primeira correspondência ganha.
_EMISSOR_PATTERNS: list[tuple[str, str]] = [
    # Saneamento (verificar antes de Energia, há sobreposição em "ÁGUAS")
    ("BRK AMBIENTAL", "Saneamento"),
    ("AEGEA", "Saneamento"),
    ("IGUA SANEAMENTO", "Saneamento"),
    ("IGUA RIO", "Saneamento"),
    ("IGUÁ", "Saneamento"),
    ("SABESP", "Saneamento"),
    ("SANEAMENTO", "Saneamento"),
    ("CASAN", "Saneamento"),
    ("COPASA", "Saneamento"),
    ("CESAN", "Saneamento"),
    ("CAESB", "Saneamento"),
    ("CAGECE", "Saneamento"),
    ("AGUAS DE", "Saneamento"),
    ("ÁGUAS DE", "Saneamento"),
    ("AGUAS DA", "Saneamento"),
    ("ÁGUAS DA", "Saneamento"),
    ("AGUAS DO", "Saneamento"),
    ("ÁGUAS DO", "Saneamento"),
    ("AGUA E ESGOTO", "Saneamento"),
    ("AMBIENTAL", "Saneamento"),
    ("AMBIENT ", "Saneamento"),

    # Saúde
    ("HAPVIDA", "Saúde"),
    ("KORA SAUDE", "Saúde"),
    ("KORA SAÚDE", "Saúde"),
    ("DASA", "Saúde"),
    ("VIVEO", "Saúde"),
    ("ONCOCLINICA", "Saúde"),
    ("ONCOCLÍNICA", "Saúde"),
    ("REDE D'OR", "Saúde"),
    ("NOTRE DAME", "Saúde"),
    ("HOSPITAL", "Saúde"),
    ("FARMACIA", "Saúde"),
    ("FARMÁCIA", "Saúde"),

    # Petróleo & Gás
    ("PETROBRAS", "Petróleo & Gás"),
    ("PETRORECONCAVO", "Petróleo & Gás"),
    ("PETRORECÔNCAVO", "Petróleo & Gás"),
    ("PRIO ", "Petróleo & Gás"),
    ("PRIO,", "Petróleo & Gás"),
    ("PRIO.", "Petróleo & Gás"),
    ("BRAVA ENERGIA", "Petróleo & Gás"),
    ("3R PETROLEUM", "Petróleo & Gás"),
    ("3R PETRÓLEO", "Petróleo & Gás"),
    ("ENAUTA", "Petróleo & Gás"),
    ("ORIGEM ENERGIA", "Petróleo & Gás"),
    ("ULTRAPAR", "Petróleo & Gás"),
    ("RAIZEN", "Petróleo & Gás"),
    ("RAÍZEN", "Petróleo & Gás"),
    ("COSAN", "Petróleo & Gás"),
    ("COMGAS", "Petróleo & Gás"),
    ("COMGÁS", "Petróleo & Gás"),
    ("GASMIG", "Petróleo & Gás"),
    ("CIA. DE GAS", "Petróleo & Gás"),
    ("COMPANHIA DE GAS", "Petróleo & Gás"),
    ("COMPANHIA DE GÁS", "Petróleo & Gás"),

    # Energia Elétrica
    ("ELETROBRAS", "Energia Elétrica"),
    ("ELETROPAULO", "Energia Elétrica"),
    ("ELETROBR", "Energia Elétrica"),
    ("ENERGISA", "Energia Elétrica"),
    ("ENGIE", "Energia Elétrica"),
    ("LIGHT", "Energia Elétrica"),
    ("ISA ENERGIA", "Energia Elétrica"),
    ("ISA CTEEP", "Energia Elétrica"),
    ("CTEEP", "Energia Elétrica"),
    ("ENEVA", "Energia Elétrica"),
    ("EDP ", "Energia Elétrica"),
    ("EDP-", "Energia Elétrica"),
    ("NEOENERGIA", "Energia Elétrica"),
    ("EQUATORIAL", "Energia Elétrica"),
    ("COPEL", "Energia Elétrica"),
    ("CESP", "Energia Elétrica"),
    ("CEMIG", "Energia Elétrica"),
    ("AES ", "Energia Elétrica"),
    ("AUREN", "Energia Elétrica"),
    ("ALUPAR", "Energia Elétrica"),
    ("CELPE", "Energia Elétrica"),
    ("COELBA", "Energia Elétrica"),
    ("COELCE", "Energia Elétrica"),
    ("CELESC", "Energia Elétrica"),
    ("CELG", "Energia Elétrica"),
    ("CHESF", "Energia Elétrica"),
    ("RGE SUL", "Energia Elétrica"),
    ("ALGAR ENERGIA", "Energia Elétrica"),
    ("ATLAS LUIZ", "Energia Elétrica"),
    ("ATHON GERA", "Energia Elétrica"),
    ("ALIANCA GERA", "Energia Elétrica"),
    ("ALIANÇA GERA", "Energia Elétrica"),
    ("BIOENERGETICA", "Energia Elétrica"),
    ("BIOENERGÉTICA", "Energia Elétrica"),
    ("EOLICA", "Energia Elétrica"),
    ("EÓLICA", "Energia Elétrica"),
    ("RENOVAVEL", "Energia Elétrica"),
    ("RENOVÁVEL", "Energia Elétrica"),
    ("HIDRO", "Energia Elétrica"),
    ("ENERGETIC", "Energia Elétrica"),
    ("ENERGÉTIC", "Energia Elétrica"),
    ("CENTRAIS ELETRICAS", "Energia Elétrica"),
    ("CENTRAIS ELÉTRICAS", "Energia Elétrica"),
    ("ELETRICIDADE", "Energia Elétrica"),
    ("DISTRIBUIDORA DE ENERGIA", "Energia Elétrica"),
    ("GERACAO E TRANSMISSAO", "Energia Elétrica"),
    ("GERAÇÃO E TRANSMISSÃO", "Energia Elétrica"),
    ("GERAÇÃO DISTRIBUIDA", "Energia Elétrica"),
    ("GERACAO DISTRIBUIDA", "Energia Elétrica"),
    ("TRANSMISSORA", "Energia Elétrica"),
    ("TRANSMISSAO", "Energia Elétrica"),
    ("TRANSMISSÃO", "Energia Elétrica"),
    ("GERAÇÃO", "Energia Elétrica"),
    ("GERACAO", "Energia Elétrica"),
    ("ENERGIA", "Energia Elétrica"),
    ("FORÇA E LUZ", "Energia Elétrica"),
    ("FORCA E LUZ", "Energia Elétrica"),
    ("ELEKTRO", "Energia Elétrica"),
    ("ELDORADO BRASIL", "Industrial"),  # celulose
    ("CELULOSE", "Industrial"),
    ("KLABIN", "Industrial"),
    ("SUZANO", "Industrial"),
    ("ULTRAFERTIL", "Industrial"),
    ("FERTIL", "Industrial"),
    ("FIBRASIL", "Telecom"),
    ("FIBRA OTICA", "Telecom"),
    ("FIBRA ÓTICA", "Telecom"),
    ("DESKTOP", "Telecom"),
    ("WINITY", "Telecom"),
    ("VERO ", "Telecom"),
    ("MEIO AMBIENTE", "Saneamento"),
    ("ORIZON", "Saneamento"),
    ("ENEL", "Energia Elétrica"),
    ("ACUCAREIRA", "Agro & Alimentos"),
    ("AÇUCAREIRA", "Agro & Alimentos"),
    ("JALLES", "Agro & Alimentos"),
    ("SAO MARTINHO", "Agro & Alimentos"),
    ("SÃO MARTINHO", "Agro & Alimentos"),
    ("CLI SUL", "Logística & Concessões"),
    ("CLI ", "Logística & Concessões"),
    ("EPR ", "Logística & Concessões"),
    ("FARO ENERGY", "Energia Elétrica"),
    ("EÓLICOS", "Energia Elétrica"),
    ("EOLICOS", "Energia Elétrica"),
    ("SOLAR ", "Energia Elétrica"),
    ("SOL.", "Energia Elétrica"),

    # Telecom
    ("ALGAR TELECOM", "Telecom"),
    ("AMERICA NET", "Telecom"),
    ("BRISANET", "Telecom"),
    ("CLARO ", "Telecom"),
    ("V.TAL", "Telecom"),
    ("VIVO ", "Telecom"),
    ("TIM ", "Telecom"),
    ("TIM,", "Telecom"),
    ("TELEFONIC", "Telecom"),
    ("TELECOM", "Telecom"),
    ("TOWER", "Telecom"),
    ("INFRA-ESTRUTURAS", "Telecom"),
    ("TRIPLE PLAY", "Telecom"),

    # Logística & Concessões
    ("RUMO", "Logística & Concessões"),
    ("VLI ", "Logística & Concessões"),
    ("ECORODOVIA", "Logística & Concessões"),
    ("ECOPISTAS", "Logística & Concessões"),
    ("ECOVIAS", "Logística & Concessões"),
    ("CCR ", "Logística & Concessões"),
    ("MOTIVA", "Logística & Concessões"),
    ("ARTERIS", "Logística & Concessões"),
    ("AUTOPISTA", "Logística & Concessões"),
    ("CONCESSIONARIA", "Logística & Concessões"),
    ("CONCESSIONÁRIA", "Logística & Concessões"),
    ("CONCESS.", "Logística & Concessões"),
    ("CONCESS ", "Logística & Concessões"),
    ("CONC. ROD", "Logística & Concessões"),
    ("CONC ROD", "Logística & Concessões"),
    ("CONCESSAO", "Logística & Concessões"),
    ("CONCESSÃO", "Logística & Concessões"),
    ("RODOVIA", "Logística & Concessões"),
    ("FERROVIA", "Logística & Concessões"),
    ("FERROVI", "Logística & Concessões"),
    ("AEROPORTO", "Logística & Concessões"),
    ("PORTUARI", "Logística & Concessões"),
    ("PORTOS", "Logística & Concessões"),
    ("VPORTS", "Logística & Concessões"),
    ("TERMINAL", "Logística & Concessões"),
    ("TERMINAIS", "Logística & Concessões"),
    ("MULTIMODAL", "Logística & Concessões"),
    ("LOGIST", "Logística & Concessões"),
    ("METROVIA", "Logística & Concessões"),
    ("METROVIÁRIA", "Logística & Concessões"),
    ("VIAPAULISTA", "Logística & Concessões"),
    ("LOCALIZA", "Logística & Concessões"),
    ("RENT A CAR", "Logística & Concessões"),
    ("MRS LOGI", "Logística & Concessões"),
    ("MRS LOGÍ", "Logística & Concessões"),
    ("TRANSPORTADORA", "Logística & Concessões"),
    ("TRANSPORTE DO", "Logística & Concessões"),
    ("SIMPAR", "Logística & Concessões"),

    # Mineração & Siderurgia
    ("VALE S/A", "Mineração & Siderurgia"),
    ("VALE S.A", "Mineração & Siderurgia"),
    ("CSN MINERA", "Mineração & Siderurgia"),
    ("CSN ", "Mineração & Siderurgia"),
    ("GERDAU", "Mineração & Siderurgia"),
    ("USIMINAS", "Mineração & Siderurgia"),
    ("MINERA", "Mineração & Siderurgia"),
    ("SIDERUR", "Mineração & Siderurgia"),

    # Bancos & Financeiro
    ("BRADESCO", "Bancos & Financeiro"),
    ("BANCO", "Bancos & Financeiro"),
    ("ITAU", "Bancos & Financeiro"),
    ("ITAÚ", "Bancos & Financeiro"),
    ("SANTANDER", "Bancos & Financeiro"),
    ("CAIXA ECONO", "Bancos & Financeiro"),
    ("BTG ", "Bancos & Financeiro"),
    ("PARTICIPAC", "Bancos & Financeiro"),
    ("PARTICIPAÇ", "Bancos & Financeiro"),
    ("HOLDING", "Bancos & Financeiro"),
    ("INVESTIMENTO", "Bancos & Financeiro"),

    # Agro & Alimentos
    ("JBS ", "Agro & Alimentos"),
    ("MARFRIG", "Agro & Alimentos"),
    ("MINERVA ", "Agro & Alimentos"),
    ("BRF ", "Agro & Alimentos"),
    ("AGROVALE", "Agro & Alimentos"),
    ("AGRO ", "Agro & Alimentos"),
    ("AGROIND", "Agro & Alimentos"),
    ("ALIMENTOS", "Agro & Alimentos"),
    ("CERRADINHO", "Agro & Alimentos"),
    ("COLOMBO", "Agro & Alimentos"),
    ("USINA", "Agro & Alimentos"),

    # Imobiliário
    ("MRV ", "Imobiliário"),
    ("CYRELA", "Imobiliário"),
    ("EZ TEC", "Imobiliário"),
    ("JHSF", "Imobiliário"),
    ("CONSTRUTORA", "Imobiliário"),
    ("INCORPOR", "Imobiliário"),

    # Varejo
    ("LOJAS RENNER", "Varejo"),
    ("AMERICANAS", "Varejo"),
    ("MAGAZINE", "Varejo"),
    ("ASSAI", "Varejo"),
    ("ASSAÍ", "Varejo"),
    ("NATURA", "Varejo"),
    ("VAREJ", "Varejo"),
    ("LOCACAO", "Varejo"),
    ("LOCAÇÃO", "Varejo"),
    ("VAMOS", "Varejo"),
]

# Prefixo do código (4 letras) -> setor.
# Aplicado depois das regras por emissor (fallback adicional).
_PREFIX_PATTERNS: dict[str, str] = {
    "PETR": "Petróleo & Gás",
    "PEJA": "Petróleo & Gás",
    "PRIO": "Petróleo & Gás",
    "BRAV": "Petróleo & Gás",
    "ORIG": "Petróleo & Gás",
    "ENAT": "Petróleo & Gás",
    "RECV": "Petróleo & Gás",
    "BBDC": "Bancos & Financeiro",
    "BRGE": "Bancos & Financeiro",
    "VALE": "Mineração & Siderurgia",
    "CMIN": "Mineração & Siderurgia",
    "CSNA": "Mineração & Siderurgia",
    "AEGP": "Saneamento",
    "BRKP": "Saneamento",
    "BRKM": "Industrial",  # Braskem
    "IGSN": "Saneamento",
    "IRJS": "Saneamento",
    "RMSA": "Saneamento",
    "HAPV": "Saúde",
    "KORA": "Saúde",
    "DASA": "Saúde",
    "ONCO": "Saúde",
    "VIVE": "Saúde",
    "RDOR": "Saúde",
    "TIMS": "Telecom",
    "VIVT": "Telecom",
}


# ----------------------------------------------------------------------------
# Cobertura
# ----------------------------------------------------------------------------

# Padrões usados para marcar papéis "minha cobertura". Cada item é uma
# substring (UPPER) testada contra o nome do emissor.
_COBERTURA_PATTERNS: list[tuple[str, str]] = [
    ("AEGEA", "Aegea"),
    ("BRK AMBIENTAL", "BRK"),
    ("IGUA SANEAMENTO", "Iguá"),
    ("IGUA RIO", "Iguá"),
    ("IGUÁ", "Iguá"),
    ("HAPVIDA", "Hapvida"),
    ("KORA SAUDE", "Kora"),
    ("KORA SAÚDE", "Kora"),
    ("DASA", "DASA"),
    ("VIVEO", "Viveo"),
    ("ONCOCLINICA", "Oncoclínicas"),
    ("ONCOCLÍNICA", "Oncoclínicas"),
    ("CSN ", "CSN"),
    ("CSN MINERA", "CSN"),
    ("PRIO ", "Prio"),
    ("PRIO,", "Prio"),
    ("PRIO.", "Prio"),
    ("BRAVA ENERGIA", "Brava"),
    ("ENAUTA", "Brava"),
    ("3R PETROLEUM", "Brava"),
    ("3R PETRÓLEO", "Brava"),
    ("ORIGEM ENERGIA", "Origem"),
]


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------

_EMISSOR_CLEAN_RE = re.compile(r"\s*\(\*+\)\s*")


def clean_emissor(name: str | None) -> str:
    if not name:
        return ""
    return _EMISSOR_CLEAN_RE.sub(" ", name).strip()


def classify(codigo: str | None, emissor: str | None) -> str:
    em_up = (emissor or "").upper()
    for pat, sector in _EMISSOR_PATTERNS:
        if pat in em_up:
            return sector
    if codigo:
        prefix = codigo[:4].upper()
        if prefix in _PREFIX_PATTERNS:
            return _PREFIX_PATTERNS[prefix]
    return "Outros"


def cobertura_label(codigo: str | None, emissor: str | None) -> str | None:
    em_up = (emissor or "").upper()
    for pat, label in _COBERTURA_PATTERNS:
        if pat in em_up:
            return label
    return None
