"""Профиль, фото, отделы и справочник коллег (ТЗ §4, §7; этап 5).

Главное — кто что видит: профиль видят люди той же компании и никто
вне её; должность и отдел коллеги меняет только администратор; фото
отдаётся только по подписанной ссылке.
"""

import io
import sys
import time
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    AuditEvent,
    Department,
    MemberStatus,
    Tenant,
    User,
    UserRole,
)
from corp_ed.ingest import sandbox
from corp_ed.repositories.audit_repository import AuditAction
from corp_ed.services.avatar_service import avatar_url
from tests.api.conftest import account_bearer, bearer, enable_test_totp
from tests.factories import make_account, make_user


def _image(
    fmt: str = "JPEG", size: tuple[int, int] = (800, 600), **save: object
) -> bytes:
    out = io.BytesIO()
    mode = "RGBA" if fmt == "PNG" else "RGB"
    Image.new(mode, size, (200, 30, 30, 255) if mode == "RGBA" else (200, 30, 30)).save(
        out, format=fmt, **save
    )
    return out.getvalue()


def _jpeg_with_gps() -> bytes:
    """Снимок с геометкой в EXIF — как с телефона."""
    image = Image.new("RGB", (640, 480), (10, 120, 200))
    exif = Image.Exif()
    gps = {1: "N", 2: (55.0, 45.0, 0.0), 3: "E", 4: (37.0, 37.0, 0.0)}
    exif[0x8825] = gps  # GPSInfo
    exif[0x0110] = "Phone Model X"  # Model
    out = io.BytesIO()
    image.save(out, format="JPEG", exif=exif)
    return out.getvalue()


async def _other_company(session: AsyncSession, code: str = "other") -> Tenant:
    other = Tenant(id=uuid4(), company_code=code, name="Other")
    session.add(other)
    await session.commit()
    return other


# --- профиль -------------------------------------------------------------------


async def test_profile_fields_are_normalized_and_shown_in_me(
    api: httpx.AsyncClient, account: User
) -> None:
    headers = bearer(account)
    response = await api.patch(
        "/api/v1/account",
        json={
            "first_name": " Анна ",
            "last_name": "Смирнова",
            "patronymic": " Сергеевна ",
            "phone": "8 (999) 123-45-67",
            "telegram": "https://t.me/anna_smirnova",
        },
        headers=headers,
    )
    assert response.status_code == 204

    me = (await api.get("/api/v1/auth/me", headers=headers)).json()
    assert me["first_name"] == "Анна"
    assert me["patronymic"] == "Сергеевна"
    assert me["phone"] == "+79991234567"
    assert me["telegram"] == "anna_smirnova"
    assert me["avatar_url"] is None

    # Пришедшее null — очистить; не пришедшее — не трогать.
    cleared = await api.patch(
        "/api/v1/account", json={"phone": None, "telegram": ""}, headers=headers
    )
    assert cleared.status_code == 204
    me = (await api.get("/api/v1/auth/me", headers=headers)).json()
    assert (me["phone"], me["telegram"], me["patronymic"]) == (None, None, "Сергеевна")


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("phone", "звоните мне", "invalid_phone"),
        ("phone", "12345", "invalid_phone"),
        ("telegram", "@ab", "invalid_telegram"),
        ("telegram", "@1anna", "invalid_telegram"),
    ],
)
async def test_invalid_contacts_are_rejected(
    api: httpx.AsyncClient, account: User, field: str, value: str, code: str
) -> None:
    response = await api.patch(
        "/api/v1/account", json={field: value}, headers=bearer(account)
    )
    assert response.status_code == 400
    assert response.json()["code"] == code


async def test_name_cannot_be_cleared(api: httpx.AsyncClient, account: User) -> None:
    response = await api.patch(
        "/api/v1/account", json={"first_name": None}, headers=bearer(account)
    )
    assert response.status_code == 422


# --- фото ----------------------------------------------------------------------


