"""Общие живые тесты Nextcloud и ownCloud 10 (виды `nextcloud`, `owncloud`):
одна и та же выдуманная компания (tests/live/nextcloud/seed.py), один и тот
же WebDAV с oc:fileid и OCS. Тестовые файлы стендов наследуют
SharedDriveSuite и задают адрес, вид и способ получить пароль приложения.

Обещание каталога, которое здесь проверяется: общий файл — один документ
на всех, у кого есть доступ (сквозной oc:fileid), и каждый сотрудник
видит только своё.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.base import AdapterAuthError, AdapterError, FetchedFile
from corp_ed.connectors.webdav.adapter import WebDavAdapter, build_adapter
from corp_ed.core import outbound
from corp_ed.core.security import hash_password
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Connector, Material, Tenant, User, UserRole
from corp_ed.domain.types import GrantStatus, MaterialVisibility, SyncRunStatus
from corp_ed.ingest.extract import detect_format, extract
from corp_ed.repositories.connector_repository import GrantRepository
from tests.factories import make_user
from tests.live.dav_stand import (
    CA,
    PASSWORD,
    DavSeeder,
    PerUserStand,
    _check,
    local_stand,
    stand_http,
)
from tests.live.nextcloud.seed import (
    EXPECTED,
    FILES,
    SHARE_USER,
    USERS,
    Ocs,
    dav_root,
)
from tests.test_connector_sync import access_of, materials_of

WRONG = "not-the-app-password"


async def materials(session: AsyncSession, connector: Connector) -> list[Material]:
    with tenant_scope(connector.tenant_id):
        return list((await materials_of(session, connector)).values())


async def readers(
    session: AsyncSession, material: Material, people: dict[str, User]
) -> set[str]:
    """Кто из сотрудников kronto видит материал (по логину)."""
    names = {user.id: login for login, user in people.items()}
    with tenant_scope(material.tenant_id):
        return {names[i] for i in await access_of(session, material)}


def find(found_in: list[Material], title: str, text: str) -> Material:
    found = [m for m in found_in if m.title == title and text in m.content]
    assert len(found) == 1, (title, [m.title for m in found_in])
    return found[0]


async def make_people(session: AsyncSession, logins: object) -> dict[str, User]:
    """Сотрудники kronto с почтой <логин>@example.com."""
    users = {}
    for login in logins:  # type: ignore[attr-defined]
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


class SharedDriveSuite:
    url: str
    kind: str
    app_password: Callable[[str, str], str]
    """(адрес, логин) → пароль приложения сотрудника."""

    @pytest.fixture(scope="class")
    @classmethod
    def app_passwords(cls) -> dict[str, str]:
        make = cls.app_password
        return {user: make(cls.url, user) for user in USERS}

    @pytest.fixture
    def ivan(self) -> Iterator[DavSeeder]:
        """Владелец общих файлов; что тест поменял — вернёт сам в finally."""
        seeder = DavSeeder(dav_root(self.url, "ivan"), "ivan", PASSWORD)
        yield seeder
        seeder.close()

    @pytest.fixture
    async def people(
        self, session: AsyncSession, tenant_ctx: Tenant
    ) -> dict[str, User]:
        return await make_people(session, USERS)

    def adapter(self, raw: object, login: str, password: str) -> WebDavAdapter:
        return build_adapter(
            self.kind,
            {"server": self.url},
            {"login": login, "password": password},
            outbound.OutboundClient(raw),  # type: ignore[arg-type]
            PerUserStand(self.url).settings,
        )

    async def listing(
        self, login: str, password: str
    ) -> dict[str, list[tuple[str, str]]]:
        """Название → [(external_id, путь)] в дереве пользователя."""
        listed: dict[str, list[tuple[str, str]]] = {}
        with local_stand(self.url):
            async with stand_http() as raw:
                adapter = self.adapter(raw, login, password)
                await adapter.check()
                async for document in adapter.list(["files"]):
                    listed.setdefault(document.title, []).append(
                        (document.external_id, document.path)
                    )
        return listed

    async def connect(
        self,
        session: AsyncSession,
        people: dict[str, User],
        passwords: dict[str, str],
    ) -> tuple[PerUserStand, Connector]:
        stand = PerUserStand(self.url)
        connector = await stand.connector(session, self.kind, {"server": self.url})
        for login, password in passwords.items():
            await stand.grant(
                session,
                connector,
                people[login].id,
                {"login": login, "password": password},
            )
        return stand, connector

    # --- листинг и файлы ---------------------------------------------------------

    async def test_shared_file_is_one_document_for_everyone_who_sees_it(
        self, app_passwords: dict[str, str]
    ) -> None:
        listings = {
            user: await self.listing(user, password)
            for user, password in app_passwords.items()
        }

        for (title, _), allowed in EXPECTED.items():
            for user in USERS:
                seen = listings[user].get(title, [])
                if user in allowed:
                    assert seen, (user, title)
                elif title != "Отпуск.docx":
                    assert not seen, (user, title)
        # Общий.txt расшарен maria дважды (ей и её группе) — у неё он один.
        assert len(listings["maria"]["Общий.txt"]) == 1
        # Сквозной oc:fileid: у всех троих — один и тот же документ.
        for title in ("Общий.txt", "Регламент.pdf", "Анкета.txt"):
            ids = {listings[user][title][0][0] for user in USERS}
            assert len(ids) == 1, title
            assert ids.pop().startswith(f"{self.kind}:id:"), title
        assert listings["ivan"]["План.md"][0][0] == listings["maria"]["План.md"][0][0]
        # Одинаковый путь — разные файлы двух сотрудников.
        ivan_leave = listings["ivan"]["Отпуск.docx"][0][0]
        assert ivan_leave != listings["maria"]["Отпуск.docx"][0][0]
        assert "Схема.png" not in listings["ivan"]
        assert listings["petr"]["Анкета.txt"][0][1] == "Кадры/Глубже"

    async def test_every_format_reaches_the_extractor(
        self, app_passwords: dict[str, str]
    ) -> None:
        texts = {
            title: text
            for (title, text), allowed in EXPECTED.items()
            if "ivan" in allowed
        }
        with local_stand(self.url):
            async with stand_http() as raw:
                adapter = self.adapter(raw, "ivan", app_passwords["ivan"])
                documents = {
                    d.title: d
                    async for d in adapter.list(["files"])
                    if d.title in texts
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
            # Ссылка — в веб-интерфейс по номеру файла.
            assert documents[title].url.startswith(f"{self.url}index.php/f/"), title

    async def test_folder_deeper_than_the_limit_stops_the_listing(
        self, app_passwords: dict[str, str], ivan: DavSeeder
    ) -> None:
        """32 уровня от корня — обходятся; 33-й — tree_too_deep."""
        deep = "/".join(["Глубоко", *(str(level) for level in range(2, 33))])
        ivan.put(f"{deep}/На дне.txt", "Тридцать второй уровень.".encode())
        try:
            assert "На дне.txt" in await self.listing("ivan", app_passwords["ivan"])
            ivan.put(f"{deep}/33/Ниже дна.txt", b"33")
            with pytest.raises(AdapterError) as caught:
                await self.listing("ivan", app_passwords["ivan"])
            assert caught.value.code == "tree_too_deep"
        finally:
            ivan.delete("Глубоко")

    # --- синхронизация ------------------------------------------------------------

    async def test_sync_gives_each_employee_only_what_they_see(
        self,
        session: AsyncSession,
        tenant_ctx: Tenant,
        session_maker: async_sessionmaker[AsyncSession],
        people: dict[str, User],
        app_passwords: dict[str, str],
        ivan: DavSeeder,
    ) -> None:
        stand, connector = await self.connect(session, people, app_passwords)

        outcome = await stand.sync(session_maker, connector)

        assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
        assert outcome.stats.failed == 0, outcome
        assert outcome.stats.grants == len(USERS)
        # Кроме засеянных — файлы-образцы, которые сервер кладёт каждому
        # новому пользователю (skeleton): у каждого свои, со своим fileid.
        assert outcome.stats.added >= len(EXPECTED)
        assert outcome.stats.skipped_formats[".png"] >= 1
        found = await materials(session, connector)
        for (title, text), allowed in EXPECTED.items():
            material = find(found, title, text)
            assert material.visibility == MaterialVisibility.RESTRICTED.value
            assert await readers(session, material, people) == allowed, title

        # ivan правит план, удаляет анкету и закрывает maria папку «Проекты».
        plan = next(f for f in FILES if f.path == "Проекты/План.md")
        form = next(f for f in FILES if f.path.endswith("Анкета.txt"))
        owner = Ocs(self.url, "ivan", PASSWORD)
        share_id = next(
            str(share["id"])
            for share in owner.shares("Проекты")
            if int(str(share["share_type"])) == SHARE_USER
            and share["share_with"] == "maria"
        )
        try:
            ivan.put(plan.path, "# План\n\nЗапуск перенесён на июнь.\n".encode())
            ivan.delete(form.path)
            owner.unshare(share_id)

            outcome = await stand.sync(session_maker, connector)

            assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
            assert outcome.stats.updated == 1
            assert outcome.stats.removed == 1
            found = await materials(session, connector)
            changed = find(found, "План.md", "на июнь")
            assert await readers(session, changed, people) == {"ivan"}
            assert not [m for m in found if m.title == "Анкета.txt"]
        finally:
            ivan.put(plan.path, plan.data)
            ivan.put(form.path, form.data)
            owner.share("Проекты", SHARE_USER, "maria")
            owner.close()

    async def test_connector_check_records_without_the_password(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        app_passwords: dict[str, str],
    ) -> None:
        record = tmp_path / self.kind
        code = await _check(
            self.url,
            CA,
            [
                "--kind",
                self.kind,
                "--config",
                f"server={self.url}",
                "--credential",
                "login=maria",
                "--credential",
                f"password={app_passwords['maria']}",
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
        assert "PROPFIND" in dumped and '"fileid": "' in dumped
        assert app_passwords["maria"] not in dumped
        assert PASSWORD not in dumped

    # Неверный пароль — последним: Nextcloud запоминает неудачные входы с
    # адреса (bruteforce protection) и замедляет следующие запросы с него.

    async def test_wrong_password_is_auth_failed(self) -> None:
        with local_stand(self.url):
            async with stand_http() as raw:
                adapter = self.adapter(raw, "maria", WRONG)
                with pytest.raises(AdapterAuthError) as caught:
                    await adapter.check()
        assert caught.value.code == "auth_failed"

    async def test_wrong_password_expires_only_that_grant(
        self,
        session: AsyncSession,
        tenant_ctx: Tenant,
        session_maker: async_sessionmaker[AsyncSession],
        people: dict[str, User],
        app_passwords: dict[str, str],
    ) -> None:
        """Второй сотрудник с неверным паролем не получает файлов первого
        (сессия первого не пускает его — core/outbound.py, cookie)."""
        stand, connector = await self.connect(
            session, people, {"ivan": app_passwords["ivan"], "maria": WRONG}
        )

        outcome = await stand.sync(session_maker, connector)

        assert outcome.stats.grants_expired == 1
        found = await materials(session, connector)
        for (title, text), allowed in EXPECTED.items():
            if "ivan" in allowed:
                material = find(found, title, text)
                assert await readers(session, material, people) == {"ivan"}, title
        assert not [m for m in found if "в июле" in m.content]
        with tenant_scope(tenant_ctx.id):
            grant = await GrantRepository(session).get(connector.id, people["maria"].id)
            assert grant is not None
            await session.refresh(grant)
            assert grant.status == GrantStatus.EXPIRED.value
            assert grant.error_code == "auth_failed"
