"""Миграция срока общих ссылок: прежние ссылки живут 30 дней от миграции.

Тестовая база собирается по моделям (create_all), поэтому миграцию
проверяем на ней: убираем колонку и прогоняем upgrade в транзакции,
которая потом откатывается, — схема остаётся как была.
"""

import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from corp_ed.domain.models import Conversation, Tenant
from tests.conftest import TEST_DATABASE_URL
from tests.factories import make_user

MIGRATION = (
    Path(__file__).parent.parent
    / "migrations"
    / "versions"
    / "a9c4e2f81b37_share_link_expiry.py"
)


FORCE = text("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'conversations'")


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("share_link_expiry", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(connection: Connection, step: str) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_migration(), step)()


async def test_existing_links_get_thirty_days_from_migration(
    session: AsyncSession, tenant_ctx: Tenant
) -> None:
    owner = make_user(email="owner@test.com", tenant_id=tenant_ctx.id)
    session.add(owner)
    await session.flush()
    shared = Conversation(
        id=uuid4(), user_id=owner.id, title="Общий", share_token="t" * 32
    )
    private = Conversation(id=uuid4(), user_id=owner.id, title="Свой")
    session.add_all([shared, private])
    await session.commit()

    # Владелец схемы, как alembic в бою (роль тестов — без DDL).
    admin = create_async_engine(TEST_DATABASE_URL)
    try:
        async with admin.connect() as conn:
            transaction = await conn.begin()
            try:
                force_before = await conn.scalar(FORCE)
                await conn.execute(
                    text("ALTER TABLE conversations DROP COLUMN share_expires_at")
                )
                await conn.run_sync(_run, "upgrade")
                rows = dict(
                    (
                        await conn.execute(
                            text(
                                "SELECT id, share_expires_at = now() + interval "
                                "'30 days' FROM conversations"
                            )
                        )
                    ).all()
                )
                force_after = await conn.scalar(FORCE)
                await conn.run_sync(_run, "downgrade")
            finally:
                await transaction.rollback()
    finally:
        await admin.dispose()

    # now() — время начала транзакции миграции: «момент миграции».
    assert rows == {shared.id: True, private.id: None}
    # FORCE RLS после миграции — как до неё (включён для всех тенантных таблиц).
    assert force_before is True
    assert force_after is True
