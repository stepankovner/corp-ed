"""Засев демо-данных для показа продукта.

Запуск: uv run python scripts/seed_demo.py

Скрипт нужен потому, что завести пользователя через HTTP сейчас нечем:
POST /users/register нерабочая, а чинить её — значит принять продуктовое
решение о том, кто вправе заводить пользователей. Решение не принято,
поэтому демо-тенант собирается здесь, в обход API.

Скрипт не часть приложения: приложение его не импортирует.
Ингест материалов сознательно не выполняется — это действие руководителя
в интерфейсе.
"""

import asyncio
import secrets
from dataclasses import dataclass
from uuid import UUID, uuid4

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
    Track,
    User,
    UserRole,
)

DEMO_COMPANY_CODE = "demo"
DEMO_COMPANY_NAME = "ООО «Сфера Роста»"

MANAGER_EMAIL = "manager@sfera-rosta.ru"
MANAGER_NAME = "Ирина Ковалёва"
INTERN_EMAIL = "intern@sfera-rosta.ru"
INTERN_NAME = "Павел Демидов"

# Материалы — то, по чему FAQ-бот будет отвечать на демонстрации.
# Текст намеренно написан как внутренний документ компании: по нему
# видно, на какие вопросы ответ есть, а на какие честно нет.
MATERIALS: tuple[tuple[Track, str, str], ...] = (
    (
        Track.MARKETING,
        "Регламент отпусков и отгулов",
        "Ежегодный оплачиваемый отпуск сотрудника составляет 28 календарных "
        "дней и может быть разделён на части. Одна из частей должна быть "
        "не короче 14 календарных дней подряд: это требование трудового "
        "законодательства, и отдел кадров не согласует график, в котором "
        "оно нарушено.\n\n"
        "Заявление на отпуск подаётся не позднее чем за 14 календарных дней "
        "до его начала через систему кадрового учёта. Заявление согласует "
        "непосредственный руководитель, после чего оно уходит в отдел кадров "
        "автоматически. Устная договорённость с руководителем заявление "
        "не заменяет.\n\n"
        "Отгул за переработку оформляется в том же порядке, но срок подачи "
        "сокращён до двух рабочих дней. Переработка должна быть подтверждена "
        "задачей в таск-трекере: без неё основания для отгула нет.\n\n"
        "О болезни нужно сообщить руководителю в мессенджере до 11:00 "
        "первого дня отсутствия. Больничный лист передаётся в отдел кадров "
        "в электронном виде в течение трёх рабочих дней после закрытия.\n\n"
        "Перенос уже согласованного отпуска возможен по инициативе "
        "сотрудника не позднее чем за семь календарных дней до начала. "
        "Более поздний перенос согласовывается только с директором "
        "направления.",
    ),
    (
        Track.ANALYTICS,
        "Доступы и учётные записи",
        "Учётная запись сотрудника создаётся в день выхода на работу. "
        "Заявку в ИТ-отдел подаёт руководитель не позднее чем за три "
        "рабочих дня до выхода, иначе первый день уйдёт на ожидание "
        "доступов.\n\n"
        "Базовый набор выдаётся всем без отдельного согласования: "
        "корпоративная почта, мессенджер, внутренняя вики и таск-трекер. "
        "Стажёры получают тот же базовый набор, что и штатные сотрудники.\n\n"
        "Расширенные доступы — аналитическая витрина, рекламные кабинеты, "
        "выгрузки из CRM — запрашиваются отдельной заявкой с обоснованием. "
        "Заявку согласует владелец системы, срок рассмотрения — два рабочих "
        "дня. Доступ выдаётся на минимально необходимом уровне: на чтение, "
        "если задача не требует записи.\n\n"
        "Пароли хранятся только в корпоративном менеджере паролей. "
        "Передавать пароль коллеге, записывать его в заметки или в переписку "
        "запрещено; если пароль всё же был передан, его нужно сменить "
        "в тот же день. Двухфакторная аутентификация обязательна для всех "
        "учётных записей, где она поддерживается.\n\n"
        "В последний рабочий день сотрудника или стажёра все доступы "
        "отзываются автоматически по заявке руководителя. Личные файлы "
        "из рабочих хранилищ нужно передать команде до этого дня.",
    ),
    (
        Track.MARKETING,
        "Порядок согласования и публикации материалов",
        "Любой материал, который уходит за пределы компании, проходит "
        "согласование по цепочке: автор — редактор — руководитель "
        "направления. Цепочка не сокращается даже для срочных задач; "
        "срочность меняет сроки, а не число согласующих.\n\n"
        "Редактор берёт материал в работу в течение двух рабочих дней. "
        "Правки оставляются комментариями в документе, автор отвечает "
        "на каждый комментарий: исправлено или почему оставлено как было. "
        "Молча закрытый комментарий считается неотработанным.\n\n"
        "Материалы, содержащие числовые обещания, сравнения с конкурентами "
        "или персональные данные клиентов, дополнительно проходят "
        "юридическую проверку. Срок — три рабочих дня, инициирует её "
        "редактор, а не автор.\n\n"
        "Публикация возможна только после отметки «согласовано» в задаче. "
        "Публикация в обход согласования считается инцидентом и разбирается "
        "с руководителем направления.\n\n"
        "Исходники опубликованных материалов складываются в общее "
        "хранилище в папку года и квартала. Это нужно, чтобы через полгода "
        "можно было понять, что именно было опубликовано и кем согласовано.",
    ),
)

BRIEF_ROLE_TITLE = "Стажёр-маркетолог"
BRIEF_GOALS = (
    "За три месяца стажёр должен самостоятельно вести контент-план "
    "одного продукта: готовить тексты, согласовывать их по регламенту "
    "и снимать метрики по опубликованному."
)
BRIEF_TASKS = (
    "Еженедельные тексты для блога и рассылки, сбор статистики по "
    "рекламным кампаниям, подготовка ежемесячного отчёта по каналам, "
    "участие в планировании контента на следующий месяц."
)
BRIEF_INTERN_LEVEL = "Junior: без коммерческого опыта, четвёртый курс вуза"


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


async def _create_materials(session: AsyncSession) -> list[Material]:
    materials = [
        Material(id=uuid4(), track=track, title=title, content=content)
        for track, title, content in MATERIALS
    ]
    session.add_all(materials)
    await session.commit()
    return materials


async def _create_brief(session: AsyncSession, author_id: UUID) -> Brief:
    brief = Brief(
        id=uuid4(),
        author_id=author_id,
        track=Track.MARKETING,
        role_title=BRIEF_ROLE_TITLE,
        goals=BRIEF_GOALS,
        tasks=BRIEF_TASKS,
        intern_level=BRIEF_INTERN_LEVEL,
    )
    session.add(brief)
    await session.commit()
    return brief


def _print_report(
    *,
    tenant: Tenant,
    manager: SeededUser,
    intern: SeededUser,
    materials: list[Material],
    brief: Brief,
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

    print("Материалы (не проиндексированы — индексация делается в интерфейсе)")
    for material in materials:
        print(f"  {material.id}  {material.track.value:<10} {material.title}")
    print()

    print("Бриф")
    print(f"  {brief.id}  {brief.role_title}")
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
            materials = await _create_materials(session)
            brief = await _create_brief(session, author_id=manager.user.id)
        finally:
            current_tenant.reset(token)

    _print_report(
        tenant=tenant,
        manager=manager,
        intern=intern,
        materials=materials,
        brief=brief,
        replaced=replaced,
    )


async def main() -> None:
    try:
        await seed()
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
