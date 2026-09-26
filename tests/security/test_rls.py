"""Row-Level Security в PostgreSQL — второй рубеж изоляции компаний.

Тесты намеренно идут сырым SQL (text()), мимо ORM-хуков: цель — доказать,
что даже код, который забыл про tenant_id, чужих данных не увидит и не
запишет. Сессия работает под ролью без SUPERUSER и BYPASSRLS (см.
conftest), как приложение в production.
"""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from corp_ed.core.database import Base
from corp_ed.core.db_policies import TENANT_TABLES
from corp_ed.core.tenant_context import current_tenant, tenant_scope
from corp_ed.domain.mixins import TenantMixin
from corp_ed.domain.models import Material, Tenant


async def _two_tenants_with_material(
    session: AsyncSession,
) -> tuple[Tenant, Tenant]:
    first = Tenant(id=uuid4(), company_code="first", name="First")
    second = Tenant(id=uuid4(), company_code="second", name="Second")
    session.add_all([first, second])
    await session.commit()
    for tenant in (first, second):
        with tenant_scope(tenant.id):
            session.add(Material(tenant_id=tenant.id, title="Док", content="x"))
            await session.commit()
    return first, second


async def _count_materials(session: AsyncSession) -> int:
    result = await session.execute(text("SELECT count(*) FROM materials"))
    return int(result.scalar_one())


def test_every_tenant_model_is_under_rls() -> None:
    """Новая тенант-модель без политики RLS — провал теста, а не утечка."""
    tenant_tables = {
        mapper.local_table.name
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, TenantMixin)
    }
    assert tenant_tables <= set(TENANT_TABLES)


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_policy_is_enabled_and_forced(session: AsyncSession, table: str) -> None:
    """Таблица из списка действительно под политикой — и для владельца
    тоже (FORCE). Список без политики в базе ничего бы не защищал."""
    row = (
        await session.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = :table AND relkind = 'r'"
            ),
            {"table": table},
        )
    ).one()
    assert tuple(row) == (True, True)
    policies = await session.scalar(
        text("SELECT count(*) FROM pg_policies WHERE tablename = :table"),
        {"table": table},
    )
    assert policies and policies >= 1


async def test_session_runs_without_bypass_privileges(session: AsyncSession) -> None:
    """Иначе все тесты ниже прошли бы впустую."""
    row = (
        await session.execute(
            text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"
            )
        )
    ).one()
    assert tuple(row) == (False, False)


async def test_role_survives_rollback_on_fresh_connections(engine: AsyncEngine) -> None:
    """Регрессия: SET ROLE в первой транзакции соединения откатывался
    вместе с ней, и соединение возвращалось в пул суперпользователем.
    Несколько соединений сразу — чтобы пулу пришлось открыть новые."""
    connections = [await engine.connect() for _ in range(4)]
    for connection in connections:
        await connection.rollback()
    for connection in connections:
        await connection.close()

    connections = [await engine.connect() for _ in range(4)]
    try:
        for connection in connections:
            row = (
                await connection.execute(
                    text(
                        "SELECT rolsuper, rolbypassrls FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
            ).one()
            assert tuple(row) == (False, False)
    finally:
        for connection in connections:
            await connection.close()


async def test_raw_sql_sees_only_current_tenant(session: AsyncSession) -> None:
    first, second = await _two_tenants_with_material(session)

    with tenant_scope(first.id):
        rows = await session.execute(text("SELECT tenant_id FROM materials"))
        assert {row.tenant_id for row in rows} == {first.id}
        await session.commit()

    with tenant_scope(second.id):
        assert await _count_materials(session) == 1
        await session.commit()


async def test_raw_sql_without_tenant_sees_nothing(session: AsyncSession) -> None:
    """Default deny: забыли выставить тенанта — пусто, а не всё."""
    await _two_tenants_with_material(session)

    token = current_tenant.set(None)
    try:
        assert await _count_materials(session) == 0
    finally:
        current_tenant.reset(token)


async def test_raw_insert_into_foreign_tenant_is_rejected(
    session: AsyncSession,
) -> None:
    first, second = await _two_tenants_with_material(session)

    with tenant_scope(first.id), pytest.raises(DBAPIError, match="row-level security"):
        await session.execute(
            text(
                "INSERT INTO materials (id, tenant_id, title, content, created_at) "
                "VALUES (:id, :tenant, 'Чужой', 'x', now())"
            ),
            {"id": uuid4(), "tenant": second.id},
        )
    await session.rollback()


async def test_raw_update_cannot_move_row_to_other_tenant(
    session: AsyncSession,
) -> None:
    first, second = await _two_tenants_with_material(session)

    with tenant_scope(first.id), pytest.raises(DBAPIError, match="row-level security"):
        await session.execute(
            text("UPDATE materials SET tenant_id = :other"), {"other": second.id}
        )
    await session.rollback()


async def test_raw_delete_cannot_touch_foreign_rows(session: AsyncSession) -> None:
    first, second = await _two_tenants_with_material(session)

    with tenant_scope(first.id):
        await session.execute(text("DELETE FROM materials"))
        await session.commit()

    with tenant_scope(second.id):
        assert await _count_materials(session) == 1
        await session.commit()


async def test_tenant_switch_inside_transaction_is_followed(
    session: AsyncSession,
) -> None:
    """Контекст сменился посреди транзакции — база узнаёт об этом сразу."""
    first, second = await _two_tenants_with_material(session)

    with tenant_scope(first.id):
        first_rows = await session.execute(text("SELECT tenant_id FROM materials"))
        assert {row.tenant_id for row in first_rows} == {first.id}
        with tenant_scope(second.id):
            second_rows = await session.execute(text("SELECT tenant_id FROM materials"))
            assert {row.tenant_id for row in second_rows} == {second.id}
    await session.rollback()


async def test_tenant_setting_does_not_outlive_transaction(
    session: AsyncSession,
) -> None:
    """set_config(..., true): значение уходит вместе с транзакцией и не
    достаётся следующему запросу на том же соединении пула."""
    first, _ = await _two_tenants_with_material(session)

    with tenant_scope(first.id):
        await _count_materials(session)
        await session.commit()

    async with session.bind.connect() as connection:  # type: ignore[union-attr]
        value = await connection.scalar(
            text("SELECT current_setting('app.tenant_id', true)")
        )
    assert value in (None, "")
