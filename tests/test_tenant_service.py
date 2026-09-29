import getpass
import inspect
import io
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed import cli
from corp_ed.cli import _parser, _read_admin_password
from corp_ed.core.exceptions import ConflictError, DomainError, WeakPasswordError
from corp_ed.core.security import verify_password
from corp_ed.core.tenant_context import current_tenant, tenant_scope
from corp_ed.domain.models import AuditEvent, Tenant, User, UserRole
from corp_ed.domain.types import DEFAULT_NOT_FOUND_MODE, NotFoundMode
from corp_ed.repositories.audit_repository import AuditRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.tenant_service import (
    MAX_SEATS,
    InvalidAdminEmailError,
    InvalidCompanyCodeError,
    InvalidSeatsError,
    TenantService,
)

ADMIN_PASSWORD = "Temp-first-login-2026"
"""Временный пароль администратора от оператора (RISKS №42)."""


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
        admin_password=ADMIN_PASSWORD,
        seats=30,
    )

    assert result.tenant.company_code == "acme-co"
    assert result.admin.role is UserRole.ADMIN
    assert result.admin.email == "admin@acme.ru"
    assert result.admin.must_change_password is True
    assert verify_password(ADMIN_PASSWORD, result.admin.hashed_password)
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
            admin_password=ADMIN_PASSWORD,
            seats=30,
        )


@pytest.mark.parametrize(
    "email", ["admin@meridian.test", "admin@localhost", "not-an-email", ""]
)
async def test_provision_rejects_email_login_would_reject(
    session: AsyncSession, email: str
) -> None:
    """Вход проверяет почту через EmailStr; заведённый с такой почтой
    администратор не смог бы войти (найдено при проверке фронта)."""
    with pytest.raises(InvalidAdminEmailError):
        await _service(session).provision(
            company_code="acme",
            name="X",
            admin_email=email,
            admin_full_name=None,
            admin_password=ADMIN_PASSWORD,
            seats=30,
        )


async def test_provision_rejects_duplicate_code(session: AsyncSession) -> None:
    service = _service(session)
    await service.provision(
        company_code="acme",
        name="A",
        admin_email="a@b.ru",
        admin_full_name=None,
        admin_password=ADMIN_PASSWORD,
        seats=30,
    )
    with pytest.raises(ConflictError):
        await service.provision(
            company_code="ACME",
            name="B",
            admin_email="b@b.ru",
            admin_full_name=None,
            admin_password=ADMIN_PASSWORD,
            seats=30,
        )


