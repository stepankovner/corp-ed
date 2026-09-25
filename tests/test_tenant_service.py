import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.cli import _parser
from corp_ed.core.exceptions import ConflictError
from corp_ed.core.security import verify_password
from corp_ed.core.tenant_context import current_tenant, tenant_scope
from corp_ed.domain.models import AuditEvent, Tenant, User, UserRole
from corp_ed.repositories.audit_repository import AuditRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.tenant_service import (
    MAX_SEATS,
    InvalidCompanyCodeError,
    InvalidSeatsError,
    TenantService,
)


def _service(session: AsyncSession) -> TenantService:
    return TenantService(
        TenantRepository(session),
        UserRepository(session),
        AuditRepository(session),
        session,
    )


async def test_provision_creates_tenant_and_admin(session: AsyncSession) -> None:
    result = await _service(session).provision(
        company_code="  ACME-Co ",
        name="ACME",
        admin_email="Admin@Acme.ru",
        admin_full_name=None,
        seats=30,
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
            company_code=code,
            name="X",
            admin_email="a@b.ru",
            admin_full_name=None,
            seats=30,
        )


async def test_provision_rejects_duplicate_code(session: AsyncSession) -> None:
    service = _service(session)
    await service.provision(
        company_code="acme",
        name="A",
        admin_email="a@b.ru",
        admin_full_name=None,
        seats=30,
    )
    with pytest.raises(ConflictError):
        await service.provision(
            company_code="ACME",
            name="B",
            admin_email="b@b.ru",
            admin_full_name=None,
            seats=30,
        )


async def test_suspend_and_resume(session: AsyncSession) -> None:
    service = _service(session)
    await service.provision(
        company_code="acme",
        name="A",
        admin_email="a@b.ru",
        admin_full_name=None,
        seats=30,
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


@pytest.mark.parametrize("seats", [0, -5, MAX_SEATS + 1])
async def test_provision_rejects_bad_seats(session: AsyncSession, seats: int) -> None:
    with pytest.raises(InvalidSeatsError):
        await _service(session).provision(
            company_code="acme",
            name="A",
            admin_email="a@b.ru",
            admin_full_name=None,
            seats=seats,
        )
    assert (await session.execute(select(Tenant))).scalars().all() == []


async def test_set_seats_is_audited(session: AsyncSession) -> None:
    service = _service(session)
    await service.provision(
        company_code="acme",
        name="A",
        admin_email="a@b.ru",
        admin_full_name=None,
        seats=30,
    )

    tenant = await service.set_seats("ACME", 80)

    assert tenant.seats == 80
    event = (
        await session.execute(
            select(AuditEvent).where(AuditEvent.action == "tenant.seats_changed")
        )
    ).scalar_one()
    assert event.details == {"from": 30, "to": 80}
    assert event.actor_user_id is None


async def test_set_seats_validates_and_needs_existing_tenant(
    session: AsyncSession,
) -> None:
    service = _service(session)
    with pytest.raises(ConflictError):
        await service.set_seats("ghost", 10)
    with pytest.raises(InvalidSeatsError):
        await service.set_seats("ghost", 0)


def test_cli_create_tenant_requires_seats() -> None:
    base = ["create-tenant", "--code", "acme", "--name", "A", "--admin-email", "a@b.ru"]
    with pytest.raises(SystemExit):
        _parser().parse_args(base)
    with pytest.raises(SystemExit):
        _parser().parse_args([*base, "--seats", "many"])
    assert _parser().parse_args([*base, "--seats", "50"]).seats == 50


def test_cli_set_seats() -> None:
    args = _parser().parse_args(["set-seats", "--code", "acme", "--seats", "80"])
    assert (args.command, args.code, args.seats) == ("set-seats", "acme", 80)
