"""Живая проверка Confluence Server/DC (Р-12 «б»): настоящий Confluence в
Docker с тестовой лицензией Atlassian и выдуманной компанией.

Как поднять стенд — tests/live/confluence_dc/README.md (compose, мастер,
seed.py, токен служебной учётки). Без переменных — пропуск:

    CONFLUENCE_DC_URL=http://127.0.0.1:8090 CONFLUENCE_DC_TOKEN=… \\
        uv run pytest tests/live/test_confluence_dc_live.py -q

Стенд слушает http://127.0.0.1: в бою адаптер ходит только по HTTPS на
публичные адреса (core/outbound.py), здесь проверка адреса пропускает
один этот стенд — всё остальное идёт как в бою.
"""

import os
from collections.abc import Iterator

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.base import AdapterAuthError, FetchedFile, FetchedPage
from corp_ed.connectors.confluence.adapter import ConfluenceAdapter, build_adapter
from corp_ed.connectors.registry import default_registry
from corp_ed.core import outbound
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.secrets import SecretBox
from corp_ed.core.security import hash_password
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Connector, Material, Tenant, User, UserRole
from corp_ed.domain.types import (
    ConnectorMode,
    MaterialVisibility,
    SyncRunStatus,
    SyncTrigger,
)
from corp_ed.ingest.extract import detect_format, extract
from corp_ed.services.connector_sync_service import ConnectorSyncService, SyncOutcome
from tests.factories import make_user
from tests.live.confluence_dc.seed import (
    EMAIL_TEMPLATE,
    EXPECTED_READERS,
    HIDDEN_FROM_SERVICE,
    PASSWORD,
    SERVICE,
    USERS,
    Page,
    Seeder,
)
from tests.test_connector_sync import access_of, materials_of

URL = os.environ.get("CONFLUENCE_DC_URL", "").rstrip("/")
TOKEN = os.environ.get("CONFLUENCE_DC_TOKEN", "")
ADMIN_TOKEN = os.environ.get("CONFLUENCE_DC_ADMIN_TOKEN", "")
KEY = Fernet.generate_key().decode()

pytestmark = pytest.mark.skipif(
    not (URL and TOKEN),
    reason="нужны CONFLUENCE_DC_URL и CONFLUENCE_DC_TOKEN (стенд Confluence DC)",
)


def _email(username: str) -> str:
    return EMAIL_TEMPLATE.replace("{username}", username)


