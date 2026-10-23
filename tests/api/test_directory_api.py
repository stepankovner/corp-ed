"""Справочник сотрудников (BH-47): подсказка при загрузке оргструктуры,
список распознанного, «Использовать» и снятие отметки, аудит."""

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import AuditEvent, Tenant, User
from tests.api.conftest import bearer

ORG = (
    "Организационная структура ООО «Ромашка»\n\n"
    "— Финансовый директор — Кукушкина Ярослава Игоревна, кабинет 402, "
    "доб. 1120, kukushkina@example.ru\n"
    "— Бухгалтерия — главный бухгалтер Петров Иван Сергеевич, кабинет 405, "
    "доб. 1150\n"
)


async def _create(api: httpx.AsyncClient, admin: User, title: str, text: str) -> dict:
    response = await api.post(
        "/api/v1/materials",
        json={"title": title, "content": text},
        headers=bearer(admin),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_org_chart_gets_a_hint_and_yes_makes_it_the_directory(
    api: httpx.AsyncClient, admin: User, session: AsyncSession, tenant_ctx: Tenant
) -> None:
    org = await _create(api, admin, "Структура компании", ORG)
    other = await _create(
        api, admin, "Регламент закупок", "Закупки согласует директор."
    )

    assert (org["directory_hint"], org["is_directory"]) == (2, False)
    assert other["directory_hint"] is None

    entries = await api.get(
        f"/api/v1/materials/{org['id']}/directory", headers=bearer(admin)
    )
    assert entries.status_code == 200
    assert entries.json()[0] == {
        "role": "Финансовый директор",
        "unit": "",
        "name": "Кукушкина Ярослава Игоревна",
        "room": "402",
        "phone": "1120",
        "email": "kukushkina@example.ru",
    }
    assert entries.json()[1]["unit"] == "Бухгалтерия"

    answered = await api.post(
        f"/api/v1/materials/{org['id']}/directory",
        json={"use": True},
        headers=bearer(admin),
    )
    assert answered.status_code == 200, answered.text
    assert (answered.json()["directory_hint"], answered.json()["is_directory"]) == (
        None,
        True,
    )

    off = await api.post(
        f"/api/v1/materials/{org['id']}/directory",
        json={"use": False},
        headers=bearer(admin),
    )
    assert off.json()["is_directory"] is False
    events = (
        await session.scalars(
            select(AuditEvent.details).where(
                AuditEvent.action == "material.directory_changed"
            )
        )
    ).all()
    assert [event["is_directory"] for event in events] == [True, False]


async def test_document_without_directory_lines_cannot_be_the_directory(
    api: httpx.AsyncClient, admin: User, tenant_ctx: Tenant
) -> None:
    text = await _create(api, admin, "Оргструктура (черновик)", "Скоро заполним.")
    assert text["directory_hint"] is None

    answered = await api.post(
        f"/api/v1/materials/{text['id']}/directory",
        json={"use": True},
        headers=bearer(admin),
    )

    assert answered.status_code == 409
    assert answered.json()["code"] == "directory_empty"


async def test_only_admins_see_and_answer(
    api: httpx.AsyncClient, admin: User, employee: User, tenant_ctx: Tenant
) -> None:
    org = await _create(api, admin, "Оргструктура", ORG)

    seen = await api.get(
        f"/api/v1/materials/{org['id']}/directory", headers=bearer(employee)
    )
    answered = await api.post(
        f"/api/v1/materials/{org['id']}/directory",
        json={"use": True},
        headers=bearer(employee),
    )

    assert (seen.status_code, answered.status_code) == (403, 403)
