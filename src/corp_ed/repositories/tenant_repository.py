from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Tenant


class TenantRepository:
    """Доступ к компаниям.

    Tenant — корень изоляции и сам тенант-моделью не является: его
    ищут до того, как тенант известен (логин по company_code).
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_company_code(self, company_code: str) -> Tenant | None:
        result = await self.session.execute(
            select(Tenant).where(Tenant.company_code == company_code.casefold())
        )
        return result.scalar_one_or_none()

    async def list_all(self) -> list[Tenant]:
        result = await self.session.scalars(
            select(Tenant).order_by(Tenant.company_code)
        )
        return list(result)

    async def get_by_id(self, tenant_id: UUID) -> Tenant | None:
        return await self.session.get(Tenant, tenant_id)

    async def create(self, tenant: Tenant) -> Tenant:
        self.session.add(tenant)
        await self.session.flush()
        return tenant
