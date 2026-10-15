"""База для сценария: тот же подход, что у `tests/conftest.py`, но без pytest.

Схема строится по моделям (`create_all`) плюс триггеры и политики RLS
(`db_policies.apply_all`), а соединения работают под ролью без SUPERUSER и
BYPASSRLS — как приложение в боевой среде: под суперпользователем RLS не
действует, и проверка изоляции компаний ничего бы не доказывала.

Адрес берётся из ACCEPTANCE_DATABASE_URL, иначе из TEST_DATABASE_URL.
Без `reset` существующая схема не трогается (создаётся только если её нет);
`reset` пересоздаёт её целиком — только для тестовой базы.
"""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from corp_ed.core.database import Base
from corp_ed.core.db_policies import apply_all
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Tenant, User, UserRole
from corp_ed.domain.types import NotFoundMode
from tests.factories import make_user

APP_ROLE = "corp_ed_app_test"
ENV_URL = ("ACCEPTANCE_DATABASE_URL", "TEST_DATABASE_URL")


class DatabaseNotConfiguredError(RuntimeError):
    pass


def database_url() -> str:
    for name in ENV_URL:
        value = os.environ.get(name)
        if value:
            return value
    raise DatabaseNotConfiguredError(
        "нужна переменная ACCEPTANCE_DATABASE_URL (или TEST_DATABASE_URL): "
        "postgresql+asyncpg://… — отдельная тестовая база"
    )


async def _build_schema(url: str, *, reset: bool) -> None:
    admin = create_async_engine(url)
    try:
        async with admin.begin() as conn:
            exists = (
                await conn.execute(text("SELECT to_regclass('public.tenants')"))
            ).scalar()
            if exists is not None and not reset:
                await _grant(conn)
                return
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(apply_all)
            await _grant(conn)
    finally:
        await admin.dispose()


async def _grant(conn: Any) -> None:
    await conn.execute(
        text(
            f"""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}')
                THEN CREATE ROLE {APP_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS;
                END IF;
            END $$
            """
        )
    )
    await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
    await conn.execute(
        text(
            "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE "
            f"ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
        )
    )


@asynccontextmanager
async def open_database(
    url: str | None = None, *, reset: bool = False
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    """Фабрика сессий под ролью приложения; схема готова."""
    url = url or database_url()
    await _build_schema(url, reset=reset)
    engine: AsyncEngine = create_async_engine(url)

    @event.listens_for(engine.sync_engine, "connect")
    def _drop_privileges(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET ROLE {APP_ROLE}")
        cursor.close()
        # SET ROLE транзакционный, а драйвер открывает транзакцию сам: без
        # commit откат на новом соединении вернул бы суперпользователя.
        dbapi_connection.commit()

    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


async def create_tenant(session: AsyncSession, label: str) -> Tenant:
    """Новая компания проверки. Код уникален на запуск: старые запуски
    остаются в базе и друг другу не мешают (изоляция по компании)."""
    tenant = Tenant(
        id=uuid4(),
        company_code=f"acc-{label}-{uuid4().hex[:8]}"[:40],
        name=f"Приёмка {label}",
        not_found_mode=NotFoundMode.GENERAL.value,
    )
    session.add(tenant)
    await session.commit()
    return tenant


async def create_employees(
    session: AsyncSession,
    tenant: Tenant,
    people: dict[str, tuple[str, str]],
) -> dict[str, User]:
    """Сотрудники kronto: логин → (почта, ФИО). Роль — сотрудник: у
    администратора kronto свои права (закрытые папки), они проверку
    испортили бы."""
    users: dict[str, User] = {}
    with tenant_scope(tenant.id):
        for login, (email, name) in people.items():
            user = make_user(
                id=uuid4(),
                tenant_id=tenant.id,
                email=email,
                full_name=name,
                role=UserRole.EMPLOYEE,
                hashed_password="not-a-real-hash",
            )
            session.add(user)
            users[login] = user
        await session.commit()
    return users


async def purge_previous_runs(url: str | None = None) -> int:
    """Убрать компании прошлых запусков приёмки (код `acc-…`) вместе с их
    сотрудниками и учётками.

    Учётка — запись всей системы с уникальной почтой, и почты сотрудников
    приёмки одни и те же в каждом запуске; без уборки второй запуск
    упёрся бы в «почта уже есть». Работает под владельцем базы и временно
    отключает проверку внешних ключей (session_replication_role), поэтому
    — только для тестовой базы, адрес которой вы сами указали.
    """
    engine = create_async_engine(url or database_url())
    removed = 0
    try:
        async with engine.begin() as conn:
            ids = [
                row[0]
                for row in await conn.execute(
                    text("SELECT id FROM tenants WHERE company_code LIKE 'acc-%'")
                )
            ]
            if not ids:
                return 0
            await conn.execute(text("SET LOCAL session_replication_role = replica"))
            accounts = [
                row[0]
                for row in await conn.execute(
                    text("SELECT account_id FROM users WHERE tenant_id = ANY(:ids)"),
                    {"ids": ids},
                )
            ]
            tables = [
                row[0]
                for row in await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND column_name = 'tenant_id' "
                        "AND table_name <> 'audit_events'"
                    )
                )
            ]
            for table in tables:
                await conn.execute(
                    text(f'DELETE FROM "{table}" WHERE tenant_id = ANY(:ids)'),
                    {"ids": ids},
                )
            await conn.execute(
                text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": ids}
            )
            if accounts:
                await conn.execute(
                    text(
                        "DELETE FROM accounts WHERE id = ANY(:accounts) AND NOT EXISTS "
                        "(SELECT 1 FROM users WHERE users.account_id = accounts.id)"
                    ),
                    {"accounts": accounts},
                )
            removed = len(ids)
    finally:
        await engine.dispose()
    return removed
