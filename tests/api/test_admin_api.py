"""Админка (ТЗ §7): настройки компании, логотип, тариф, аналитика, папки
с доступом по отделам."""

import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.api.v1.dependencies import get_team_notifier
from corp_ed.core.config import EMBEDDING_DIM
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    AuditEvent,
    Chunk,
    Department,
    Folder,
    Material,
    MaterialStatus,
    QaLog,
    Tenant,
    User,
    UserRole,
)
from corp_ed.main import app
from tests.api.conftest import account_bearer, bearer
from tests.factories import make_account, make_user
from tests.team_notify_helpers import RecordingNotifier


def _png(width: int = 300, height: int = 100) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (59, 52, 214)).save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
async def worker(session: AsyncSession, tenant_ctx: Tenant) -> User:
    user = make_user(tenant_id=tenant_ctx.id, email="worker2@test.com")
    session.add(user)
    await session.commit()
    return user


# --- настройки компании -------------------------------------------------------


async def test_admin_changes_company_settings_and_journal_keeps_old_and_new(
    api: httpx.AsyncClient, admin: User, session: AsyncSession, tenant_ctx: Tenant
) -> None:
    headers = bearer(admin)
    before = (await api.get("/api/v1/company", headers=headers)).json()
    assert before["name"] == "Test Co"
    assert before["not_found_mode"] == "general"
    assert before["mfa_policy"] == "any"
    assert before["email_domains"] == []

    response = await api.patch(
        "/api/v1/company",
        json={
            "name": "  ООО «Меридиан»  ",
            "not_found_mode": "strict",
            "allow_remember_device": False,
            "email_domains": [
                "@Meridian.RU",
                "https://msk.meridian.ru/",
                "meridian.ru",
            ],
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "ООО «Меридиан»"
    assert body["not_found_mode"] == "strict"
    assert body["allow_remember_device"] is False
    assert body["email_domains"] == ["meridian.ru", "msk.meridian.ru"]

    with tenant_scope(tenant_ctx.id):
        event = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "tenant.settings_updated")
            )
        ).one()
    assert event.details["not_found_mode"] == {"old": "general", "new": "strict"}

    bad = await api.patch(
        "/api/v1/company", json={"email_domains": ["не домен"]}, headers=headers
    )
    assert bad.status_code == 400
    assert bad.json()["code"] == "invalid_domain"


async def test_employee_cannot_open_company_settings(
    api: httpx.AsyncClient, employee: User
) -> None:
    headers = bearer(employee)
    assert (await api.get("/api/v1/company", headers=headers)).status_code == 403
    assert (
        await api.patch("/api/v1/company", json={"name": "x"}, headers=headers)
    ).status_code == 403
    assert (await api.get("/api/v1/analytics", headers=headers)).status_code == 403


async def test_strong_policy_closes_company_to_employee_without_app(
    api: httpx.AsyncClient, admin: User, employee: User
) -> None:
    await api.patch(
        "/api/v1/company", json={"mfa_policy": "strong"}, headers=bearer(admin)
    )
    response = await api.get("/api/v1/people", headers=bearer(employee))
    assert response.status_code == 403
    assert response.json()["code"] == "mfa_setup_required"


async def test_company_domains_limit_who_joins_by_any_invite(
    api: httpx.AsyncClient, admin: User, session: AsyncSession
) -> None:
    headers = bearer(admin)
    await api.patch(
        "/api/v1/company", json={"email_domains": ["meridian.ru"]}, headers=headers
    )
    invite = (await api.post("/api/v1/invites", json={}, headers=headers)).json()
    outsider = make_account("anna@gmail.com", full_name="Анна Смирнова")
    insider = make_account("petr@msk.meridian.ru", full_name="Пётр Петров")
    session.add_all([outsider, insider])
    await session.commit()

    refused = await api.post(
        "/api/v1/invites/accept",
        json={"secret": invite["token"]},
        headers=account_bearer(outsider),
    )
    assert refused.status_code == 422
    assert "@meridian.ru" in refused.json()["detail"]
    joined = await api.post(
        "/api/v1/invites/accept",
        json={"secret": invite["token"]},
        headers=account_bearer(insider),
    )
    assert joined.status_code == 200, joined.text


