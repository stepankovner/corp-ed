"""Синхронизация коннекторов: оба режима, права, бюджет, ошибки, изоляция."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.base import FetchedFile, FetchedPage
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.core.secrets import SecretBox
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    AuditEvent,
    Connector,
    ConnectorSyncRun,
    ConnectorUserGrant,
    IngestJob,
    Material,
    MaterialAccess,
    MaterialStatus,
    Tenant,
    User,
)
from corp_ed.domain.types import (
    ConnectorMode,
    ConnectorStatus,
    GrantStatus,
    MaterialVisibility,
    RemoteDocumentKind,
    SyncRunStatus,
    SyncTrigger,
)
from corp_ed.repositories.audit_repository import AuditAction
from corp_ed.services.connector_sync_service import (
    ERROR_AUTH,
    ERROR_BUDGET,
    ERROR_CREDENTIALS_MISSING,
    ERROR_CREDENTIALS_UNREADABLE,
    ERROR_SOURCE_UNAVAILABLE,
    ConnectorSyncService,
    SyncOutcome,
)
from corp_ed.services.retention_service import RetentionService
from tests.fake_connector import (
    FAKE_KIND,
    FAKE_PER_USER_KIND,
    FakeSource,
    doc,
    make_registry,
    plain_extractor,
)

KEY = Fernet.generate_key().decode()
BASE_URL = "https://portal.example.com/"


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def source() -> FakeSource:
    return FakeSource()


def make_settings(**overrides: object) -> ConnectorSettings:
    return ConnectorSettings(secrets_keys=KEY, **overrides)  # type: ignore[arg-type]


@pytest.fixture
async def http() -> OutboundClient:
    return OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    )


def make_service(
    session_maker: async_sessionmaker[AsyncSession],
    source: FakeSource,
    secrets: SecretBox,
    http: OutboundClient,
    settings: ConnectorSettings | None = None,
) -> ConnectorSyncService:
    return ConnectorSyncService(
        session_maker,
        http,
        make_registry(source),
        secrets,
        settings or make_settings(),
        extractor=plain_extractor,
    )


@pytest.fixture
def service(
    session_maker: async_sessionmaker[AsyncSession],
    source: FakeSource,
    secrets: SecretBox,
    http: OutboundClient,
) -> ConnectorSyncService:
    return make_service(session_maker, source, secrets, http)


async def make_connector(
    session: AsyncSession,
    secrets: SecretBox,
    *,
    kind: str = FAKE_KIND,
    token: str | None = "t-org",
    modules: list[str] | None = None,
    status: ConnectorStatus = ConnectorStatus.ACTIVE,
) -> Connector:
    per_user = kind == FAKE_PER_USER_KIND
    connector = Connector(
        kind=kind,
        name="Портал",
        mode=(ConnectorMode.PER_USER if per_user else ConnectorMode.ORGANIZATION).value,
        modules=modules or ["docs"],
        config={"base_url": BASE_URL},
        credentials=(
            secrets.encrypt({"token": token}) if token and not per_user else None
        ),
        credentials_set_at=datetime.now(UTC) if token and not per_user else None,
        status=status.value,
    )
    session.add(connector)
    await session.commit()
    return connector


async def make_grant(
    session: AsyncSession,
    secrets: SecretBox,
    connector: Connector,
    user: User,
    token: str,
) -> ConnectorUserGrant:
    grant = ConnectorUserGrant(
        connector_id=connector.id,
        user_id=user.id,
        credentials=secrets.encrypt({"token": token}),
    )
    session.add(grant)
    await session.commit()
    return grant


async def run(service: ConnectorSyncService, connector: Connector) -> SyncOutcome:
    outcome = await service.run(
        connector.tenant_id, connector.id, trigger=SyncTrigger.MANUAL
    )
    assert outcome is not None
    return outcome


async def materials_of(
    session: AsyncSession, connector: Connector
) -> dict[str, Material]:
    result = await session.scalars(
        select(Material)
        .where(Material.connector_id == connector.id)
        .execution_options(populate_existing=True)
    )
    return {m.external_id or "": m for m in result}


async def access_of(session: AsyncSession, material: Material) -> set[UUID]:
    result = await session.scalars(
        select(MaterialAccess.user_id).where(MaterialAccess.material_id == material.id)
    )
    return set(result)


async def runs_of(
    session: AsyncSession, connector: Connector
) -> list[ConnectorSyncRun]:
    result = await session.scalars(
        select(ConnectorSyncRun)
        .where(ConnectorSyncRun.connector_id == connector.id)
        .order_by(ConnectorSyncRun.started_at)
        .execution_options(populate_existing=True)
    )
    return list(result)


async def reload(session: AsyncSession, connector: Connector) -> Connector:
    result = await session.scalars(
        select(Connector)
        .where(Connector.id == connector.id)
        .execution_options(populate_existing=True)
    )
    return result.one()


# --- режим organization ---------------------------------------------------------


async def test_first_sync_creates_materials_queues_ingest_and_mirrors_acl(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    secrets: SecretBox,
    source: FakeSource,
    service: ConnectorSyncService,
) -> None:
    source.add(doc("d1", "Регламент отпусков"), "Отпуск 28 дней.")
    source.add(
        doc(
            "d2",
            "Зарплаты",
            visibility=MaterialVisibility.RESTRICTED,
            emails=[employee.email.upper(), "nobody@else.ru"],
        ),
        "Секретно.",
    )
    connector = await make_connector(session, secrets)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.as_dict() | {"seen": 2, "added": 2} == outcome.stats.as_dict()
    found = await materials_of(session, connector)
    assert set(found) == {"d1", "d2"}
    public, secret = found["d1"], found["d2"]
    assert public.title == "Регламент отпусков"
    assert public.content == "Отпуск 28 дней."
    assert public.source_url == "https://portal.example.com/docs/d1"
    assert public.external_version == "v1"
    assert public.source_format == "txt" and public.source_sha256
    assert public.status is MaterialStatus.PENDING
    assert public.visibility == "tenant" and await access_of(session, public) == set()
    assert secret.visibility == "restricted"
    # Почта сопоставлена без учёта регистра; незнакомая — отброшена.
    assert await access_of(session, secret) == {employee.id}

    jobs = (await session.scalars(select(IngestJob))).all()
    assert {job.material_id for job in jobs} == {public.id, secret.id}
    [row] = await runs_of(session, connector)
    assert row.status == "succeeded" and row.stats["added"] == 2
    assert row.trigger == "manual" and row.finished_at is not None
    assert (await reload(session, connector)).last_sync_at is not None
    assert source.listed_modules == [["docs"]]


async def test_unchanged_documents_are_not_downloaded_again(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    source: FakeSource,
    service: ConnectorSyncService,
) -> None:
    source.add(doc("d1"), "Текст.")
    connector = await make_connector(session, secrets)
    await run(service, connector)
    outcome = await run(service, connector)

    assert source.fetch_calls == ["d1"]
    assert (outcome.stats.seen, outcome.stats.added, outcome.stats.updated) == (1, 0, 0)
    assert outcome.status is SyncRunStatus.SUCCEEDED


async def test_changed_document_is_refetched_and_reindexed(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    source: FakeSource,
    service: ConnectorSyncService,
) -> None:
    source.add(doc("d1", "Старое"), "Старый текст.")
    connector = await make_connector(session, secrets)
    await run(service, connector)
    # Как будто воркер уже проиндексировал.
    material = (await materials_of(session, connector))["d1"]
    material.status = MaterialStatus.READY
    await session.commit()
    job = (await session.scalars(select(IngestJob))).one()
    job.status = "DONE"  # type: ignore[assignment]
    await session.commit()

    source.documents = [doc("d1", "Новое", version="v2")]
    source.contents["d1"] = FetchedFile(b"New text.", "d1.txt")
    outcome = await run(service, connector)

    assert outcome.stats.updated == 1
    material = (await materials_of(session, connector))["d1"]
    assert (material.title, material.content, material.external_version) == (
        "Новое",
        "New text.",
        "v2",
    )
    assert material.status is MaterialStatus.PENDING
    assert len((await session.scalars(select(IngestJob))).all()) == 2


async def test_new_version_with_same_content_skips_reindex(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    source: FakeSource,
    service: ConnectorSyncService,
) -> None:
    source.add(doc("d1"), "Тот же текст.")
    connector = await make_connector(session, secrets)
    await run(service, connector)
    source.documents = [doc("d1", version="v2")]
    outcome = await run(service, connector)

    assert source.fetch_calls == ["d1", "d1"]
    assert outcome.stats.updated == 0
    assert (await materials_of(session, connector))["d1"].external_version == "v2"
    assert len((await session.scalars(select(IngestJob))).all()) == 1


async def test_vanished_documents_are_removed_after_a_complete_listing(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    source: FakeSource,
    service: ConnectorSyncService,
) -> None:
    source.add(doc("d1"), "Один.")
    source.add(doc("d2"), "Два.")
    connector = await make_connector(session, secrets)
    await run(service, connector)
    source.documents = [source.documents[1]]
    outcome = await run(service, connector)

    assert outcome.stats.removed == 1
    assert set(await materials_of(session, connector)) == {"d2"}


async def test_budget_makes_run_partial_and_never_deletes(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    secrets: SecretBox,
    source: FakeSource,
    http: OutboundClient,
) -> None:
    service = make_service(
        session_maker, source, secrets, http, make_settings(max_documents_per_run=1)
    )
    source.add(doc("d1"), "Один.")
    source.add(doc("d2"), "Два.")
    connector = await make_connector(session, secrets)

    first = await run(service, connector)
    assert first.status is SyncRunStatus.PARTIAL
    assert first.stats.added == 1 and first.stats.removed == 0
    [row] = await runs_of(session, connector)
    assert row.error_code == ERROR_BUDGET

    # Второй запуск: неизменный d1 бюджет не тратит, d2 докачивается.
    second = await run(service, connector)
    assert second.status is SyncRunStatus.SUCCEEDED
    assert second.stats.added == 1
    assert set(await materials_of(session, connector)) == {"d1", "d2"}


async def test_deadline_stops_the_run(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    secrets: SecretBox,
    source: FakeSource,
    http: OutboundClient,
) -> None:
    clock = iter(
        [
            datetime(2026, 9, 25, tzinfo=UTC) + timedelta(minutes=m)
            for m in (0, 0, 0, 30, 30, 30, 30, 30)
        ]
    )
    service = ConnectorSyncService(
        session_maker,
        http,
        make_registry(source),
        secrets,
        make_settings(max_run_minutes=20),
        extractor=plain_extractor,
        now=lambda: next(clock),
    )
    source.add(doc("d1"), "Один.")
    source.add(doc("d2"), "Два.")
    connector = await make_connector(session, secrets)

    outcome = await run(service, connector)
    assert outcome.status is SyncRunStatus.PARTIAL
    assert outcome.stats.added == 1


async def test_rejected_credentials_stop_the_connector(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    source: FakeSource,
    service: ConnectorSyncService,
) -> None:
    source.rejected_tokens.add("t-org")
    source.add(doc("d1"), "Текст.")
    connector = await make_connector(session, secrets)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.FAILED
    assert outcome.error_code == ERROR_AUTH and not outcome.retryable
    reloaded = await reload(session, connector)
    assert reloaded.status == "error" and reloaded.last_error_code == ERROR_AUTH
    assert await materials_of(session, connector) == {}
    events = (
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == AuditAction.CONNECTOR_STOPPED.value
            )
        )
    ).all()
    assert len(events) == 1 and events[0].details == {"code": ERROR_AUTH}
    # Остановленный не запускается, пока админ не даст новые данные.
    assert (
        await service.run(
            connector.tenant_id, connector.id, trigger=SyncTrigger.SCHEDULE
        )
        is None
    )
    assert len(await runs_of(session, connector)) == 1


async def test_unavailable_source_is_retryable_and_keeps_connector_active(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    source: FakeSource,
    service: ConnectorSyncService,
) -> None:
    source.unavailable = True
    connector = await make_connector(session, secrets)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.FAILED
    assert outcome.error_code == ERROR_SOURCE_UNAVAILABLE and outcome.retryable
    reloaded = await reload(session, connector)
    assert reloaded.status == "active"
    assert reloaded.last_error_code == ERROR_SOURCE_UNAVAILABLE


async def test_one_broken_document_does_not_stop_the_run(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    source: FakeSource,
    service: ConnectorSyncService,
) -> None:
    source.add(doc("bad"), "x")
    source.fetch_errors["bad"] = "http_500"
    source.add(doc("exe", filename="virus.exe"), "MZ...")
    source.add(doc("big"), "x" * 10)
    source.add(doc("good"), "Да.")  # 5 байт в UTF-8
    connector = await make_connector(session, secrets)
    service.settings = make_settings(max_document_bytes=8)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.PARTIAL
    assert (outcome.stats.failed, outcome.stats.added) == (3, 1)
    assert set(await materials_of(session, connector)) == {"good"}


async def test_pages_become_markdown(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    source: FakeSource,
    service: ConnectorSyncService,
) -> None:
    page = doc("p1", "Вики", kind=RemoteDocumentKind.PAGE, module="wiki", filename=None)
    source.documents.append(page)
    source.contents["p1"] = FetchedPage(
        "<h1>Правила</h1><script>x()</script><p>Текст.</p>"
    )
    connector = await make_connector(session, secrets, modules=["docs", "wiki"])

    await run(service, connector)

    material = (await materials_of(session, connector))["p1"]
    assert material.content == "# Правила\n\nТекст."
    assert material.source_format == "html" and material.source_filename is None


async def test_missing_or_unreadable_credentials_stop_the_connector(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    source: FakeSource,
    service: ConnectorSyncService,
) -> None:
    without = await make_connector(session, secrets, token=None)
    outcome = await run(service, without)
    assert outcome.error_code == ERROR_CREDENTIALS_MISSING
    assert (await reload(session, without)).status == "error"

    foreign_key = SecretBox([Fernet.generate_key().decode()])
    unreadable = await make_connector(session, foreign_key)
    outcome = await run(service, unreadable)
    assert outcome.error_code == ERROR_CREDENTIALS_UNREADABLE
    assert (await reload(session, unreadable)).status == "error"
    assert source.check_calls == []


async def test_paused_connector_is_skipped(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, status=ConnectorStatus.PAUSED)
    assert (
        await service.run(
            connector.tenant_id, connector.id, trigger=SyncTrigger.SCHEDULE
        )
        is None
    )
    assert await runs_of(session, connector) == []


async def test_sync_never_touches_another_tenant(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    source: FakeSource,
    service: ConnectorSyncService,
) -> None:
    """У другой компании такой же коннектор и такой же external_id:
    удаление «исчезнувших» документов не должно достать до неё."""
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        foreign_connector = await make_connector(session, secrets)
        foreign_material = Material(
            title="Чужой",
            content="x",
            connector_id=foreign_connector.id,
            external_id="d1",
            external_version="v1",
        )
        session.add(foreign_material)
        await session.commit()

    source.add(doc("d2"), "Свой.")
    connector = await make_connector(session, secrets)
    await run(service, connector)

    with tenant_scope(other.id):
        assert set(await materials_of(session, foreign_connector)) == {"d1"}
    assert set(await materials_of(session, connector)) == {"d2"}


# --- режим per_user -------------------------------------------------------------


async def test_per_user_visibility_follows_each_employee_listing(
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    secrets: SecretBox,
    source: FakeSource,
    service: ConnectorSyncService,
) -> None:
    source.add(doc("d1"), "Один.")
    source.add(doc("d2"), "Два.")
    source.visible_to = {"t-admin": {"d1", "d2"}, "t-emp": {"d2"}}
    connector = await make_connector(session, secrets, kind=FAKE_PER_USER_KIND)
    await make_grant(session, secrets, connector, admin, "t-admin")
    await make_grant(session, secrets, connector, employee, "t-emp")

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.grants == 2 and outcome.stats.added == 2
    assert sorted(source.fetch_calls) == ["d1", "d2"]  # каждый — один раз
    found = await materials_of(session, connector)
    assert all(m.visibility == "restricted" for m in found.values())
    assert await access_of(session, found["d1"]) == {admin.id}
    assert await access_of(session, found["d2"]) == {admin.id, employee.id}


async def test_per_user_rights_disappear_with_the_listing(
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    secrets: SecretBox,
    source: FakeSource,
    service: ConnectorSyncService,
) -> None:
    source.add(doc("d1"), "Один.")
    source.add(doc("d2"), "Два.")
    connector = await make_connector(session, secrets, kind=FAKE_PER_USER_KIND)
    await make_grant(session, secrets, connector, admin, "t-admin")
    await make_grant(session, secrets, connector, employee, "t-emp")
    await run(service, connector)

    source.visible_to = {"t-emp": set()}  # у сотрудника отозвали всё
    await run(service, connector)
    found = await materials_of(session, connector)
    assert await access_of(session, found["d1"]) == {admin.id}
    assert await access_of(session, found["d2"]) == {admin.id}

    source.visible_to = {"t-admin": set(), "t-emp": set()}
    outcome = await run(service, connector)
    assert outcome.stats.removed == 2
    assert await materials_of(session, connector) == {}


async def test_rejected_employee_token_expires_only_that_grant(
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    secrets: SecretBox,
    source: FakeSource,
    service: ConnectorSyncService,
) -> None:
    source.add(doc("d1"), "Один.")
    source.rejected_tokens.add("t-emp")
    connector = await make_connector(session, secrets, kind=FAKE_PER_USER_KIND)
    await make_grant(session, secrets, connector, admin, "t-admin")
    grant = await make_grant(session, secrets, connector, employee, "t-emp")

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert (outcome.stats.grants, outcome.stats.grants_expired) == (2, 1)
    await session.refresh(grant)
    assert grant.status == GrantStatus.EXPIRED.value and grant.error_code == ERROR_AUTH
    assert (await reload(session, connector)).status == "active"
    assert await access_of(session, (await materials_of(session, connector))["d1"]) == {
        admin.id
    }
    events = (
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == AuditAction.CONNECTOR_GRANT_EXPIRED.value
            )
        )
    ).all()
    assert len(events) == 1 and events[0].details["user_id"] == str(employee.id)


async def test_per_user_without_grants_does_nothing(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    source: FakeSource,
    service: ConnectorSyncService,
) -> None:
    source.add(doc("d1"), "Один.")
    connector = await make_connector(session, secrets, kind=FAKE_PER_USER_KIND)
    outcome = await run(service, connector)
    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.as_dict()["seen"] == 0
    assert source.check_calls == []


# --- хранение журнала запусков ----------------------------------------------------


async def test_purge_removes_old_sync_runs(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    secrets: SecretBox,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets)
    await run(service, connector)
    [row] = await runs_of(session, connector)
    row.started_at = datetime.now(UTC) - timedelta(days=100)
    await session.commit()

    report = await RetentionService(
        session_maker, qa_log_days=90, sync_run_days=90
    ).purge()

    assert report.sync_runs == 1
    assert await runs_of(session, connector) == []


# --- ротация ключа ---------------------------------------------------------------


async def test_rotation_reencrypts_connectors_and_grants(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
    secrets: SecretBox,
) -> None:
    from corp_ed.services.connector_secrets_rotation import ConnectorSecretsRotation

    connector = await make_connector(session, secrets)
    per_user = await make_connector(session, secrets, kind=FAKE_PER_USER_KIND)
    grant = await make_grant(session, secrets, per_user, employee, "t-emp")
    foreign = await make_connector(session, SecretBox([Fernet.generate_key().decode()]))

    new_key = Fernet.generate_key().decode()
    rotated = SecretBox([new_key, KEY])
    report = await ConnectorSecretsRotation(session_maker, rotated).rotate()

    assert (report.connectors, report.grants, report.unreadable) == (1, 1, 1)
    only_new = SecretBox([new_key])
    fresh = await reload(session, connector)
    await session.refresh(fresh, ["credentials"])
    assert only_new.decrypt(fresh.credentials or "") == {"token": "t-org"}
    await session.refresh(grant, ["credentials"])
    assert only_new.decrypt(grant.credentials) == {"token": "t-emp"}
    unreadable = await reload(session, foreign)
    await session.refresh(unreadable, ["credentials"])
    with pytest.raises(Exception):  # noqa: B017 — чужой ключ так и остался нечитаемым
        only_new.decrypt(unreadable.credentials or "")
