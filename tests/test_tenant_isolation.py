from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import TenantContextMissingError, TenantMismatchError
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


async def test_write_with_foreign_tenant_id_is_rejected(
    tenant_ctx: Tenant, session: AsyncSession
) -> None:
    foreign_tenant = Tenant(id=uuid4(), company_code="other", name="Other Co")
    session.add(foreign_tenant)
    await session.commit()

    user = User(
        id=uuid4(),
        tenant_id=foreign_tenant.id,  # ← чужой, контекст указывает на другого
        email="a@b.c",
        hashed_password="x",
        full_name=None,
        role=UserRole.MANAGER,
    )
    session.add(user)

    with pytest.raises(TenantMismatchError):
        await session.commit()


async def test_tenant_id_inferred_from_context(
    tenant_ctx: Tenant, session: AsyncSession
) -> None:
    user = User(
        id=uuid4(),
        email="a@b.c",
        hashed_password="x",
        full_name=None,
        role=UserRole.MANAGER,
    )
    session.add(user)
    await session.commit()

    result = await session.execute(select(User))
    saved_user = result.scalars().first()
    assert saved_user is not None
    assert saved_user.tenant_id == tenant_ctx.id


async def test_write_without_tenant_context_is_rejected(session: AsyncSession) -> None:
    user = User(
        id=uuid4(),
        email="a@b.c",
        hashed_password="x",
        full_name=None,
        role=UserRole.MANAGER,
    )
    session.add(user)
    with pytest.raises(TenantContextMissingError):
        await session.commit()


async def test_tenant_id_cannot_be_changed(
    tenant_ctx: Tenant, session: AsyncSession
) -> None:
    user1 = User(
        id=uuid4(),
        tenant_id=tenant_ctx.id,
        email="a@b.c",
        hashed_password="x",
        full_name=None,
        role=UserRole.MANAGER,
    )
    session.add(user1)
    await session.commit()

    tenant1 = Tenant(id=uuid4(), company_code="other", name="Other Co")
    session.add(tenant1)
    await session.commit()

    user1.tenant_id = tenant1.id

    with pytest.raises(TenantMismatchError):
        await session.commit()
