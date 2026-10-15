"""Клиент агента сетевых папок (agent/, kronto_agent.kronto) против
настоящих ручек /api/v1/agent: протокол сходится с обеих сторон
(docs/SMB-AGENT.md §3) — формы meta, multipart, ответы, завершение.

Клиент агента синхронный, ручки — асинхронные: клиент работает в потоке,
а его запросы исполняются в цикле теста через ASGITransport.
"""

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.secrets import SecretBox
from corp_ed.domain.models import Connector, Material, Tenant, User
from corp_ed.domain.types import ConnectorMode
from corp_ed.main import app
from corp_ed.services.agent_ingest_service import issue_token
from tests.api.conftest import TEST_SECRETS_KEY

AGENT_SRC = Path(__file__).resolve().parents[2] / "agent" / "src"
sys.path.insert(0, str(AGENT_SRC))
kronto = pytest.importorskip("kronto_agent.kronto")


class _Bridge(httpx.BaseTransport):
    """Синхронный транспорт → ASGI-приложение в цикле теста."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.inner = httpx.ASGITransport(app=app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        forwarded = httpx.Request(
            request.method, request.url, headers=request.headers, content=body
        )

        async def call() -> httpx.Response:
            response = await self.inner.handle_async_request(forwarded)
            content = await response.aread()
            return httpx.Response(
                response.status_code, headers=response.headers, content=content
            )

        return asyncio.run_coroutine_threadsafe(call(), self.loop).result(60)


@pytest.fixture
async def agent_key(session: AsyncSession, tenant_ctx: Tenant) -> str:
    connector = Connector(
        tenant_id=tenant_ctx.id,
        kind="smb_agent",
        name="fs01",
        mode=ConnectorMode.ORGANIZATION.value,
        modules=["files"],
        config={},
    )
    session.add(connector)
    await session.commit()
    key = issue_token(connector, SecretBox([TEST_SECRETS_KEY]), datetime.now(UTC))
    await session.commit()
    return key


def _meta(external_id: str, emails: list[str], version: str = "1-10") -> dict[str, Any]:
    # Ровно как собирает агент (kronto_agent.sync): путь с обратными
    # слэшами, время в Z, заголовок — имя без расширения.
    return {
        "external_id": external_id,
        "version": version,
        "title": "Положение о командировках",
        "path": "\\\\fs01\\docs\\Кадры\\Положение о командировках.md",
        "filename": "Положение о командировках.md",
        "modified_at": "2026-10-15T09:00:00Z",
        "allowed_emails": emails,
    }


async def test_agent_client_full_cycle(
    api: httpx.AsyncClient,
    session: AsyncSession,
    agent_key: str,
    admin: User,
    employee: User,
) -> None:
    client = kronto.KrontoClient(
        "http://test",
        agent_key,
        transport=_Bridge(asyncio.get_running_loop()),
        sleep=lambda _: None,
    )
    doc = "\\\\fs01\\docs\\кадры\\положение о командировках.md"
    other = "\\\\fs01\\docs\\архив\\старое.md"
    text = "Командировки оформляются заранее.".encode()

    run = await asyncio.to_thread(client.start_run)
    for external_id, emails in ((doc, ["admin@test.com"]), (other, [])):
        meta = {"run_id": run, **_meta(external_id, emails)}
        result = await asyncio.to_thread(
            client.put_document, meta, meta["filename"], text
        )
        assert result.status == "added", result
    await asyncio.to_thread(client.finish, run, True, {"files_seen": 2})

    state = await asyncio.to_thread(client.state)
    assert state == {doc: "1-10", other: "1-10"}

    # Второй обход: файл тот же — только права; второго файла больше нет.
    run = await asyncio.to_thread(client.start_run)
    unknown = await asyncio.to_thread(
        client.access,
        run,
        [
            {"external_id": doc, "allowed_emails": ["employee@test.com"]},
            {"external_id": "\\\\fs01\\docs\\новое.md", "allowed_emails": []},
        ],
    )
    assert unknown == ["\\\\fs01\\docs\\новое.md"]
    await asyncio.to_thread(client.finish, run, True, {})
    client.close()

    session.expire_all()
    remaining = (
        await session.scalars(select(Material.external_id).order_by(Material.title))
    ).all()
    assert remaining == [doc]


async def test_agent_client_sees_rejected_key(api: httpx.AsyncClient) -> None:
    client = kronto.KrontoClient(
        "http://test",
        "kag_00000000-0000-0000-0000-000000000000_"
        "00000000-0000-0000-0000-000000000000_" + "x" * 43,
        transport=_Bridge(asyncio.get_running_loop()),
        sleep=lambda _: None,
    )
    with pytest.raises(kronto.KrontoAuthError):
        await asyncio.to_thread(client.start_run)
    client.close()
