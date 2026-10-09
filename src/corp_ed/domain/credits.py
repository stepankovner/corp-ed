"""Кредиты — единица расхода ассистента (досье 10.2).

Один тип кредита, без деления на входные и выходные токены: 1 кредит —
до BILLING_TOKENS_PER_CREDIT токенов вопроса и ответа вместе; длинное
списывает больше пропорционально. Пул компании — кредитов на место ×
число мест в месяц; сверх пула — купленные пакеты (domain/credit_packs.py).
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


def billing_day(now: datetime, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """Сутки в поясе биллинга, в которые попадает now: для личного
    дневного лимита. Новый день — с полуночи по местному времени."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    start = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    # Через 26 часов — уже следующие сутки и при переходе на летнее время.
    end = (start + timedelta(hours=26)).replace(hour=0)
    return start, end


@dataclass(frozen=True)
class CreditUsage:
    """Расход пула компании за текущий месяц и купленные кредиты."""

    period_start: datetime
    period_end: datetime
    seats: int
    credits_per_seat: int
    used: int
    warn_at_percent: int = 80
    """Порог предупреждения: событие аудита и плашка администратору."""
    purchased: int = 0
    """Купленные кредиты, которые ещё не сгорели (PurchasedBalance)."""
    purchased_expires_at: datetime | None = None
    purchased_expiring: int = 0

    @property
    def pool(self) -> int:
        return self.seats * self.credits_per_seat

    @property
    def warn_at(self) -> int:
        """Сколько кредитов потрачено, когда пора предупредить."""
        return math.ceil(self.pool * self.warn_at_percent / 100)

    @property
    def warning(self) -> bool:
        """Порог предупреждения достигнут (и при исчерпанном пуле тоже)."""
        return self.used >= self.warn_at

    @property
    def remaining(self) -> int:
        return max(0, self.pool - self.used)

    @property
    def exhausted(self) -> bool:
        """Месячный пул израсходован."""
        return self.used >= self.pool

    @property
    def stopped(self) -> bool:
        """Вопросы остановлены: пул израсходован и купленных кредитов нет."""
        return self.exhausted and self.purchased <= 0
