"""Живая проверка вида `wikijs` (режим organization): официальный образ
ghcr.io/requarks/wiki:2 в Docker, ключ API с полным доступом, группы с
правилами страниц (START, TAG, REGEX, запреты, локаль), гостевой раздел,
отключённый пользователь, неопубликованная страница, вторая локаль.

Главная проверка — права адаптера совпадают с ответом самого Wiki.js:
сотрудники входят по паролю и открывают каждую страницу (200 / 403).

Как поднять стенд — tests/live/wikijs/README.md (compose, seed.py). Без
переменных — пропуск:

    WIKIJS_URL=https://127.0.0.1:8470/ WIKIJS_TOKEN=… \\
    DAV_STAND_CA=~/dav-tls/cert.pem \\
        uv run pytest tests/live/test_wikijs_live.py -q

WIKIJS_MEMBER_TOKEN (ключ группы «Сотрудники») — для проверки, что ключ
без полного доступа не принимается; без него этот тест пропускается.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.base import AdapterAuthError, FetchedPage, RemoteDocument
from corp_ed.connectors.registry import (
    AdapterRegistry,
    UnknownKindError,
    default_registry,
)
from corp_ed.connectors.wikijs import WIKIJS_SPEC, register
from corp_ed.connectors.wikijs.adapter import WikiJsAdapter, build_adapter
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
from tests.live.dav_stand import CA, PASSWORD, _check, local_stand, stand_http, trust
from tests.live.oc_family import make_people, materials, readers
from tests.live.wikijs.seed import EXPECTED, TEXT, TITLES, WikiApi, email

URL = os.environ.get("WIKIJS_URL", "")
TOKEN = os.environ.get("WIKIJS_TOKEN", "")
MEMBER_TOKEN = os.environ.get("WIKIJS_MEMBER_TOKEN", "")
KEY = Fernet.generate_key().decode()
EMPLOYEES = ("ivan", "maria", "petr")
"""Сотрудники kronto (почта — <логин>@example.com); admin и olga — нет."""

pytestmark = pytest.mark.skipif(
    not (URL and TOKEN and CA),
    reason="нужны WIKIJS_URL, WIKIJS_TOKEN и DAV_STAND_CA (стенд Wiki.js в Docker)",
)


def _settings() -> ConnectorSettings:
    return ConnectorSettings(secrets_keys=KEY)  # type: ignore[arg-type]


def _registry(settings: ConnectorSettings) -> AdapterRegistry:
    """Каталог сборки плюс wikijs (пока интегратор не добавил его в
    default_registry) — скрытый вид разрешён, как в connector-check."""
    registry = default_registry(settings)
    try:
        registry.spec("wikijs")
    except UnknownKindError:
        register(registry, settings)
    return registry


def _adapter(raw: httpx.AsyncClient, token: str = TOKEN) -> WikiJsAdapter:
    return build_adapter(
        WIKIJS_SPEC,
        {"base_url": URL},
        {"token": token},
        outbound.OutboundClient(raw),
        _settings(),
    )


async def _listed() -> dict[tuple[str, str], RemoteDocument]:
    with local_stand(URL):
        async with stand_http() as raw:
            adapter = _adapter(raw)
            await adapter.check()
            documents = [d async for d in adapter.list(["pages"])]
    root = URL.rstrip("/") + "/"
    keyed = {}
    for document in documents:
        locale, _, path = document.url.removeprefix(root).partition("/")
        keyed[(locale, path)] = document
    return keyed


@pytest.fixture(scope="module")
def wiki_answers() -> dict[tuple[str, str], set[str]]:
    """Что отвечает сам Wiki.js: кто из гостя и сотрудников открывает
    страницу (200). Вход — по разу на сотрудника: login в Wiki.js
    ограничен пятью попытками в минуту."""
    tokens: dict[str, str | None] = {"guest": None}
    api = WikiApi(URL)
    try:
        for login in EMPLOYEES:
            api.login(login, PASSWORD)
            tokens[login] = api.token
    finally:
        api.close()
    answers: dict[tuple[str, str], set[str]] = {}
    with httpx.Client(verify=trust(CA), timeout=60) as http:
        for locale, path in [*EXPECTED, ("en", "general/hidden")]:
            seen = set()
            for who, token in tokens.items():
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                response = http.get(
                    f"{URL.rstrip('/')}/{locale}/{path}", headers=headers
                )
                assert response.status_code in (200, 403), (who, path)
                if response.status_code == 200:
                    seen.add(who)
            answers[(locale, path)] = seen
    return answers


async def test_listing_mirrors_seeded_rules() -> None:
    documents = await _listed()
    assert set(documents) == set(EXPECTED)
    for key, allowed in EXPECTED.items():
        document = documents[key]
        assert document.title == TITLES[key], key
        assert document.path == f"{key[0]}/{key[1].rpartition('/')[0]}", key
        if allowed is None:
            assert document.visibility is MaterialVisibility.TENANT, key
            assert document.allowed_emails == frozenset(), key
        else:
            assert document.visibility is MaterialVisibility.RESTRICTED, key
            assert document.allowed_emails == {email(u) for u in allowed}, key


async def test_listing_matches_wikijs_own_answer(
    wiki_answers: dict[tuple[str, str], set[str]],
) -> None:
    """Права адаптера — те же, что у показа страницы в Wiki.js."""
    documents = await _listed()
    # Неопубликованную не открывает никто из сотрудников — её и нет.
    assert wiki_answers[("en", "general/hidden")] == set()
    assert ("en", "general/hidden") not in documents
    for key, document in documents.items():
        seen = wiki_answers[key]
        if document.visibility is MaterialVisibility.TENANT:
            assert "guest" in seen, key
            continue
        assert "guest" not in seen, key
        mirrored = {e.split("@")[0] for e in document.allowed_emails} - {"admin"}
        assert mirrored == seen, key


async def test_pages_come_as_rendered_html() -> None:
    with local_stand(URL):
        async with stand_http() as raw:
            adapter = _adapter(raw)
            documents = [d async for d in adapter.list(["pages"])]
            fetched = [
                (d, await adapter.fetch(d, max_bytes=1_000_000)) for d in documents
            ]
    assert len(fetched) == len(EXPECTED)
    texts = set(TEXT.values())
    for document, content in fetched:
        assert isinstance(content, FetchedPage), document.title
        assert "¶" not in content.html
        assert content.html.count(f"{document.title}</h1>") == 1, document.title
        assert any(text in content.html for text in texts), document.title


async def test_wrong_key_is_an_auth_error() -> None:
    """Испорченный ключ Wiki.js обслуживает как гостя: «ключ не принят»."""
    with local_stand(URL):
        async with stand_http() as raw:
            with pytest.raises(AdapterAuthError) as caught:
                await _adapter(raw, "eyJhbGciOiJSUzI1NiJ9.e30.not-a-key").check()
    assert caught.value.code == "unauthorized"


async def test_revoked_key_is_an_auth_error() -> None:
    api = WikiApi(URL)
    api.token = TOKEN
    try:
        created = api.mutate(
            """mutation { authentication { createApiKey(name: "kronto-revoked",
                 expiration: "1d", fullAccess: true) {
                   responseResult { succeeded message } key } } }""",
            ("authentication", "createApiKey"),
        )
        keys = api.query("{ authentication { apiKeys { id name isRevoked } } }")
        key_id = max(
            k["id"]
            for k in keys["authentication"]["apiKeys"]
            if k["name"] == "kronto-revoked" and not k["isRevoked"]
        )
        api.mutate(
            """mutation ($id: Int!) { authentication { revokeApiKey(id: $id) {
                 responseResult { succeeded message } } } }""",
            ("authentication", "revokeApiKey"),
            id=key_id,
        )
    finally:
        api.close()
    with local_stand(URL):
        async with stand_http() as raw:
            with pytest.raises(AdapterAuthError) as caught:
                await _adapter(raw, created["key"]).check()
    assert caught.value.code == "unauthorized"


@pytest.mark.skipif(not MEMBER_TOKEN, reason="нужен WIKIJS_MEMBER_TOKEN")
async def test_group_key_is_refused() -> None:
    with local_stand(URL):
        async with stand_http() as raw:
            with pytest.raises(AdapterAuthError) as caught:
                await _adapter(raw, MEMBER_TOKEN).check()
    assert caught.value.code == "admin_required"


# --- синхронизация ------------------------------------------------------------


@pytest.fixture
async def people(session: AsyncSession, tenant_ctx: Tenant) -> dict[str, User]:
    return await make_people(session, (*EMPLOYEES, "olga"))


async def _connector(session: AsyncSession) -> Connector:
    connector = Connector(
        kind="wikijs",
        name="Wiki.js (стенд)",
        mode=ConnectorMode.ORGANIZATION.value,
        modules=["pages"],
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
                _registry(_settings()),
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
    api.token = TOKEN
    yield api
    api.close()


async def test_sync_mirrors_access_and_follows_changes(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    people: dict[str, User],
    wiki: WikiApi,
) -> None:
    connector = await _connector(session)

    await _sync(session_maker, connector)

    by_title = {m.title: m for m in await materials(session, connector)}
    assert set(by_title) == set(TITLES.values()) - {"Неопубликованная"}
    for key, allowed in EXPECTED.items():
        material = by_title[TITLES[key]]
        assert TEXT[key] in material.content, key
        if allowed is None:
            assert material.visibility == MaterialVisibility.TENANT.value, key
            continue
        assert material.visibility == MaterialVisibility.RESTRICTED.value, key
        assert await readers(session, material, people) == allowed - {"admin"}, key

    # Правка текста, удаление страницы, выход maria из «Кадров» — права
    # меняются без новой версии страницы.
    pages = wiki.pages()
    groups = wiki.groups()
    users = wiki.users()
    vacation = pages[("en", "general/vacation")]
    idea = pages[("en", "drafts/idea")]
    wiki.update_page(vacation, "Отпуск", "# Отпуск\n\nОтпуск — 31 день, версия 2.\n")
    wiki.mutate(
        """mutation ($id: Int!) { pages { delete(id: $id) {
             responseResult { succeeded message } } } }""",
        ("pages", "delete"),
        id=idea,
    )
    assign = """mutation ($g: Int!, $u: Int!) { groups { %s(groupId: $g, userId: $u) {
                  responseResult { succeeded message } } } }"""
    wiki.mutate(
        assign % "unassignUser",
        ("groups", "unassignUser"),
        g=groups["Кадры"],
        u=users["maria"],
    )
    try:
        outcome = await _sync(session_maker, connector)

        assert outcome.stats.updated == 1
        assert outcome.stats.removed == 1
        by_title = {m.title: m for m in await materials(session, connector)}
        assert "версия 2" in by_title["Отпуск"].content
        assert "Идея" not in by_title
        assert await readers(session, by_title["Зарплаты"], people) == set()
        assert await readers(session, by_title["Отчёт 2026"], people) == set()
    finally:
        wiki.update_page(
            vacation, "Отпуск", f"# Отпуск\n\n{TEXT[('en', 'general/vacation')]}\n"
        )
        wiki.mutate(
            """mutation ($c: String!) { pages { create(content: $c, description: "",
                 editor: "markdown", isPublished: true, isPrivate: false,
                 locale: "en", path: "drafts/idea", tags: [], title: "Идея") {
                   responseResult { succeeded message } } } }""",
            ("pages", "create"),
            c=f"# Идея\n\n{TEXT[('en', 'drafts/idea')]}\n",
        )
        wiki.mutate(
            assign % "assignUser",
            ("groups", "assignUser"),
            g=groups["Кадры"],
            u=users["maria"],
        )


async def test_connector_check_records_without_the_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import corp_ed.cli as cli

    monkeypatch.setattr(cli, "default_registry", _registry)
    record = tmp_path / "wikijs"
    code = await _check(
        URL,
        CA,
        [
            "--kind",
            "wikijs",
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
    assert "pages.list" in dumped
    assert TOKEN not in dumped
    assert "stacktrace" not in dumped
