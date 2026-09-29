"""Заявка на созвон со страницы тарифов (досье 10.1).

Путь клиента по досье: тариф на сайте → дата и время созвона → данные
компании и телефон → команда перезванивает и подтверждает → созвон.
"""

from datetime import date, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo


class LeadStatus(StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    """Перезвонили, время подтверждено."""
    SCHEDULED = "scheduled"
    """Созвон назначен или прошёл."""
    REJECTED = "rejected"
    """Случайная заявка, спам или не наш клиент."""


class LeadTariff(StrEnum):
    BASE = "base"
    """Базовый — 1 490 ₽ за место в месяц (досье 10.4)."""
    CUSTOM = "custom"
    """Тарифы выше — «по запросу», их цены не определены (досье 15)."""


CALL_TIMEZONE = ZoneInfo("Europe/Moscow")
"""Даты и окна созвона — по Москве: команда и клиенты в России."""

CALL_SLOTS = ("10:00–12:00", "12:00–14:00", "14:00–16:00", "16:00–18:00")
"""Окна созвона по московскому времени. Команда перезванивает и
подтверждает точное время — это выбор удобного окна, а не бронь."""


def call_dates(today: date, days_ahead: int) -> tuple[date, date]:
    """Первая и последняя дата, на которые можно записаться."""
    return today + timedelta(days=1), today + timedelta(days=days_ahead)


def is_workday(day: date) -> bool:
    """Созвоны — по будним дням (праздники команда переносит сама)."""
    return day.weekday() < 5
