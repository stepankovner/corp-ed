"""Автоудаление учёток без компании (решение 22.10): письмо за неделю,
удаление через 30 дней с регистрации; учётки, бывшие в компании, команда
и заявители на рассмотрении не трогаются."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.domain.models import (
    Account,
    AuditEvent,
    CompanyRequest,
    MemberStatus,
    OutboxEmail,
    StaffMember,
    Tenant,
)
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.companyless_service import CompanylessService
from tests.factories import make_account, make_user

NOW = datetime.now(UTC)


async def _account(
    session: AsyncSession, email: str, *, days: int, verified: bool = True
) -> Account:
    account = make_account(email, first_name="Ирина", verified=verified)
    account.created_at = NOW - timedelta(days=days)
    session.add(account)
    await session.commit()
    return account


async def _alive(session: AsyncSession, email: str) -> Account | None:
    session.expunge_all()
    return (
        await session.scalars(select(Account).where(Account.email == email))
    ).first()


async def _warnings(session: AsyncSession, email: str) -> list[OutboxEmail]:
    return list(
        await session.scalars(
            select(OutboxEmail).where(
                OutboxEmail.to_email == email,
                OutboxEmail.kind == "companyless_warning",
            )
        )
    )


async def test_warns_a_week_before_and_then_deletes(
    session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    await _account(session, "lonely@mail.ru", days=24)
    await _account(session, "fresh@mail.ru", days=10)
    service = CompanylessService(session_maker)

    report = await service.run(NOW)

    assert (report.warned, report.deleted) == (1, 0)
    [mail] = await _warnings(session, "lonely@mail.ru")
    delete_on = (NOW + timedelta(days=7)).date()
    assert f"{delete_on:%d.%m.%Y}" in mail.subject
    assert "/join" in mail.text_body and "/settings/companies" in mail.text_body
    assert await _warnings(session, "fresh@mail.ru") == []

    # Через три дня — ни второго письма, ни удаления: неделя не прошла.
    report = await service.run(NOW + timedelta(days=3))
    assert (report.warned, report.deleted) == (0, 0)
    assert await _alive(session, "lonely@mail.ru") is not None

    report = await service.run(NOW + timedelta(days=7, minutes=1))

    assert report.deleted == 1
    assert await _alive(session, "lonely@mail.ru") is None
    assert await _alive(session, "fresh@mail.ru") is not None
    event = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "account.deleted")
        )
    ).one()
    assert event.details["reason"] == "companyless"


async def test_unverified_account_is_deleted_without_a_letter(
    session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    await _account(session, "typo@mail.ru", days=31, verified=False)
    await _account(session, "typo2@mail.ru", days=24, verified=False)

    report = await CompanylessService(session_maker).run(NOW)

    assert (report.warned, report.deleted) == (0, 1)
    assert await _alive(session, "typo@mail.ru") is None
    assert await _alive(session, "typo2@mail.ru") is not None
    assert await _warnings(session, "typo2@mail.ru") == []


async def test_members_staff_and_applicants_are_kept(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    # Был в компании и ушёл — строка членства осталась.
    left = await _account(session, "left@mail.ru", days=90)
    session.add(
        make_user(
            email=left.email,
            account=left,
            tenant_id=tenant_ctx.id,
            status=MemberStatus.LEFT,
        )
    )
    # Ждёт одобрения вступления.
    pending = await _account(session, "pending@mail.ru", days=90)
    session.add(
        make_user(
            email=pending.email,
            account=pending,
            tenant_id=tenant_ctx.id,
            status=MemberStatus.PENDING,
        )
    )
    # Был в компании, которую потом удалили: строк членства нет, отметка есть.
    former = await _account(session, "former@mail.ru", days=90)
    former.joined_company_at = NOW - timedelta(days=80)
    # Команда kronto и заявитель с заявкой на рассмотрении.
    staff = await _account(session, "staff@mail.ru", days=90)
    session.add(StaffMember(account_id=staff.id))
    applicant = await _account(session, "applicant@mail.ru", days=90)
    session.add(
        CompanyRequest(
            account_id=applicant.id, company_name="ООО «Лес»", inn="1234567894"
        )
    )
    await session.commit()

    report = await CompanylessService(session_maker).run(NOW)

    assert (report.warned, report.deleted) == (0, 0)
    for email in ("left", "pending", "former", "staff", "applicant"):
        assert await _alive(session, f"{email}@mail.ru") is not None, email


async def test_rejected_request_restarts_the_warning(
    session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    account = await _account(session, "maybe@mail.ru", days=40)
    service = CompanylessService(session_maker)
    assert (await service.run(NOW)).warned == 1

    # Подал заявку — письмо больше не в силе.
    request = CompanyRequest(account_id=account.id, company_name="ООО «Поле»")
    session.add(request)
    await session.commit()
    report = await service.run(NOW + timedelta(days=8))
    assert (report.reprieved, report.deleted) == (1, 0)

    # Заявку отклонили — сначала новое письмо, удаление через неделю.
    request.status = "rejected"
    await session.commit()
    report = await service.run(NOW + timedelta(days=9))
    assert (report.warned, report.deleted) == (1, 0)
    assert len(await _warnings(session, "maybe@mail.ru")) == 2
    report = await service.run(NOW + timedelta(days=16, minutes=1))
    assert report.deleted == 1


async def test_membership_marks_the_account_for_good(
    session: AsyncSession, tenant_ctx: Tenant
) -> None:
    account = await _account(session, "joined@mail.ru", days=1)
    assert account.joined_company_at is None

    await UserRepository(session).create(
        make_user(email=account.email, account=account, tenant_id=tenant_ctx.id)
    )
    await session.commit()

    await session.refresh(account)
    assert account.joined_company_at is not None
