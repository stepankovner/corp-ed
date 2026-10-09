"""Синхронизация с адаптером Google Drive против поддельного Google: режим
organization, права по почтам и «вся компания», экспорт Документа,
изменения и удаления при следующем запуске, отзыв ключа."""

import io
import re
import zipfile
from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.registry import default_registry
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.secrets import SecretBox
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Connector, Tenant, User
from corp_ed.domain.types import (
    ConnectorMode,
    ConnectorStatus,
    MaterialVisibility,
    SyncRunStatus,
    SyncTrigger,
)
from corp_ed.ingest.extract import SourceFormat
from corp_ed.services.connector_sync_service import ConnectorSyncService, SyncOutcome
from tests.connectors.fake_google import (
    ADMIN,
    DIRECTORY_API,
    DOMAIN,
    DRIVE_API,
    TOKEN_URL,
    FakeGoogle,
    perm,
    sample_google,
    service_account_key,
)
from tests.factories import make_user
from tests.test_connector_sync import access_of, materials_of, reload

KEY = Fernet.generate_key().decode()


def docx(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml", f"<w:document><w:t>{text}</w:t></w:document>"
        )
    return buffer.getvalue()


async def extractor(fmt: SourceFormat, data: bytes) -> str:
    """Вместо песочницы: текст txt как есть, текст docx — из document.xml."""
    if fmt is SourceFormat.DOCX:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml").decode()
        return " ".join(re.findall(r"<w:t>([^<]*)</w:t>", xml))
    return data.decode("utf-8")


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def server() -> FakeGoogle:
    server = sample_google()
    server.content["f-plan"] = docx("План запуска — в мае.")
    return server


@pytest.fixture
def service(
    session_maker: async_sessionmaker[AsyncSession],
    server: FakeGoogle,
    secrets: SecretBox,
) -> ConnectorSyncService:
    settings = ConnectorSettings(
        secrets_keys=KEY,
        google_token_url=TOKEN_URL,
        google_drive_api=DRIVE_API,
        google_directory_api=DIRECTORY_API,
    )  # type: ignore[arg-type]
    return ConnectorSyncService(
        session_maker,
        server.client(),
        default_registry(settings),
        secrets,
        settings,
        extractor=extractor,  # type: ignore[arg-type]
    )


async def make_connector(
    session: AsyncSession,
    secrets: SecretBox,
    *,
    modules: list[str],
    key: str | None = None,
) -> Connector:
    connector = Connector(
        kind="gdrive",
        name="Google Диск",
        mode=ConnectorMode.ORGANIZATION.value,
        modules=modules,
        config={"domains": DOMAIN, "admin_email": ADMIN},
        credentials=secrets.encrypt(
            {"service_account_key": key or service_account_key()}
        ),
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


async def _user(session: AsyncSession, email: str, name: str) -> User:
    from corp_ed.core.security import hash_password
    from corp_ed.domain.models import UserRole

    user = make_user(
        email=email,
        full_name=name,
        role=UserRole.EMPLOYEE,
        hashed_password=hash_password("Password-1234"),
    )
    session.add(user)
    await session.commit()
    return user


@pytest.fixture
async def anna(session: AsyncSession, tenant_ctx: Tenant) -> User:
    return await _user(session, "anna@example.com", "Анна")


@pytest.fixture
async def boris(session: AsyncSession, tenant_ctx: Tenant) -> User:
    return await _user(session, "Boris@Example.com", "Борис")


async def test_shared_drives_become_restricted_materials(
    session: AsyncSession,
    tenant_ctx: Tenant,
    anna: User,
    boris: User,
    secrets: SecretBox,
    server: FakeGoogle,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, modules=["shared_drives"])

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
    assert outcome.stats.added == 3
    assert outcome.stats.skipped_formats == {".png": 1}
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert set(materials) == {
            "gdrive:f-vacation",
            "gdrive:f-order",
            "gdrive:f-salary",
        }
        vacation = materials["gdrive:f-vacation"]
        assert vacation.content == "Отпуск — 28 дней."
        assert vacation.visibility == MaterialVisibility.RESTRICTED.value
        assert vacation.source_url == "https://docs.google.com/d/f-vacation/view"
        assert await access_of(session, vacation) == {anna.id, boris.id}
        # Папка с ограниченным доступом: только Борис.
        assert await access_of(session, materials["gdrive:f-salary"]) == {boris.id}


async def test_user_drives_export_documents_and_follow_changes(
    session: AsyncSession,
    tenant_ctx: Tenant,
    anna: User,
    boris: User,
    secrets: SecretBox,
    server: FakeGoogle,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, modules=["user_drives"])

    first = await run(service, connector)

    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        plan = materials["gdrive:f-plan"]
        assert plan.content == "План запуска — в мае."
        assert plan.source_format == "docx"
        assert plan.source_filename == "План.docx"
        assert await access_of(session, plan) == {anna.id, boris.id}
        partner = materials["gdrive:f-partner"]
        assert await access_of(session, partner) == {anna.id}
    assert first.stats.skipped_formats == {".form": 1}

    # Между запусками: Документ изменён и закрыт от Бориса, файл для
    # партнёра удалён.
    server.files["f-plan"]["modifiedTime"] = "2026-09-02T08:00:00.000Z"
    server.files["f-plan"]["_perms"] = [perm("user", "anna@example.com", "owner")]
    server.content["f-plan"] = docx("План запуска — в июне.")
    server.files.pop("f-partner")

    second = await run(service, connector)

    assert second.stats.updated == 1
    assert second.stats.removed == 1
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert "gdrive:f-partner" not in materials
        plan = materials["gdrive:f-plan"]
        assert plan.content == "План запуска — в июне."
        assert await access_of(session, plan) == {anna.id}
        # PDF «всем по ссылке» — вся компания.
        report = materials["gdrive:f-report"]
        assert report.visibility == MaterialVisibility.TENANT.value
        assert await access_of(session, report) == set()


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ({"key_revoked": True}, "invalid_grant"),
        ({"delegated": set()}, "delegation_missing_drive"),
    ],
)
async def test_rejected_key_or_delegation_stops_connector(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    server: FakeGoogle,
    service: ConnectorSyncService,
    setup: dict[str, Any],
    code: str,
) -> None:
    for name, value in setup.items():
        setattr(server, name, value)
    connector = await make_connector(session, secrets, modules=["shared_drives"])

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.FAILED
    assert outcome.error_code == code
    with tenant_scope(tenant_ctx.id):
        reloaded = await reload(session, connector)
        assert reloaded.status == ConnectorStatus.ERROR.value
        assert reloaded.last_error_code == code
