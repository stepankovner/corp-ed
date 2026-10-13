"""Заменённая редакция документа (BH-41, решение Артёма 06.10): подсказка
при загрузке, пометка «заменён», документ уходит из поиска и ответов, но
остаётся в «Источниках»."""

from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import AuditEvent, Tenant, User
from corp_ed.repositories.audit_repository import AuditAction
from tests.api.conftest import bearer
from tests.api.test_admin_api import _material_in, _sources

NEW_TEXT = (
    "Утверждено приказом № 7-К от 15.01.2026. Настоящая редакция вступает в "
    "силу с 01.02.2026; предыдущая редакция (2025 года) утрачивает силу."
)


async def _create(api: httpx.AsyncClient, admin: User, title: str) -> dict:
    response = await api.post(
        "/api/v1/materials",
        json={"title": title, "content": NEW_TEXT},
        headers=bearer(admin),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _audit(session: AsyncSession, action: str) -> list[AuditEvent]:
    return list(
        (
            await session.scalars(select(AuditEvent).where(AuditEvent.action == action))
        ).all()
    )


async def test_new_edition_asks_and_yes_takes_old_one_out_of_answers(
    api: httpx.AsyncClient,
    admin: User,
    employee: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    old = await _material_in(
        session, tenant_ctx, None, "Положение о командировках 2025"
    )
    other = await _material_in(session, tenant_ctx, None, "Регламент закупок")

    new = await _create(api, admin, "Положение о командировках 2026")
    # Подсказка — только прежняя редакция; соседний документ не трогаем.
    assert new["edition_hint"] == [str(old.id)]
    assert new["superseded_at"] is None
    # Без ответа администратора из поиска ничего не уходит.
    assert "Положение о командировках 2025" in await _sources(api, employee)

    # Подсказка ждёт ответа: её видно и в списке, и в карточке документа.
    listed = (await api.get("/api/v1/materials", headers=bearer(admin))).json()
    hint = {m["id"]: m["edition_hint"] for m in listed}
    assert hint[new["id"]] == [str(old.id)]
    assert hint[str(other.id)] == []
    one = await api.get(f"/api/v1/materials/{new['id']}", headers=bearer(admin))
    assert one.json()["edition_hint"] == [str(old.id)]

    answered = await api.post(
        f"/api/v1/materials/{new['id']}/edition-hint",
        json={"accept": True},
        headers=bearer(admin),
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["edition_hint"] == []

    listed = (await api.get("/api/v1/materials", headers=bearer(admin))).json()
    by_id = {m["id"]: m for m in listed}
    # Прежняя редакция хранится и видна администратору — с пометкой.
    assert by_id[str(old.id)]["superseded_at"] is not None
    assert by_id[str(old.id)]["superseded_by_id"] == new["id"]
    assert by_id[new["id"]]["superseded_at"] is None
    assert by_id[new["id"]]["edition_hint"] == []
    assert by_id[str(other.id)]["superseded_at"] is None

    titles = await _sources(api, employee)
    assert "Положение о командировках 2025" not in titles
    assert "Регламент закупок" in titles

    events = await _audit(session, AuditAction.MATERIAL_SUPERSEDED)
    assert [e.target_id for e in events] == [str(old.id)]
    assert events[0].actor_user_id == admin.id
    assert events[0].details == {
        "title": "Положение о командировках 2025",
        "superseded_by": new["id"],
    }


async def test_no_keeps_both_editions_and_drops_the_hint(
    api: httpx.AsyncClient,
    admin: User,
    employee: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    old = await _material_in(
        session, tenant_ctx, None, "Положение о командировках 2025"
    )
    new = await _create(api, admin, "Положение о командировках 2026")

    answered = await api.post(
        f"/api/v1/materials/{new['id']}/edition-hint",
        json={"accept": False},
        headers=bearer(admin),
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["edition_hint"] == []

    # API работает в той же сессии; refresh — чтобы увидеть то, что
    # сделала сама база (ON DELETE SET NULL).
    await session.refresh(old)
    stored = old
    assert stored.superseded_at is None
    assert "Положение о командировках 2025" in await _sources(api, employee)
    assert await _audit(session, AuditAction.MATERIAL_SUPERSEDED) == []


async def test_mark_by_hand_and_restore(
    api: httpx.AsyncClient,
    admin: User,
    employee: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    old = await _material_in(session, tenant_ctx, None, "Положение об оплате 2024")

    marked = await api.patch(
        f"/api/v1/materials/{old.id}",
        json={"superseded": True},
        headers=bearer(admin),
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["superseded_at"] is not None
    assert marked.json()["superseded_by_id"] is None
    assert await _sources(api, employee) == []
    mine = (await api.get("/api/v1/sources/mine", headers=bearer(employee))).json()
    assert mine["files"] == []

    # Повторная пометка ничего не меняет и не пишет второе событие.
    again = await api.patch(
        f"/api/v1/materials/{old.id}",
        json={"superseded": True},
        headers=bearer(admin),
    )
    assert again.json()["superseded_at"] == marked.json()["superseded_at"]
    assert len(await _audit(session, AuditAction.MATERIAL_SUPERSEDED)) == 1

    restored = await api.patch(
        f"/api/v1/materials/{old.id}",
        json={"superseded": False},
        headers=bearer(admin),
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["superseded_at"] is None
    # Чанки не удалялись — документ сразу снова в ответах.
    assert await _sources(api, employee) == ["Положение об оплате 2024"]
    mine = (await api.get("/api/v1/sources/mine", headers=bearer(employee))).json()
    assert [f["documents"] for f in mine["files"]] == [1]
    assert [
        e.target_id for e in await _audit(session, AuditAction.MATERIAL_RESTORED)
    ] == [str(old.id)]


async def test_hint_skips_documents_already_out_of_search(
    api: httpx.AsyncClient,
    admin: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    old = await _material_in(
        session, tenant_ctx, None, "Положение о командировках 2025"
    )
    new = await _create(api, admin, "Положение о командировках 2026")
    assert new["edition_hint"] == [str(old.id)]

    # Прежнюю пометили вручную раньше, чем ответили на подсказку, —
    # вопрос больше не нужен.
    await api.patch(
        f"/api/v1/materials/{old.id}",
        json={"superseded": True},
        headers=bearer(admin),
    )
    one = await api.get(f"/api/v1/materials/{new['id']}", headers=bearer(admin))
    assert one.json()["edition_hint"] == []

    # Заменённая редакция не предлагается как кандидат для следующих.
    newest = await _create(api, admin, "Положение о командировках 2027")
    assert newest["edition_hint"] == [new["id"]]


async def test_hint_never_points_to_another_company(
    api: httpx.AsyncClient,
    admin: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        foreign = await _material_in(
            session, other, None, "Положение о командировках 2025"
        )

    new = await _create(api, admin, "Положение о командировках 2026")
    assert new["edition_hint"] == []

    # Чужой документ не пометить и не вернуть.
    response = await api.patch(
        f"/api/v1/materials/{foreign.id}",
        json={"superseded": True},
        headers=bearer(admin),
    )
    assert response.status_code == 404
    response = await api.post(
        f"/api/v1/materials/{foreign.id}/edition-hint",
        json={"accept": True},
        headers=bearer(admin),
    )
    assert response.status_code == 404


async def test_employee_cannot_mark_or_answer(
    api: httpx.AsyncClient,
    employee: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    old = await _material_in(session, tenant_ctx, None, "Положение об оплате 2024")

    response = await api.patch(
        f"/api/v1/materials/{old.id}",
        json={"superseded": True},
        headers=bearer(employee),
    )
    assert response.status_code == 403
    response = await api.post(
        f"/api/v1/materials/{old.id}/edition-hint",
        json={"accept": True},
        headers=bearer(employee),
    )
    assert response.status_code == 403

    # API работает в той же сессии; refresh — чтобы увидеть то, что
    # сделала сама база (ON DELETE SET NULL).
    await session.refresh(old)
    stored = old
    assert stored.superseded_at is None


async def test_deleting_new_edition_keeps_old_one_marked(
    api: httpx.AsyncClient,
    admin: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    old = await _material_in(
        session, tenant_ctx, None, "Положение о командировках 2025"
    )
    new = await _create(api, admin, "Положение о командировках 2026")
    await api.post(
        f"/api/v1/materials/{new['id']}/edition-hint",
        json={"accept": True},
        headers=bearer(admin),
    )

    deleted = await api.delete(f"/api/v1/materials/{new['id']}", headers=bearer(admin))
    assert deleted.status_code == 204

    # API работает в той же сессии; refresh — чтобы увидеть то, что
    # сделала сама база (ON DELETE SET NULL).
    await session.refresh(old)
    stored = old
    # Ссылка на удалённую редакцию снимается, пометка — нет: вернуть в
    # поиск — решение администратора.
    assert stored.superseded_by_id is None
    assert stored.superseded_at is not None


async def test_uploaded_file_gets_the_hint_from_its_text(
    api: httpx.AsyncClient,
    admin: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    # Название без года — подсказку даёт текст: «прежняя редакция
    # утрачивает силу» (разбор файла — до нарезки).
    old = await _material_in(session, tenant_ctx, None, "Регламент закупок")
    response = await api.post(
        "/api/v1/materials/upload",
        data={"title": "Регламент закупок"},
        files={"file": ("zakupki.txt", NEW_TEXT.encode(), "text/plain")},
        headers=bearer(admin),
    )
    assert response.status_code == 201, response.text
    assert response.json()["edition_hint"] == [str(old.id)]
