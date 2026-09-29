from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import TenantContextMissingError, TenantMismatchError
from corp_ed.core.tenant_context import current_tenant, tenant_scope
from corp_ed.domain.models import Tenant, User, UserRole
from corp_ed.repositories.user_repository import UserRepository


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
        role=UserRole.ADMIN,
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
        role=UserRole.ADMIN,
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
        role=UserRole.ADMIN,
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
        role=UserRole.ADMIN,
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
        role=UserRole.ADMIN,
    )
    session.add(user1)
    await session.commit()

    tenant1 = Tenant(id=uuid4(), company_code="other", name="Other Co")
    session.add(tenant1)
    await session.commit()

    user1.tenant_id = tenant1.id

    with pytest.raises(TenantMismatchError):
        await session.commit()


async def test_get_by_id_does_not_bypass_filter_via_identity_map(
    tenant_ctx: Tenant, session: AsyncSession
) -> None:
    """session.get отдаёт объект из identity map без SQL — мимо фильтра.

    Найдено тестом токена с чужим tenant_id: пользователь уже был
    загружен в сессию, и get вернул его для другого тенанта.
    Репозитории ищут по id через select, и фильтр применяется всегда.
    """
    user = User(
        id=uuid4(),
        tenant_id=tenant_ctx.id,
        email="a@b.c",
        hashed_password="x",
        role=UserRole.ADMIN,
    )
    session.add(user)
    await session.commit()
    assert user in session  # объект в identity map

    other = Tenant(id=uuid4(), company_code="other", name="Other Co")
    session.add(other)
    await session.commit()

    with tenant_scope(other.id):
        assert await UserRepository(session).get_by_id(user.id) is None


async def test_tenant_scope_restores_previous_value(tenant_ctx: Tenant) -> None:
    other = uuid4()
    with tenant_scope(other):
        assert current_tenant.get() == other
    assert current_tenant.get() == tenant_ctx.id


async def test_tenant_scope_restores_on_exception(tenant_ctx: Tenant) -> None:
    with pytest.raises(RuntimeError), tenant_scope(uuid4()):
        raise RuntimeError
    assert current_tenant.get() == tenant_ctx.id
