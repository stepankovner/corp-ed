"""Синхронизация одного коннектора: источник → материалы → очередь ингеста.

Запускается воркером (worker.py::SyncWorker) в tenant_scope компании.
Один запуск:

1. учётные данные расшифровываются и проверяются (adapter.check);
   источник их отверг — коннектор (или грант сотрудника) останавливается
   до вмешательства человека, повторов нет;
2. adapter.list — постраничный обход; каждый документ сравнивается с
   базой по (connector_id, external_id): новый или изменился (version)
   → adapter.fetch → тот же конвейер, что у ручной загрузки
   (detect_format + песочница; страницы — html_to_markdown) → материал
   и задача ингеста; не изменился — только права;
3. права: режим organization — visibility и список почт от адаптера
   сопоставляются с users; режим per_user — документ виден тому, в чьём
   листинге он есть;
4. исчезнувшие из источника документы удаляются — но только если обход
   дошёл до конца: обрезанный бюджетом или сбоем листинг не повод
   удалять то, чего в нём не оказалось.

Каждый документ — своя транзакция: сбой одного не откатывает остальные,
а упавший воркер оставляет уже сделанное. Бюджет запуска — документы и
минуты (ConnectorSettings); остаток — следующим запуском.

Секреты живут в памяти только на время запуска и не логируются.
"""

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    FetchedContent,
    FetchedFile,
    FetchedPage,
    RemoteDocument,
    SourceAdapter,
    refreshed_credentials,
)
from corp_ed.connectors.html import html_to_markdown
from corp_ed.connectors.registry import AdapterRegistry, UnknownKindError
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient, OutboundURLError
from corp_ed.core.secrets import SecretBox, SecretDecryptionError
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    Connector,
    ConnectorSyncRun,
    Material,
    MaterialStatus,
)
from corp_ed.domain.types import (
    ConnectorMode,
    ConnectorStatus,
    GrantStatus,
    MaterialVisibility,
    SyncRunStatus,
    SyncTrigger,
)
from corp_ed.ingest.extract import ExtractionError, SourceFormat, detect_format
from corp_ed.ingest.sandbox import extract_isolated
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.connector_repository import (
    ConnectorRepository,
    GrantRepository,
    SyncRunRepository,
)
from corp_ed.repositories.ingest_job_repository import IngestJobRepository
from corp_ed.repositories.material_repository import MaterialRepository
from corp_ed.repositories.user_repository import UserRepository

logger = structlog.get_logger()

Extractor = Callable[[SourceFormat, bytes], Awaitable[str]]

# Коды остановки/ошибки запуска — видны админу компании.
ERROR_AUTH = "auth_failed"
ERROR_CREDENTIALS_MISSING = "credentials_missing"
ERROR_CREDENTIALS_UNREADABLE = "credentials_unreadable"
ERROR_KIND_UNKNOWN = "kind_unknown"
ERROR_SOURCE_UNAVAILABLE = "source_unavailable"
ERROR_BUDGET = "budget_exhausted"
ERROR_INTERNAL = "internal_error"
SOURCE_FORMAT_HTML = "html"


@dataclass
class SyncStats:
    seen: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    failed: int = 0
    # Сколько сотрудников (гранты) обошли в режиме per_user.
    grants: int = 0
    grants_expired: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class SyncOutcome:
    status: SyncRunStatus
    stats: SyncStats
    error_code: str | None = None
    # Стоит ли воркеру повторить задачу (источник временно недоступен).
    retryable: bool = False


@dataclass
class _Run:
    connector: Connector
    """ORM-объект: читать только после session.refresh — откат после сбоя
    документа сбрасывает его атрибуты."""
    connector_id: UUID
    kind: str
    modules: list[str]
    config: dict[str, str]
    row: ConnectorSyncRun
    stats: SyncStats
    deadline: datetime
    budget: int
    complete: bool = True
    error_code: str | None = None
    # external_id → material_id всех документов, встреченных в запуске.
    seen: dict[str, UUID] = field(default_factory=dict)


