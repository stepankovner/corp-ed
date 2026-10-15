"""Живая проверка вида `mediawiki` (режим organization): официальный образ
mediawiki 1.43 в Docker, две вики — открытая для чтения без входа и
закрытая (чтение — группе staff, пространство «Кадры» закрыто Lockdown),
бот-пароль, сопоставление «логин = почта».

Как поднять стенд — tests/live/mediawiki/README.md (compose, seed.sh).
Без переменных — пропуск:

    MEDIAWIKI_URL=https://127.0.0.1:8500/ \\
    MEDIAWIKI_PRIVATE_URL=https://127.0.0.1:8501/ \\
    DAV_STAND_CA=~/dav-tls/cert.pem \\
        uv run pytest tests/live/test_mediawiki_live.py -q

Вид ещё не в default_registry (подключит интегратор): тесты собирают свой
реестр, а для `cli connector-check` подменяют default_registry в cli.
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    FetchedFile,
    FetchedPage,
    RemoteDocument,
)
from corp_ed.connectors.mediawiki import adapter as adapter_module
from corp_ed.connectors.mediawiki import register
from corp_ed.connectors.mediawiki.adapter import MediaWikiAdapter, build_adapter
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
from tests.live.dav_stand import CA, _check, local_stand, stand_http, trust
from tests.live.mediawiki import (
    BOT,
    BOT_PASSWORD,
    MAIN_PAGES,
    MAPPING,
    NO_READ_BOT,
    NO_READ_PASSWORD,
    PRIVATE_READERS,
    PUBLIC_READERS,
    TEXT,
    USERS,
    WikiApi,
)
from tests.live.oc_family import make_people, materials, readers

URL = os.environ.get("MEDIAWIKI_URL", "")
PRIVATE_URL = os.environ.get("MEDIAWIKI_PRIVATE_URL", "")
KEY = Fernet.generate_key().decode()

pytestmark = pytest.mark.skipif(
    not (URL and PRIVATE_URL and CA),
    reason="нужны MEDIAWIKI_URL, MEDIAWIKI_PRIVATE_URL и DAV_STAND_CA (стенд в Docker)",
)


def _email(login: str) -> str:
    return f"{login}@example.com"


def _settings() -> ConnectorSettings:
    return ConnectorSettings(secrets_keys=KEY)  # type: ignore[arg-type]


def _registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    register(registry, _settings())
    return registry


def _adapter(
    raw: httpx.AsyncClient,
    url: str,
    *,
    namespaces: str = "",
    login: str = BOT,
    password: str = BOT_PASSWORD,
) -> MediaWikiAdapter:
    config = {"api_url": f"{url}api.php", "user_emails": MAPPING}
    if namespaces:
        config["namespaces"] = namespaces
    return build_adapter(
        config,
        {"username": login, "password": password},
        outbound.OutboundClient(raw),
        _settings(),
    )


async def _listing(
    url: str, namespaces: str = "", modules: tuple[str, ...] = ("pages", "files")
) -> tuple[dict[str, RemoteDocument], dict[str, object]]:
    """Документы по заголовку и содержимое каждого (fetch)."""
    with local_stand(url):
        async with stand_http() as raw:
            adapter = _adapter(raw, url, namespaces=namespaces)
            await adapter.check()
            documents = {d.title: d async for d in adapter.list(list(modules))}
            fetched = {
                title: await adapter.fetch(document, max_bytes=1_000_000)
                for title, document in documents.items()
            }
    return documents, dict(fetched)


async def test_public_wiki_pages_files_and_readers() -> None:
    documents, fetched = await _listing(URL)

    assert set(documents) == MAIN_PAGES | {"Регламент.txt"}
    for title, document in documents.items():
        # Открытая без входа вики — всё равно явный список: Ghost в вики нет.
        assert document.visibility is MaterialVisibility.RESTRICTED, title
        assert document.allowed_emails == {_email(u) for u in PUBLIC_READERS}, title
        assert document.url.startswith(URL), title
    assert documents["Отпуск"].path == "Компания"
    vacation = fetched["Отпуск"]
    assert isinstance(vacation, FetchedPage)
    assert vacation.html.startswith("<h1>Отпуск</h1>")
    assert TEXT["Отпуск"] in vacation.html
    # Шаблон развёрнут: HTML — результат action=parse.
    assert "Отдел кадров" in vacation.html
    assert "limit report" not in vacation.html
    rules = fetched["Регламент.txt"]
    assert isinstance(rules, FetchedFile)
    assert "Регламент закупок" in rules.data.decode("utf-8")
    assert documents["Регламент.txt"].version and documents["Регламент.txt"].size


async def test_pages_are_continued(monkeypatch: pytest.MonkeyPatch) -> None:
    """Продолжение (API:Continue) на живой вики: по две страницы за запрос."""
    monkeypatch.setattr(adapter_module, "PAGES_LIMIT", 2)
    documents, _ = await _listing(URL, modules=("pages",))
    assert set(documents) == MAIN_PAGES


async def test_listed_namespaces_by_name() -> None:
    documents, fetched = await _listing(URL, "основное, Компания, Help", ("pages",))

    assert set(documents) == MAIN_PAGES | {"Компания:Правила", "Справка:Поиск"}
    assert documents["Справка:Поиск"].path == "Компания/Справка"
    for title in ("Компания:Правила", "Справка:Поиск"):
        page = fetched[title]
        assert isinstance(page, FetchedPage) and TEXT[title] in page.html


async def test_private_wiki_with_lockdown_needs_namespaces() -> None:
    """Lockdown в siteinfo — без списка пространств ничего не берём."""
    with local_stand(PRIVATE_URL):
        async with stand_http() as raw:
            with pytest.raises(AdapterConfigError) as caught:
                await _adapter(raw, PRIVATE_URL).check()
    assert caught.value.code == "namespaces_required"


async def test_private_wiki_readers_without_img_auth_files() -> None:
    """Читает только staff (Petr — нет); «Кадры» под Lockdown не взяты;
    файлы за img_auth.php не листятся — сессию бот-пароля он не принимает."""
    documents, fetched = await _listing(PRIVATE_URL, "0, Файл")

    assert set(documents) == MAIN_PAGES
    for title, document in documents.items():
        assert document.allowed_emails == {_email(u) for u in PRIVATE_READERS}, title
    page = fetched["Отпуск"]
    assert isinstance(page, FetchedPage) and TEXT["Отпуск"] in page.html


def test_img_auth_refuses_the_bot_password_session() -> None:
    """Почему файлы закрытой вики не берутся: с той же сессией api.php
    отвечает, а img_auth.php — 403 (BotPasswordSessionProvider)."""
    api = f"{PRIVATE_URL}api.php"
    with httpx.Client(verify=trust(CA)) as http:
        token = http.get(
            api,
            params={
                "action": "query",
                "meta": "tokens",
                "type": "login",
                "format": "json",
            },
        ).json()["query"]["tokens"]["logintoken"]
        login = http.post(
            api,
            data={
                "action": "login",
                "lgname": BOT,
                "lgpassword": BOT_PASSWORD,
                "lgtoken": token,
                "format": "json",
            },
        ).json()
        files = http.get(
            api,
            params={
                "action": "query",
                "list": "allimages",
                "aiprop": "url",
                "assert": "user",
                "format": "json",
                "formatversion": "2",
            },
        ).json()
        link = files["query"]["allimages"][0]["url"]
        image = http.get(link)
    assert login["login"]["result"] == "Success"
    assert "/img_auth.php/" in link
    assert image.status_code == 403


def test_private_wiki_is_closed_without_login() -> None:
    with httpx.Client(verify=trust(CA)) as http:
        answer = http.get(
            f"{PRIVATE_URL}api.php",
            params={"action": "query", "list": "allpages", "format": "json"},
        ).json()
        image = http.get(
            f"{PRIVATE_URL}img_auth.php/0/04/%D0%A0%D0%B5%D0%B3%D0%BB%D0%B0%D0%BC"
            "%D0%B5%D0%BD%D1%82.txt"
        )
    assert answer["error"]["code"] == "readapidenied"
    assert image.status_code == 403


async def test_bot_without_read_right_is_refused() -> None:
    with local_stand(PRIVATE_URL):
        async with stand_http() as raw:
            adapter = _adapter(
                raw,
                PRIVATE_URL,
                namespaces="0",
                login=NO_READ_BOT,
                password=NO_READ_PASSWORD,
            )
            with pytest.raises(AdapterAuthError) as caught:
                await adapter.check()
    assert caught.value.code == "read_denied"


async def test_wrong_bot_password_is_an_auth_error() -> None:
    with local_stand(URL):
        async with stand_http() as raw:
            adapter = _adapter(raw, URL, password="kronto0not0the0right0password001")
            with pytest.raises(AdapterAuthError) as caught:
                await adapter.check()
    assert caught.value.code == "unauthorized"


# --- синхронизация ------------------------------------------------------------


@pytest.fixture
async def people(session: AsyncSession, tenant_ctx: Tenant) -> dict[str, User]:
    return await make_people(session, USERS)


async def _connector(session: AsyncSession) -> Connector:
    connector = Connector(
        kind="mediawiki",
        name="MediaWiki (стенд)",
        mode=ConnectorMode.ORGANIZATION.value,
        modules=["pages", "files"],
        config={"api_url": f"{URL}api.php", "user_emails": MAPPING},
        credentials=SecretBox([KEY]).encrypt(
            {"username": BOT, "password": BOT_PASSWORD}
        ),
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
def wiki() -> Iterator[WikiApi]:
    api = WikiApi(URL)
    yield api
    api.close()


async def test_sync_mirrors_readers_and_follows_changes(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    people: dict[str, User],
    wiki: WikiApi,
) -> None:
    connector = await _connector(session)

    outcome = await _sync(session_maker, connector)

    assert outcome.stats.added == len(MAIN_PAGES) + 1
    by_title = {m.title: m for m in await materials(session, connector)}
    assert set(by_title) == MAIN_PAGES | {"Регламент.txt"}
    for title, material in by_title.items():
        assert material.visibility == MaterialVisibility.RESTRICTED.value, title
        assert await readers(session, material, people) == PUBLIC_READERS, title
    assert TEXT["Отпуск"] in by_title["Отпуск"].content

    # Правка, удаление и блокировка Petr: права — без новой версии.
    vacation = wiki.wikitext("Отпуск")
    sick = wiki.wikitext("Больничный")
    wiki.edit("Отпуск", "Ежегодный отпуск — 31 день, версия 2.")
    wiki.delete("Больничный")
    wiki.block("Petr")
    try:
        outcome = await _sync(session_maker, connector)

        assert outcome.stats.updated == 1
        assert outcome.stats.removed == 1
        by_title = {m.title: m for m in await materials(session, connector)}
        assert "Больничный" not in by_title
        assert "версия 2" in by_title["Отпуск"].content
        for title, material in by_title.items():
            assert await readers(session, material, people) == {"ivan", "maria"}, title
    finally:
        wiki.edit("Отпуск", vacation)
        wiki.edit("Больничный", sick)
        wiki.unblock("Petr")


async def test_connector_check_records_without_the_password(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import corp_ed.cli as cli

    original = cli.default_registry

    def with_mediawiki(settings: ConnectorSettings) -> AdapterRegistry:
        registry = original(settings)
        register(registry, settings)
        return registry

    monkeypatch.setattr(cli, "default_registry", with_mediawiki)
    record = tmp_path / "mediawiki"
    code = await _check(
        URL,
        CA,
        [
            "--kind",
            "mediawiki",
            "--config",
            f"api_url={URL}api.php",
            "--config",
            f"user_emails={MAPPING}",
            "--credential",
            f"username={BOT}",
            "--credential",
            f"password={BOT_PASSWORD}",
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
    assert "query.allpages" in dumped
    assert BOT_PASSWORD not in dumped
    assert "BPsession" not in dumped
    calls = [json.loads(f.read_text(encoding="utf-8")) for f in record.glob("*.json")]
    login = next(c for c in calls if c["method"] == "login")
    assert login["params"]["lgpassword"] == "<redacted>"