# --- логотип -------------------------------------------------------------------


async def test_logo_is_fitted_into_square_and_shown_to_members(
    api: httpx.AsyncClient, admin: User, employee: User
) -> None:
    uploaded = await api.put(
        "/api/v1/company/logo",
        files={"file": ("logo.png", _png(), "image/png")},
        headers=bearer(admin),
    )
    assert uploaded.status_code == 200, uploaded.text
    url = uploaded.json()["logo_url"]

    image = await api.get(url)
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/webp"
    with Image.open(io.BytesIO(image.content)) as logo:
        assert logo.size == (256, 256)
        # Вытянутый логотип вписан, а не обрезан: сверху — прозрачные поля.
        assert logo.convert("RGBA").getpixel((128, 2))[3] == 0
        assert logo.convert("RGBA").getpixel((128, 128))[3] == 255

    me = (await api.get("/api/v1/auth/me", headers=bearer(employee))).json()
    assert me["company"]["logo_url"] == url
    assert me["companies"][0]["logo_url"] == url

    forged = url.replace("sig=", "sig=0")[: len(url)]
    assert (await api.get(forged)).status_code == 404
    bad = await api.put(
        "/api/v1/company/logo",
        files={"file": ("logo.png", b"not an image", "image/png")},
        headers=bearer(admin),
    )
    assert bad.status_code == 400
    assert bad.json()["code"] == "invalid_logo"
    assert (
        await api.put(
            "/api/v1/company/logo",
            files={"file": ("logo.png", _png(), "image/png")},
            headers=bearer(employee),
        )
    ).status_code == 403

    assert (
        await api.delete("/api/v1/company/logo", headers=bearer(admin))
    ).status_code == 204
    me = (await api.get("/api/v1/auth/me", headers=bearer(employee))).json()
    assert me["company"]["logo_url"] is None


# --- тариф -----------------------------------------------------------------------


async def test_tariff_request_goes_to_team(
    api: httpx.AsyncClient, admin: User, employee: User
) -> None:
    sent: list[str] = []
    app.dependency_overrides[get_team_notifier] = lambda: RecordingNotifier(sent)
    response = await api.post(
        "/api/v1/company/tariff-request",
        json={"tariff": "extended", "seats": 40, "comment": "Растём"},
        headers=bearer(admin),
    )
    assert response.status_code == 202
    assert len(sent) == 1
    assert "Базовый → Расширенный" in sent[0]
    assert "40" in sent[0]
    assert "Растём" in sent[0]
    assert (
        await api.post(
            "/api/v1/company/tariff-request",
            json={"tariff": "extended"},
            headers=bearer(employee),
        )
    ).status_code == 403
    assert (
        await api.post(
            "/api/v1/company/tariff-request",
            json={"tariff": "gold"},
            headers=bearer(admin),
        )
    ).status_code == 422


# --- аналитика ---------------------------------------------------------------------


def _log(tenant: Tenant, user: User | None, question: str, **fields: Any) -> QaLog:
    return QaLog(
        tenant_id=tenant.id,
        user_id=user.id if user else None,
        question=question,
        question_embedding=[0.1] * EMBEDDING_DIM,
        embedding_model="fake",
        prompt_version="test",
        answer_given=fields.pop("answer_given", True),
        origin=fields.pop("origin", "documents"),
        **fields,
    )


