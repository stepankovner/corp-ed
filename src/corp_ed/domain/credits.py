"""Кредиты — единица расхода ассистента (досье 10.2).

Один тип кредита, без деления на входные и выходные токены. Предложение
досье (параметры не утверждены): 1 кредит — одно обычное обращение
около 2 000 токенов в сумме; длинное списывает больше пропорционально.
Пул компании — кредитов на место × число мест в месяц.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def credits_for(tokens: int, tokens_per_credit: int) -> int:
    """Сколько кредитов стоит обращение: не меньше одного."""
    if tokens_per_credit <= 0:
        raise ValueError("tokens_per_credit must be positive")
    return max(1, math.ceil(max(tokens, 0) / tokens_per_credit))


def billing_period(now: datetime, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """Календарный месяц, в который попадает now, в поясе биллинга.

    Границы — полночь первого числа по местному времени: вопрос,
    заданный 1 октября в 00:30 по Москве (30 сентября 21:30 UTC),
    списывается уже с октябрьского пула.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(zone)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # 1-е число + 32 дня всегда попадает в следующий месяц.
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


@dataclass(frozen=True)
class CreditUsage:
    """Расход пула компании за текущий месяц."""

    period_start: datetime
    period_end: datetime
    seats: int
    credits_per_seat: int
    used: int

    @property
    def pool(self) -> int:
        return self.seats * self.credits_per_seat

    @property
    def remaining(self) -> int:
        return max(0, self.pool - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.pool
