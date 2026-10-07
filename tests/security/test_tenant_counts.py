"""Подсчёты по компании называют компанию сами, а не полагаются на RLS.

В select(func.count()).select_from(Model) сущности нет в списке колонок:
хук изоляции (core/database.py) такой запрос не фильтрует, и роль без RLS
(суперпользователь, как в CI) посчитала бы строки всех компаний. Найдено
07.10 на песочнице сайта; тот же вид был у подсчёта администраторов.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import tenant_scope
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services import demo_service


@contextmanager
def _statements(session: AsyncSession) -> Iterator[list[str]]:
    seen: list[str] = []
    engine = session.bind.sync_engine  # type: ignore[union-attr]

    def capture(*args: object) -> None:
        seen.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def _counts_users_of_one_company(statements: list[str]) -> bool:
    counts = [s for s in statements if "count(" in s and "FROM users" in s]
    return bool(counts) and all("users.tenant_id =" in s for s in counts)


async def test_admin_count_names_the_company(session: AsyncSession) -> None:
    with tenant_scope(uuid4()), _statements(session) as statements:
        await UserRepository(session).count_active_admins()
    assert _counts_users_of_one_company(statements), statements


async def test_sandbox_member_check_names_the_company(session: AsyncSession) -> None:
    tenant_id = uuid4()
    with tenant_scope(tenant_id), _statements(session) as statements:
        await demo_service._has_other_members(session, tenant_id, uuid4())
    assert _counts_users_of_one_company(statements), statements
