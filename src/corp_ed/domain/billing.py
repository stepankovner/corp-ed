"""Подписка компании и оплата (решения владельца 09.10).

Цена — за место в месяц: Базовый 990 ₽, Расширенный 1 490 ₽;
Корпоративный — по договорённости, в самообслуживание не входит.
Период оплаты — месяц, квартал или год; квартал и год — со скидкой
(PAYMENTS_DISCOUNT_QUARTER_PERCENT и PAYMENTS_DISCOUNT_YEAR_PERCENT,
по умолчанию 5 % и 10 %). Добавленные в середине периода места
оплачиваются сразу — пропорционально оставшимся дням периода, включая
сегодняшний; сокращение мест — со следующего периода.

Здесь только чистые функции: суммы, даты, проверка реквизитов, номер
счёта в назначении платежа. Все суммы — в копейках (int), дроби — через
Fraction, округление одно: до копейки, половина — вверх. Цены страницы
тарифов на сайте — во frontend/src/lib/tariffs.ts; блок оплаты на
странице тарифа берёт цены отсюда (GET /billing).
"""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from fractions import Fraction

from corp_ed.domain.credit_packs import add_months
from corp_ed.domain.tariffs import Tariff

SEAT_PRICES_KOPECKS: dict[Tariff, int] = {
    Tariff.BASE: 990_00,
    Tariff.EXTENDED: 1_490_00,
}
"""Цена места в месяц. Корпоративного тарифа здесь нет: цена — по
договорённости, счёт выставляет команда."""


class BillingPeriod(StrEnum):
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"

    @property
    def months(self) -> int:
        return {"month": 1, "quarter": 3, "year": 12}[self.value]


class PaymentMethod(StrEnum):
    INVOICE = "invoice"
    """Счёт юрлицу или ИП, оплата с расчётного счёта (без чека)."""
    CARD = "card"
    """Карта или СБП по ссылке, с чеком (54-ФЗ)."""


@dataclass(frozen=True)
class Discounts:
    quarter_percent: float
    year_percent: float

    def percent(self, period: BillingPeriod) -> Fraction:
        value = {
            BillingPeriod.MONTH: 0.0,
            BillingPeriod.QUARTER: self.quarter_percent,
            BillingPeriod.YEAR: self.year_percent,
        }[period]
        # Через строку: 7.5 и 3.3 из конфигурации — ровно 7,5 % и 3,3 %, а
        # не их двоичные приближения.
        return Fraction(str(value))


