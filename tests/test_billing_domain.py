"""Подписка компании: суммы периода со скидкой, доплата за места
пропорционально оставшимся дням, реквизиты покупателя (ИНН и КПП),
номер счёта в назначении платежа (решения владельца 09.10)."""

from datetime import date

import pytest

from corp_ed.domain.billing import (
    BillingPeriod,
    Discounts,
    InvalidRequisitesError,
    find_invoice_number,
    invoice_label,
    kopecks_from_rubles,
    period_amount,
    period_end,
    seat_price,
    topup_amount,
    validate_inn,
    validate_kpp,
    validate_requisites,
)
from corp_ed.domain.tariffs import Tariff

DISCOUNTS = Discounts(quarter_percent=5, year_percent=10)


# --- суммы периода ------------------------------------------------------------------


def test_month_is_seats_times_price() -> None:
    assert seat_price(Tariff.BASE) == 990_00
    assert seat_price(Tariff.EXTENDED) == 1_490_00
    assert period_amount(Tariff.BASE, 30, BillingPeriod.MONTH, DISCOUNTS) == 29_700_00


def test_quarter_and_year_get_the_configured_discount() -> None:
    # 3 × 1 490 × 10 мест − 5 % = 42 465 ₽
    assert period_amount(Tariff.EXTENDED, 10, BillingPeriod.QUARTER, DISCOUNTS) == (
        42_465_00
    )
    # 12 × 990 × 7 мест − 10 % = 74 844 ₽
    assert period_amount(Tariff.BASE, 7, BillingPeriod.YEAR, DISCOUNTS) == 74_844_00
    no_discount = Discounts(quarter_percent=0, year_percent=0)
    assert period_amount(Tariff.BASE, 1, BillingPeriod.YEAR, no_discount) == 11_880_00


def test_discount_rounds_half_up_to_a_kopeck() -> None:
    # 3 × 990 × 1 − 7,5 % = 2 747,25 ₽; 12 × 990 − 3,3 % = 11 487,96 ₽
    odd = Discounts(quarter_percent=7.5, year_percent=3.3)
    assert period_amount(Tariff.BASE, 1, BillingPeriod.QUARTER, odd) == 2_747_25
    assert period_amount(Tariff.BASE, 1, BillingPeriod.YEAR, odd) == 11_487_96


def test_enterprise_has_no_self_service_price() -> None:
    with pytest.raises(ValueError):
        seat_price(Tariff.ENTERPRISE)


def test_period_end_is_the_same_day_months_later() -> None:
    assert period_end(date(2026, 10, 9), BillingPeriod.MONTH) == date(2026, 11, 9)
    assert period_end(date(2026, 11, 30), BillingPeriod.QUARTER) == date(2027, 2, 28)
    assert period_end(date(2028, 2, 29), BillingPeriod.YEAR) == date(2029, 2, 28)
    assert period_end(date(2026, 1, 31), BillingPeriod.MONTH) == date(2026, 2, 28)


# --- доплата за места ---------------------------------------------------------------


def test_topup_is_proportional_to_remaining_days() -> None:
    # Месяц 01.10–01.11 (31 день), добавили 2 места 17.10: осталось 15 дней
    # вместе с сегодняшним. 2 × 990 × 15 / 31 = 958,0645… → 958,06 ₽.
    amount = topup_amount(
        Tariff.BASE,
        2,
        BillingPeriod.MONTH,
        DISCOUNTS,
        start=date(2026, 10, 1),
        end=date(2026, 11, 1),
        today=date(2026, 10, 17),
    )
    assert amount == 958_06


def test_topup_on_the_first_day_is_the_full_period() -> None:
    amount = topup_amount(
        Tariff.EXTENDED,
        3,
        BillingPeriod.MONTH,
        DISCOUNTS,
        start=date(2026, 10, 1),
        end=date(2026, 11, 1),
        today=date(2026, 10, 1),
    )
    assert amount == 3 * 1_490_00


def test_topup_on_the_last_day_is_one_day() -> None:
    # Последний день периода (31.10) оплачивается: 990 / 31 = 31,935… → 31,94.
    amount = topup_amount(
        Tariff.BASE,
        1,
        BillingPeriod.MONTH,
        DISCOUNTS,
        start=date(2026, 10, 1),
        end=date(2026, 11, 1),
        today=date(2026, 10, 31),
    )
    assert amount == 31_94


def test_topup_after_the_period_is_zero() -> None:
    kwargs = {"start": date(2026, 10, 1), "end": date(2026, 11, 1)}
    for today in (date(2026, 11, 1), date(2026, 12, 5)):
        assert (
            topup_amount(
                Tariff.BASE, 4, BillingPeriod.MONTH, DISCOUNTS, today=today, **kwargs
            )
            == 0
        )


