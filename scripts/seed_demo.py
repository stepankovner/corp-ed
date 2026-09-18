"""Засев демо-данных для показа продукта.

Запуск: uv run python scripts/seed_demo.py

Скрипт нужен потому, что завести пользователя через HTTP сейчас нечем:
POST /users/register нерабочая, а чинить её — значит принять продуктовое
решение о том, кто вправе заводить пользователей. Решение не принято,
поэтому демо-тенант собирается здесь, в обход API.

Скрипт не часть приложения: приложение его не импортирует.
Материалы и брифы он не создаёт — их заводит руководитель в интерфейсе,
и это первые кадры демонстрации.
"""

import asyncio
import secrets
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.database import get_engine, get_session_maker
from corp_ed.core.security import hash_password
from corp_ed.core.tenant_context import current_tenant
from corp_ed.domain.models import (
    Brief,
    Chunk,
    Material,
    Program,
    Tenant,
    User,
    UserRole,
)

DEMO_COMPANY_CODE = "demo"
DEMO_COMPANY_NAME = "ООО «Вектор»"

MANAGER_EMAIL = "a.kovaleva@vector.ru"
MANAGER_NAME = "Анна Ковалёва"
INTERN_EMAIL = "i.morozov@vector.ru"
INTERN_NAME = "Илья Морозов"


@dataclass(frozen=True)
class SeededUser:
    """Созданный пользователь вместе с паролем в открытом виде.

    Пароль нигде не хранится — он существует только до печати отчёта,
    поэтому носится рядом с пользователем, а не достаётся из базы.
    """

    user: User
    password: str


async def _drop_demo_tenant(session: AsyncSession) -> bool:
    """Удалить демо-тенанта со всеми его данными. True, если он был.

    Удаление идёт явными DELETE по tenant_id: bulk-запросы проходят мимо
    обоих хуков изоляции, поэтому фильтр здесь обязателен руками.
    Порядок обратный порядку внешних ключей: сначала то, что ссылается.

    Затрагивается только тенант с company_code="demo" — данные других
    компаний скрипт не трогает.
    """
    result = await session.execute(
        select(Tenant).where(Tenant.company_code == DEMO_COMPANY_CODE)
    )
    tenant = result.scalar_one_or_none()
    if tenant is None:
        return False

    tenant_id = tenant.id
    for model in (Chunk, Program, Brief, Material, User):
        await session.execute(delete(model).where(model.tenant_id == tenant_id))
    await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
    await session.commit()
    return True


async def _create_tenant(session: AsyncSession) -> Tenant:
    tenant = Tenant(
        id=uuid4(),
        company_code=DEMO_COMPANY_CODE,
        name=DEMO_COMPANY_NAME,
    )
    session.add(tenant)
    await session.commit()
    return tenant


async def _create_user(
    session: AsyncSession,
    *,
    email: str,
    full_name: str,
    role: UserRole,
) -> SeededUser:
    """Создать пользователя со случайным паролем.

    Пароль генерируется, а не зашит в код: демо-стенд может оказаться
    доступен извне, и учётная запись с известным всем паролем — дыра.
    """
    password = secrets.token_urlsafe(9)
    user = User(
        id=uuid4(),
        email=email,
        full_name=full_name,
        role=role,
        hashed_password=hash_password(password),
    )
    session.add(user)
    await session.commit()
    return SeededUser(user=user, password=password)


def _print_report(
    *,
    tenant: Tenant,
    manager: SeededUser,
    intern: SeededUser,
    replaced: bool,
) -> None:
    print()
    if replaced:
        print("Прежний демо-тенант удалён и создан заново.")
    print(f"Компания: {tenant.name}")
    print(f"Код компании (поле на экране входа): {tenant.company_code}")
    print()

    print("Пользователи")
    print(f"  {'роль':<10} {'email':<26} пароль")
    for label, seeded in (("manager", manager), ("intern", intern)):
        print(f"  {label:<10} {seeded.user.email:<26} {seeded.password}")
    print()

    print("Материалов и брифов нет: их заводит руководитель в интерфейсе.")
    print()


async def seed() -> None:
    session_maker = get_session_maker()
    async with session_maker() as session:
        replaced = await _drop_demo_tenant(session)
        tenant = await _create_tenant(session)

        # Дальше пишутся тенант-скоупные сущности: без контекста хук
        # _check_tenant_on_write отклонит запись.
        token = current_tenant.set(tenant.id)
        try:
            manager = await _create_user(
                session,
                email=MANAGER_EMAIL,
                full_name=MANAGER_NAME,
                role=UserRole.MANAGER,
            )
            intern = await _create_user(
                session,
                email=INTERN_EMAIL,
                full_name=INTERN_NAME,
                role=UserRole.INTERN,
            )
        finally:
            current_tenant.reset(token)

    _print_report(
        tenant=tenant,
        manager=manager,
        intern=intern,
        replaced=replaced,
    )


async def main() -> None:
    try:
        await seed()
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
