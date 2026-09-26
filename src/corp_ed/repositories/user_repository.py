from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.models import User, UserRole


class UserRepository:
    """Доступ к данным пользователей в БД.

    Все выборки идут через ORM и фильтруются хуком изоляции по тенанту
    из контекста — поэтому явного tenant_id в сигнатурах нет.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: UUID) -> User | None:
        # Не session.get: если объект уже в identity map сессии, get
        # вернёт его без SQL — а значит, мимо фильтра по тенанту. Запрос
        # через select всегда идёт в базу и всегда получает фильтр.
        result = await self.session.scalars(select(User).where(User.id == user_id))
        return result.first()

    async def get_by_email(self, email: str) -> User | None:
        result = await self.session.scalars(
            select(User).where(User.email == email.casefold())
        )
        return result.first()

    async def ids_by_emails(self, emails: Iterable[str]) -> dict[str, UUID]:
        """{почта: id} для сопоставления ACL источника с сотрудниками.

        Почты в базе хранятся в casefold; адаптер отдаёт как в источнике.
        Неизвестные почты просто не попадают в ответ: у людей без учётки
        у нас документ и не должен быть виден.
        """
        wanted = {email.strip().casefold() for email in emails if email}
        if not wanted:
            return {}
        result = await self.session.execute(
            select(User.email, User.id).where(
                User.tenant_id == require_tenant(), User.email.in_(wanted)
            )
        )
        return {row.email: row.id for row in result}

    async def list_all(self) -> list[User]:
        result = await self.session.scalars(select(User).order_by(User.created_at))
        return list(result)

    async def count_active_admins(self) -> int:
        # count() — колоночный select: хук изоляции его тоже фильтрует,
        # потому что в запросе участвует тенант-модель User (all_mappers).
        result = await self.session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.role == UserRole.ADMIN, User.is_active.is_(True))
        )
        return int(result or 0)

    async def create(self, user: User) -> User:
        self.session.add(user)
        await self.session.flush()
        await self.session.refresh(user)
        return user
