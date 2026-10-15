"""Ручки агента сетевых папок (docs/SMB-AGENT.md §3): ключ, документы,
права из NTFS, удаление после полного обхода."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.connectors.registry import AdapterRegistry, default_registry
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.core.secrets import SecretBox
from corp_ed.domain.models import (
    Connector,
    ConnectorSyncRun,
    Material,
    MaterialAccess,
    Tenant,
    User,
)
from corp_ed.domain.types import ConnectorMode, ConnectorStatus, SyncTrigger
from corp_ed.services.agent_ingest_service import issue_token
from corp_ed.services.connector_sync_service import ConnectorSyncService
from tests.api.conftest import TEST_SECRETS_KEY

URL = "/api/v1/agent"


@pytest.fixture
async def connector(session: AsyncSession, tenant_ctx: Tenant) -> Connector:
    row = Connector(
        tenant_id=tenant_ctx.id,
        kind="smb_agent",
        name="Файловый сервер",
        mode=ConnectorMode.ORGANIZATION.value,
        modules=["files"],
        config={},
    )
    session.add(row)
    await session.commit()
    return row


@pytest.fixture
async def token(session: AsyncSession, connector: Connector) -> str:
    raw = issue_token(connector, SecretBox([TEST_SECRETS_KEY]), datetime.now(UTC))
    await session.commit()
    return raw


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def start(api: httpx.AsyncClient, token: str) -> str:
    response = await api.post(f"{URL}/runs", headers=auth(token))
    assert response.status_code == 200, response.text
    return str(response.json()["run_id"])


async def put(
    api: httpx.AsyncClient,
    token: str,
    run_id: str,
    external_id: str,
    *,
    emails: list[str],
    version: str = "1",
    text: str = "Командировки оформляются заранее.",
    filename: str | None = None,
) -> httpx.Response:
    name = filename or f"{external_id}.md"
    meta = {
        "run_id": run_id,
        "external_id": external_id,
        "version": version,
        "title": external_id,
        "path": f"\\\\fs01\\docs\\{name}",
        "filename": name,
        "allowed_emails": emails,
    }
    return await api.put(
        f"{URL}/documents",
        headers=auth(token),
        data={"meta": json.dumps(meta)},
        files={"file": (name, text.encode(), "application/octet-stream")},
    )


async def viewers(session: AsyncSession, external_id: str) -> set[UUID]:
    material = await session.scalar(
        select(Material).where(Material.external_id == external_id)
    )
    assert material is not None
    rows = await session.scalars(
        select(MaterialAccess.user_id).where(MaterialAccess.material_id == material.id)
    )
    return set(rows)


async def test_full_run_sets_access_and_removes_untouched(
    api: httpx.AsyncClient,
    session: AsyncSession,
    token: str,
    admin: User,
    employee: User,
) -> None:
    run = await start(api, token)
    for external_id, emails in (
        ("kadry/polozhenie", ["ADMIN@test.com"]),
        ("obshchee/komandirovki", ["employee@test.com", "stranger@test.com"]),
        ("arkhiv/staroe", ["admin@test.com", "employee@test.com"]),
    ):
        response = await put(api, token, run, external_id, emails=emails)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "added"
    finished = await api.post(
        f"{URL}/runs/{run}/finish", headers=auth(token), json={"complete": True}
    )
    assert finished.json()["status"] == "succeeded"

    # Почта сравнивается без регистра; чужая почта — не сотрудник, пропущена.
    assert await viewers(session, "kadry/polozhenie") == {admin.id}
    assert await viewers(session, "obshchee/komandirovki") == {employee.id}

    state = await api.get(f"{URL}/state", headers=auth(token))
    assert {d["external_id"] for d in state.json()["documents"]} == {
        "kadry/polozhenie",
        "obshchee/komandirovki",
        "arkhiv/staroe",
    }

    # Второй обход: файл не менялся, права — да; «Архив» исчез с сервера.
    run = await start(api, token)
    same = await put(api, token, run, "kadry/polozhenie", emails=["admin@test.com"])
    assert same.json()["status"] == "unchanged"
    access = await api.post(
        f"{URL}/access",
        headers=auth(token),
        json={
            "run_id": run,
            "documents": [
                {"external_id": "obshchee/komandirovki", "allowed_emails": []},
                {"external_id": "novoe/neizvestnoe", "allowed_emails": []},
            ],
        },
    )
    assert access.json() == {"updated": 1, "unknown": ["novoe/neizvestnoe"]}
    finished = await api.post(
        f"{URL}/runs/{run}/finish",
        headers=auth(token),
        json={"complete": True, "stats": {"skipped_acl": 2, "note": "x"}},
    )
    body = finished.json()
    assert body["stats"]["removed"] == 1
    assert body["stats"]["agent_skipped_acl"] == 2
    assert "agent_note" not in body["stats"]
    assert await viewers(session, "obshchee/komandirovki") == set()
    session.expire_all()
    assert (
        await session.scalar(
            select(Material).where(Material.external_id == "arkhiv/staroe")
        )
        is None
    )


async def test_incomplete_run_deletes_nothing(
    api: httpx.AsyncClient, session: AsyncSession, token: str, admin: User
) -> None:
    run = await start(api, token)
    await put(api, token, run, "a", emails=["admin@test.com"])
    await api.post(
        f"{URL}/runs/{run}/finish", headers=auth(token), json={"complete": True}
    )
    run = await start(api, token)
    finished = await api.post(
        f"{URL}/runs/{run}/finish", headers=auth(token), json={"complete": False}
    )
    assert finished.json()["status"] == "partial"
    assert finished.json()["stats"]["removed"] == 0
    assert await session.scalar(select(Material).where(Material.external_id == "a"))


async def test_new_run_closes_the_open_one(
    api: httpx.AsyncClient, session: AsyncSession, token: str
) -> None:
    first = await start(api, token)
    await start(api, token)
    response = await put(api, token, first, "a", emails=[])
    assert response.status_code == 409
    old = await session.get(ConnectorSyncRun, UUID(first))
    assert old is not None
    await session.refresh(old)
    assert (old.status, old.error_code, old.trigger) == (
        "failed",
        "superseded",
        SyncTrigger.AGENT.value,
    )


@pytest.mark.parametrize(
    "header",
    [
        None,
        "Bearer nope",
        f"Bearer kag_{uuid4()}_{uuid4()}_{'x' * 43}",
    ],
)
async def test_bad_keys_are_refused(
    api: httpx.AsyncClient, token: str, header: str | None
) -> None:
    headers = {"Authorization": header} if header else {}
    assert (await api.post(f"{URL}/runs", headers=headers)).status_code == 401


async def test_reissued_key_revokes_the_old_one(
    api: httpx.AsyncClient, session: AsyncSession, connector: Connector, token: str
) -> None:
    fresh = issue_token(connector, SecretBox([TEST_SECRETS_KEY]), datetime.now(UTC))
    await session.commit()
    assert (await api.post(f"{URL}/runs", headers=auth(token))).status_code == 401
    assert (await api.post(f"{URL}/runs", headers=auth(fresh))).status_code == 200


async def test_key_of_another_connector_kind_is_refused(
    api: httpx.AsyncClient, session: AsyncSession, connector: Connector, token: str
) -> None:
    connector.kind = "webdav"
    await session.commit()
    assert (await api.post(f"{URL}/runs", headers=auth(token))).status_code == 401


async def test_stopped_connector_gets_403(
    api: httpx.AsyncClient, session: AsyncSession, connector: Connector, token: str
) -> None:
    connector.status = ConnectorStatus.PAUSED.value
    await session.commit()
    assert (await api.post(f"{URL}/runs", headers=auth(token))).status_code == 403


async def test_bad_documents(
    api: httpx.AsyncClient, token: str, session: AsyncSession
) -> None:
    run = await start(api, token)
    bad_meta = await api.put(
        f"{URL}/documents",
        headers=auth(token),
        data={"meta": "{not json"},
        files={"file": ("a.md", b"x", "text/markdown")},
    )
    assert bad_meta.status_code == 422
    unsupported = await put(
        api, token, run, "bin", emails=[], filename="a.exe", text="MZ\x90\x00"
    )
    assert unsupported.status_code == 415
    finished = await api.post(
        f"{URL}/runs/{run}/finish", headers=auth(token), json={"complete": True}
    )
    stats: dict[str, Any] = finished.json()["stats"]
    assert stats["failed"] == 1
    assert finished.json()["status"] == "partial"


def test_registry_knows_push_kind_but_hides_it() -> None:
    registry: AdapterRegistry = default_registry(ConnectorSettings())
    assert "smb_agent" in registry.push_kinds()
    assert registry.spec("smb_agent").base is False
    assert "smb_agent" not in {spec.kind for spec in registry.kinds()}


async def test_scheduled_sync_skips_push_kind(
    session_maker: Any, connector: Connector, tenant_ctx: Tenant
) -> None:
    service = ConnectorSyncService(
        session_maker,
        OutboundClient(httpx.AsyncClient()),
        default_registry(ConnectorSettings()),
        SecretBox([TEST_SECRETS_KEY]),
        ConnectorSettings(),
    )
    outcome = await service.run(
        tenant_ctx.id, connector.id, trigger=SyncTrigger.SCHEDULE
    )
    assert outcome is None
