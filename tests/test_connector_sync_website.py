"""Синхронизация с адаптером публичного сайта против поддельного сайта:
режим organization без ключей, видимость «вся компания», заголовок
страницы из HTML, обновление по lastmod и удаление пропавших страниц."""

from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.registry import default_registry
from corp_ed.connectors.website import adapter as website_adapter
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.secrets import SecretBox
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Connector, Tenant
from corp_ed.domain.types import (
    ConnectorMode,
    MaterialVisibility,
    SyncRunStatus,
    SyncTrigger,
)
from corp_ed.services.connector_sync_service import ConnectorSyncService, SyncOutcome
from tests.connectors.fake_site import BASE, FakeSite, page
from tests.fake_connector import plain_extractor
from tests.test_connector_sync import materials_of

KEY = Fernet.generate_key().decode()


def urlset(*entries: tuple[str, str]) -> bytes:
    items = "".join(
        f"<url><loc>{BASE}{path}</loc><lastmod>{lastmod}</lastmod></url>"
        for path, lastmod in entries
    )
    return f"<urlset>{items}</urlset>".encode()


@pytest.fixture(autouse=True)
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    # Синхронизация собирает адаптер с паузами; в тесте — без них.
    monkeypatch.setattr(website_adapter, "MIN_INTERVAL", 0.0)


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def site() -> FakeSite:
    site = FakeSite(robots=f"User-agent: *\nSitemap: {BASE}sitemap.xml\n")
    site.add(
        "/sitemap.xml",
        urlset(
            ("help/", "2026-09-01"),
            ("help/delivery", "2026-09-01"),
            ("help/rules.txt", "2026-09-01"),
        ),
    )
    site.add("/help/", page("Справка", "Всё о сервисе."))
    site.add("/help/delivery", page("Доставка", "Доставка — 3 дня."))
    site.add(
        "/help/rules.txt",
        "Правила: возврат 14 дней.".encode(),
        content_type="text/plain",
    )
    return site


@pytest.fixture
def service(
    session_maker: async_sessionmaker[AsyncSession],
    site: FakeSite,
    secrets: SecretBox,
) -> ConnectorSyncService:
    settings = ConnectorSettings(secrets_keys=KEY)  # type: ignore[arg-type]
    return ConnectorSyncService(
        session_maker,
        site.client(),
        default_registry(settings),
        secrets,
        settings,
        extractor=plain_extractor,
    )


async def make_connector(session: AsyncSession, secrets: SecretBox) -> Connector:
    connector = Connector(
        kind="website",
        name="Справка",
        mode=ConnectorMode.ORGANIZATION.value,
        modules=["pages", "files"],
        config={"url": f"{BASE}help/"},
        credentials=secrets.encrypt({}),
        credentials_set_at=datetime.now(UTC),
    )
    session.add(connector)
    await session.commit()
    return connector


async def run(service: ConnectorSyncService, connector: Connector) -> SyncOutcome:
    outcome = await service.run(
        connector.tenant_id, connector.id, trigger=SyncTrigger.MANUAL
    )
    assert outcome is not None
    return outcome


async def test_public_pages_become_company_wide_materials(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    site: FakeSite,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome.error_code
    assert outcome.stats.added == 3
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
    assert set(materials) == {
        f"web:{BASE}help/",
        f"web:{BASE}help/delivery",
        f"web:{BASE}help/rules.txt",
    }
    root = materials[f"web:{BASE}help/"]
    # Заголовок — из <title>, а не из адреса (в карте его нет).
    assert root.title == "Справка"
    assert "Всё о сервисе." in root.content
    assert "Меню" not in root.content  # навигация отброшена
    assert root.source_url == f"{BASE}help/"
    assert root.external_version == "lastmod:2026-09-01"
    for material in materials.values():
        assert material.visibility == MaterialVisibility.TENANT.value
    assert materials[f"web:{BASE}help/rules.txt"].content == "Правила: возврат 14 дней."


async def test_changed_and_vanished_pages_are_synced(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    site: FakeSite,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets)
    await run(service, connector)
    gets = len(site.calls("GET"))

    site.add(
        "/sitemap.xml", urlset(("help/", "2026-09-01"), ("help/delivery", "2026-10-01"))
    )
    site.add("/help/delivery", page("Доставка", "Доставка — 2 дня."))
    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert (outcome.stats.updated, outcome.stats.removed) == (1, 1)
    # Скачана заново только изменённая страница (плюс robots, начальная
    # страница для проверки и карта).
    assert site.calls("GET")[gets:] == [
        "/robots.txt",
        "/help/",
        "/sitemap.xml",
        "/help/delivery",
    ]
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
    assert set(materials) == {f"web:{BASE}help/", f"web:{BASE}help/delivery"}
    assert "2 дня" in materials[f"web:{BASE}help/delivery"].content
