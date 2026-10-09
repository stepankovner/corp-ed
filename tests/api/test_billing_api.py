"""Оплата через банк (решения владельца 09.10): реквизиты, подписка счётом
и картой, вебхук Точки — подпись, сопоставление, повторная доставка,
расхождения на ручной разбор; пакеты кредитов; наша панель; места.

Банк — поддельная Точка (tests/payments/fake_tochka.py) за настоящим
клиентом TochkaProvider."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.api.v1.dependencies import (
    get_payment_provider,
    get_payments_settings,
    get_team_notifier,
)
from corp_ed.core.config import PaymentSettings, get_billing_settings
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.billing import BillingPeriod, Discounts, topup_amount
from corp_ed.domain.models import (
    CreditGrant,
    CreditOrder,
    Invoice,
    InvoiceRef,
    Notification,
    PaymentEvent,
    StaffMember,
    Subscription,
    Tenant,
    User,
    UserRole,
)
from corp_ed.main import app
from tests.api.conftest import bearer
from tests.factories import make_user
from tests.payments.fake_tochka import FakeTochka, sign
from tests.team_notify_helpers import RecordingNotifier

BILLING = "/api/v1/billing"
REQUISITES = "/api/v1/company/requisites"
WEBHOOK = "/api/v1/payments/tochka/webhook"
ORDERS = "/api/v1/credits/orders"
COMPANY = {
    "legal_name": "ООО «Ромашка»",
    "inn": "7707083893",
    "kpp": "773601001",
    "address": "Москва, ул. Тверская, д. 1",
    "documents_email": "buh@romashka.ru",
}
DISCOUNTS = Discounts(quarter_percent=5, year_percent=10)


@pytest.fixture
def team() -> Iterator[list[str]]:
    sent: list[str] = []
    app.dependency_overrides[get_team_notifier] = lambda: RecordingNotifier(sent)
    yield sent
    app.dependency_overrides.pop(get_team_notifier, None)


@pytest.fixture
def bank(api: httpx.AsyncClient, team: list[str]) -> FakeTochka:
    """Оплата включена: PAYMENTS_PROVIDER=tochka, банк — поддельный."""
    fake = FakeTochka()
    provider = fake.provider()
    app.dependency_overrides[get_payments_settings] = lambda: PaymentSettings(
        provider="tochka"
    )
    app.dependency_overrides[get_payment_provider] = lambda: provider
    return fake


@pytest.fixture
async def staff(session: AsyncSession, admin: User) -> User:
    assert admin.account is not None
    session.add(StaffMember(account_id=admin.account.id))
    await session.commit()
    return admin


async def _requisites(api: httpx.AsyncClient, admin: User, **changes: Any) -> None:
    response = await api.put(
        REQUISITES, json={**COMPANY, **changes}, headers=bearer(admin)
    )
    assert response.status_code == 200, response.text


async def _choose(
    api: httpx.AsyncClient, admin: User, period: str = "month", method: str = "invoice"
) -> dict[str, Any]:
    response = await api.post(
        f"{BILLING}/subscription",
        json={"period": period, "payment_method": method},
        headers=bearer(admin),
    )
    assert response.status_code == 200, response.text
    invoice: dict[str, Any] = response.json()["invoice"]
    return invoice


def _bill_ref(bank: FakeTochka, number: str) -> str:
    [ref] = [
        ref
        for ref, body in bank.bills.items()
        if body["Data"]["Content"]["Invoice"]["number"] == number
    ]
    return ref


async def _count(session: AsyncSession, model: type) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


# --- выключено по умолчанию -----------------------------------------------------------


async def test_payments_are_off_by_default(
    api: httpx.AsyncClient, session: AsyncSession, admin: User, team: list[str]
) -> None:
    overview = await api.get(BILLING, headers=bearer(admin))
    assert overview.status_code == 200, overview.text
    assert overview.json()["enabled"] is False
    assert overview.json()["subscription"] is None

    choice = await api.post(
        f"{BILLING}/subscription",
        json={"period": "month", "payment_method": "invoice"},
        headers=bearer(admin),
    )
    assert choice.status_code == 409
    assert choice.json()["code"] == "payments_disabled"

    card = await api.post(
        ORDERS,
        json={"pack": "pack_500", "payment_method": "card"},
        headers=bearer(admin),
    )
    assert card.json()["code"] == "payments_disabled"
    # Прежний поток: заказ по счёту ждёт команду, счёта в банке нет.
    order = await api.post(ORDERS, json={"pack": "pack_500"}, headers=bearer(admin))
    assert order.status_code == 201
    assert order.json()["invoice_id"] is None
    assert len(team) == 1

    webhook = await api.post(WEBHOOK, content=b"x.y.z")
    assert webhook.status_code == 404
    assert await _count(session, Invoice) == 0


# --- реквизиты ------------------------------------------------------------------------


async def test_admin_saves_checked_requisites(
    api: httpx.AsyncClient, admin: User, employee: User
) -> None:
    assert (await api.get(REQUISITES, headers=bearer(admin))).json() is None

    await _requisites(api, admin)
    saved = (await api.get(REQUISITES, headers=bearer(admin))).json()
    assert (saved["inn"], saved["kpp"], saved["payer_type"]) == (
        "7707083893",
        "773601001",
        "company",
    )

    bad_inn = await api.put(
        REQUISITES, json={**COMPANY, "inn": "7707083894"}, headers=bearer(admin)
    )
    assert bad_inn.status_code == 422
    assert (bad_inn.json()["code"], bad_inn.json()["field"]) == (
        "invalid_requisites",
        "inn",
    )
    sole_trader_kpp = await api.put(
        REQUISITES, json={**COMPANY, "inn": "500100732259"}, headers=bearer(admin)
    )
    assert sole_trader_kpp.json()["field"] == "kpp"
    await _requisites(api, admin, inn="500100732259", kpp=None, legal_name="ИП Петров")
    assert (await api.get(REQUISITES, headers=bearer(admin))).json()[
        "payer_type"
    ] == "ip"

    assert (await api.get(REQUISITES, headers=bearer(employee))).status_code == 403
    denied = await api.put(REQUISITES, json=COMPANY, headers=bearer(employee))
    assert denied.status_code == 403


# --- подписка счётом и вебхук ---------------------------------------------------------


async def test_quotes_carry_the_configured_discounts(
    api: httpx.AsyncClient, admin: User, bank: FakeTochka
) -> None:
    data = (await api.get(BILLING, headers=bearer(admin))).json()
    assert data["enabled"] is True
    assert data["seat_price_kopecks"] == 990_00
    assert [
        (q["period"], q["discount_percent"], q["amount_kopecks"])
        for q in data["quotes"]
    ] == [
        ("month", 0, 30 * 990_00),
        ("quarter", 5, 30 * 3 * 990_00 * 95 // 100),
        ("year", 10, 30 * 12 * 990_00 * 90 // 100),
    ]


async def test_invoice_needs_requisites(
    api: httpx.AsyncClient, admin: User, bank: FakeTochka
) -> None:
    response = await api.post(
        f"{BILLING}/subscription",
        json={"period": "year", "payment_method": "invoice"},
        headers=bearer(admin),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "requisites_required"
    card_year = await api.post(
        f"{BILLING}/subscription",
        json={"period": "year", "payment_method": "card"},
        headers=bearer(admin),
    )
    assert card_year.json()["code"] == "card_monthly_only"


async def test_invoice_is_paid_by_the_bank_webhook(
    api: httpx.AsyncClient,
    session: AsyncSession,
    admin: User,
    bank: FakeTochka,
    team: list[str],
    tenant_ctx: Tenant,
) -> None:
    await _requisites(api, admin)
    invoice = await _choose(api, admin, period="quarter")

    assert invoice["number"] == "KR-00001"
    assert invoice["amount_kopecks"] == 84_645_00
    assert invoice["status"] == "awaiting_payment"
    assert "KR-00001" in invoice["purpose"]
    ref = _bill_ref(bank, "KR-00001")
    side = bank.bills[ref]["Data"]["SecondSide"]
    assert (side["taxCode"], side["kpp"]) == ("7707083893", "773601001")

    pdf = await api.get(
        f"{BILLING}/invoices/{invoice['id']}/pdf", headers=bearer(admin)
    )
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")
    assert 'filename="KR-00001.pdf"' in pdf.headers["content-disposition"]

    # Банк сам сопоставил платёж со счётом (ИНН, сумма, номер) — и прислал
    # вебхук о поступлении.
    bank.pay_bill(ref)
    body = bank.incoming_webhook(
        amount="84645.00",
        purpose="Оплата по счёту № KR-00001 от 09.10.2026. Без НДС",
        inn="7707083893",
        payment_id="pay-1",
    )
    response = await api.post(
        WEBHOOK, content=body, headers={"content-type": "text/plain"}
    )
    assert response.status_code == 200, response.text

    data = (await api.get(BILLING, headers=bearer(admin))).json()
    [paid] = data["invoices"]
    assert paid["status"] == "paid"
    sub = data["subscription"]
    today = datetime.now(get_billing_settings().zone).date()
    assert sub["status"] == "active"
    assert (sub["period"], sub["seats"]) == ("quarter", 30)
    assert date.fromisoformat(sub["current_start"]) == today
    assert date.fromisoformat(sub["current_end"]) > today + timedelta(days=88)
    assert paid["period_start"] == sub["current_start"]
    [event] = (await session.scalars(select(PaymentEvent))).all()
    assert (event.status, event.charge_key) == ("matched", "in:pay-1")
    with tenant_scope(tenant_ctx.id):
        notices = (
            await session.scalars(
                select(Notification.kind).where(Notification.user_id == admin.id)
            )
        ).all()
    assert "billing_paid" in notices
    # Команде писать не о чем: всё сошлось само.
    assert team == []

    # Банк повторил доставку (не дождался 200) — второго зачисления нет.
    again = await api.post(WEBHOOK, content=body)
    assert again.status_code == 200
    assert await _count(session, PaymentEvent) == 1


async def test_bad_signature_is_rejected_before_any_write(
    api: httpx.AsyncClient, session: AsyncSession, admin: User, bank: FakeTochka
) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = sign(
        {
            "webhookType": "incomingPayment",
            "paymentId": "1",
            "purpose": "KR-00001",
            "SidePayer": {"amount": "1.0", "inn": "7707083893"},
        },
        stranger,
    )
    for body in (forged, b"garbage", b""):
        response = await api.post(WEBHOOK, content=body)
        assert response.status_code == 400
    assert await _count(session, PaymentEvent) == 0


async def test_webhook_has_a_rate_limit(
    api: httpx.AsyncClient, bank: FakeTochka
) -> None:
    codes = [(await api.post(WEBHOOK, content=b"x")).status_code for _ in range(125)]
    assert codes[0] == 400
    assert codes[-1] == 429


@pytest.mark.parametrize(
    ("amount", "inn", "purpose", "problem", "status"),
    [
        (
            "100.00",
            "7707083893",
            "Оплата по счёту KR-00001",
            "amount_differs",
            "mismatch",
        ),
        (
            "29700.00",
            "7736207543",
            "Оплата по счёту KR-00001",
            "inn_differs",
            "mismatch",
        ),
        (
            "29700.00",
            "7707083893",
            "Оплата по счёту KR-00777",
            "unknown_invoice",
            "unmatched",
        ),
        (
            "29700.00",
            "7707083893",
            "Оплата за подписку",
            "no_invoice_number",
            "unmatched",
        ),
    ],
)
async def test_mismatches_go_to_the_team(
    api: httpx.AsyncClient,
    session: AsyncSession,
    admin: User,
    bank: FakeTochka,
    team: list[str],
    amount: str,
    inn: str,
    purpose: str,
    problem: str,
    status: str,
) -> None:
    await _requisites(api, admin)
    invoice = await _choose(api, admin)
    bank.pay_bill(_bill_ref(bank, "KR-00001"))

    body = bank.incoming_webhook(amount=amount, purpose=purpose, inn=inn)
    assert (await api.post(WEBHOOK, content=body)).status_code == 200

    [event] = (await session.scalars(select(PaymentEvent))).all()
    assert (event.status, event.problem) == (status, problem)
    data = (await api.get(BILLING, headers=bearer(admin))).json()
    assert data["invoices"][0]["id"] == invoice["id"]
    assert data["invoices"][0]["status"] == "awaiting_payment"
    assert data["subscription"]["status"] == "awaiting_payment"
    [message] = team
    assert problem in message
    # Ни ИНН, ни названия плательщика — в чужой мессенджер.
    assert inn not in message and "Ромашка" not in message


async def test_payment_waits_until_the_bank_confirms_the_invoice(
    api: httpx.AsyncClient,
    session: AsyncSession,
    admin: User,
    bank: FakeTochka,
) -> None:
    from corp_ed.api.v1.dependencies import get_billing, get_payment_service

    await _requisites(api, admin)
    await _choose(api, admin)
    body = bank.incoming_webhook(
        amount="29700.0", purpose="KR-00001", inn="7707083893", payment_id="p-7"
    )
    assert (await api.post(WEBHOOK, content=body)).status_code == 200
    [event] = (await session.scalars(select(PaymentEvent))).all()
    assert (event.status, event.problem) == ("pending", "bank_not_confirmed")

    # Банк подтвердил — планировщик разбирает событие снова.
    bank.pay_bill(_bill_ref(bank, "KR-00001"))
    billing = get_billing(
        settings=PaymentSettings(provider="tochka"),
        provider=app.dependency_overrides[get_payment_provider](),
        billing=get_billing_settings(),
        notifier=RecordingNotifier([]),
    )
    from corp_ed.api.v1.dependencies import get_session_factory

    payments = get_payment_service(
        app.dependency_overrides[get_session_factory](), billing
    )
    await payments.process_open(older_than=timedelta(0))
    await session.refresh(event)
    assert event.status == "matched"
    data = (await api.get(BILLING, headers=bearer(admin))).json()
    assert data["subscription"]["status"] == "active"


# --- пакеты кредитов ------------------------------------------------------------------


async def test_credit_pack_by_card_is_credited_once(
    api: httpx.AsyncClient,
    session: AsyncSession,
    admin: User,
    bank: FakeTochka,
    team: list[str],
    tenant_ctx: Tenant,
) -> None:
    response = await api.post(
        ORDERS,
        json={"pack": "pack_500", "payment_method": "card"},
        headers=bearer(admin),
    )
    assert response.status_code == 201, response.text
    order = response.json()
    assert order["payment_method"] == "card"
    assert order["payment_url"].startswith("https://merch.tochka.com/")
    [(operation, link)] = bank.links.items()
    data = link["Data"]
    # Чек — на почту администратора: реквизитов нет.
    assert data["Client"]["email"] == "admin@test.com"
    assert data["Items"][0]["amount"] == 1490.0
    assert team == []

    bank.approve(operation)
    body = bank.acquiring_webhook(operation)
    assert (await api.post(WEBHOOK, content=body)).status_code == 200
    # Тот же платёж другим телом (банк добавил поле) — не второй грант.
    other = bank.acquiring_webhook(operation, amount="1490.00")
    assert (await api.post(WEBHOOK, content=other)).status_code == 200

    with tenant_scope(tenant_ctx.id):
        [grant] = (await session.scalars(select(CreditGrant))).all()
        stored = await session.get(CreditOrder, order["id"])
    assert grant.credits == 500
    assert stored is not None and stored.status == "paid"
    statuses = sorted(
        (e.status, e.problem)
        for e in (await session.scalars(select(PaymentEvent))).all()
    )
    assert statuses == [("ignored", "already_counted"), ("matched", None)]


async def test_authorized_but_not_charged_is_not_paid(
    api: httpx.AsyncClient, session: AsyncSession, admin: User, bank: FakeTochka
) -> None:
    await api.post(
        ORDERS,
        json={"pack": "pack_500", "payment_method": "card"},
        headers=bearer(admin),
    )
    [operation] = bank.links
    body = bank.acquiring_webhook(operation, status="AUTHORIZED")
    assert (await api.post(WEBHOOK, content=body)).status_code == 200
    [event] = (await session.scalars(select(PaymentEvent))).all()
    assert (event.status, event.problem) == ("ignored", "not_approved")


async def test_credit_pack_invoice_and_manual_mark(
    api: httpx.AsyncClient,
    session: AsyncSession,
    staff: User,
    bank: FakeTochka,
    tenant_ctx: Tenant,
) -> None:
    await _requisites(api, staff)
    order = (
        await api.post(ORDERS, json={"pack": "pack_2000"}, headers=bearer(staff))
    ).json()
    assert order["invoice_id"] is not None
    [ref] = bank.bills
    assert bank.bills[ref]["Data"]["Content"]["Invoice"]["totalAmount"] == 5490.0

    # Деньги пришли мимо автоматики — команда отмечает заказ, как раньше.
    url = f"/api/v1/staff/companies/{tenant_ctx.id}/credit-orders/{order['id']}/paid"
    assert (await api.post(url, headers=bearer(staff))).status_code == 200
    with tenant_scope(tenant_ctx.id):
        invoice = await session.get(Invoice, order["invoice_id"])
    assert invoice is not None and invoice.status == "paid"


# --- наша панель ----------------------------------------------------------------------


async def test_team_resolves_a_mismatched_payment(
    api: httpx.AsyncClient,
    session: AsyncSession,
    staff: User,
    bank: FakeTochka,
    tenant_ctx: Tenant,
) -> None:
    await _requisites(api, staff)
    invoice = await _choose(api, staff)
    body = bank.incoming_webhook(
        amount="29000.00", purpose="KR-00001", inn="7707083893"
    )
    await api.post(WEBHOOK, content=body)

    payments = (
        await api.get("/api/v1/staff/billing/payments", headers=bearer(staff))
    ).json()
    [item] = payments
    assert (item["status"], item["problem"], item["company_name"]) == (
        "mismatch",
        "amount_differs",
        "Test Co",
    )
    resolve = f"/api/v1/staff/billing/payments/{item['id']}/resolve"
    no_note = await api.post(resolve, json={}, headers=bearer(staff))
    assert no_note.json()["code"] == "note_required"
    done = await api.post(
        resolve,
        json={
            "tenant_id": str(tenant_ctx.id),
            "invoice_id": invoice["id"],
            "note": "Доплатят 700 ₽ отдельно",
        },
        headers=bearer(staff),
    )
    assert done.status_code == 204, done.text
    data = (await api.get(BILLING, headers=bearer(staff))).json()
    assert data["invoices"][0]["status"] == "paid"
    assert data["subscription"]["status"] == "active"
    assert (
        await api.get("/api/v1/staff/billing/payments", headers=bearer(staff))
    ).json() == []

    listed = (
        await api.get(
            "/api/v1/staff/billing/invoices?status=all", headers=bearer(staff)
        )
    ).json()
    assert [(i["number"], i["company_code"], i["status"]) for i in listed] == [
        ("KR-00001", "test", "paid")
    ]
    overview = (await api.get("/api/v1/staff/billing", headers=bearer(staff))).json()
    assert overview["enabled"] is True
    assert overview["subscriptions"][0]["status"] == "active"


async def test_billing_pages_are_for_the_team_only(
    api: httpx.AsyncClient, admin: User, bank: FakeTochka
) -> None:
    for url in ("/api/v1/staff/billing", "/api/v1/staff/billing/payments"):
        assert (await api.get(url, headers=bearer(admin))).status_code == 404


# --- места ----------------------------------------------------------------------------


async def _paid_month(
    api: httpx.AsyncClient, admin: User, bank: FakeTochka, method: str = "invoice"
) -> None:
    await _requisites(api, admin)
    await _choose(api, admin, method=method)
    if method == "invoice":
        ref = _bill_ref(bank, "KR-00001")
        bank.pay_bill(ref)
        body = bank.incoming_webhook(
            amount="29700.00", purpose="KR-00001", inn="7707083893"
        )
    else:
        [operation] = bank.links
        bank.approve(operation)
        body = bank.acquiring_webhook(operation)
    assert (await api.post(WEBHOOK, content=body)).status_code == 200


async def test_added_seats_are_charged_for_the_rest_of_the_period(
    api: httpx.AsyncClient,
    session: AsyncSession,
    staff: User,
    bank: FakeTochka,
    tenant_ctx: Tenant,
) -> None:
    await _paid_month(api, staff, bank)
    response = await api.patch(
        f"/api/v1/staff/companies/{tenant_ctx.id}",
        json={"seats": 34},
        headers=bearer(staff),
    )
    assert response.status_code == 200, response.text
    assert response.json()["seats"] == 34

    data = (await api.get(BILLING, headers=bearer(staff))).json()
    topup = next(item for item in data["invoices"] if item["kind"] == "seats")
    sub = data["subscription"]
    expected = topup_amount(
        "base",
        4,
        BillingPeriod.MONTH,
        DISCOUNTS,
        start=date.fromisoformat(sub["current_start"]),
        end=date.fromisoformat(sub["current_end"]),
        today=datetime.now(get_billing_settings().zone).date(),
    )
    # Сегодня — первый день периода: доплата за весь месяц.
    assert topup["amount_kopecks"] == expected == 4 * 990_00
    assert topup["status"] == "awaiting_payment"

    ref = _bill_ref(bank, topup["number"])
    bank.pay_bill(ref)
    body = bank.incoming_webhook(
        amount="3960.00", purpose=f"Оплата {topup['number']}", inn="7707083893"
    )
    await api.post(WEBHOOK, content=body)
    data = (await api.get(BILLING, headers=bearer(staff))).json()
    assert data["subscription"]["seats"] == 34


async def test_fewer_seats_wait_for_the_next_period(
    api: httpx.AsyncClient,
    session: AsyncSession,
    staff: User,
    bank: FakeTochka,
    tenant_ctx: Tenant,
) -> None:
    await _paid_month(api, staff, bank)
    response = await api.patch(
        f"/api/v1/staff/companies/{tenant_ctx.id}",
        json={"seats": 20},
        headers=bearer(staff),
    )
    assert response.status_code == 200, response.text
    # Оплачено 30 до конца периода — их и оставляем.
    assert response.json()["seats"] == 30
    data = (await api.get(BILLING, headers=bearer(staff))).json()
    assert data["subscription"]["next_seats"] == 20
    assert [item["kind"] for item in data["invoices"]] == ["subscription"]


async def test_seats_without_payments_change_as_before(
    api: httpx.AsyncClient, staff: User, tenant_ctx: Tenant
) -> None:
    response = await api.patch(
        f"/api/v1/staff/companies/{tenant_ctx.id}",
        json={"seats": 20},
        headers=bearer(staff),
    )
    assert response.json()["seats"] == 20


# --- изоляция компаний ----------------------------------------------------------------


async def test_company_sees_only_its_invoices(
    api: httpx.AsyncClient, session: AsyncSession, admin: User, bank: FakeTochka
) -> None:
    await _requisites(api, admin)
    invoice = await _choose(api, admin)

    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        stranger = make_user(
            tenant_id=other.id, email="boss@other.ru", role=UserRole.ADMIN
        )
        assert stranger.account is not None
        stranger.account.totp_enabled_at = datetime.now(UTC)
        session.add(stranger)
        await session.commit()

    data = (await api.get(BILLING, headers=bearer(stranger))).json()
    assert data["invoices"] == [] and data["requisites"] is None
    pdf = await api.get(
        f"{BILLING}/invoices/{invoice['id']}/pdf", headers=bearer(stranger)
    )
    assert pdf.status_code == 404
    # Номер сквозной: у второй компании счёт — следующий, не № 1.
    await _requisites(api, stranger, inn="7736207543", legal_name="ООО «Другая»")
    second = await _choose(api, stranger)
    assert second["number"] == "KR-00002"
    refs = (await session.scalars(select(InvoiceRef.number))).all()
    assert sorted(refs) == [1, 2]
    with tenant_scope(other.id):
        assert [
            s.tenant_id for s in (await session.scalars(select(Subscription))).all()
        ] == [other.id]
