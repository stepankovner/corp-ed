"""Наша панель (ТЗ §9): только команда kronto с надёжным вторым фактором;
заявки, компании, расход на модели, помощь со входом, заявки на созвон."""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.api.v1.dependencies import get_billing_settings
from corp_ed.core.config import EMBEDDING_DIM, BillingSettings
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    AuditEvent,
    CompanyRequest,
    Lead,
    OutboxEmail,
    QaLog,
    StaffMember,
    Tenant,
    User,
    UserRole,
)
from corp_ed.main import app
from tests.api.conftest import bearer
from tests.factories import make_account, make_user


@pytest.fixture
async def staff(session: AsyncSession, admin: User) -> User:
    """Администратор тестовой компании, он же — команда kronto."""
    assert admin.account is not None
    session.add(StaffMember(account_id=admin.account.id))
    await session.commit()
    return admin


def _log(tenant: Tenant, question: str, **fields: Any) -> QaLog:
    return QaLog(
        tenant_id=tenant.id,
        question=question,
        question_embedding=[0.1] * EMBEDDING_DIM,
        embedding_model="fake",
        prompt_version="test",
        answer_given=True,
        origin="documents",
        **fields,
    )


async def _events(session: AsyncSession, action: str) -> list[AuditEvent]:
    return list(
        (
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.action == action)
                .order_by(AuditEvent.created_at)
            )
        ).all()
    )


async def test_panel_is_hidden_from_everyone_but_staff_with_strong_factor(
    api: httpx.AsyncClient, admin: User, employee: User, session: AsyncSession
) -> None:
    # Администратор компании, но не команда kronto: панели «нет».
    response = await api.get("/api/v1/staff/overview", headers=bearer(admin))
    assert response.status_code == 404
    me = await api.get("/api/v1/auth/me", headers=bearer(admin))
    assert me.json()["staff"] is False

    # В команде, но без приложения и ключа: сначала защита.
    assert employee.account is not None
    session.add(StaffMember(account_id=employee.account.id))
    await session.commit()
    response = await api.get("/api/v1/staff/overview", headers=bearer(employee))
    assert response.status_code == 403
    assert response.json()["code"] == "mfa_setup_required"

    # В команде и с приложением — пускает.
    assert admin.account is not None
    session.add(StaffMember(account_id=admin.account.id))
    await session.commit()
    response = await api.get("/api/v1/staff/overview", headers=bearer(admin))
    assert response.status_code == 200, response.text
    assert response.json()["companies"] == 1
    me = await api.get("/api/v1/auth/me", headers=bearer(admin))
    assert me.json()["staff"] is True


async def test_staff_approves_request_and_company_appears(
    api: httpx.AsyncClient, staff: User, session: AsyncSession
) -> None:
    applicant = make_account("lena@romashka.ru", full_name="Лена Ромашкина")
    session.add(applicant)
    await session.flush()
    request = CompanyRequest(
        account_id=applicant.id, company_name="ООО «Ромашка»", seats=5, comment="Пилот"
    )
    session.add(request)
    await session.commit()

    listed = await api.get("/api/v1/staff/requests", headers=bearer(staff))
    assert listed.status_code == 200, listed.text
    [item] = listed.json()
    assert item["applicant_email"] == "lena@romashka.ru"
    assert item["applicant_name"] == "Лена Ромашкина"

    pilot = (date.today() + timedelta(days=30)).isoformat()
    response = await api.post(
        f"/api/v1/staff/requests/{request.id}/approve",
        json={"tariff": "extended", "seats": 7, "pilot_until": pilot},
        headers=bearer(staff),
    )
    assert response.status_code == 200, response.text
    company = response.json()
    assert company["name"] == "ООО «Ромашка»"
    assert (company["tariff"], company["seats"], company["pilot_until"]) == (
        "extended",
        7,
        pilot,
    )
    assert company["admins"] == ["lena@romashka.ru"]
    assert company["members"] == 1

    # Заявка рассмотрена — второй раз не одобрить.
    again = await api.post(
        f"/api/v1/staff/requests/{request.id}/approve",
        json={},
        headers=bearer(staff),
    )
    assert again.status_code == 409
    assert (await api.get("/api/v1/staff/requests", headers=bearer(staff))).json() == []

    # В журнале — кто из команды одобрил.
    assert staff.account is not None
    [approved] = await _events(session, "company_request.approved")
    assert approved.details["staff_account_id"] == str(staff.account.id)
    [pilot_event] = await _events(session, "tenant.pilot_changed")
    assert pilot_event.details["to"] == pilot