@pytest.fixture
def stand_address(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Пропустить проверку адреса только для стенда (http, 127.0.0.1)."""
    original = outbound.validate_outbound_url
    stand = httpx.URL(URL)

    async def validate(
        url: str, *, resolver: outbound.Resolver | None = None
    ) -> outbound.OutboundTarget:
        target = httpx.URL(url)
        if (target.host, target.port) == (stand.host, stand.port):
            return outbound.OutboundTarget(
                url=url, host=target.host, port=target.port or 80, address=target.host
            )
        return await original(url, resolver=resolver)

    monkeypatch.setattr(outbound, "validate_outbound_url", validate)
    yield


def _settings() -> ConnectorSettings:
    return ConnectorSettings(secrets_keys=KEY)  # type: ignore[arg-type]


def _adapter(raw: httpx.AsyncClient) -> ConfluenceAdapter:
    return build_adapter(
        {"base_url": URL, "spaces": "HR,ENG", "email_template": EMAIL_TEMPLATE},
        {"token": TOKEN},
        outbound.OutboundClient(raw),
        _settings(),
    )


async def test_listing_mirrors_confluence_read_restrictions(
    stand_address: None,
) -> None:
    async with httpx.AsyncClient() as raw:
        adapter = _adapter(raw)
        await adapter.check()
        documents = [d async for d in adapter.list(["pages", "attachments"])]

    pages = {d.title: d for d in documents if d.module == "pages"}
    # Служебная учётка видит ровно то, что ей разрешено: страницу, где
    # её нет в ограничении, Confluence ей не отдаёт — в ответы она не
    # попадёт (безопасно: утечки нет, но и покрытия нет).
    assert set(pages) == set(EXPECTED_READERS)
    assert not set(HIDDEN_FROM_SERVICE) & set(pages)
    for title, readers in EXPECTED_READERS.items():
        page = pages[title]
        if readers is None:
            assert page.visibility is MaterialVisibility.TENANT, title
            assert page.allowed_emails == frozenset(), title
        else:
            assert page.visibility is MaterialVisibility.RESTRICTED, title
            assert page.allowed_emails == {_email(u) for u in readers}, title
        assert page.url and page.url.startswith(URL), title

    # Вложения наследуют читателей страницы.
    attachments = {d.title: d for d in documents if d.module == "attachments"}
    assert set(attachments) == {"Заявление.docx", "Сетка.md"}
    assert attachments["Заявление.docx"].visibility is MaterialVisibility.TENANT
    assert attachments["Сетка.md"].allowed_emails == pages["Зарплаты"].allowed_emails
    assert pages["Премии"].path == "Кадры/Зарплаты"
    assert attachments["Сетка.md"].path == "Кадры/Зарплаты"


async def test_login_and_password_where_basic_auth_is_on(stand_address: None) -> None:
    """Логин и пароль служебной учётки: 7.19 и 8.5 — да; 10.x по умолчанию
    выключает Basic в REST («Basic Authentication has been disabled») —
    там только токен (проверено 01.10)."""
    async with httpx.AsyncClient() as raw:
        adapter = build_adapter(
            {"base_url": URL, "spaces": "HR,ENG", "email_template": EMAIL_TEMPLATE},
            {"username": SERVICE, "password": PASSWORD},
            outbound.OutboundClient(raw),
            _settings(),
        )
        try:
            await adapter.check()
        except AdapterAuthError as exc:
            assert exc.code == "basic_auth_disabled"
            pytest.skip("Basic в REST выключен (Confluence 10+) — только токен")
        titles = {d.title async for d in adapter.list(["pages"])}

    assert titles == set(EXPECTED_READERS)


async def test_page_and_attachment_reach_the_extractor(stand_address: None) -> None:
    async with httpx.AsyncClient() as raw:
        adapter = _adapter(raw)
        documents = {d.title: d async for d in adapter.list(["pages", "attachments"])}
        page = await adapter.fetch(documents["Архитектура"], max_bytes=1_000_000)
        file = await adapter.fetch(documents["Заявление.docx"], max_bytes=1_000_000)

    assert isinstance(page, FetchedPage)
    assert "Монолит и воркер" in page.html
    assert isinstance(file, FetchedFile)
    detected = detect_format(file.filename, file.data)
    assert "Заявление на отпуск" in extract(detected.format, file.data)


@pytest.fixture
async def people(session: AsyncSession, tenant_ctx: Tenant) -> dict[str, User]:
    """Сотрудники компании в kronto с той же почтой, что в Confluence."""
    users = {}
    for username in USERS:
        user = make_user(
            email=_email(username),
            full_name=username,
            role=UserRole.EMPLOYEE,
            hashed_password=hash_password("Password-1234"),
        )
        session.add(user)
        users[username] = user
    await session.commit()
    return users


async def _connector(session: AsyncSession) -> Connector:
    connector = Connector(
        kind="confluence",
        name="Confluence DC",
        mode=ConnectorMode.ORGANIZATION.value,
        modules=["pages", "attachments"],
        config={"base_url": URL, "spaces": "HR,ENG", "email_template": EMAIL_TEMPLATE},
        credentials=SecretBox([KEY]).encrypt({"token": TOKEN}),
    )
    session.add(connector)
    await session.commit()
    return connector


async def _sync(
    session_maker: async_sessionmaker[AsyncSession], connector: Connector
) -> SyncOutcome:
    async with httpx.AsyncClient() as raw:
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


async def _by_title(session: AsyncSession, connector: Connector) -> dict[str, Material]:
    with tenant_scope(connector.tenant_id):
        return {m.title: m for m in (await materials_of(session, connector)).values()}


async def _readers(
    session: AsyncSession, material: Material, people: dict[str, User]
) -> set[str]:
    """Кто из сотрудников kronto видит закрытый материал (по логину)."""
    names = {user.id: name for name, user in people.items()}
    with tenant_scope(material.tenant_id):
        return {names[i] for i in await access_of(session, material)}


async def test_sync_gives_each_employee_only_what_confluence_lets_them_read(
    stand_address: None,
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    people: dict[str, User],
) -> None:
    connector = await _connector(session)

    await _sync(session_maker, connector)

    by_title = await _by_title(session, connector)
    for title, readers in EXPECTED_READERS.items():
        material = by_title[title]
        if readers is None:
            assert material.visibility == MaterialVisibility.TENANT.value, title
            continue
        assert material.visibility == MaterialVisibility.RESTRICTED.value, title
        # admin есть в Confluence, но не в kronto — прав не получает.
        assert await _readers(session, material, people) == readers - {"admin"}, title
    assert not set(HIDDEN_FROM_SERVICE) & set(by_title)
    assert "Монолит и воркер" in by_title["Архитектура"].content
    assert "Заявление на отпуск" in by_title["Заявление.docx"].content
    assert "Грейд 1" in by_title["Сетка.md"].content
    assert by_title["Заявление.docx"].visibility == MaterialVisibility.TENANT.value
    assert await _readers(session, by_title["Сетка.md"], people) == {
        "alice",
        "carol",
        "svc-kronto",
    }


@pytest.fixture
def confluence_admin() -> Iterator[Seeder]:
    if not ADMIN_TOKEN:
        pytest.skip("нужен CONFLUENCE_DC_ADMIN_TOKEN: тест меняет права на стенде")
    admin = Seeder(URL, ADMIN_TOKEN)
    yield admin
    admin.close()


async def test_access_revoked_in_confluence_leaves_kronto_on_next_sync(
    stand_address: None,
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    people: dict[str, User],
    confluence_admin: Seeder,
) -> None:
    """Права меняются в Confluence без новой версии страницы — kronto
    обязан догнать их следующим запуском: группа, ограничение, удаление.
    Стенд возвращается в исходное состояние в finally."""
    admin = confluence_admin
    draft = admin.create_page(Page("Черновик", "ENG", "<p>Временная страница.</p>"))
    architecture = admin.page_id("ENG", "Архитектура")
    connector = await _connector(session)
    try:
        await _sync(session_maker, connector)
        before = await _by_title(session, connector)
        assert "carol" in await _readers(session, before["Зарплаты"], people)
        assert before["Архитектура"].visibility == MaterialVisibility.TENANT.value
        assert "Черновик" in before

        admin.leave("carol", "hr")
        admin.restrict(architecture, groups=["hr"])
        admin.trash(draft)

        outcome = await _sync(session_maker, connector)

        after = await _by_title(session, connector)
        for title in ("Зарплаты", "Премии", "Сетка.md"):
            assert await _readers(session, after[title], people) == {
                "alice",
                "svc-kronto",
            }, title
        assert after["Архитектура"].visibility == MaterialVisibility.RESTRICTED.value
        assert await _readers(session, after["Архитектура"], people) == {
            "alice",
            "svc-kronto",
        }
        # Права поменялись, текст — нет: переиндексации не было.
        assert outcome.stats.updated == 0
        assert "Черновик" not in after
        assert outcome.stats.removed == 1
    finally:
        admin.join("carol", "hr")
        admin.unrestrict(architecture)
        admin.trash_and_purge(draft)
