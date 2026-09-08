from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import TenantContextMissingError
from corp_ed.domain.models import Tenant, User, UserRole


async def test_tenant_context_is_required(session: AsyncSession) -> None:
    with pytest.raises(TenantContextMissingError):
        await session.execute(select(User))


async def test_user_is_visible_within_tenant(
    tenant_ctx: Tenant, session: AsyncSession
) -> None:
    user = User(
        id=uuid4(),
        tenant_id=tenant_ctx.id,
        email="a@b.c",
        hashed_password="x",
        full_name=None,
        role=UserRole.MANAGER,
    )
    session.add(user)
    await session.commit()

    result = await session.execute(select(User))
    assert len(result.scalars().all()) == 1