class _StopRunError(Exception):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class ConnectorSyncService:
    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        http: OutboundClient,
        registry: AdapterRegistry,
        secrets: SecretBox,
        settings: ConnectorSettings,
        *,
        extractor: Extractor = extract_isolated,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_maker = session_maker
        self.http = http
        self.registry = registry
        self.secrets = secrets
        self.settings = settings
        self.extractor = extractor
        self.now = now

    async def run(
        self, tenant_id: UUID, connector_id: UUID, *, trigger: SyncTrigger
    ) -> SyncOutcome | None:
        """Выполнить запуск. None — коннектор не найден или не активен."""
        with tenant_scope(tenant_id):
            async with self.session_maker() as session:
                return await self._run(session, connector_id, trigger)

    async def _run(
        self, session: AsyncSession, connector_id: UUID, trigger: SyncTrigger
    ) -> SyncOutcome | None:
        connectors = ConnectorRepository(session)
        connector = await connectors.get_with_credentials(connector_id)
        if connector is None or connector.status != ConnectorStatus.ACTIVE.value:
            logger.info("sync_skipped", connector_id=str(connector_id))
            return None

        started = self.now()
        row = await SyncRunRepository(session).create(
            ConnectorSyncRun(connector_id=connector.id, trigger=trigger.value)
        )
        await session.commit()
        run = _Run(
            connector=connector,
            connector_id=connector.id,
            kind=connector.kind,
            modules=list(connector.modules),
            config={str(k): str(v) for k, v in connector.config.items()},
            row=row,
            stats=SyncStats(),
            deadline=started + timedelta(minutes=self.settings.max_run_minutes),
            budget=self.settings.max_documents_per_run,
        )
        log = logger.bind(connector_id=str(connector.id), run_id=str(row.id))

        status: SyncRunStatus
        retryable = False
        try:
            if connector.mode == ConnectorMode.ORGANIZATION.value:
                await self._sync_organization(session, run)
            else:
                await self._sync_per_user(session, run)
            if run.error_code is not None:
                status = SyncRunStatus.FAILED
            elif not run.complete or run.stats.failed:
                status = SyncRunStatus.PARTIAL
            else:
                status = SyncRunStatus.SUCCEEDED
        except _StopRunError as exc:
            await session.rollback()
            status, run.error_code, retryable = (
                SyncRunStatus.FAILED,
                exc.code,
                exc.retryable,
            )
            log.warning("sync_stopped", code=exc.code)
        except Exception:
            await session.rollback()
            status, run.error_code = SyncRunStatus.FAILED, ERROR_INTERNAL
            log.exception("sync_failed")

        await self._finish(session, run, status)
        log.info("sync_finished", status=status.value, **run.stats.as_dict())
        return SyncOutcome(status, run.stats, run.error_code, retryable)

    # --- режимы ---------------------------------------------------------------

    async def _sync_organization(self, session: AsyncSession, run: _Run) -> None:
        connector = run.connector
        if connector.credentials is None:
            await self._stop_connector(session, run, ERROR_CREDENTIALS_MISSING)
            return
        credentials = self._decrypt(connector.credentials)
        if credentials is None:
            await self._stop_connector(session, run, ERROR_CREDENTIALS_UNREADABLE)
            return
        adapter: SourceAdapter | None = None
        try:
            adapter = self._adapter(run, credentials)
            if adapter is None:
                return
            await adapter.check()
            await self._walk(session, run, adapter, viewer=None)
        except (AdapterAuthError, AdapterConfigError) as exc:
            await self._stop_connector(session, run, exc.code)
            return
        except (AdapterError, OutboundURLError) as exc:
            raise _StopRunError(
                ERROR_SOURCE_UNAVAILABLE,
                retryable=getattr(exc, "retryable", False),
            ) from exc
        finally:
            # Токены могли обновиться и до сбоя: старый refresh уже не
            # действует, новый нужно сохранить при любом исходе.
            if adapter is not None:
                await self._persist_refresh(session, run, adapter, grant_id=None)
        if run.complete:
            run.stats.removed = await MaterialRepository(
                session
            ).delete_by_connector_except(run.connector_id, run.seen.keys())
            await session.commit()

    async def _sync_per_user(self, session: AsyncSession, run: _Run) -> None:
        connector = run.connector
        grants = GrantRepository(session)
        materials = MaterialRepository(session)
        # Секреты приложения (client_secret) — общие для всех сотрудников,
        # складываются с токенами каждого; без них адаптер OAuth не
        # продлит токен.
        app_credentials: Mapping[str, str] = {}
        if connector.credentials is not None:
            decrypted = self._decrypt(connector.credentials)
            if decrypted is None:
                await self._stop_connector(session, run, ERROR_CREDENTIALS_UNREADABLE)
                return
            app_credentials = decrypted
        # Плоские кортежи, а не ORM-объекты: откат транзакции после сбоя
        # документа сбрасывает загруженные атрибуты, и следующее чтение
        # ушло бы в базу из синхронного кода.
        active = [
            (grant.id, grant.user_id, grant.credentials)
            for grant in await grants.list_active_with_credentials(connector.id)
        ]
        for grant_id, user_id, token in active:
            run.stats.grants += 1
            credentials = self._decrypt(token)
            if credentials is None:
                await self._expire_grant(
                    session, run, grant_id, ERROR_CREDENTIALS_UNREADABLE
                )
                continue
            adapter: SourceAdapter | None = None
            try:
                adapter = self._adapter(run, {**app_credentials, **credentials})
                if adapter is None:
                    return
                await adapter.check()
                mine = await self._walk(session, run, adapter, viewer=user_id)
            except AdapterAuthError as exc:
                await self._expire_grant(session, run, grant_id, exc.code)
                continue
            except AdapterConfigError as exc:
                # Проблема подключения, а не сотрудника: гранты целы.
                await self._stop_connector(session, run, exc.code)
                return
            except (AdapterError, OutboundURLError) as exc:
                raise _StopRunError(
                    ERROR_SOURCE_UNAVAILABLE,
                    retryable=getattr(exc, "retryable", False),
                ) from exc
            finally:
                if adapter is not None:
                    await self._persist_refresh(
                        session, run, adapter, grant_id=grant_id
                    )
            if not run.complete:
                # Листинг сотрудника оборван бюджетом: чего в нём не
                # оказалось — не значит, что прав больше нет.
                break
            await materials.revoke_access_except(run.connector_id, user_id, mine)
            await session.commit()

        if run.complete:
            run.stats.removed = await materials.delete_by_connector_except(
                run.connector_id, run.seen.keys()
            )
            await session.commit()

    # --- обход ----------------------------------------------------------------

    async def _walk(
        self,
        session: AsyncSession,
        run: _Run,
        adapter: SourceAdapter,
        *,
        viewer: UUID | None,
    ) -> set[UUID]:
        """Обойти листинг; вернуть id материалов, которые в нём встретились.

        AdapterAuthError поднимается наверх: что делать с отвергнутыми
        учётными данными, решает режим (остановить коннектор или грант).
        """
        walked: set[UUID] = set()
        materials = MaterialRepository(session)
        users = UserRepository(session)
        jobs = IngestJobRepository(session)
        log = logger.bind(connector_id=str(run.connector_id))

        listing = adapter.list(list(run.modules))
        try:
            async for document in listing:
                # Бюджет — на РАБОТУ (скачанные и упавшие документы), а не на
                # просмотренные: иначе большой источник с неизменными
                # документами никогда не дошёл бы до новых.
                if _work_done(run.stats) >= run.budget or self.now() >= run.deadline:
                    run.complete = False
                    log.info("sync_budget_exhausted", seen=len(run.seen))
                    break
                if document.external_id in run.seen:
                    material = await materials.get_by_id(run.seen[document.external_id])
                    if material is None:
                        continue
                else:
                    run.stats.seen += 1
                    try:
                        material = await self._upsert(session, run, adapter, document)
                    except (AdapterError, ExtractionError, OutboundURLError) as exc:
                        if isinstance(exc, AdapterAuthError | AdapterConfigError):
                            raise
                        await session.rollback()
                        run.stats.failed += 1
                        log.warning(
                            "sync_document_failed",
                            external_id=document.external_id,
                            code=getattr(exc, "code", type(exc).__name__),
                        )
                        continue
                    run.seen[document.external_id] = material.id
                    if material.status is MaterialStatus.PENDING:
                        await jobs.enqueue(material.tenant_id, material.id)

                await self._apply_access(materials, users, material, document, viewer)
                await session.commit()
                walked.add(material.id)
        except (AdapterAuthError, AdapterConfigError):
            await session.rollback()
            raise
        except (AdapterError, OutboundURLError) as exc:
            await session.rollback()
            raise _StopRunError(
                ERROR_SOURCE_UNAVAILABLE, retryable=getattr(exc, "retryable", False)
            ) from exc
        return walked

    async def _upsert(
        self,
        session: AsyncSession,
        run: _Run,
        adapter: SourceAdapter,
        document: RemoteDocument,
    ) -> Material:
        materials = MaterialRepository(session)
        material = await materials.get_by_external_id(
            run.connector_id, document.external_id
        )
        if material is not None and material.external_version == document.version:
            material.synced_at = self.now()
            return material

        content = await adapter.fetch(
            document, max_bytes=self.settings.max_document_bytes
        )
        markdown, meta = await self._to_markdown(document, content)
        if material is None:
            material = Material(
                title=document.title[:200] or document.external_id[:200],
                content=markdown,
                connector_id=run.connector_id,
                external_id=document.external_id,
                source_url=document.url[:2048],
                external_version=document.version[:128],
                synced_at=self.now(),
                visibility=MaterialVisibility.RESTRICTED.value,
                **meta,
            )
            await materials.create(material)
            run.stats.added += 1
        else:
            if material.source_sha256 == meta["source_sha256"]:
                # Версия сменилась, содержимое — нет: переиндексация не нужна.
                material.external_version = document.version[:128]
                material.synced_at = self.now()
                return material
            material.title = document.title[:200] or material.title
            material.content = markdown
            material.source_url = document.url[:2048]
            material.external_version = document.version[:128]
            material.synced_at = self.now()
            material.status = MaterialStatus.PENDING
            material.status_error = None
            for key, value in meta.items():
                setattr(material, key, value)
            run.stats.updated += 1
        return material

    async def _to_markdown(
        self, document: RemoteDocument, content: FetchedContent
    ) -> tuple[str, dict[str, str | int | None]]:
        if isinstance(content, FetchedFile):
            if len(content.data) > self.settings.max_document_bytes:
                raise ExtractionError("document_too_large")
            detected = detect_format(content.filename, content.data)
            markdown = await self.extractor(detected.format, content.data)
            return markdown, {
                "source_filename": detected.filename,
                "source_format": detected.format.value,
                "source_sha256": hashlib.sha256(content.data).hexdigest(),
                "source_size": len(content.data),
            }
        if isinstance(content, FetchedPage):
            markdown = html_to_markdown(content.html)
            return markdown, {
                "source_filename": None,
                "source_format": SOURCE_FORMAT_HTML,
                "source_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
                "source_size": len(content.html.encode("utf-8")),
            }
        raise ExtractionError("unsupported_format")

    async def _apply_access(
        self,
        materials: MaterialRepository,
        users: UserRepository,
        material: Material,
        document: RemoteDocument,
        viewer: UUID | None,
    ) -> None:
        if viewer is not None:
            material.visibility = MaterialVisibility.RESTRICTED.value
            await materials.grant_access(material.id, viewer)
            return
        material.visibility = document.visibility.value
        if document.visibility is MaterialVisibility.RESTRICTED:
            known = await users.ids_by_emails(document.allowed_emails)
            await materials.replace_access(material.id, known.values())
        else:
            await materials.replace_access(material.id, ())

    # --- вспомогательное ------------------------------------------------------

    def _decrypt(self, token: str) -> Mapping[str, str] | None:
        try:
            return self.secrets.decrypt(token)
        except SecretDecryptionError:
            logger.error("connector_credentials_unreadable")
            return None

    def _adapter(
        self, run: _Run, credentials: Mapping[str, str]
    ) -> SourceAdapter | None:
        """Собрать адаптер. Фабрика может бросить AdapterAuthError или
        AdapterConfigError (не хватает полей) — их разбирает вызывающий."""
        try:
            return self.registry.build(run.kind, run.config, credentials, self.http)
        except UnknownKindError:
            run.error_code = ERROR_KIND_UNKNOWN
            logger.error("connector_kind_unknown", kind=run.kind)
            return None

    async def _persist_refresh(
        self,
        session: AsyncSession,
        run: _Run,
        adapter: SourceAdapter,
        *,
        grant_id: UUID | None,
    ) -> None:
        """Сохранить токены, обновлённые адаптером по ходу работы."""
        refreshed = refreshed_credentials(adapter)
        if refreshed is None:
            return
        await session.rollback()
        token = self.secrets.encrypt(refreshed)
        if grant_id is not None:
            grant = await GrantRepository(session).get_by_id(grant_id)
            if grant is None:
                return
            grant.credentials = token
        else:
            connector = run.connector
            await session.refresh(connector)
            connector.credentials = token
        await session.commit()
        logger.info(
            "connector_credentials_refreshed",
            connector_id=str(run.connector_id),
            grant_id=str(grant_id) if grant_id else None,
        )

    async def _stop_connector(
        self, session: AsyncSession, run: _Run, code: str
    ) -> None:
        """Источник отверг учётные данные: без человека дальше нельзя."""
        await session.rollback()
        connector = run.connector
        await session.refresh(connector)
        connector.status = ConnectorStatus.ERROR.value
        connector.last_error_code = code
        run.error_code = code
        AuditRepository(session).record(
            AuditAction.CONNECTOR_STOPPED,
            tenant_id=connector.tenant_id,
            target_type="connector",
            target_id=connector.id,
            details={"code": code},
        )
        await session.commit()

    async def _expire_grant(
        self, session: AsyncSession, run: _Run, grant_id: UUID, code: str
    ) -> None:
        await session.rollback()
        grant = await GrantRepository(session).get_by_id(grant_id)
        if grant is None:
            return
        grant.status = GrantStatus.EXPIRED.value
        grant.error_code = code
        run.stats.grants_expired += 1
        AuditRepository(session).record(
            AuditAction.CONNECTOR_GRANT_EXPIRED,
            tenant_id=grant.tenant_id,
            actor_id=None,
            target_type="connector",
            target_id=grant.connector_id,
            details={"user_id": str(grant.user_id), "code": code},
        )
        await session.commit()

    async def _finish(
        self, session: AsyncSession, run: _Run, status: SyncRunStatus
    ) -> None:
        finished = self.now()
        row = run.row
        connector = run.connector
        await session.refresh(row)
        await session.refresh(connector)
        row.status = status.value
        row.finished_at = finished
        row.stats = run.stats.as_dict()
        row.error_code = run.error_code or (None if run.complete else ERROR_BUDGET)
        connector.last_sync_at = finished
        if connector.status == ConnectorStatus.ACTIVE.value:
            connector.last_error_code = run.error_code
        session.add_all([row, connector])
        await session.commit()


def _work_done(stats: SyncStats) -> int:
    return stats.added + stats.updated + stats.failed


def status_kind(status: SyncRunStatus) -> Literal["ok", "warn", "error"]:
    """Для CLI и логов: как показывать итог."""
    if status is SyncRunStatus.SUCCEEDED:
        return "ok"
    if status is SyncRunStatus.PARTIAL:
        return "warn"
    return "error"
