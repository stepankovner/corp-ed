from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import CompanyRequest


class CompanyRequestRepository:
    """Заявки «Подключить компанию». Не тенантские: компании ещё нет."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, request: CompanyRequest) -> CompanyRequest:
        self.session.add(request)
        await self.session.flush()
        return request

    async def get_for_update(self, request_id: UUID) -> CompanyRequest | None:
        result = await self.session.scalars(
            select(CompanyRequest)
            .where(CompanyRequest.id == request_id)
            .with_for_update()
        )
        return result.first()

    async def list_by_account(self, account_id: UUID) -> list[CompanyRequest]:
        result = await self.session.scalars(
            select(CompanyRequest)
            .where(CompanyRequest.account_id == account_id)
            .order_by(CompanyRequest.created_at.desc())
        )
        return list(result)

    async def open_for_account(self, account_id: UUID) -> CompanyRequest | None:
        result = await self.session.scalars(
            select(CompanyRequest).where(
                CompanyRequest.account_id == account_id,
                CompanyRequest.status == "new",
            )
        )
        return result.first()

    async def list_by_status(self, status: str | None) -> list[CompanyRequest]:
        query = select(CompanyRequest).order_by(CompanyRequest.created_at)
        if status is not None:
            query = query.where(CompanyRequest.status == status)
        return list(await self.session.scalars(query))
