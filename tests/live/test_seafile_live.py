"""Живая проверка вида `seafile` (SeafDAV): официальный образ
seafileltd/seafile-mc в Docker с MariaDB и memcached, три пользователя,
группа, библиотеки, расшаренные пользователю и группе.

Как поднять стенд — tests/live/seafile/README.md (сертификат, compose,
включить SeafDAV, seed.py). Без переменных — пропуск:

    SEAFILE_URL=https://127.0.0.1:8446/ DAV_STAND_CA=~/dav-tls/cert.pem \\
        uv run pytest tests/live/test_seafile_live.py -q

Обещание каталога, которое здесь проверяется: у Seafile сквозного номера
файла в WebDAV нет, поэтому общая библиотека индексируется копией на
каждого сотрудника, и каждая копия видна только ему.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.base import AdapterAuthError, AdapterError, FetchedFile
from corp_ed.connectors.webdav.adapter import WebDavAdapter, build_adapter
from corp_ed.core import outbound
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Connector, Tenant, User
from corp_ed.domain.types import GrantStatus, MaterialVisibility, SyncRunStatus
from corp_ed.ingest.extract import detect_format, extract
from corp_ed.repositories.connector_repository import GrantRepository
from tests.live.dav_stand import (
    CA,
    PASSWORD,
    DavSeeder,
    PerUserStand,
    _check,
    local_stand,
    stand_http,
)
from tests.live.oc_family import make_people, materials, readers
from tests.live.seafile.seed import (
    EXPECTED,
    FILES,
    USERS,
    SeafileApi,
    dav_root,
    email,
    user_ids,
)

URL = os.environ.get("SEAFILE_URL", "")
DAV = dav_root(URL) if URL else ""
KIND = "seafile"
WRONG = "not-the-password"

pytestmark = pytest.mark.skipif(
    not (URL and CA),
    reason="нужны SEAFILE_URL и DAV_STAND_CA (стенд Seafile в Docker)",
)


def _adapter(raw: object, login: str, password: str = PASSWORD) -> WebDavAdapter:
    return build_adapter(
        KIND,
        {"server": DAV},
        {"login": email(login), "password": password},
        outbound.OutboundClient(raw),  # type: ignore[arg-type]
        PerUserStand(URL).settings,
    )


async def _listing(login: str) -> dict[str, list[tuple[str, str]]]:
    """Название → [(external_id, путь)] в дереве пользователя."""
    listed: dict[str, list[tuple[str, str]]] = {}
    with local_stand(URL):
        async with stand_http() as raw:
            adapter = _adapter(raw, login)
            await adapter.check()
            async for document in adapter.list(["files"]):
                listed.setdefault(document.title, []).append(
                    (document.external_id, document.path)
                )
    return listed


@pytest.fixture
def ivan() -> Iterator[DavSeeder]:
    """Владелец библиотек; что тест поменял — вернёт сам в finally."""
    seeder = DavSeeder(DAV, email("ivan"), PASSWORD)
    yield seeder
    seeder.close()


async def test_shared_library_is_a_copy_for_each_employee() -> None:
    listings = {login: await _listing(login) for login in USERS}

    for (title, _), allowed in EXPECTED.items():
        for login in USERS:
            seen = listings[login].get(title, [])
            if login in allowed:
                assert seen, (login, title)
            elif title != "Отпуск.docx":
                assert not seen, (login, title)
    for title in ("Регламент.pdf", "Анкета.txt"):
        ids = {listings[login][title][0][0] for login in USERS}
        assert len(ids) == len(USERS), title
        assert all(i.startswith(f"{KIND}:u:") for i in ids), title
    assert "Схема.png" not in listings["ivan"]
    # Библиотека — первая папка пути.
    assert listings["petr"]["Анкета.txt"][0][1] == "Кадры/Глубже"


async def test_every_format_reaches_the_extractor() -> None:
    texts = {
        title: text for (title, text), allowed in EXPECTED.items() if "ivan" in allowed
    }
    with local_stand(URL):
        async with stand_http() as raw:
            adapter = _adapter(raw, "ivan")
            documents = {
                d.title: d async for d in adapter.list(["files"]) if d.title in texts
            }
            fetched = {
                title: await adapter.fetch(document, max_bytes=1_000_000)
                for title, document in documents.items()
            }
    for title, text in texts.items():
        file = fetched[title]
        assert isinstance(file, FetchedFile)
        detected = detect_format(file.filename, file.data)
        assert text in extract(detected.format, file.data), title
        # Веб-ссылки по WebDAV нет: ссылка — сам файл в SeafDAV.
        assert documents[title].url.startswith(DAV), title


async def test_folder_deeper_than_the_limit_stops_the_listing(
    ivan: DavSeeder,
) -> None:
    """32 уровня от корня SeafDAV (библиотека — первый) обходятся, 33-й —
    tree_too_deep."""
    deep = "/".join(["Личное", *(str(level) for level in range(2, 33))])
    ivan.put(f"{deep}/На дне.txt", "Тридцать второй уровень.".encode())
    try:
        assert "На дне.txt" in await _listing("ivan")
        ivan.put(f"{deep}/33/Ниже дна.txt", b"33")
        with pytest.raises(AdapterError) as caught:
            await _listing("ivan")
        assert caught.value.code == "tree_too_deep"
    finally:
        ivan.delete("Личное/2")


# --- синхронизация ------------------------------------------------------------


@pytest.fixture
async def people(session: AsyncSession, tenant_ctx: Tenant) -> dict[str, User]:
    return await make_people(session, USERS)


async def _connect(
    session: AsyncSession, people: dict[str, User], passwords: dict[str, str]
) -> tuple[PerUserStand, Connector]:
    stand = PerUserStand(URL)
    connector = await stand.connector(session, KIND, {"server": DAV})
    for login, password in passwords.items():
        await stand.grant(
            session,
            connector,
            people[login].id,
            {"login": email(login), "password": password},
        )
    return stand, connector


async def test_sync_gives_each_employee_their_own_copy(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    people: dict[str, User],
    ivan: DavSeeder,
) -> None:
    stand, connector = await _connect(session, people, dict.fromkeys(USERS, PASSWORD))

    outcome = await stand.sync(session_maker, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
    assert outcome.stats.failed == 0, outcome
    copies = sum(len(allowed) for allowed in EXPECTED.values())
    assert outcome.stats.added == copies
    assert outcome.stats.skipped_formats == {".png": 1}
    found = await materials(session, connector)
    for (title, text), allowed in EXPECTED.items():
        same = [m for m in found if m.title == title and text in m.content]
        assert len(same) == len(allowed), title
        owners: set[str] = set()
        for material in same:
            assert material.visibility == MaterialVisibility.RESTRICTED.value
            who = await readers(session, material, people)
            assert len(who) == 1, title
            owners |= who
        assert owners == allowed, title

    # ivan правит план, удаляет анкету и закрывает maria «Проекты».
    plan = next(f for f in FILES if f.path == "Проекты/План.md")
    form = next(f for f in FILES if f.path.endswith("Анкета.txt"))
    maria_id = user_ids(URL)["maria"]
    owner = SeafileApi(URL, email("ivan"), PASSWORD)
    try:
        ivan.put(plan.path, "# План\n\nЗапуск перенесён на июнь.\n".encode())
        ivan.delete(form.path)
        owner.unshare("Проекты", "user", maria_id)

        outcome = await stand.sync(session_maker, connector)

        assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
        # Копия ivan обновлена; копия maria и три копии анкеты удалены.
        assert outcome.stats.updated == 1
        assert outcome.stats.removed == 1 + 3
        found = await materials(session, connector)
        plans = [m for m in found if m.title == "План.md"]
        assert len(plans) == 1 and "на июнь" in plans[0].content
        assert await readers(session, plans[0], people) == {"ivan"}
        assert not [m for m in found if m.title == "Анкета.txt"]
    finally:
        ivan.put(plan.path, plan.data)
        ivan.put(form.path, form.data)
        owner.share("Проекты", "user", maria_id)
        owner.close()


async def test_connector_check_records_without_the_password(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record = tmp_path / KIND
    code = await _check(
        URL,
        CA,
        [
            "--kind",
            KIND,
            "--config",
            f"server={DAV}",
            "--credential",
            f"login={email('maria')}",
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
    dumped = "\n".join(f.read_text(encoding="utf-8") for f in record.glob("*.json"))
    assert "PROPFIND" in dumped
    assert PASSWORD not in dumped


async def test_wrong_password_is_auth_failed() -> None:
    with local_stand(URL):
        async with stand_http() as raw:
            adapter = _adapter(raw, "maria", WRONG)
            with pytest.raises(AdapterAuthError) as caught:
                await adapter.check()
    assert caught.value.code == "auth_failed"


async def test_wrong_password_expires_only_that_grant(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    people: dict[str, User],
) -> None:
    stand, connector = await _connect(
        session, people, {"ivan": PASSWORD, "maria": WRONG}
    )

    outcome = await stand.sync(session_maker, connector)

    assert outcome.stats.grants_expired == 1
    found = await materials(session, connector)
    assert found
    for material in found:
        assert await readers(session, material, people) == {"ivan"}
    with tenant_scope(tenant_ctx.id):
        grant = await GrantRepository(session).get(connector.id, people["maria"].id)
        assert grant is not None
        await session.refresh(grant)
        assert grant.status == GrantStatus.EXPIRED.value
        assert grant.error_code == "auth_failed"
