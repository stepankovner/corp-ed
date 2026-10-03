from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.database import ACCOUNT_SCOPE_OPTION
from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.models import Account, MemberStatus, User, UserRole


class UserRepository:
    """Членства в компании (таблица users) — ТЗ §2.

    Все выборки идут через ORM и фильтруются хуком изоляции по тенанту
    из контекста — поэтому явного tenant_id в сигнатурах нет. Исключение
    — memberships_of_account: свои членства во всех компаниях, их
    ограничивает правило RLS own_membership.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: UUID) -> User | None:
        # Не session.get: если объект уже в identity map сессии, get
        # вернёт его без SQL — а значит, мимо фильтра по тенанту. Запрос
        # через select всегда идёт в базу и всегда получает фильтр.
        result = await self.session.scalars(select(User).where(User.id == user_id))
        return result.first()

    async def get_by_id_for_update(self, user_id: UUID) -> User | None:
        # of=User: учётка присоединена внешним соединением, а FOR UPDATE
        # к nullable-стороне внешнего соединения PostgreSQL не применяет.
        result = await self.session.scalars(
            select(User).where(User.id == user_id).with_for_update(of=User)
        )
        return result.first()

    async def get_by_account(self, account_id: UUID) -> User | None:
        """Членство учётки в компании из контекста."""
        result = await self.session.scalars(
            select(User).where(User.account_id == account_id)
        )
        return result.first()

    async def memberships_of_account(self, account_id: UUID) -> list[User]:
        """Членства учётки во всех компаниях (кроме ушедших)."""
        result = await self.session.scalars(
            select(User)
            .where(User.account_id == account_id, User.status != MemberStatus.LEFT)
            .order_by(User.created_at)
            .execution_options(**{ACCOUNT_SCOPE_OPTION: True})
        )
        return list(result)

    async def get_by_email(self, email: str) -> User | None:
        result = await self.session.scalars(
            select(User).join(Account).where(Account.email == email.strip().casefold())
        )
        return result.first()

    async def ids_by_emails(self, emails: Iterable[str]) -> dict[str, UUID]:
        """{почта: id членства} для сопоставления ACL источника с сотрудниками.

        Почты в базе хранятся в casefold; адаптер отдаёт как в источнике.
        Неизвестные почты просто не попадают в ответ: у людей без учётки
        у нас документ и не должен быть виден. Ушедшие из компании — тоже.
        """
        wanted = {email.strip().casefold() for email in emails if email}
        if not wanted:
            return {}
        result = await self.session.execute(
            select(Account.email, User.id)
            .join(Account, User.account_id == Account.id)
            .where(
                User.tenant_id == require_tenant(),
                Account.email.in_(wanted),
                User.status != MemberStatus.LEFT,
            )
        )
        return {row.email: row.id for row in result}

    async def list_members(self) -> list[User]:
        """Люди компании: работающие, заблокированные и ждущие одобрения."""
        result = await self.session.scalars(
            select(User)
            .where(User.status != MemberStatus.LEFT)
            .order_by(User.created_at)
        )
        return list(result)

    async def count_active_admins(self) -> int:
        # count() — колоночный select: хук изоляции его тоже фильтрует,
        # потому что в запросе участвует тенант-модель User (all_mappers).
        result = await self.session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.role == UserRole.ADMIN, User.status == MemberStatus.ACTIVE)
        )
        return int(result or 0)

    async def create(self, user: User) -> User:
        self.session.add(user)
        await self.session.flush()
        await self.session.refresh(user)
        return user
