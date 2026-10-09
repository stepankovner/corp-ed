"""Тарифы компании (решение Артёма 30.09).

Отменяет решение команды 25.09 «число подключений не тарифицируется»
(DECISIONS.md, 2026-09-30). Цена — за место в месяц, кредитов на место
(420 в месяц, BillingSettings) у всех тарифов одинаково:

- Базовый — до BASE_MAX_SYSTEMS разных рабочих систем (вид коннектора,
  kind), только базовые; подключений к одной системе — сколько угодно
  (решение владельца 09.10: раньше считались подключения);
- Расширенный — те же базовые системы, число систем тарифом не
  ограничено;
- Корпоративный — по запросу: размещение в контуре клиента, системы вне
  базового списка, индивидуальные условия.

Здесь — только то, что проверяет код: лимит систем и доступ к
небазовым коннекторам (KindSpec.base). Цены и тексты страницы тарифов —
в одном месте фронта, frontend/src/lib/tariffs.ts: сменить цену — одна
строка. Технический потолок подключений (CONNECTOR_MAX_PER_TENANT или
tenants.connector_limit) — защита от скрипта, он действует в любом
тарифе и считает подключения, а не системы.
"""

from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum


class Tariff(StrEnum):
    BASE = "base"
    EXTENDED = "extended"
    ENTERPRISE = "enterprise"


DEFAULT_TARIFF = Tariff.BASE
"""Тариф новой компании, пока команда не выбрала другой."""

BASE_MAX_SYSTEMS = 5


@dataclass(frozen=True)
class TariffPlan:
    tariff: Tariff
    title: str
    max_systems: int | None
    """Сколько разных систем даёт тариф; None — тарифом не ограничено."""
    non_base_connectors: bool
    """Можно ли подключать системы вне базового списка."""


PLANS: dict[Tariff, TariffPlan] = {
    Tariff.BASE: TariffPlan(
        tariff=Tariff.BASE,
        title="Базовый",
        max_systems=BASE_MAX_SYSTEMS,
        non_base_connectors=False,
    ),
    Tariff.EXTENDED: TariffPlan(
        tariff=Tariff.EXTENDED,
        title="Расширенный",
        max_systems=None,
        non_base_connectors=False,
    ),
    Tariff.ENTERPRISE: TariffPlan(
        tariff=Tariff.ENTERPRISE,
        title="Корпоративный",
        max_systems=None,
        non_base_connectors=True,
    ),
}


def plan_for(tariff: str | Tariff) -> TariffPlan:
    return PLANS[Tariff(tariff)]


def system_fits(plan: TariffPlan, used: Collection[str], kind: str) -> bool:
    """Влезает ли подключение к системе kind в тариф, если уже подключены
    системы used. Ещё одно подключение к подключённой системе — не новая
    система."""
    if plan.max_systems is None or kind in used:
        return True
    return len(used) < plan.max_systems
