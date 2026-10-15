"""Живая проверка вида `dokuwiki` (режим organization): официальный образ
dokuwiki/dokuwiki в Docker, пользователи и группы authplain, ACL с
открытым пространством, закрытыми для групп и запретом на страницу для
одного пользователя, страницы и медиафайлы.

Как поднять стенд — tests/live/dokuwiki/README.md (compose, токены,
seed.py). Без переменных — пропуск:

    DOKUWIKI_URL=https://127.0.0.1:8490/ DOKUWIKI_TOKEN=… \\
    DAV_STAND_CA=~/dav-tls/cert.pem \\
        uv run pytest tests/live/test_dokuwiki_live.py -q

DOKUWIKI_MEMBER_TOKEN (токен обычного пользователя) — для проверки, что
такой ключ не принимается; без него этот тест пропускается.
"""

import base64
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.base import AdapterAuthError, FetchedFile, FetchedPage
from corp_ed.connectors.dokuwiki import register
from corp_ed.connectors.dokuwiki.adapter import SPEC, DokuWikiAdapter, build_adapter
from corp_ed.connectors.registry import AdapterRegistry
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
from tests.live.dav_stand import CA, PASSWORD, _check, local_stand, stand_http
from tests.live.dokuwiki.seed import (
    ALL,
    EXPECTED,
    MEDIA,
    PAGES,
    TEXT,
    TITLES,
    USERS,
    DokuWikiApi,
)
from tests.live.oc_family import make_people, materials, readers

URL = os.environ.get("DOKUWIKI_URL", "")
TOKEN = os.environ.get("DOKUWIKI_TOKEN", "")
MEMBER_TOKEN = os.environ.get("DOKUWIKI_MEMBER_TOKEN", "")
KEY = Fernet.generate_key().decode()
USER_LIST = "\n".join(f"{login} {login}@example.com" for login in sorted(ALL))

pytestmark = pytest.mark.skipif(
    not (URL and TOKEN and CA),
    reason="нужны DOKUWIKI_URL, DOKUWIKI_TOKEN и DAV_STAND_CA (стенд DokuWiki)",
)


def _email(login: str) -> str:
    return f"{login}@example.com"


def _settings() -> ConnectorSettings:
    return ConnectorSettings(secrets_keys=KEY)  # type: ignore[arg-type]


def _registry() -> AdapterRegistry:
    """Вид ещё не в default_registry (подключает интегратор)."""
    registry = AdapterRegistry(enabled_preview_kinds=frozenset({"dokuwiki"}))
    register(registry, _settings())
    return registry


def _adapter(raw: object, credentials: dict[str, str] | None = None) -> DokuWikiAdapter:
    return build_adapter(
        SPEC,
        {"base_url": URL, "users": USER_LIST},
        credentials or {"token": TOKEN},
        outbound.OutboundClient(raw),  # type: ignore[arg-type]
        _settings(),
    )


async def _listing(
    credentials: dict[str, str] | None = None, *, per_item: bool = False
) -> dict[str, Any]:
    with local_stand(URL):
        async with stand_http() as raw:
            adapter = _adapter(raw, credentials)
            await adapter.check()
            if per_item:
                # Без таблицы правил — каждый документ проверяется сам.
                async def no_table() -> None:
                    return None

                adapter._exact_scopes = no_table  # type: ignore[method-assign]
            return {d.external_id: d async for d in adapter.list(["pages", "media"])}


def _assert_expected(documents: dict[str, Any]) -> None:
    for external_id, allowed in EXPECTED.items():
        document = documents[external_id]
        assert document.visibility is MaterialVisibility.RESTRICTED, external_id
        assert document.allowed_emails == {_email(u) for u in allowed}, external_id


async def test_listing_mirrors_dokuwiki_acl() -> None:
    documents = await _listing()
    _assert_expected(documents)
    for page, title in TITLES.items():
        document = documents[f"page:{page}"]
        assert document.title == title, page
        assert document.url == f"{URL}doku.php?id={page}", page
        assert document.version.isdigit(), page
    assert documents["media:hr:otpuska.docx"].path == "hr"
    assert (
        documents["media:hr:otpuska.docx"].size
        == MEDIA["hr:otpuska.docx"].stat().st_size
    )


async def test_grouping_by_namespace_matches_per_item_checks() -> None:
    """Группировка по пространствам даёт ровно то же, что aclCheck на
    каждый документ: проверено на самой DokuWiki, включая запрет на
    одну страницу пространства."""
    grouped = await _listing()
    single = await _listing(per_item=True)
    assert set(grouped) == set(single)
    for external_id, document in grouped.items():
        assert document.allowed_emails == single[external_id].allowed_emails, (
            external_id
        )


async def test_login_and_password_work_too() -> None:
    documents = await _listing({"login": "admin", "password": PASSWORD})
    _assert_expected(documents)