async def test_suspend_and_resume(session: AsyncSession) -> None:
    service = _service(session)
    await service.provision(
        company_code="acme",
        name="A",
        admin_email="a@b.ru",
        admin_full_name=None,
        admin_password=ADMIN_PASSWORD,
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
            admin_password=ADMIN_PASSWORD,
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
        admin_password=ADMIN_PASSWORD,
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


async def test_new_tenant_refuses_by_default(session: AsyncSession) -> None:
    """Решение 28.09 (Q1): новая компания — честный отказ, пока продукт
    не решил иначе; общий ответ включает команда."""
    result = await _service(session).provision(
        company_code="acme",
        name="A",
        admin_email="a@b.ru",
        admin_full_name=None,
        admin_password=ADMIN_PASSWORD,
        seats=30,
    )
    assert result.tenant.not_found_mode == "strict"


async def test_tenant_row_without_mode_refuses(session: AsyncSession) -> None:
    """И вставка мимо сервиса: значение по умолчанию в ORM и в базе."""
    code = f"raw{uuid4().hex[:8]}"
    await session.execute(
        text("INSERT INTO tenants (id, company_code, name) VALUES (:id, :code, 'R')"),
        {"id": uuid4(), "code": code},
    )
    mode = (
        await session.execute(
            text("SELECT not_found_mode FROM tenants WHERE company_code = :code"),
            {"code": code},
        )
    ).scalar_one()
    assert mode == DEFAULT_NOT_FOUND_MODE.value == "strict"
    assert Tenant.__table__.c.not_found_mode.default.arg == "strict"
    await session.rollback()


async def test_set_not_found_mode_is_audited(session: AsyncSession) -> None:
    service = _service(session)
    await service.provision(
        company_code="acme",
        name="A",
        admin_email="a@b.ru",
        admin_full_name=None,
        admin_password=ADMIN_PASSWORD,
        seats=30,
    )

    tenant = await service.set_not_found_mode("acme", NotFoundMode.GENERAL)

    assert tenant.not_found_mode == "general"
    event = (
        await session.execute(
            select(AuditEvent).where(
                AuditEvent.action == "tenant.not_found_mode_changed"
            )
        )
    ).scalar_one()
    assert event.details == {"from": "strict", "to": "general"}


async def test_database_rejects_unknown_not_found_mode(
    session: AsyncSession, tenant_ctx: Tenant
) -> None:
    """Опечатка в режиме мимо CLI (ручной SQL) не должна молча включить
    неизвестное поведение: CHECK в базе."""
    with pytest.raises(IntegrityError):
        await session.execute(
            update(Tenant)
            .where(Tenant.id == tenant_ctx.id)
            .values(not_found_mode="lenient")
        )
    await session.rollback()


async def test_database_rejects_non_positive_seats(
    session: AsyncSession, tenant_ctx: Tenant
) -> None:
    with pytest.raises(IntegrityError):
        await session.execute(
            update(Tenant).where(Tenant.id == tenant_ctx.id).values(seats=0)
        )
    await session.rollback()


def test_cli_not_found_mode() -> None:
    args = _parser().parse_args(
        ["set-not-found-mode", "--code", "acme", "--mode", "general"]
    )
    assert args.mode == "general"
    create = _parser().parse_args(
        ["create-tenant", "--code", "acme", "--name", "A", "--seats", "5"]
        + ["--admin-email", "a@b.ru"]
    )
    assert create.not_found_mode == "strict"
    with pytest.raises(SystemExit):
        _parser().parse_args(["set-not-found-mode", "--code", "acme", "--mode", "x"])


def test_cli_help_needs_no_settings() -> None:
    """`--help` в контейнере без .env (проверка образа в CI) не должен
    падать на чтении SECRET_KEY и DATABASE_URL."""
    result = subprocess.run(
        [sys.executable, "-m", "corp_ed.cli", "--help"],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "create-tenant" in result.stdout


# --- временный пароль администратора от оператора (RISKS №42) ------------------


@pytest.mark.parametrize(
    "password", ["short-pass", "password1234", "admin@acme.ru-2026", "a" * 129]
)
async def test_provision_checks_admin_password_policy(
    session: AsyncSession, password: str
) -> None:
    """Та же политика, что у пароля пользователя: короткий, словарный,
    с почтой внутри, слишком длинный — отказ до создания компании."""
    with pytest.raises(WeakPasswordError):
        await _service(session).provision(
            company_code="acme",
            name="A",
            admin_email="admin@acme.ru",
            admin_full_name=None,
            admin_password=password,
            seats=30,
        )
    assert (await session.execute(select(Tenant))).scalars().all() == []


def test_cli_reads_admin_password_from_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{ADMIN_PASSWORD}\r\nlater\n"))
    assert _read_admin_password(from_stdin=True) == ADMIN_PASSWORD


def test_cli_asks_admin_password_twice_in_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter([ADMIN_PASSWORD, ADMIN_PASSWORD])
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr(getpass, "getpass", lambda prompt: next(answers))
    assert _read_admin_password(from_stdin=False) == ADMIN_PASSWORD


def test_cli_rejects_mismatched_admin_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter([ADMIN_PASSWORD, "Something-else-2026"])
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr(getpass, "getpass", lambda prompt: next(answers))
    with pytest.raises(DomainError, match="не совпадают"):
        _read_admin_password(from_stdin=False)


def test_cli_without_terminal_needs_stdin_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Скрипт без терминала не зависает на вводе, а просит флаг."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(DomainError, match="--admin-password-stdin"):
        _read_admin_password(from_stdin=False)


def test_cli_never_prints_the_admin_password() -> None:
    """Вывод create-tenant собран из полей результата: пароля среди них нет."""
    source = inspect.getsource(cli)
    assert "temporary_password" not in source
    assert "admin_password=" in source


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True