async def test_staff_rejects_request(
    api: httpx.AsyncClient, staff: User, session: AsyncSession
) -> None:
    applicant = make_account("spam@example.ru")
    session.add(applicant)
    await session.flush()
    request = CompanyRequest(account_id=applicant.id, company_name="Спам")
    session.add(request)
    await session.commit()

    response = await api.post(
        f"/api/v1/staff/requests/{request.id}/reject", headers=bearer(staff)
    )
    assert response.status_code == 204
    every = await api.get("/api/v1/staff/requests?status=all", headers=bearer(staff))
    assert [r["status"] for r in every.json()] == ["rejected"]


async def test_staff_changes_seats_tariff_pilot_and_suspends(
    api: httpx.AsyncClient, staff: User, session: AsyncSession, tenant_ctx: Tenant
) -> None:
    # Потрачено 500 кредитов — один пул на место (420) меньше потраченного.
    session.add(_log(tenant_ctx, "Вопрос", credits=500, input_tokens=10))
    await session.commit()
    url = f"/api/v1/staff/companies/{tenant_ctx.id}"

    stop = await api.patch(url, json={"seats": 1}, headers=bearer(staff))
    assert stop.status_code == 409
    assert stop.json()["code"] == "seats_stop_pool"

    response = await api.patch(
        url,
        json={"seats": 40, "tariff": "extended", "pilot_until": "2026-12-31"},
        headers=bearer(staff),
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert (data["seats"], data["tariff"], data["pilot_until"]) == (
        40,
        "extended",
        "2026-12-31",
    )
    assert data["credits_used"] == 500
    assert data["pool"] == 40 * 420
    assert data["questions_month"] == 1
    assert data["admins"] == ["admin@test.com"]

    # null — снять срок пилота.
    response = await api.patch(url, json={"pilot_until": None}, headers=bearer(staff))
    assert response.status_code == 200, response.text
    assert response.json()["pilot_until"] is None

    # Свою компанию из панели не приостановить: её токен перестал бы
    # приниматься.
    own = await api.patch(url, json={"is_active": False}, headers=bearer(staff))
    assert own.status_code == 409
    assert own.json()["code"] == "own_company"

    # Другую — приостановить и вернуть.
    other = Tenant(id=uuid4(), company_code="paused", name="На паузе")
    session.add(other)
    await session.commit()
    other_url = f"/api/v1/staff/companies/{other.id}"
    paused = await api.patch(
        other_url, json={"is_active": False}, headers=bearer(staff)
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["is_active"] is False
    resumed = await api.patch(
        other_url, json={"is_active": True}, headers=bearer(staff)
    )
    assert resumed.json()["is_active"] is True

    assert staff.account is not None
    for action in (
        "tenant.seats_changed",
        "tenant.tariff_changed",
        "tenant.suspended",
        "tenant.resumed",
    ):
        [event] = await _events(session, action)
        assert event.details["staff_account_id"] == str(staff.account.id), action
    assert len(await _events(session, "tenant.pilot_changed")) == 2


async def test_spend_counts_tokens_by_day_model_and_company(
    api: httpx.AsyncClient, staff: User, session: AsyncSession, tenant_ctx: Tenant
) -> None:
    other = Tenant(id=uuid4(), company_code="other", name="Другая")
    session.add(other)
    session.add_all(
        [
            _log(
                tenant_ctx,
                "Первый",
                input_tokens=1000,
                output_tokens=200,
                credits=1,
                llm_model="yandexgpt",
            ),
            _log(
                tenant_ctx,
                "Старый",
                input_tokens=9999,
                created_at=datetime.now(UTC) - timedelta(days=40),
            ),
        ]
    )
    await session.commit()
    with tenant_scope(other.id):
        session.add(
            _log(
                other,
                "Чужой",
                input_tokens=500,
                output_tokens=300,
                credits=1,
                llm_model="qwen",
            )
        )
        await session.commit()

    def priced() -> BillingSettings:
        return BillingSettings(llm_rub_per_1k_tokens=0.5)

    app.dependency_overrides[get_billing_settings] = priced
    try:
        response = await api.get("/api/v1/staff/spend?days=7", headers=bearer(staff))
    finally:
        app.dependency_overrides.pop(get_billing_settings, None)
    assert response.status_code == 200, response.text
    data = response.json()
    assert (data["questions"], data["input_tokens"], data["output_tokens"]) == (
        2,
        1500,
        500,
    )
    assert data["credits"] == 2
    assert data["rub"] == 1.0  # 2 000 токенов × 0,5 ₽ за тысячу
    assert len(data["days"]) == 7
    assert sum(day["tokens"] for day in data["days"]) == 2000
    assert [m["model"] for m in data["models"]] == ["yandexgpt", "qwen"]
    assert [(c["name"], c["tokens"]) for c in data["companies"]] == [
        ("Test Co", 1200),
        ("Другая", 800),
    ]


async def test_people_search_shows_login_state_and_sends_reset_link(
    api: httpx.AsyncClient,
    staff: User,
    employee: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    # Человек ещё в одной компании — её тоже видно.
    assert employee.account is not None
    other = Tenant(id=uuid4(), company_code="second", name="Вторая")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        session.add(
            make_user(
                tenant_id=other.id,
                email=employee.account.email,
                account=employee.account,
                role=UserRole.ADMIN,
            )
        )
        await session.commit()

    short = await api.get("/api/v1/staff/people?q=em", headers=bearer(staff))
    assert short.json() == []

    found = await api.get(
        f"/api/v1/staff/people?q={employee.account.email[:5]}", headers=bearer(staff)
    )
    assert found.status_code == 200, found.text
    [person] = [p for p in found.json() if p["id"] == str(employee.account.id)]
    assert {(c["company_name"], c["role"]) for c in person["companies"]} == {
        ("Test Co", "employee"),
        ("Вторая", "admin"),
    }
    assert (person["totp"], person["passkeys"], person["staff"]) == (False, 0, False)

    sent = await api.post(
        f"/api/v1/staff/people/{employee.account.id}/password-reset",
        headers=bearer(staff),
    )
    assert sent.status_code == 202
    mails = (
        await session.scalars(
            select(OutboxEmail).where(OutboxEmail.to_email == employee.account.email)
        )
    ).all()
    assert len(mails) == 1
    assert staff.account is not None
    [event] = await _events(session, "auth.password.reset_requested")
    assert event.details["staff_account_id"] == str(staff.account.id)


async def test_leads_list_and_status(
    api: httpx.AsyncClient, staff: User, session: AsyncSession
) -> None:
    lead = Lead(
        company_name="ООО «Север»",
        contact_name="Анна",
        phone="+79991234567",
        email=None,
        seats=60,
        tariff="base",
        preferred_date=date.today() + timedelta(days=1),
        preferred_slot="10:00–12:00",
        comment=None,
        policy_version="test",
        consented_at=datetime.now(UTC),
    )
    session.add(lead)
    await session.commit()

    listed = await api.get("/api/v1/staff/leads?status=new", headers=bearer(staff))
    assert [item["company_name"] for item in listed.json()] == ["ООО «Север»"]
    updated = await api.patch(
        f"/api/v1/staff/leads/{lead.id}",
        json={"status": "contacted"},
        headers=bearer(staff),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["status"] == "contacted"


async def test_companies_list_counts_people_without_crossing_tenants(
    api: httpx.AsyncClient, staff: User, employee: User, session: AsyncSession
) -> None:
    other = Tenant(id=uuid4(), company_code="third", name="Третья", seats=3)
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        session.add(
            make_user(tenant_id=other.id, email="boss@third.ru", role=UserRole.ADMIN)
        )
        await session.commit()

    response = await api.get("/api/v1/staff/companies", headers=bearer(staff))
    assert response.status_code == 200, response.text
    rows = {row["name"]: row for row in response.json()}
    assert rows["Test Co"]["members"] == 2
    assert rows["Test Co"]["admins"] == ["admin@test.com"]
    assert rows["Третья"]["members"] == 1
    assert rows["Третья"]["admins"] == ["boss@third.ru"]


async def test_cli_adds_and_removes_staff(
    session: AsyncSession,
    session_maker: Any,
    employee: User,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """В команду kronto попадают только из CLI — через API не добавить."""
    from corp_ed import cli

    monkeypatch.setattr(cli, "get_session_maker", lambda: session_maker)
    assert employee.account is not None
    email, account_id = employee.account.email, employee.account.id

    assert (
        await cli._run(cli._parser().parse_args(["staff", "add", "--email", email]))
        == 0
    )
    out = capsys.readouterr().out
    assert "в команде" in out
    # Без приложения или ключа панель не откроется — CLI говорит об этом.
    assert "приложение" in out
    assert await session.get(StaffMember, account_id) is not None

    assert await cli._run(cli._parser().parse_args(["staff", "list"])) == 0
    assert email in capsys.readouterr().out

    assert (
        await cli._run(cli._parser().parse_args(["staff", "remove", "--email", email]))
        == 0
    )
    session.expire_all()
    assert await session.get(StaffMember, account_id) is None
    actions = [e.action for e in await _events(session, "staff.added")] + [
        e.action for e in await _events(session, "staff.removed")
    ]
    assert actions == ["staff.added", "staff.removed"]