async def test_analytics_is_anonymous_and_counts_by_day(
    api: httpx.AsyncClient,
    admin: User,
    employee: User,
    worker: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    old = datetime.now(UTC) - timedelta(days=20)
    session.add_all(
        [
            _log(tenant_ctx, employee, "Как заказать пропуск?", feedback=1),
            _log(tenant_ctx, worker, "как заказать пропуск", credits=2),
            _log(tenant_ctx, admin, "Как заказать пропуск?"),
            _log(
                tenant_ctx,
                employee,
                "Когда премия?",
                answer_given=False,
                origin="general_knowledge",
                feedback=-1,
                feedback_reason="outdated",
                feedback_comment="Звоните [телефон]",
            ),
            _log(
                tenant_ctx,
                worker,
                "Где столовая?",
                answer_given=False,
                origin="none",
            ),
            _log(tenant_ctx, worker, "Старый вопрос", created_at=old),
        ]
    )
    await session.commit()

    response = await api.get("/api/v1/analytics?days=7", headers=bearer(admin))
    assert response.status_code == 200, response.text
    data = response.json()
    assert (data["questions"], data["answered"], data["general"], data["refused"]) == (
        5,
        3,
        1,
        1,
    )
    assert (data["likes"], data["dislikes"]) == (1, 1)
    assert data["reasons"] == {"outdated": 1}
    assert data["active_people"] == 3
    assert data["credits"] == 2
    assert len(data["days"]) == 7
    assert data["days"][-1]["questions"] == 5
    assert data["frequent"] == [
        {"question": "Как заказать пропуск?", "asked": 3, "people": 3, "answered": 3}
    ]
    assert data["comments"][0]["comment"] == "Звоните [телефон]"
    # Ни имён, ни почты, ни идентификаторов людей.
    text = response.text
    for user in (admin, employee, worker):
        assert str(user.id) not in text
        assert user.email not in text

    month = (await api.get("/api/v1/analytics?days=30", headers=bearer(admin))).json()
    assert month["questions"] == 6
    assert (
        await api.get("/api/v1/analytics?days=5", headers=bearer(admin))
    ).status_code == 422


# --- папки с доступом по отделам ---------------------------------------------------


async def _material_in(
    session: AsyncSession, tenant: Tenant, folder: Folder | None, title: str
) -> Material:
    material = Material(
        id=uuid4(),
        tenant_id=tenant.id,
        title=title,
        content="текст",
        folder_id=folder.id if folder else None,
        status=MaterialStatus.READY,
    )
    session.add(material)
    await session.flush()
    session.add(
        Chunk(
            material_id=material.id,
            position=0,
            content=f"{title}: оклад указан в приказе.",
            embedding=[0.1] * EMBEDDING_DIM,
            model="fake",
            model_version="fake",
        )
    )
    await session.commit()
    return material


async def _sources(api: httpx.AsyncClient, user: User) -> list[str]:
    response = await api.post(
        "/api/v1/conversations",
        json={"question": "Какой оклад?"},
        headers=bearer(user),
    )
    assert response.status_code == 200, response.text

    done = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ][-1]
    return [source["title"] for source in done["answer"]["sources"]]


