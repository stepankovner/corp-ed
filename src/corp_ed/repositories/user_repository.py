from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import User, UserRole


class UserRepository:
    """Доступ к данным пользователей в БД."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        result = await self.session.scalars(select(User).where(User.email == email))
        return result.first()

    async def create(self, user: User) -> User:
        self.session.add(user)
        await self.session.flush()
        await self.session.refresh(user)
        return user

    async def list_by_role(self, role: UserRole) -> list[User]:
        """Пользователи тенанта с указанной ролью. Фильтр по тенанту — хук."""
        result = await self.session.scalars(
            select(User).where(User.role == role).order_by(User.created_at)
        )
        return list(result)