async def test_avatar_is_reencoded_without_metadata(
    api: httpx.AsyncClient, account: User
) -> None:
    headers = bearer(account)
    uploaded = await api.put(
        "/api/v1/account/avatar",
        files={"file": ("photo.jpg", _jpeg_with_gps(), "image/jpeg")},
        headers=headers,
    )
    assert uploaded.status_code == 200, uploaded.text
    url = uploaded.json()["avatar_url"]
    assert (await api.get("/api/v1/auth/me", headers=headers)).json()[
        "avatar_url"
    ] == url

    # По ссылке — без входа: <img> не шлёт заголовок Authorization.
    image = await api.get(url)
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/webp"
    picture = Image.open(io.BytesIO(image.content))
    assert picture.size == (256, 256)
    assert not picture.getexif()
    assert b"Phone Model X" not in image.content


async def test_transparent_png_and_webp_are_accepted(
    api: httpx.AsyncClient, account: User
) -> None:
    for name, data in (("a.png", _image("PNG")), ("a.webp", _image("WEBP"))):
        response = await api.put(
            "/api/v1/account/avatar",
            files={"file": (name, data, "application/octet-stream")},
            headers=bearer(account),
        )
        assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "payload",
    [
        b"%PDF-1.7 not an image",
        _image("GIF", (64, 64)),
        b"",
    ],
)
async def test_non_photos_are_rejected(
    api: httpx.AsyncClient, account: User, payload: bytes
) -> None:
    response = await api.put(
        "/api/v1/account/avatar",
        files={"file": ("photo.jpg", payload, "image/jpeg")},
        headers=bearer(account),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_avatar"


async def test_giant_dimensions_are_rejected_before_decoding(
    api: httpx.AsyncClient, account: User
) -> None:
    """Сжатый файл маленький, а в пикселях — 48 Мп: «бомба» не распаковывается."""
    out = io.BytesIO()
    Image.new("1", (8000, 6000)).save(out, format="PNG")
    response = await api.put(
        "/api/v1/account/avatar",
        files={"file": ("bomb.png", out.getvalue(), "image/png")},
        headers=bearer(account),
    )
    assert response.status_code == 400
    assert "мегапикселей" in response.json()["detail"]


async def test_avatar_is_decoded_outside_the_api_process(
    api: httpx.AsyncClient, account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Фото разбирает дочерний процесс песочницы: его падение — та же
    ошибка, что для битой картинки, а не 500 и не фото без обработки."""
    monkeypatch.setattr(
        sandbox,
        "_worker_command",
        lambda mode, cpu_seconds: [
            sys.executable,
            "-I",
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGSEGV)",
        ],
    )
    response = await api.put(
        "/api/v1/account/avatar",
        files={"file": ("photo.jpg", _image(), "image/jpeg")},
        headers=bearer(account),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_avatar"
    assert response.json()["detail"] == "Загрузите фото в JPEG, PNG или WebP до 5 МБ"


async def test_avatar_waits_when_sandbox_cannot_close_network(
    api: httpx.AsyncClient, account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """В production песочница без фильтра сети не работает: это не вина
    файла — 503, а не «загрузите другое фото»."""
    monkeypatch.setattr(
        sandbox,
        "_worker_command",
        lambda mode, cpu_seconds: [
            sys.executable,
            "-I",
            "-c",
            "import json; print(json.dumps("
            "{'ok': False, 'code': 'sandbox_unavailable'}))",
        ],
    )
    response = await api.put(
        "/api/v1/account/avatar",
        files={"file": ("photo.jpg", _image(), "image/jpeg")},
        headers=bearer(account),
    )
    assert response.status_code == 503


async def test_avatar_link_needs_a_valid_fresh_signature(
    api: httpx.AsyncClient, account: User
) -> None:
    headers = bearer(account)
    url = (
        await api.put(
            "/api/v1/account/avatar",
            files={"file": ("a.jpg", _image(), "image/jpeg")},
            headers=headers,
        )
    ).json()["avatar_url"]
    assert account.account is not None
    account_id = account.account.id
    version = url.split("v=")[1].split("&")[0]

    forged = url[:-4] + "0000"
    stranger = url.replace(str(account_id), str(uuid4()))
    expired = avatar_url(account_id, version, now=time.time() - 3 * 86_400)
    assert (await api.get(forged)).status_code == 404
    assert (await api.get(stranger)).status_code == 404
    assert (await api.get(expired)).status_code == 404

    # Новое фото — прежняя ссылка больше не отдаёт ничего.
    await api.put(
        "/api/v1/account/avatar",
        files={"file": ("b.png", _image("PNG"), "image/png")},
        headers=headers,
    )
    assert (await api.get(url)).status_code == 404

    removed = await api.delete("/api/v1/account/avatar", headers=headers)
    assert removed.status_code == 204
    assert (await api.get("/api/v1/auth/me", headers=headers)).json()[
        "avatar_url"
    ] is None


async def test_avatar_works_without_a_company(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    lone = make_account("lone@test.com", first_name="Лена", last_name="Одна")
    session.add(lone)
    await session.commit()
    response = await api.put(
        "/api/v1/account/avatar",
        files={"file": ("a.jpg", _image(), "image/jpeg")},
        headers=account_bearer(lone),
    )
    assert response.status_code == 200


# --- справочник ----------------------------------------------------------------


async def test_directory_shows_only_working_colleagues_of_this_company(
    api: httpx.AsyncClient,
    account: User,
    admin_account: User,
    session: AsyncSession,
) -> None:
    other = await _other_company(session)
    with tenant_scope(other.id):
        session.add(make_user(email="stranger@other.com", full_name="Чужой Человек"))
        await session.commit()
    session.add_all(
        [
            make_user(email="new@test.com", status=MemberStatus.PENDING),
            make_user(email="blocked@test.com", status=MemberStatus.BLOCKED),
            make_user(email="gone@test.com", status=MemberStatus.LEFT),
        ]
    )
    await session.commit()

    response = await api.get("/api/v1/people", headers=bearer(account))

    assert response.status_code == 200
    emails = {person["email"] for person in response.json()}
    assert emails == {account.email, admin_account.email}
    me = next(p for p in response.json() if p["email"] == account.email)
    assert me["member_id"] == str(account.id)
    assert me["role"] == "employee"
    assert "hashed_password" not in me


async def test_directory_needs_a_company(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    lone = make_account("lone@test.com")
    session.add(lone)
    await session.commit()
    response = await api.get("/api/v1/people", headers=account_bearer(lone))
    assert response.status_code == 403
    assert response.json()["code"] == "no_company"


async def test_person_card_of_other_company_is_not_found(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    other = await _other_company(session)
    with tenant_scope(other.id):
        stranger = make_user(email="stranger@other.com")
        session.add(stranger)
        await session.commit()
    response = await api.get(f"/api/v1/people/{stranger.id}", headers=bearer(account))
    assert response.status_code == 404


# --- должность и отдел ---------------------------------------------------------


async def _department(api: httpx.AsyncClient, admin: User, name: str) -> str:
    created = await api.post(
        "/api/v1/departments", json={"name": name}, headers=bearer(admin)
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def test_person_sets_own_position_and_department(
    api: httpx.AsyncClient, account: User, admin_account: User
) -> None:
    sales = await _department(api, admin_account, "Продажи")
    headers = bearer(account)

    response = await api.patch(
        f"/api/v1/people/{account.id}",
        json={"position": " Менеджер ", "department_id": sales},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["position"] == "Менеджер"
    assert response.json()["department"] == {"id": sales, "name": "Продажи"}
    company = (await api.get("/api/v1/auth/me", headers=headers)).json()["company"]
    assert company["position"] == "Менеджер"
    assert company["department"]["name"] == "Продажи"

    cleared = await api.patch(
        f"/api/v1/people/{account.id}",
        json={"position": "", "department_id": None},
        headers=headers,
    )
    assert (cleared.json()["position"], cleared.json()["department"]) == (None, None)


async def test_employee_cannot_change_colleague(
    api: httpx.AsyncClient, account: User, admin_account: User
) -> None:
    response = await api.patch(
        f"/api/v1/people/{admin_account.id}",
        json={"position": "Стажёр"},
        headers=bearer(account),
    )
    assert response.status_code == 403


async def test_admin_corrects_colleague_and_it_is_audited(
    api: httpx.AsyncClient,
    account: User,
    admin_account: User,
    session: AsyncSession,
) -> None:
    response = await api.patch(
        f"/api/v1/people/{account.id}",
        json={"position": "Ведущий инженер"},
        headers=bearer(admin_account),
    )
    assert response.status_code == 200
    [event] = (
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == AuditAction.USER_PROFILE_UPDATED.value
            )
        )
    ).all()
    assert event.actor_user_id == admin_account.id
    assert event.details["after"]["position"] == "Ведущий инженер"


async def test_department_of_other_company_cannot_be_chosen(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    other = await _other_company(session)
    with tenant_scope(other.id):
        foreign = Department(name="Чужой отдел")
        session.add(foreign)
        await session.commit()
    response = await api.patch(
        f"/api/v1/people/{account.id}",
        json={"department_id": str(foreign.id)},
        headers=bearer(account),
    )
    assert response.status_code == 404


# --- отделы --------------------------------------------------------------------


async def test_departments_crud_by_admin(
    api: httpx.AsyncClient, account: User, admin_account: User
) -> None:
    admin = bearer(admin_account)
    sales = await _department(api, admin_account, "  Отдел   продаж ")
    await api.patch(
        f"/api/v1/people/{account.id}",
        json={"department_id": sales},
        headers=bearer(account),
    )

    listed = await api.get("/api/v1/departments", headers=bearer(account))
    assert listed.status_code == 200
    # Выбрал сам — ждёт подтверждения администратора (ТЗ §7).
    assert listed.json() == [
        {"id": sales, "name": "Отдел продаж", "members": 1, "unconfirmed": 1}
    ]

    duplicate = await api.post(
        "/api/v1/departments", json={"name": "отдел ПРОДАЖ"}, headers=admin
    )
    assert duplicate.status_code == 409

    renamed = await api.patch(
        f"/api/v1/departments/{sales}", json={"name": "Продажи"}, headers=admin
    )
    assert renamed.json() == {
        "id": sales,
        "name": "Продажи",
        "members": 1,
        "unconfirmed": 1,
    }

    deleted = await api.delete(f"/api/v1/departments/{sales}", headers=admin)
    assert deleted.status_code == 204
    person = await api.get(f"/api/v1/people/{account.id}", headers=bearer(account))
    assert person.json()["department"] is None


async def test_employee_cannot_manage_departments(
    api: httpx.AsyncClient, account: User
) -> None:
    response = await api.post(
        "/api/v1/departments", json={"name": "Свой отдел"}, headers=bearer(account)
    )
    assert response.status_code == 403


async def test_departments_are_isolated_between_companies(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    await _department(api, admin_account, "Продажи")
    other = await _other_company(session)
    with tenant_scope(other.id):
        other_admin = make_user(
            email="admin@other.com", role=UserRole.ADMIN, tenant_id=other.id
        )
        assert other_admin.account is not None
        enable_test_totp(other_admin.account)
        session.add(other_admin)
        await session.commit()

    listed = await api.get("/api/v1/departments", headers=bearer(other_admin))
    assert listed.json() == []
    # То же название в другой компании — можно.
    same = await api.post(
        "/api/v1/departments", json={"name": "Продажи"}, headers=bearer(other_admin)
    )
    assert same.status_code == 201


async def test_directory_is_alphabetical_with_nameless_last(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    session.add_all(
        [
            make_user(email="yakov@test.com", full_name="Яков Яковлев"),
            make_user(email="boris@test.com", full_name="Борис Акулов"),
        ]
    )
    await session.commit()

    response = await api.get("/api/v1/people", headers=bearer(account))

    # Учётка worker@test.com — без имени (до 03.10): в конце, а не первой.
    assert [p["email"] for p in response.json()] == [
        "boris@test.com",
        "yakov@test.com",
        "worker@test.com",
    ]
