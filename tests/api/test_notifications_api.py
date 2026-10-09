"""Уведомления, недельная сводка, первые шаги, поддержка (ТЗ §8)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.api.v1.dependencies import get_team_notifier
from corp_ed.core.config import EMBEDDING_DIM
from corp_ed.domain.models import (
    AuditEvent,
    Invite,
    Notification,
    NotificationSetting,
    OutboxEmail,
    QaLog,
    StaffMember,
    SupportRequest,
    Tenant,
    User,
    UserRole,
)
from corp_ed.main import app
from corp_ed.services.digest_service import DigestService, digest_due, week_start
from tests.api.conftest import account_bearer, bearer
from tests.conftest import make_credit_service
from tests.factories import make_account
from tests.team_notify_helpers import RecordingNotifier
from tests.test_credits import spend

MSK = ZoneInfo("Europe/Moscow")


def _log(tenant: Tenant, question: str, **fields: Any) -> QaLog:
    return QaLog(
        tenant_id=tenant.id,
        question=question,
        question_embedding=[0.1] * EMBEDDING_DIM,
        embedding_model="fake",
        prompt_version="test",
        answer_given=fields.pop("answer_given", True),
        origin="documents",
        **fields,
    )


async def _mails(session: AsyncSession, kind: str) -> list[OutboxEmail]:
    return list(
        (
            await session.scalars(select(OutboxEmail).where(OutboxEmail.kind == kind))
        ).all()
    )


# --- колокольчик и письма -------------------------------------------------------


async def test_credit_thresholds_reach_admins_by_bell_and_mail_per_settings(
    api: httpx.AsyncClient,
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
) -> None:
    # Письма о лимите администратор выключил — колокольчик остаётся.
    session.add(NotificationSetting(user_id=admin.id, email_credits=False))
    tenant_ctx.seats = 1  # пул 420, порог 336
    await session.commit()
    service = make_credit_service(session)

    async def answer(credits: int) -> None:
        usage = await service.ensure_available()
        spend(session, employee, credits)
        await session.flush()
        await service.note_spend(usage, credits)
        await session.commit()

    await answer(340)

    inbox = await api.get("/api/v1/notifications", headers=bearer(admin))
    assert inbox.status_code == 200, inbox.text
    data = inbox.json()
    assert data["unread"] == 1
    [item] = data["items"]
    assert (item["kind"], item["link"], item["read"]) == (
        "credits_warning",
        "/admin/tariff",
        False,
    )
    assert "80 %" in item["title"]
    assert await _mails(session, "notice_credits_warning") == []
    # Сотруднику — ни колокольчика, ни письма.
    employee_inbox = await api.get("/api/v1/notifications", headers=bearer(employee))
    assert employee_inbox.json() == {"items": [], "unread": 0}

    # Включил обратно — исчерпанный пул приходит и письмом.
    put = await api.put(
        "/api/v1/notifications/settings",
        json={"email_credits": True},
        headers=bearer(admin),
    )
    assert put.json()["email_credits"] is True
    await answer(100)
    [mail] = await _mails(session, "notice_credits_exhausted")
    assert mail.to_email == "admin@test.com"
    assert "/settings/notifications" in mail.text_body

    # Повторный порог в том же месяце — без второго уведомления.
    await service.note_spend(await service.usage(), 0)
    await session.commit()
    assert len((await session.scalars(select(Notification))).all()) == 2


async def test_mark_read_touches_only_own_notifications(
    api: httpx.AsyncClient,
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
) -> None:
    mine = [
        Notification(
            tenant_id=tenant_ctx.id,
            user_id=admin.id,
            kind="join_request",
            title=t,
            body="",
        )
        for t in ("Первая", "Вторая")
    ]
    other = Notification(
        tenant_id=tenant_ctx.id,
        user_id=employee.id,
        kind="join_request",
        title="Чужая",
        body="",
    )
    session.add_all([*mine, other])
    await session.commit()

    one = await api.post(
        "/api/v1/notifications/read",
        json={"ids": [str(mine[0].id), str(other.id)]},
        headers=bearer(admin),
    )
    assert one.status_code == 200, one.text
    assert one.json()["unread"] == 1
    everything = await api.post(
        "/api/v1/notifications/read", json={}, headers=bearer(admin)
    )
    assert everything.json()["unread"] == 0
    # Чужое уведомление осталось непрочитанным.
    theirs = await api.get("/api/v1/notifications", headers=bearer(employee))
    assert theirs.json()["unread"] == 1


async def test_employee_has_no_email_settings(
    api: httpx.AsyncClient, employee: User
) -> None:
    response = await api.get("/api/v1/notifications/settings", headers=bearer(employee))
    assert response.status_code == 403


async def test_join_request_notifies_admins(
    api: httpx.AsyncClient,
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin_account: User,
) -> None:
    created = await api.post(
        "/api/v1/invites",
        json={"requires_approval": True},
        headers=bearer(admin_account),
    )
    assert created.status_code == 201, created.text
    joiner = make_account("new@test.com", full_name="Новый Сотрудник")
    session.add(joiner)
    await session.commit()

    response = await api.post(
        "/api/v1/invites/accept",
        json={"secret": created.json()["token"]},
        headers=account_bearer(joiner),
    )
    assert response.json()["outcome"] == "pending"
    [notice] = (await session.scalars(select(Notification))).all()
    assert notice.user_id == admin_account.id
    assert notice.kind == "join_request"
    assert "Новый Сотрудник" in notice.title
    assert notice.link == "/admin/users"
    assert len(await _mails(session, "notice_join_request")) == 1


# --- недельная сводка -------------------------------------------------------------


def test_digest_waits_for_monday_nine_moscow() -> None:
    monday_early = datetime(2026, 10, 5, 8, 59, tzinfo=MSK)
    monday = datetime(2026, 10, 5, 9, 0, tzinfo=MSK)
    wednesday = datetime(2026, 10, 7, 3, 0, tzinfo=MSK)
    sunday = datetime(2026, 10, 11, 20, 0, tzinfo=MSK)
    assert not digest_due(monday_early, MSK)
    assert digest_due(monday, MSK)
    # Воркер лежал в понедельник — сводка всё равно уйдёт на этой неделе
    # (раз в неделю — по отметке в журнале).
    assert digest_due(wednesday, MSK)
    assert digest_due(sunday, MSK)


async def test_weekly_digest_once_per_week_and_not_for_empty_week(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
) -> None:
    quiet = Tenant(id=uuid4(), company_code="quiet", name="Тихая")
    session.add(quiet)
    # Понедельник этой недели: отметка в журнале пишется по часам базы.
    monday = week_start(datetime.now(UTC), MSK) + timedelta(hours=10)
    for i in range(3):
        session.add(
            _log(tenant_ctx, f"Вопрос {i}", created_at=monday - timedelta(days=2))
        )
    await session.commit()
    service = DigestService(session_maker, zone="Europe/Moscow")
    admin_id = admin.id

    first = await service.send_due(monday)
    assert (first.sent, first.skipped) == (1, 1)
    again = await service.send_due(monday + timedelta(hours=1))
    assert (again.sent, again.skipped) == (0, 2)

    session.expire_all()
    [notice] = (
        await session.scalars(
            select(Notification).where(Notification.kind == "weekly_digest")
        )
    ).all()
    assert notice.user_id == admin_id
    assert notice.title == "Неделя в kronto: 3 вопроса"
    assert len(await _mails(session, "notice_weekly_digest")) == 1


# --- первые шаги ------------------------------------------------------------------


async def test_onboarding_checks_itself_and_remembers_what_was_shown(
    api: httpx.AsyncClient,
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
) -> None:
    state = (await api.get("/api/v1/onboarding", headers=bearer(admin))).json()
    # Сотрудник уже есть — шаг «пригласить людей» выполнен.
    assert (state["documents"], state["people"], state["question"]) == (
        False,
        True,
        False,
    )
    session.add(_log(tenant_ctx, "Первый вопрос"))
    await session.commit()
    state = (await api.get("/api/v1/onboarding", headers=bearer(admin))).json()
    assert state["question"] is True

    hidden = await api.post("/api/v1/onboarding/checklist", headers=bearer(admin))
    assert hidden.json()["checklist_hidden"] is True
    tips = await api.post("/api/v1/onboarding/tips", headers=bearer(employee))
    assert tips.json()["tips_seen"] is True
    assert tips.json()["checklist_hidden"] is False
    bad = await api.post("/api/v1/onboarding/other", headers=bearer(employee))
    assert bad.status_code == 422


async def test_onboarding_invite_counts_as_people(
    api: httpx.AsyncClient, session: AsyncSession, tenant_ctx: Tenant, admin: User
) -> None:
    assert (await api.get("/api/v1/onboarding", headers=bearer(admin))).json()[
        "people"
    ] is False
    session.add(
        Invite(
            tenant_id=tenant_ctx.id,
            token_hash="x" * 64,
            code_hash="y" * 64,
            role=UserRole.EMPLOYEE,
            max_uses=10,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )
    await session.commit()
    assert (await api.get("/api/v1/onboarding", headers=bearer(admin))).json()[
        "people"
    ] is True


# --- поддержка --------------------------------------------------------------------


@pytest.fixture
def notifier() -> Any:
    sent: list[str] = []
    app.dependency_overrides[get_team_notifier] = lambda: RecordingNotifier(sent)
    yield sent
    app.dependency_overrides.pop(get_team_notifier, None)


async def test_support_request_reaches_team_without_personal_data(
    api: httpx.AsyncClient,
    session: AsyncSession,
    employee: User,
    admin: User,
    notifier: list[str],
) -> None:
    text = "Не могу войти: код из письма не приходит уже час, почта anna@test.com"
    response = await api.post(
        "/api/v1/support",
        json={"topic": "login", "message": text},
        headers=bearer(employee),
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert (created["topic"], created["status"]) == ("login", "new")

    # В Telegram команды — номер, тема и короткий id компании; ни текста,
    # ни почты, ни кода компании (он повторяет название).
    [message] = notifier
    assert created["id"][:8] in message
    assert "вход" in message
    assert f"компания {str(employee.tenant_id)[:8]}" in message
    assert "test" not in message
    assert "anna@test.com" not in message and "employee@test.com" not in message
    assert "код из письма" not in message

    mine = await api.get("/api/v1/support/mine", headers=bearer(employee))
    assert [item["id"] for item in mine.json()] == [created["id"]]
    assert (await api.get("/api/v1/support/mine", headers=bearer(admin))).json() == []

    # Команда видит текст и почту в панели и отмечает ответ.
    assert admin.account is not None
    session.add(StaffMember(account_id=admin.account.id))
    await session.commit()
    listed = await api.get("/api/v1/staff/support?status=new", headers=bearer(admin))
    [item] = listed.json()
    assert (item["email"], item["company"], item["message"]) == (
        "employee@test.com",
        "Test Co",
        text,
    )
    answered = await api.patch(
        f"/api/v1/staff/support/{created['id']}",
        json={"status": "answered"},
        headers=bearer(admin),
    )
    assert answered.json()["status"] == "answered"
    assert (
        await api.get("/api/v1/staff/support?status=new", headers=bearer(admin))
    ).json() == []


async def test_support_is_hidden_from_non_staff_and_rate_limited(
    api: httpx.AsyncClient,
    session: AsyncSession,
    employee: User,
    notifier: list[str],
) -> None:
    assert (
        await api.get("/api/v1/staff/support", headers=bearer(employee))
    ).status_code == 404
    short = await api.post(
        "/api/v1/support",
        json={"topic": "other", "message": "ой"},
        headers=bearer(employee),
    )
    assert short.status_code == 422
    for _ in range(5):
        ok = await api.post(
            "/api/v1/support",
            json={"topic": "other", "message": "Вопрос про работу сервиса"},
            headers=bearer(employee),
        )
        assert ok.status_code == 201
    limited = await api.post(
        "/api/v1/support",
        json={"topic": "other", "message": "Шестое обращение за час"},
        headers=bearer(employee),
    )
    assert limited.status_code == 429
    assert len((await session.scalars(select(SupportRequest))).all()) == 5
    events = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "support.requested")
        )
    ).all()
    assert len(events) == 5
