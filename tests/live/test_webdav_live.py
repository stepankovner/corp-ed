"""Живая проверка вида `webdav` (сетевой диск, NAS): Apache httpd с mod_dav
в Docker, два пользователя и папка, закрытая от одного из них.

Как поднять стенд — tests/live/webdav/README.md (сертификат, compose,
seed.py). Без переменных — пропуск:

    WEBDAV_URL=https://127.0.0.1:8443/dav/ DAV_STAND_CA=~/dav-tls/cert.pem \\
        uv run pytest tests/live/test_webdav_live.py -q

Стенд слушает https://127.0.0.1 с самоподписанным сертификатом: адаптер
WebDAV ходит только по https (почему не http — tests/live/dav_stand.py),
проверка адреса пропускает один этот стенд — остальное как в бою.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.base import AdapterAuthError, AdapterError, FetchedFile
from corp_ed.connectors.webdav.adapter import WebDavAdapter, build_adapter
from corp_ed.core import outbound
from corp_ed.core.security import hash_password
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Material, Tenant, User, UserRole
from corp_ed.domain.types import GrantStatus, MaterialVisibility, SyncRunStatus
from corp_ed.ingest.extract import detect_format, extract
from corp_ed.repositories.connector_repository import GrantRepository
from tests.factories import make_user
from tests.live.dav_stand import (
    CA,
    PASSWORD,
    DavSeeder,
    PerUserStand,
    local_stand,
    stand_http,
)
from tests.live.webdav.seed import EXPECTED, TEXT, USERS, seed
from tests.test_connector_sync import access_of, materials_of

URL = os.environ.get("WEBDAV_URL", "")

pytestmark = pytest.mark.skipif(
    not (URL and CA),
    reason="нужны WEBDAV_URL и DAV_STAND_CA (стенд WebDAV в Docker)",
)


def _adapter(raw: object, login: str, password: str = PASSWORD) -> WebDavAdapter:
    stand = PerUserStand(URL)
    return build_adapter(
        "webdav",
        {"server": URL},
        {"login": login, "password": password},
        outbound.OutboundClient(raw),  # type: ignore[arg-type]
        stand.settings,
    )


@pytest.fixture
def owner() -> Iterator[DavSeeder]:
    """ivan — правит дерево; после теста дерево возвращается к seed.py."""
    seeder = DavSeeder(URL, "ivan", PASSWORD)
    yield seeder
    seed(URL)
    seeder.close()


async def _titles(login: str) -> dict[str, str]:
    with local_stand(URL):
        async with stand_http() as raw:
            adapter = _adapter(raw, login)
            await adapter.check()
            return {d.title: d.path async for d in adapter.list(["files"])}


async def test_each_user_lists_what_the_server_lets_them_read() -> None:
    for login in USERS:
        listed = await _titles(login)
        expected = {title for title, readers in EXPECTED.items() if login in readers}
        assert set(listed) == expected, login
    assert (await _titles("ivan"))["Правила.md"] == "Общие/Политики/Удалённая работа"


async def test_every_format_reaches_the_extractor() -> None:
    with local_stand(URL):
        async with stand_http() as raw:
            adapter = _adapter(raw, "ivan")
            documents = {d.title: d async for d in adapter.list(["files"])}
            fetched = {
                title: await adapter.fetch(document, max_bytes=1_000_000)
                for title, document in documents.items()
            }
    for title, text in TEXT.items():
        file = fetched[title]
        assert isinstance(file, FetchedFile)
        detected = detect_format(file.filename, file.data)
        assert text in extract(detected.format, file.data), title
        assert documents[title].url.startswith(URL)


async def test_wrong_password_is_auth_failed() -> None:
    with local_stand(URL):
        async with stand_http() as raw:
            adapter = _adapter(raw, "maria", "not-the-password")
            with pytest.raises(AdapterAuthError) as caught:
                await adapter.check()
    assert caught.value.code == "auth_failed"


async def test_folder_deeper_than_the_limit_stops_the_listing(
    owner: DavSeeder,
) -> None:
    """32 уровня от корня — обходятся; 33-й — tree_too_deep, а не молча
    обрезанный листинг."""
    deep = "/".join(["Глубоко", *(str(level) for level in range(2, 33))])
    owner.put(f"{deep}/На дне.txt", "Тридцать второй уровень.".encode())
    try:
        assert "На дне.txt" in await _titles("ivan")
        owner.put(f"{deep}/33/Ниже дна.txt", b"33")
        with pytest.raises(AdapterError) as caught:
            await _titles("ivan")
        assert caught.value.code == "tree_too_deep"
    finally:
        owner.delete("Глубоко")


# --- синхронизация ------------------------------------------------------------


@pytest.fixture
async def people(session: AsyncSession, tenant_ctx: Tenant) -> dict[str, User]:
    users = {}
    for login in USERS:
        user = make_user(
            email=f"{login}@example.com",
            full_name=login,
            role=UserRole.EMPLOYEE,
            hashed_password=hash_password("Password-1234"),
        )
        session.add(user)
        users[login] = user
    await session.commit()
    return users


async def _materials(
    session: AsyncSession, tenant: Tenant, connector: object
) -> list[Material]:
    with tenant_scope(tenant.id):
        return list((await materials_of(session, connector)).values())  # type: ignore[arg-type]


async def _who(
    session: AsyncSession, material: Material, people: dict[str, User]
) -> set[str]:
    names = {user.id: login for login, user in people.items()}
    with tenant_scope(material.tenant_id):
        return {names[i] for i in await access_of(session, material)}


async def test_sync_mirrors_access_and_follows_changes(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    people: dict[str, User],
    owner: DavSeeder,
) -> None:
    stand = PerUserStand(URL)
    connector = await stand.connector(session, "webdav", {"server": URL})
    for login, user in people.items():
        await stand.grant(
            session, connector, user.id, {"login": login, "password": PASSWORD}
        )

    outcome = await stand.sync(session_maker, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
    assert outcome.stats.failed == 0, outcome
    # Сквозного номера файла у NAS нет: общий файл — копия на каждого,
    # кто его видит, и каждая — только своему.
    copies = sum(len(readers) for readers in EXPECTED.values())
    assert outcome.stats.added == copies
    assert outcome.stats.skipped_formats == {".png": len(USERS)}
    materials = await _materials(session, tenant_ctx, connector)
    for title, readers in EXPECTED.items():
        found = [m for m in materials if m.title == title]
        assert len(found) == len(readers), title
        owners = set()
        for material in found:
            assert material.visibility == MaterialVisibility.RESTRICTED.value
            who = await _who(session, material, people)
            assert len(who) == 1, title
            owners |= who
            assert TEXT[title] in material.content, title
        assert owners == readers, title

    owner.put("Общие/Глоссарий.txt", "Глоссарий, версия 2: ОМС.\n".encode())
    owner.delete("Общие/Политики/Security policy.pdf")
    outcome = await stand.sync(session_maker, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
    assert outcome.stats.updated == len(USERS)
    assert outcome.stats.removed == len(USERS)
    materials = await _materials(session, tenant_ctx, connector)
    assert not [m for m in materials if m.title == "Security policy.pdf"]
    glossary = [m for m in materials if m.title == "Глоссарий.txt"]
    assert all("версия 2" in m.content for m in glossary) and len(glossary) == 2


async def test_wrong_password_expires_only_that_grant(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    people: dict[str, User],
) -> None:
    stand = PerUserStand(URL)
    connector = await stand.connector(session, "webdav", {"server": URL})
    await stand.grant(
        session, connector, people["ivan"].id, {"login": "ivan", "password": PASSWORD}
    )
    bad = await stand.grant(
        session,
        connector,
        people["maria"].id,
        {"login": "maria", "password": "not-the-password"},
    )

    outcome = await stand.sync(session_maker, connector)

    assert outcome.stats.grants_expired == 1
    assert outcome.stats.added == len(
        [title for title, readers in EXPECTED.items() if "ivan" in readers]
    )
    with tenant_scope(tenant_ctx.id):
        grant = await GrantRepository(session).get_by_id(bad.id)
        assert grant is not None
        await session.refresh(grant)
        assert grant.status == GrantStatus.EXPIRED.value
        assert grant.error_code == "auth_failed"


async def test_too_deep_tree_fails_the_run_without_deleting(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    people: dict[str, User],
    owner: DavSeeder,
) -> None:
    stand = PerUserStand(URL)
    connector = await stand.connector(session, "webdav", {"server": URL})
    await stand.grant(
        session, connector, people["ivan"].id, {"login": "ivan", "password": PASSWORD}
    )
    first = await stand.sync(session_maker, connector)
    assert first.status is SyncRunStatus.SUCCEEDED, first

    owner.put("/".join(["Глубоко", *(str(n) for n in range(2, 34))]) + "/x.txt", b"x")
    try:
        outcome = await stand.sync(session_maker, connector)
    finally:
        owner.delete("Глубоко")

    assert outcome.status is SyncRunStatus.FAILED, outcome
    assert outcome.stats.removed == 0
    assert len(await _materials(session, tenant_ctx, connector)) == first.stats.added


# --- cli connector-check -----------------------------------------------------------


async def test_connector_check_records_without_the_password(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.live.dav_stand import _check

    record = tmp_path / "webdav"
    code = await _check(
        URL,
        CA,
        [
            "--kind",
            "webdav",
            "--config",
            f"server={URL}",
            "--credential",
            "login=ivan",
            "--credential",
            f"password={PASSWORD}",
            "--fetch",
            "2",
            "--record",
            str(record),
        ],
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "check: ok" in out
    assert f"документов: {len(EXPECTED)}" in out
    dumped = "\n".join(f.read_text(encoding="utf-8") for f in record.glob("*.json"))
    assert "PROPFIND" in dumped
    assert PASSWORD not in dumped