def round_kopecks(value: Fraction) -> int:
    """Округление до копейки, половина — вверх (суммы неотрицательные)."""
    if value < 0:
        raise ValueError("amount must not be negative")
    return int((value * 2 + 1) // 2)


def seat_price(tariff: Tariff | str) -> int:
    price = SEAT_PRICES_KOPECKS.get(Tariff(tariff))
    if price is None:
        raise ValueError(f"no self-service price for tariff {tariff}")
    return price


def _seat_period_price(
    tariff: Tariff | str, period: BillingPeriod, discounts: Discounts
) -> Fraction:
    """Место за весь период со скидкой — без округления."""
    discount = discounts.percent(period)
    return Fraction(seat_price(tariff) * period.months) * (100 - discount) / 100


def period_amount(
    tariff: Tariff | str, seats: int, period: BillingPeriod, discounts: Discounts
) -> int:
    """Сумма за период: места × цена × месяцы − скидка периода."""
    return round_kopecks(_seat_period_price(tariff, period, discounts) * seats)


def period_end(start: date, period: BillingPeriod) -> date:
    """Конец периода (не включительно): тот же день через 1, 3 или 12
    месяцев; нет такого дня — последний день месяца."""
    return add_months(start, period.months)


def topup_amount(
    tariff: Tariff | str,
    added_seats: int,
    period: BillingPeriod,
    discounts: Discounts,
    *,
    start: date,
    end: date,
    today: date,
) -> int:
    """Доплата за места, добавленные в середине оплаченного периода.

    Период [start, end): оплачиваются дни с сегодняшнего по последний
    включительно — добавили в последний день, платят за один день.
    Период уже кончился — доплаты нет (места войдут в следующий счёт);
    ещё не начался — доплата за весь период. Цена дня — от цены периода
    со скидкой: квартал и год дешевле и в доплате.
    """
    total = (end - start).days
    if added_seats <= 0 or total <= 0:
        return 0
    remaining = min(max((end - today).days, 0), total)
    per_seat = _seat_period_price(tariff, period, discounts)
    return round_kopecks(per_seat * added_seats * remaining / total)


def kopecks_from_rubles(value: object) -> int:
    """Сумма из банка («40.0», «1490.10», 0.33) — в копейки, без float.

    Больше двух знаков после точки, отрицательная сумма, мусор —
    ValueError: такую сумму лучше разобрать руками, чем округлить."""
    if value is None or isinstance(value, bool):
        raise ValueError("amount is missing")
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"bad amount: {value!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"bad amount: {value!r}")
    kopecks = amount * 100
    if kopecks != kopecks.to_integral_value():
        raise ValueError(f"amount has fractions of a kopeck: {value!r}")
    return int(kopecks)


def rubles(kopecks: int) -> float:
    """Копейки — в рубли для API банка (1234.56). Деление на 100 точно
    представимо с двумя знаками после округления."""
    return round(kopecks / 100, 2)


# --- номер счёта -----------------------------------------------------------------

INVOICE_PREFIX = "KR"

# Латиница или кириллица (КР), любой регистр, дефис, пробел или ничего.
_NUMBER_IN_PURPOSE = re.compile(r"(?<![\w])(?:KR|КР)[\s\-–—№]*0*(\d{1,9})(?!\d)", re.I)


def invoice_label(number: int) -> str:
    """Номер счёта для документа и назначения платежа: KR-00042.

    Сквозной по всем компаниям: банк и наш вебхук сопоставляют платёж со
    счётом по номеру в назначении, и два счёта № 3 разных компаний
    перепутались бы. Префикс отличает номер от даты и сумм в назначении."""
    return f"{INVOICE_PREFIX}-{number:05d}"


def find_invoice_number(purpose: str | None) -> int | None:
    """Номер счёта из назначения платежа или None."""
    if not purpose:
        return None
    match = _NUMBER_IN_PURPOSE.search(purpose)
    return int(match.group(1)) if match else None


def invoice_purpose(number: int, issued: date) -> str:
    return (
        f"Оплата по счёту № {invoice_label(number)} от {issued:%d.%m.%Y} "
        "за доступ к сервису kronto. Без НДС"
    )


# --- реквизиты покупателя ----------------------------------------------------------


class InvalidRequisitesError(ValueError):
    """Реквизиты не прошли проверку; field — какое поле подсветить."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


_INN10_WEIGHTS = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN11_WEIGHTS = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN12_WEIGHTS = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)


def _check_digit(digits: list[int], weights: tuple[int, ...]) -> int:
    return sum(d * w for d, w in zip(digits, weights, strict=False)) % 11 % 10


def validate_inn(value: str) -> str:
    """ИНН: 10 цифр у организации, 12 — у ИП; контрольные цифры — по
    алгоритму ФНС."""
    inn = (value or "").strip()
    if not inn.isascii() or not inn.isdigit() or len(inn) not in (10, 12):
        raise InvalidRequisitesError("inn", "ИНН — 10 цифр у организации или 12 у ИП")
    digits = [int(ch) for ch in inn]
    if len(inn) == 10:
        valid = _check_digit(digits, _INN10_WEIGHTS) == digits[9]
    else:
        valid = (
            _check_digit(digits, _INN11_WEIGHTS) == digits[10]
            and _check_digit(digits, _INN12_WEIGHTS) == digits[11]
        )
    if not valid:
        raise InvalidRequisitesError(
            "inn", "ИНН с ошибкой: не сходятся контрольные цифры"
        )
    return inn


_KPP = re.compile(r"\d{4}[\dA-Z]{2}\d{3}")


def validate_kpp(value: str) -> str:
    """КПП: 9 знаков — 4 цифры кода налоговой, 2 цифры или заглавные
    латинские буквы причины постановки, 3 цифры номера."""
    kpp = (value or "").strip()
    if not _KPP.fullmatch(kpp):
        raise InvalidRequisitesError("kpp", "КПП — 9 знаков, например 773601001")
    return kpp


def validate_requisites(*, inn: str, kpp: str | None) -> tuple[str, str | None]:
    """Тип покупателя по ИНН и проверенный КПП: у организации КПП
    обязателен, у ИП его нет."""
    inn = validate_inn(inn)
    kpp = (kpp or "").strip() or None
    if len(inn) == 10:
        if kpp is None:
            raise InvalidRequisitesError("kpp", "Укажите КПП организации")
        return "company", validate_kpp(kpp)
    if kpp is not None:
        raise InvalidRequisitesError("kpp", "У ИП нет КПП — оставьте поле пустым")
    return "ip", None
