"""Calendario de dias uteis B3 (feriados nacionais + B3-especificos).

Feriados B3 considerados:
  - Todos os feriados nacionais brasileiros (via python-holidays)
  - Sexta-feira Santa (ja coberto pelo python-holidays Brazil)
  - Carnaval segunda + terca (movel; computado a partir da Pascoa)
  - Corpus Christi (Pascoa + 60 dias)
  - Aniversario de Sao Paulo (25/jan)
  - Consciencia Negra 20/nov (federal desde 2024; ja em python-holidays)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache

import holidays as _holidays_lib
import pytz

_BRT = pytz.timezone("America/Sao_Paulo")


def _easter_sunday(year: int) -> date:
    """Algoritmo gregoriano anonimo para Pascoa."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


@lru_cache(maxsize=64)
def _b3_holidays_for_year(year: int) -> frozenset[date]:
    nacional = _holidays_lib.country_holidays("BR", years=[year])
    feriados: set[date] = {d for d in nacional}
    e = _easter_sunday(year)
    feriados.update({
        date(year, 1, 25),               # Aniversario de Sao Paulo
        e - timedelta(days=48),          # Carnaval segunda
        e - timedelta(days=47),          # Carnaval terca
        e + timedelta(days=60),          # Corpus Christi
    })
    return frozenset(feriados)


def is_b3_business_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in _b3_holidays_for_year(d.year)


def last_b3_business_day(today: date) -> date:
    """Ultimo dia util B3 estritamente anterior a `today`."""
    d = today - timedelta(days=1)
    while not is_b3_business_day(d):
        d -= timedelta(days=1)
    return d


def today_brt() -> date:
    return datetime.now(_BRT).date()


def resolve_default_date() -> date:
    """Data de referencia padrao: ultimo dia util B3 anterior a hoje (BRT)."""
    return last_b3_business_day(today_brt())
