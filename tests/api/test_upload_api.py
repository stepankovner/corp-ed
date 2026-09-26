"""Загрузка документов файлом, переименование и удаление."""

import hashlib
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.api.v1.endpoints import materials as materials_endpoint
from corp_ed.core.config import EMBEDDING_DIM, HttpSettings
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    AuditEvent,
    Chunk,
    IngestJob,
    Material,
    Tenant,
    User,
)
from tests.api.conftest import bearer
from tests.ingest import samples

DOCX = samples.docx(
    [("Положение об отпусках", "Heading1"), ("Отпуск — 28 дней.", None)]
)


async def _upload(
    api: httpx.AsyncClient,
    user: User,
    *,
    filename: str = "Polozhenie_v3_final.docx",
    data: bytes = DOCX,
    title: str | None = "Положение об отпусках",
) -> httpx.Response:
    form = {"title": title} if title is not None else {}
    return await api.post(
        "/api/v1/materials/upload",
        files={"file": (filename, data, "application/octet-stream")},
        data=form,
        headers=bearer(user),
    )


async def test_admin_uploads_docx(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    response = await _upload(api, admin_account)

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Положение об отпусках"
    assert body["source_filename"] == "Polozhenie_v3_final.docx"
    assert body["source_format"] == "docx"
    assert body["status"] == "pending"
    assert "content" not in body

    with tenant_scope(admin_account.tenant_id):
        material = (
            await session.execute(
                select(Material).where(Material.id == UUID(body["id"]))
            )
        ).scalar_one()
        await session.commit()
    assert "# Положение об отпусках" in material.content
    assert material.source_sha256 and len(material.source_sha256) == 64
    jobs = (await session.execute(select(IngestJob))).scalars().all()
    assert [job.material_id for job in jobs] == [material.id]


async def test_employee_cannot_upload(api: httpx.AsyncClient, account: User) -> None:
    assert (await _upload(api, account)).status_code == 403


async def test_anonymous_cannot_upload(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/api/v1/materials/upload",
        files={"file": ("a.docx", DOCX)},
        data={"title": "x"},
    )
    assert response.status_code == 401


async def test_unsupported_format_is_415_with_code(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    response = await _upload(api, admin_account, filename="old.doc", data=b"\xd0\xcf")

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_format"
    assert "docx" in response.json()["detail"]


async def test_disguised_file_is_rejected(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    response = await _upload(api, admin_account, filename="a.docx", data=b"%PDF-1.7")

    assert response.status_code == 422
    assert response.json()["code"] == "format_mismatch"


async def test_zip_bomb_is_rejected_before_parsing(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    response = await _upload(api, admin_account, data=samples.zip_bomb_docx())

    assert response.status_code == 422
    assert response.json()["code"] == "archive_too_large"


async def test_rejection_does_not_leak_parser_details(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    response = await _upload(
        api, admin_account, filename="a.pdf", data=b"%PDF-1.7 broken"
    )

    assert response.status_code == 422
    assert set(response.json()) == {"detail", "code"}
    assert "Traceback" not in response.text
    assert "mupdf" not in response.text.lower()


async def test_title_is_required(api: httpx.AsyncClient, admin_account: User) -> None:
    response = await _upload(api, admin_account, title=None)
    assert response.status_code == 422


async def test_duplicate_file_is_conflict(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    first = await _upload(api, admin_account)
    second = await _upload(api, admin_account, filename="copy.docx", title="Копия")

    assert second.status_code == 409
    assert second.json()["material_id"] == first.json()["id"]


async def test_same_file_in_other_company_is_allowed(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    """Дубликат ищется только в своей компании — хеш не выдаёт чужие файлы."""
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        session.add(
            Material(
                tenant_id=other.id,
                title="Чужой",
                content="x",
                source_sha256=hashlib.sha256(DOCX).hexdigest(),
            )
        )
        await session.commit()

    assert (await _upload(api, admin_account)).status_code == 201


async def test_oversized_upload_is_rejected(
    api: httpx.AsyncClient, admin_account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        materials_endpoint,
        "get_http_settings",
        lambda: HttpSettings(max_upload_bytes=100),
    )
    response = await _upload(api, admin_account)

    assert response.status_code == 422
    assert response.json()["code"] == "document_too_large"


async def test_upload_is_audited(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    response = await _upload(api, admin_account)

    event = (
        await session.execute(
            select(AuditEvent).where(AuditEvent.action == "material.created")
        )
    ).scalar_one()
    assert event.target_id == response.json()["id"]
    assert event.details["format"] == "docx"


# --- переименование и удаление ------------------------------------------------


async def test_rename_requeues_ingest(
    api: httpx.AsyncClient,
    admin_account: User,
    material: Material,
    session: AsyncSession,
) -> None:
    response = await api.patch(
        f"/api/v1/materials/{material.id}",
        json={"title": "Новое название"},
        headers=bearer(admin_account),
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Новое название"
    assert response.json()["status"] == "pending"
    assert len((await session.execute(select(IngestJob))).scalars().all()) == 1


async def test_delete_removes_material_and_chunks(
    api: httpx.AsyncClient,
    admin_account: User,
    material: Material,
    session: AsyncSession,
) -> None:
    session.add(
        Chunk(
            material_id=material.id,
            position=0,
            content="x",
            embedding=[0.1] * EMBEDDING_DIM,
            model="m",
            model_version="v",
        )
    )
    await session.commit()

    response = await api.delete(
        f"/api/v1/materials/{material.id}", headers=bearer(admin_account)
    )

    assert response.status_code == 204
    with tenant_scope(admin_account.tenant_id):
        assert (await session.execute(select(Chunk))).scalars().all() == []
        assert (await session.execute(select(Material))).scalars().all() == []
        await session.commit()


async def test_cannot_delete_other_company_material(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        foreign = Material(tenant_id=other.id, title="Чужой", content="x")
        session.add(foreign)
        await session.commit()

    response = await api.delete(
        f"/api/v1/materials/{foreign.id}", headers=bearer(admin_account)
    )

    assert response.status_code == 404
    with tenant_scope(other.id):
        assert (await session.execute(select(Material))).scalars().all() != []
        await session.commit()


async def test_employee_cannot_delete(
    api: httpx.AsyncClient, account: User, material: Material
) -> None:
    response = await api.delete(
        f"/api/v1/materials/{material.id}", headers=bearer(account)
    )
    assert response.status_code == 403
