from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Tenant


class TenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_company_code(self, company_code: str) -> Tenant | None:
        result = await self.session.execute(
            select(Tenant).where(Tenant.company_code == company_code)
        )
        return result.scalar_one_or_none()
