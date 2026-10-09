"""Пакеты кредитов сверх месячного пула (решение владельца 09.10).

Пул исчерпан — администратор докупает пакет; оплата пока по счёту
(онлайн-оплата картой — следующий этап). Купленные кредиты живут
PACK_VALID_MONTHS месяцев с зачисления; списываются после месячного пула,
первыми — те, что раньше сгорают (CreditService.note_spend).

Цены и размеры — ЕДИНСТВЕННОЕ место: фронт берёт их из GET /credits/packs
и сам не хардкодит. Скидка за объём и наценка к цене кредита в тарифе:
место «Базового» — 990 ₽ за 420 кредитов (≈ 2,36 ₽), пакеты — 2,98 ₽,
2,75 ₽ и 2,60 ₽ за кредит. Сумма — в копейках, как в будущем счёте.
"""

import calendar
from dataclasses import dataclass
from datetime import datetime

PACK_VALID_MONTHS = 12


@dataclass(frozen=True)
class CreditPack:
    code: str
    credits: int
    price_kopecks: int


PACKS: tuple[CreditPack, ...] = (
    CreditPack(code="pack_500", credits=500, price_kopecks=1_490_00),
    CreditPack(code="pack_2000", credits=2_000, price_kopecks=5_490_00),
    CreditPack(code="pack_5000", credits=5_000, price_kopecks=12_990_00),
)


def pack_by_code(code: str) -> CreditPack | None:
    return next((pack for pack in PACKS if pack.code == code), None)


def add_months(moment: datetime, months: int) -> datetime:
    """Тот же день через months месяцев; нет такого дня — последний день
    месяца (29 февраля + 12 месяцев = 28 февраля)."""
    index = moment.month - 1 + months
    year, month = moment.year + index // 12, index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def pack_expiry(granted_at: datetime) -> datetime:
    """Когда сгорают кредиты, зачисленные в granted_at."""
    return add_months(granted_at, PACK_VALID_MONTHS)
