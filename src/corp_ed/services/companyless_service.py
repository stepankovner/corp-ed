"""Автоудаление учёток без компании (решение 22.10, вариант в) разбора 2.6).

Зарегистрироваться может любой, но без компании учётка ничего не умеет, а
имя и почта лежат — персональные данные без цели. Учётку, которая за
COMPANYLESS_TTL с регистрации ни разу не была членом компании, ночная
задача (python -m corp_ed.cli purge) удаляет тем же путём, что и
самоудаление (account_service.erase_account). За WARNING_BEFORE до этого
на подтверждённую почту уходит письмо: вступить по приглашению или подать
заявку на свою компанию.

Не трогает: учётки, хоть раз бывшие в компании (joined_company_at или
любая строка членства, в том числе «ушёл» и «заблокирован»), ждущие
одобрения вступления, с заявкой «Подключить компанию» на рассмотрении и
команду kronto. Неподтверждённой почте письмо не шлём — адрес мог ввести
не его владелец; такая учётка войти не может и удаляется в срок без
предупреждения.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from corp_ed.core.database import ACCOUNT_SCOPE_OPTION
from corp_ed.core.tenant_context import account_scope
from corp_ed.domain.models import Account, CompanyRequest, StaffMember, User
from corp_ed.repositories.audit_repository import AuditRepository
from corp_ed.services import email_templates
from corp_ed.services.account_service import erase_account
from corp_ed.services.email_service import EmailService

logger = structlog.get_logger()

COMPANYLESS_TTL = timedelta(days=30)
"""Сколько учётка может прожить, ни разу не вступив в компанию."""

WARNING_BEFORE = timedelta(days=7)
"""За сколько до удаления предупреждаем письмом."""


@dataclass(frozen=True)
class CompanylessReport:
    warned: int = 0
    deleted: int = 0
    reprieved: int = 0
    """Предупреждённые, которые с тех пор подали заявку или вступают в
    компанию: отметка письма снята, при новом поводе — новое письмо."""


def _without_company() -> list[ColumnElement[bool]]:
    """Условия на саму учётку; членства проверяет _has_memberships."""
    return [
        Account.joined_company_at.is_(None),
        ~exists().where(StaffMember.account_id == Account.id),
        ~exists().where(
            CompanyRequest.account_id == Account.id,
            CompanyRequest.status == "new",
        ),
    ]


class CompanylessService:
    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self.session_maker = session_maker

    async def run(self, now: datetime | None = None) -> CompanylessReport:
        now = now or datetime.now(UTC)
        reprieved = await self._reprieve()
        warned = deleted = 0
        async with self.session_maker() as session:
            candidates = list(
                (
                    await session.scalars(
                        select(Account.id).where(
                            Account.created_at
                            < now - (COMPANYLESS_TTL - WARNING_BEFORE),
                            *_without_company(),
                        )
                    )
                ).all()
            )
        for account_id in candidates:
            if await self._has_memberships(account_id):
                continue
            async with self.session_maker() as session:
                account = await session.get(Account, account_id)
                if account is None:
                    continue
                due = account.created_at + COMPANYLESS_TTL
                if account.email_verified_at is None:
                    if due <= now:
                        await erase_account(
                            session,
                            AuditRepository(session),
                            account,
                            reason="companyless",
                        )
                        deleted += 1
                    continue
                if account.deletion_warned_at is None:
                    self._warn(session, account, max(due, now + WARNING_BEFORE))
                    account.deletion_warned_at = now
                    await session.commit()
                    warned += 1
                elif due <= now and account.deletion_warned_at <= now - WARNING_BEFORE:
                    await erase_account(
                        session, AuditRepository(session), account, reason="companyless"
                    )
                    deleted += 1
        if warned or deleted or reprieved:
            logger.info(
                "companyless_accounts",
                warned=warned,
                deleted=deleted,
                reprieved=reprieved,
            )
        return CompanylessReport(warned=warned, deleted=deleted, reprieved=reprieved)

    async def _reprieve(self) -> int:
        """Снять отметку письма с тех, кто больше не подходит под удаление:
        иначе после отклонённой заявки учётку удалили бы без нового письма."""
        async with self.session_maker() as session:
            warned = list(
                (
                    await session.scalars(
                        select(Account.id).where(
                            Account.deletion_warned_at.is_not(None)
                        )
                    )
                ).all()
            )
            if not warned:
                return 0
            still = set(
                (
                    await session.scalars(
                        select(Account.id).where(
                            Account.id.in_(warned), *_without_company()
                        )
                    )
                ).all()
            )
        reprieve = [
            account_id
            for account_id in warned
            if account_id not in still or await self._has_memberships(account_id)
        ]
        if not reprieve:
            return 0
        async with self.session_maker() as session:
            for account_id in reprieve:
                account = await session.get(Account, account_id)
                if account is not None:
                    account.deletion_warned_at = None
            await session.commit()
        return len(reprieve)

    async def _has_memberships(self, account_id: UUID) -> bool:
        """Любая строка членства — в том числе ушёл, заблокирован, ждёт
        одобрения. Членства под RLS: читаются от имени самой учётки."""
        with account_scope(account_id):
            async with self.session_maker() as session:
                found = await session.scalar(
                    select(User.id)
                    .where(User.account_id == account_id)
                    .limit(1)
                    .execution_options(**{ACCOUNT_SCOPE_OPTION: True})
                )
        return found is not None

    @staticmethod
    def _warn(session: AsyncSession, account: Account, delete_on: datetime) -> None:
        mail = EmailService(session)
        mail.enqueue(
            account.email,
            email_templates.companyless_warning(
                name=account.first_name,
                delete_on=delete_on.date(),
                join_url=mail.url("/join"),
                request_url=mail.url("/settings/companies"),
            ),
        )
