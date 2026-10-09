"""Живая проверка вида `outline` (режим organization): официальный образ
outlinewiki/outline в Docker, вход через dex (OIDC), API-ключ
администратора, группа, открытая и закрытые коллекции, участник
вложенного документа.

Как поднять стенд — tests/live/outline/README.md (compose, вход
пользователей и ключ — login.mjs, seed.py). Без переменных — пропуск:

    OUTLINE_URL=https://127.0.0.1:8447/ OUTLINE_TOKEN=… \\
    DAV_STAND_CA=~/dav-tls/cert.pem \\
        uv run pytest tests/live/test_outline_live.py -q

OUTLINE_MEMBER_TOKEN (ключ обычного участника) — для проверки, что ключ
не администратора не принимается; без него этот тест пропускается.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.base import AdapterAuthError, FetchedMarkdown
from corp_ed.connectors.outline.adapter import (
    OUTLINE_SPEC,
    OutlineAdapter,
    build_adapter,
)
from corp_ed.connectors.registry import default_registry
from corp_ed.core import outbound
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.secrets import SecretBox
from corp_ed.domain.models import Connector, Tenant, User
from corp_ed.domain.types import (
    ConnectorMode,
    MaterialVisibility,
    SyncRunStatus,
    SyncTrigger,
)
from corp_ed.services.connector_sync_service import ConnectorSyncService, SyncOutcome
from tests.live.dav_stand import CA, _check, local_stand, stand_http
from tests.live.oc_family import make_people, materials, readers
from tests.live.outline.seed import EXPECTED, PATHS, TEXT, USERS, OutlineApi

URL = os.environ.get("OUTLINE_URL", "")
TOKEN = os.environ.get("OUTLINE_TOKEN", "")
MEMBER_TOKEN = os.environ.get("OUTLINE_MEMBER_TOKEN", "")
KEY = Fernet.generate_key().decode()

pytestmark = pytest.mark.skipif(
    not (URL and TOKEN and CA),
    reason="нужны OUTLINE_URL, OUTLINE_TOKEN и DAV_STAND_CA (стенд Outline в Docker)",
)


def _email(login: str) -> str:
    return f"{login}@example.com"


def _settings() -> ConnectorSettings:
    return ConnectorSettings(secrets_keys=KEY)  # type: ignore[arg-type]


def _adapter(raw: object, token: str = TOKEN) -> OutlineAdapter:
    return build_adapter(
        OUTLINE_SPEC,
        {"base_url": URL},
        {"token": token},
        outbound.OutboundClient(raw),  # type: ignore[arg-type]
        _settings(),
    )


async def test_listing_mirrors_outline_permissions() -> None:
    with local_stand(URL):
        async with stand_http() as raw:
            adapter = _adapter(raw)
            await adapter.check()
            documents = {d.title: d async for d in adapter.list(["documents"])}

    for title, allowed in EXPECTED.items():
        document = documents[title]
        if allowed is None:
            assert document.visibility is MaterialVisibility.TENANT, title
            assert document.allowed_emails == frozenset(), title
        else:
            assert document.visibility is MaterialVisibility.RESTRICTED, title
            assert document.allowed_emails == {_email(u) for u in allowed}, title
        assert document.path == PATHS[title], title
        assert document.url.startswith(f"{URL}doc/"), title


async def test_documents_come_as_markdown() -> None:
    with local_stand(URL):
        async with stand_http() as raw:
            adapter = _adapter(raw)
            documents = {d.title: d async for d in adapter.list(["documents"])}
            fetched = {
                title: await adapter.fetch(documents[title], max_bytes=1_000_000)
                for title in EXPECTED
            }
    for title, content in fetched.items():
        assert isinstance(content, FetchedMarkdown)
        assert content.markdown.startswith(f"# {title}"), title
        assert TEXT[title] in content.markdown, title


async def test_wrong_key_is_an_auth_error() -> None:
    """Неверный ключ — ошибка учётных данных подключения: у видов с
    токеном её код — unauthorized («Источник не принял токен»)."""
    with local_stand(URL):
        async with stand_http() as raw:
            with pytest.raises(AdapterAuthError) as caught:
                await _adapter(raw, "ol_api_not_a_real_key_0000000000000000").check()
    assert caught.value.code == "unauthorized"


@pytest.mark.skipif(not MEMBER_TOKEN, reason="нужен OUTLINE_MEMBER_TOKEN")
async def test_member_key_is_refused() -> None:
    """Без почт (их видит только администратор) закрытые документы не
    увидел бы никто — ключ участника не принимается сразу."""
    with local_stand(URL):
        async with stand_http() as raw:
            with pytest.raises(AdapterAuthError) as caught:
                await _adapter(raw, MEMBER_TOKEN).check()
    assert caught.value.code == "admin_required"


# --- синхронизация ------------------------------------------------------------


@pytest.fixture
async def people(session: AsyncSession, tenant_ctx: Tenant) -> dict[str, User]:
    """Сотрудники kronto с той же почтой; admin в kronto нет."""
    return await make_people(session, USERS)


async def _connector(session: AsyncSession) -> Connector:
    connector = Connector(
        kind="outline",
        name="Outline (стенд)",
        mode=ConnectorMode.ORGANIZATION.value,
        modules=["documents"],
        config={"base_url": URL},
        credentials=SecretBox([KEY]).encrypt({"token": TOKEN}),
    )
    session.add(connector)
    await session.commit()
    return connector


async def _sync(
    session_maker: async_sessionmaker[AsyncSession], connector: Connector
) -> SyncOutcome:
    with local_stand(URL):
        async with stand_http() as raw:
            service = ConnectorSyncService(
                session_maker,
                outbound.OutboundClient(raw),
                default_registry(_settings()),
                SecretBox([KEY]),
                _settings(),
            )
            outcome = await service.run(
                connector.tenant_id, connector.id, trigger=SyncTrigger.MANUAL
            )
    assert outcome is not None
    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
    assert outcome.stats.failed == 0, outcome
    return outcome


@pytest.fixture
def outline() -> Iterator[OutlineApi]:
    api = OutlineApi(URL, TOKEN)
    yield api
    api.close()


async def test_sync_mirrors_access_and_follows_changes(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    people: dict[str, User],
    outline: OutlineApi,
) -> None:
    connector = await _connector(session)

    await _sync(session_maker, connector)

    by_title = {m.title: m for m in await materials(session, connector)}
    for title, allowed in EXPECTED.items():
        material = by_title[title]
        assert TEXT[title] in material.content, title
        if allowed is None:
            assert material.visibility == MaterialVisibility.TENANT.value, title
            continue
        assert material.visibility == MaterialVisibility.RESTRICTED.value, title
        assert await readers(session, material, people) == allowed - {"admin"}, title

    # Правка текста, удаление документа и выход petr из группы hr —
    # права меняются без новой версии документа.
    users = outline.users()
    group = outline.group("hr")
    leave = outline.document("Отпуск")
    form = outline.document("Заявление")
    outline.call("documents.update", id=leave, text="Отпуск — 28 дней, версия 2.")
    outline.call("documents.delete", id=form)
    outline.call("groups.remove_user", id=group, userId=users["petr"])
    try:
        outcome = await _sync(session_maker, connector)

        assert outcome.stats.updated == 1
        assert outcome.stats.removed == 1
        by_title = {m.title: m for m in await materials(session, connector)}
        assert "версия 2" in by_title["Отпуск"].content
        assert "Заявление" not in by_title
        assert await readers(session, by_title["Зарплаты"], people) == {"maria"}
    finally:
        outline.call("documents.update", id=leave, text=TEXT["Отпуск"])
        outline.call("documents.restore", id=form)
        outline.call("groups.add_user", id=group, userId=users["petr"])


async def test_connector_check_records_without_the_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record = tmp_path / "outline"
    code = await _check(
        URL,
        CA,
        [
            "--kind",
            "outline",
            "--config",
            f"base_url={URL}",
            "--credential",
            f"token={TOKEN}",
            "--fetch",
            "2",
            "--record",
            str(record),
        ],
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "check: ok" in out
    dumped = "\n".join(f.read_text(encoding="utf-8") for f in record.glob("*.json"))
    assert "documents.list" in dumped
    assert TOKEN not in dumped
