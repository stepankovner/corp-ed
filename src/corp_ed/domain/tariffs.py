"""Тарифы компании (решение Артёма 30.09).

Отменяет решение команды 25.09 «число подключений не тарифицируется»
(DECISIONS.md, 2026-09-30). Цена — за место в месяц, лимит обращений
(420 кредитов на место, BillingSettings) у всех тарифов один:

- Базовый — до BASE_MAX_CONNECTORS подключений, только базовые системы;
- Расширенный — те же базовые системы, число подключений тарифом не
  ограничено;
- Корпоративный — по запросу: размещение в контуре клиента, системы вне
  базового списка, индивидуальные условия.

Здесь — только то, что проверяет код: лимит подключений и доступ к
небазовым коннекторам (KindSpec.base). Цены и тексты страницы тарифов —
в одном месте фронта, frontend/src/lib/tariffs.ts: сменить цену — одна
строка. Технический потолок подключений (CONNECTOR_MAX_PER_TENANT или
tenants.connector_limit) — защита от скрипта, он действует в любом
тарифе.
"""

from dataclasses import dataclass
from enum import StrEnum


class Tariff(StrEnum):
    BASE = "base"
    EXTENDED = "extended"
    ENTERPRISE = "enterprise"


DEFAULT_TARIFF = Tariff.BASE
"""Тариф новой компании, пока команда не выбрала другой."""

BASE_MAX_CONNECTORS = 5


@dataclass(frozen=True)
class TariffPlan:
    tariff: Tariff
    title: str
    max_connectors: int | None
    """Сколько подключений даёт тариф; None — тарифом не ограничено."""
    non_base_connectors: bool
    """Можно ли подключать системы вне базового списка."""


PLANS: dict[Tariff, TariffPlan] = {
    Tariff.BASE: TariffPlan(
        tariff=Tariff.BASE,
        title="Базовый",
        max_connectors=BASE_MAX_CONNECTORS,
        non_base_connectors=False,
    ),
    Tariff.EXTENDED: TariffPlan(
        tariff=Tariff.EXTENDED,
        title="Расширенный",
        max_connectors=None,
        non_base_connectors=False,
    ),
    Tariff.ENTERPRISE: TariffPlan(
        tariff=Tariff.ENTERPRISE,
        title="Корпоративный",
        max_connectors=None,
        non_base_connectors=True,
    ),
}


def plan_for(tariff: str | Tariff) -> TariffPlan:
    return PLANS[Tariff(tariff)]


def connector_limit(plan: TariffPlan, technical_limit: int) -> int:
    """Сколько подключений компании можно завести: тариф, но не больше
    технического потолка."""
    if plan.max_connectors is None:
        return technical_limit
    return min(plan.max_connectors, technical_limit)