async def test_restricted_folder_is_seen_by_its_department_and_admins_only(
    api: httpx.AsyncClient,
    admin: User,
    employee: User,
    worker: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    hr = Department(name="Кадры")
    session.add(hr)
    await session.flush()
    employee.department_id = hr.id
    await session.commit()

    created = await api.post(
        "/api/v1/folders",
        json={"name": "Зарплаты", "restricted": True, "department_ids": [str(hr.id)]},
        headers=bearer(admin),
    )
    assert created.status_code == 201, created.text
    folder_body = created.json()
    assert folder_body["departments"] == [{"id": str(hr.id), "name": "Кадры"}]
    folder = await session.get(Folder, folder_body["id"])
    assert folder is not None
    secret = await _material_in(session, tenant_ctx, folder, "Положение об оплате")

    assert await _sources(api, employee) == ["Положение об оплате"]
    assert await _sources(api, admin) == ["Положение об оплате"]
    assert await _sources(api, worker) == []

    mine = (await api.get("/api/v1/sources/mine", headers=bearer(employee))).json()
    assert mine["files"] == [
        {
            "folder_id": folder_body["id"],
            "name": "Зарплаты",
            "restricted": True,
            "documents": 1,
        }
    ]
    assert (await api.get("/api/v1/sources/mine", headers=bearer(worker))).json()[
        "files"
    ] == []

    # Непустую папку не удалить: документы стали бы видны всем.
    refused = await api.delete(f"/api/v1/folders/{folder.id}", headers=bearer(admin))
    assert refused.status_code == 409
    assert refused.json()["code"] == "folder_not_empty"

    # Перенос в общие — видно всем; пустую папку можно удалить.
    moved = await api.patch(
        f"/api/v1/materials/{secret.id}",
        json={"folder_id": None},
        headers=bearer(admin),
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["folder_id"] is None
    assert await _sources(api, worker) == ["Положение об оплате"]
    assert (
        await api.delete(f"/api/v1/folders/{folder.id}", headers=bearer(admin))
    ).status_code == 204


async def test_open_folder_and_department_of_other_company(
    api: httpx.AsyncClient,
    admin: User,
    worker: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    headers = bearer(admin)
    # Заранее: после отказа ниже общая с тестом сессия откатывается.
    worker_headers = bearer(worker)
    opened = (
        await api.post("/api/v1/folders", json={"name": "Регламенты"}, headers=headers)
    ).json()
    folder = await session.get(Folder, opened["id"])
    await _material_in(session, tenant_ctx, folder, "Регламент отпусков")
    assert await _sources(api, worker) == ["Регламент отпусков"]

    duplicate = await api.post(
        "/api/v1/folders", json={"name": "регламенты"}, headers=headers
    )
    assert duplicate.status_code == 409

    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        foreign = Department(name="Чужой")
        session.add(foreign)
        await session.commit()
    response = await api.patch(
        f"/api/v1/folders/{opened['id']}",
        json={"restricted": True, "department_ids": [str(foreign.id)]},
        headers=headers,
    )
    assert response.status_code == 404

    listed = (await api.get("/api/v1/folders", headers=headers)).json()
    assert [(f["name"], f["documents"]) for f in listed] == [("Регламенты", 1)]
    assert (await api.get("/api/v1/folders", headers=worker_headers)).status_code == 403


async def test_upload_into_folder(
    api: httpx.AsyncClient, admin: User, session: AsyncSession
) -> None:
    folder = (
        await api.post(
            "/api/v1/folders",
            json={"name": "Кадры", "restricted": True},
            headers=bearer(admin),
        )
    ).json()
    response = await api.post(
        "/api/v1/materials/upload",
        data={"title": "Отпуска", "folder_id": folder["id"]},
        files={"file": ("otpusk.txt", "Отпуск 28 дней.".encode(), "text/plain")},
        headers=bearer(admin),
    )
    assert response.status_code == 201, response.text
    assert response.json()["folder_id"] == folder["id"]
    unknown = await api.post(
        "/api/v1/materials/upload",
        data={"title": "Ещё", "folder_id": str(uuid4())},
        files={"file": ("other.txt", "Другой текст.".encode(), "text/plain")},
        headers=bearer(admin),
    )
    assert unknown.status_code == 404


async def test_user_role_admin_sees_restricted_folder_without_department(
    api: httpx.AsyncClient, session: AsyncSession, tenant_ctx: Tenant, admin: User
) -> None:
    second_admin = make_user(
        tenant_id=tenant_ctx.id, email="boss2@test.com", role=UserRole.ADMIN
    )
    assert second_admin.account is not None
    second_admin.account.totp_enabled_at = datetime.now(UTC)
    session.add(second_admin)
    await session.commit()
    folder = (
        await api.post(
            "/api/v1/folders",
            json={"name": "Совет директоров", "restricted": True},
            headers=bearer(admin),
        )
    ).json()
    await _material_in(
        session, tenant_ctx, await session.get(Folder, folder["id"]), "Протокол"
    )
    assert await _sources(api, second_admin) == ["Протокол"]