async def test_documents_come_as_html_and_files() -> None:
    with local_stand(URL):
        async with stand_http() as raw:
            adapter = _adapter(raw)
            documents = {
                d.external_id: d async for d in adapter.list(["pages", "media"])
            }
            pages = {
                page: await adapter.fetch(
                    documents[f"page:{page}"], max_bytes=1_000_000
                )
                for page in PAGES
            }
            files = {
                media: await adapter.fetch(
                    documents[f"media:{media}"], max_bytes=10_000_000
                )
                for media in MEDIA
            }
    for page, content in pages.items():
        assert isinstance(content, FetchedPage)
        assert TEXT[page] in content.html, page
        assert TITLES[page] in content.html, page
    for media, content in files.items():
        assert isinstance(content, FetchedFile)
        assert content.data == MEDIA[media].read_bytes(), media
        assert content.filename == media.rpartition(":")[2]


async def test_wrong_token_is_an_auth_error() -> None:
    with local_stand(URL):
        async with stand_http() as raw:
            with pytest.raises(AdapterAuthError) as caught:
                await _adapter(raw, {"token": "not.a.token"}).check()
            assert caught.value.code == "unauthorized"
            with pytest.raises(AdapterAuthError) as caught:
                await _adapter(raw, {"login": "admin", "password": "wrong"}).check()
            assert caught.value.code == "unauthorized"


@pytest.mark.skipif(not MEMBER_TOKEN, reason="нужен DOKUWIKI_MEMBER_TOKEN")
async def test_member_token_is_refused() -> None:
    """Чужие права проверяет только суперпользователь — ключ обычного
    пользователя не принимается сразу."""
    with local_stand(URL):
        async with stand_http() as raw:
            with pytest.raises(AdapterAuthError) as caught:
                await _adapter(raw, {"token": MEMBER_TOKEN}).check()
    assert caught.value.code == "admin_required"


# --- синхронизация ------------------------------------------------------------


@pytest.fixture
async def people(session: AsyncSession, tenant_ctx: Tenant) -> dict[str, User]:
    """Сотрудники kronto с той же почтой; admin в kronto нет."""
    return await make_people(session, USERS)


async def _connector(session: AsyncSession) -> Connector:
    connector = Connector(
        kind="dokuwiki",
        name="DokuWiki (стенд)",
        mode=ConnectorMode.ORGANIZATION.value,
        modules=["pages", "media"],
        config={"base_url": URL, "users": USER_LIST},
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
                _registry(),
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
def dokuwiki() -> Iterator[DokuWikiApi]:
    api = DokuWikiApi(URL)
    yield api
    api.close()


async def test_sync_mirrors_access_and_follows_changes(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    people: dict[str, User],
    dokuwiki: DokuWikiApi,
) -> None:
    connector = await _connector(session)

    await _sync(session_maker, connector)

    by_id = {m.external_id: m for m in await materials(session, connector)}
    for external_id, allowed in EXPECTED.items():
        material = by_id[external_id]
        assert material.visibility == MaterialVisibility.RESTRICTED.value
        assert await readers(session, material, people) == allowed - {"admin"}, (
            external_id
        )
    assert TEXT["hr:salaries"] in by_id["page:hr:salaries"].content
    assert "отпуск" in by_id["media:hr:otpuska.docx"].content.lower()

    # Правка страницы, удаление страницы и файла, снятие запрета petr —
    # права меняются без новой ревизии страницы.
    dokuwiki.save_page("public:vacation", PAGES["public:vacation"] + "\nВерсия 2.")
    dokuwiki.save_page("sales:plan", "")
    dokuwiki.call("core.deleteMedia", media="public:zakupki.docx")
    dokuwiki.call("plugin.acl.delAcl", scope="hr:salaries", user="petr")
    try:
        outcome = await _sync(session_maker, connector)

        assert outcome.stats.updated == 1
        assert outcome.stats.removed == 2
        by_id = {m.external_id: m for m in await materials(session, connector)}
        assert "Версия 2." in by_id["page:public:vacation"].content
        assert "page:sales:plan" not in by_id
        assert "media:public:zakupki.docx" not in by_id
        assert await readers(session, by_id["page:hr:salaries"], people) == {
            "maria",
            "petr",
        }
    finally:
        dokuwiki.save_page("public:vacation", PAGES["public:vacation"])
        dokuwiki.save_page("sales:plan", PAGES["sales:plan"])
        dokuwiki.save_media(
            "public:zakupki.docx", MEDIA["public:zakupki.docx"].read_bytes()
        )
        dokuwiki.call("plugin.acl.addAcl", scope="hr:salaries", user="petr", level=0)


async def test_connector_check_records_without_the_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import corp_ed.cli as cli

    record = tmp_path / "dokuwiki"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli, "default_registry", lambda settings: _registry())
        code = await _check(
            URL,
            CA,
            [
                "--kind",
                "dokuwiki",
                "--config",
                f"base_url={URL}",
                "--config",
                f"users={USER_LIST}",
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
    assert "core.aclCheck" in dumped
    assert TOKEN not in dumped
    assert base64.b64encode(PASSWORD.encode()).decode() not in dumped
