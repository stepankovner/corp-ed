import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.cli import _parser
from corp_ed.core.exceptions import ConflictError
from corp_ed.core.security import verify_password
from corp_ed.core.tenant_context import current_tenant, tenant_scope
from corp_ed.domain.models import Tenant, User, UserRole
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.tenant_service import InvalidCompanyCodeError, TenantService


def _service(session: AsyncSession) -> TenantService:
    return TenantService(TenantRepository(session), UserRepository(session), session)


async def test_provision_creates_tenant_and_admin(session: AsyncSession) -> None:
    result = await _service(session).provision(
        company_code="  ACME-Co ",
        name="ACME",
        admin_email="Admin@Acme.ru",
        admin_full_name=None,
    )

    assert result.tenant.company_code == "acme-co"
    assert result.admin.role is UserRole.ADMIN
    assert result.admin.email == "admin@acme.ru"
    assert result.admin.must_change_password is True
    assert verify_password(result.temporary_password, result.admin.hashed_password)
    # Контекст тенанта не утёк из сервиса.
    assert current_tenant.get() is None

    with tenant_scope(result.tenant.id):
        users = (await session.execute(select(User))).scalars().all()
    assert [user.id for user in users] == [result.admin.id]


@pytest.mark.parametrize("code", ["a", "bad code", "код", "a" * 64, "-lead", "x;drop"])
async def test_provision_rejects_bad_company_code(
    session: AsyncSession, code: str
) -> None:
    with pytest.raises(InvalidCompanyCodeError):
        await _service(session).provision(
            company_code=code, name="X", admin_email="a@b.ru", admin_full_name=None
        )


async def test_provision_rejects_duplicate_code(session: AsyncSession) -> None:
    service = _service(session)
    await service.provision(
        company_code="acme", name="A", admin_email="a@b.ru", admin_full_name=None
    )
    with pytest.raises(ConflictError):
        await service.provision(
            company_code="ACME", name="B", admin_email="b@b.ru", admin_full_name=None
        )


async def test_suspend_and_resume(session: AsyncSession) -> None:
    service = _service(session)
    await service.provision(
        company_code="acme", name="A", admin_email="a@b.ru", admin_full_name=None
    )

    suspended = await service.set_active("acme", active=False)
    assert suspended.is_active is False

    resumed = await service.set_active("acme", active=True)
    assert resumed.is_active is True
    tenant = (await session.execute(select(Tenant))).scalar_one()
    assert tenant.is_active is True


def test_cli_requires_all_tenant_fields() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(["create-tenant", "--code", "acme"])
