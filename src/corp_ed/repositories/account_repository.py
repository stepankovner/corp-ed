from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Account


class AccountRepository:
    """Учётные записи (ТЗ §2). Таблица не тенантская: вход ищет учётку по
    почте до того, как известна компания."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, account_id: UUID) -> Account | None:
        result = await self.session.scalars(
            select(Account).where(Account.id == account_id)
        )
        return result.first()

    async def get_for_update(self, account_id: UUID) -> Account | None:
        result = await self.session.scalars(
            select(Account).where(Account.id == account_id).with_for_update()
        )
        return result.first()

    async def get_by_email(self, email: str) -> Account | None:
        result = await self.session.scalars(
            select(Account).where(Account.email == normalize_email(email))
        )
        return result.first()

    async def add(self, account: Account) -> Account:
        self.session.add(account)
        await self.session.flush()
        return account

    async def delete(self, account: Account) -> None:
        await self.session.delete(account)
        await self.session.flush()


def normalize_email(email: str) -> str:
    """Почта для хранения и поиска: без пробелов по краям, casefold."""
    return email.strip().casefold()