def test_topup_before_the_period_is_the_full_period() -> None:
    amount = topup_amount(
        Tariff.BASE,
        1,
        BillingPeriod.MONTH,
        DISCOUNTS,
        start=date(2026, 10, 1),
        end=date(2026, 11, 1),
        today=date(2026, 9, 20),
    )
    assert amount == 990_00


def test_topup_in_a_leap_year_counts_february_29() -> None:
    # Год 01.03.2027–01.03.2028 — 366 дней (29.02.2028). Добавили место
    # 29.02.2028: остался один день. 12 × 990 × 0,9 / 366 = 29,213… → 29,21.
    amount = topup_amount(
        Tariff.BASE,
        1,
        BillingPeriod.YEAR,
        DISCOUNTS,
        start=date(2027, 3, 1),
        end=date(2028, 3, 1),
        today=date(2028, 2, 29),
    )
    assert amount == 29_21
    # Тот же год без високосного дня: 365 дней, полгода осталось.
    half = topup_amount(
        Tariff.BASE,
        1,
        BillingPeriod.YEAR,
        DISCOUNTS,
        start=date(2026, 3, 1),
        end=date(2027, 3, 1),
        today=date(2026, 8, 31),
    )
    # 182 дня из 365: 10 692 × 182 / 365 = 5 331,35…
    assert half == 5_331_35


def test_topup_for_a_quarter_uses_the_quarter_discount() -> None:
    # Квартал 15.11.2026–15.02.2027 (92 дня), с 15.01.2027 осталось 31.
    # 3 × 1 490 × 0,95 × 5 мест × 31 / 92 = 7 154,429…
    amount = topup_amount(
        Tariff.EXTENDED,
        5,
        BillingPeriod.QUARTER,
        DISCOUNTS,
        start=date(2026, 11, 15),
        end=date(2027, 2, 15),
        today=date(2027, 1, 15),
    )
    assert amount == 7_154_43


def test_topup_without_added_seats_is_zero() -> None:
    assert (
        topup_amount(
            Tariff.BASE,
            0,
            BillingPeriod.MONTH,
            DISCOUNTS,
            start=date(2026, 10, 1),
            end=date(2026, 11, 1),
            today=date(2026, 10, 2),
        )
        == 0
    )


# --- реквизиты ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "inn",
    [
        "7707083893",  # юрлицо, 10 цифр
        "500100732259",  # ИП, 12 цифр
        "7736207543",
    ],
)
def test_valid_inn(inn: str) -> None:
    assert validate_inn(inn) == inn


@pytest.mark.parametrize(
    "inn",
    [
        "7707083894",
        "500100732258",
        "123",
        "77070838930",
        "77070838a3",
        "",
        "0000000001",
    ],
)
def test_invalid_inn(inn: str) -> None:
    with pytest.raises(InvalidRequisitesError) as error:
        validate_inn(inn)
    assert error.value.field == "inn"


def test_kpp_format() -> None:
    assert validate_kpp("773601001") == "773601001"
    assert validate_kpp("7736AB001") == "7736AB001"
    for bad in ("77360100", "7736010011", "77360100X", "ab3601001"):
        with pytest.raises(InvalidRequisitesError):
            validate_kpp(bad)


def test_company_needs_kpp_and_sole_trader_has_none() -> None:
    company = validate_requisites(inn="7707083893", kpp="773601001")
    assert company == ("company", "773601001")
    with pytest.raises(InvalidRequisitesError) as error:
        validate_requisites(inn="7707083893", kpp=None)
    assert error.value.field == "kpp"
    assert validate_requisites(inn="500100732259", kpp=None) == ("ip", None)
    with pytest.raises(InvalidRequisitesError):
        validate_requisites(inn="500100732259", kpp="773601001")


# --- номер счёта ------------------------------------------------------------------


def test_invoice_number_is_found_in_the_purpose() -> None:
    assert invoice_label(42) == "KR-00042"
    assert find_invoice_number("Оплата по счёту № KR-00042 от 09.10.2026") == 42
    # Бухгалтер набрал кириллицей, строчными, через пробел или без дефиса.
    assert find_invoice_number("оплата сч. кр 00042, без НДС") == 42
    assert find_invoice_number("По счету КР-123456 за доступ") == 123456
    assert find_invoice_number("Оплата по счёту 42 от 09.10.2026") is None
    assert find_invoice_number("") is None


def test_rubles_from_the_bank_become_kopecks_exactly() -> None:
    assert kopecks_from_rubles("40.0") == 40_00
    assert kopecks_from_rubles("1490.10") == 1_490_10
    assert kopecks_from_rubles(0.33) == 33
    assert kopecks_from_rubles("29700") == 29_700_00
    for bad in ("", "abc", "1.005", "-5", None):
        with pytest.raises(ValueError):
            kopecks_from_rubles(bad)
