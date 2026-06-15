from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import User


class UserRepository:
    """Доступ к данным пользователей в БД."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: int) -> User | None:
        result = await self.session.scalars(select(User).where(User.id == user_id))
        return result.first()

    async def get_by_email(self, email: str) -> User | None:
        result = await self.session.scalars(select(User).where(User.email == email))
        return result.first()

    async def create(self, user: User) -> User:
        self.session.add(user)
        await self.session.commit()
        await self.session.refresh(user)
        return user
