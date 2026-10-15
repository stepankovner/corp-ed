"""Приём документов от агента сетевых папок (docs/SMB-AGENT.md §3).

Агент в сети клиента обходит файловый сервер сам и присылает сюда
изменённые файлы и права. Здесь — тот же конвейер, что у коннекторов
(connector_sync_service.store_document: формат, лимиты, песочница), и та
же модель прав режима organization: документ виден только сотрудникам,
чьи почты агент вычислил из прав NTFS.

Ключ агента — `kag_<тенант>_<подключение>_<секрет>`: тенант и подключение
в открытую, чтобы найти запись без перебора; в учётных данных подключения
(SecretBox) — только SHA-256 секрета, сравнение — за постоянное время.

Обход: POST /runs открывает запуск (журнал connector_sync_runs, trigger
agent), каждый документ и каждое обновление прав ставят synced_at = now,
POST /runs/{id}/finish с complete удаляет документы подключения, которых
обход не коснулся. Незавершённый обход ничего не удаляет.
"""

import hashlib
import hmac
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.connectors.base import FetchedFile, RemoteDocument
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.secrets import SecretBox, SecretDecryptionError
from corp_ed.domain.models import Connector, ConnectorSyncRun, Material, MaterialStatus
from corp_ed.domain.types import (
    ConnectorStatus,
    MaterialVisibility,
    RemoteDocumentKind,
    SyncRunStatus,
    SyncTrigger,
)
from corp_ed.repositories.connector_repository import ConnectorRepository
from corp_ed.repositories.ingest_job_repository import IngestJobRepository
from corp_ed.repositories.material_repository import MaterialRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.connector_sync_service import (
    DocumentChange,
    Extractor,
    store_document,
)

logger = structlog.get_logger()

TOKEN_PREFIX = "kag"  # noqa: S105 — префикс формата, не секрет
TOKEN_FIELD = "agent_token_sha256"  # noqa: S105 — имя поля
MAX_ACCESS_BATCH = 500
MAX_EMAILS_PER_DOCUMENT = 5000
STATE_PAGE_MAX = 1000
RUN_SUPERSEDED = "superseded"


class AgentTokenError(Exception):
    """Ключ агента неверен, отозван или подключение не агентское."""


@dataclass(frozen=True)
class AgentToken:
    tenant_id: UUID
    connector_id: UUID
    secret: str


def parse_token(raw: str) -> AgentToken:
    parts = raw.split("_")
    if len(parts) != 4 or parts[0] != TOKEN_PREFIX or len(parts[3]) < 32:
        raise AgentTokenError("malformed")
    try:
        return AgentToken(UUID(parts[1]), UUID(parts[2]), parts[3])
    except ValueError as exc:
        raise AgentTokenError("malformed") from exc


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def issue_token(connector: Connector, box: SecretBox, now: datetime) -> str:
    """Выпустить ключ агента; прежний перестаёт действовать. Ключ виден
    один раз — в kronto остаётся только его хеш."""
    secret = secrets.token_urlsafe(32).replace("_", "-")
    connector.credentials = box.encrypt({TOKEN_FIELD: _digest(secret)})
    connector.credentials_set_at = now
    return f"{TOKEN_PREFIX}_{connector.tenant_id}_{connector.id}_{secret}"


async def authenticate(
    session: AsyncSession, token: AgentToken, box: SecretBox, kinds: frozenset[str]
) -> Connector:
    """Подключение по ключу. Вызывать внутри tenant_scope(token.tenant_id).

    AgentTokenError — нет такого подключения, оно не агентское, ключ не
    совпал; ConnectorStoppedError — подключение не активно."""
    connector = await ConnectorRepository(session).get_with_credentials(
        token.connector_id
    )
    if connector is None or connector.kind not in kinds or not connector.credentials:
        raise AgentTokenError("unknown")
    try:
        stored = box.decrypt(connector.credentials).get(TOKEN_FIELD, "")
    except SecretDecryptionError as exc:
        raise AgentTokenError("unreadable") from exc
    if not stored or not hmac.compare_digest(stored, _digest(token.secret)):
        raise AgentTokenError("mismatch")
    if connector.status != ConnectorStatus.ACTIVE.value:
        raise ConnectorStoppedError(connector.status)
    return connector


class ConnectorStoppedError(Exception):
    """Подключение остановлено или отключено админом: агент ждёт."""


class RunNotFoundError(Exception):
    pass


@dataclass(frozen=True)
class AgentDocument:
    external_id: str
    version: str
    title: str
    path: str
    filename: str
    modified_at: datetime | None
    allowed_emails: Sequence[str]


class AgentIngestService:
    def __init__(
        self,
        session: AsyncSession,
        connector: Connector,
        settings: ConnectorSettings,
        *,
        extractor: Extractor,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session = session
        self.connector = connector
        # Плоские значения: откат после неудачного документа сбрасывает
        # атрибуты ORM-объекта, а читать их потом — запрос из синхронного кода.
        self.connector_id = connector.id
        self.tenant_id = connector.tenant_id
        self.settings = settings
        self.extractor = extractor
        self.now = now

    async def state(
        self, *, after: str | None, limit: int
    ) -> tuple[list[tuple[str, str]], str | None]:
        limit = max(1, min(limit, STATE_PAGE_MAX))
        rows = await MaterialRepository(self.session).external_versions(
            self.connector_id, after=after, limit=limit
        )
        following = rows[-1][0] if len(rows) == limit else None
        return rows, following

    async def start_run(self) -> ConnectorSyncRun:
        # Прежний незавершённый обход — как упавший: без удаления.
        for open_run in await self._open_runs():
            open_run.status = SyncRunStatus.FAILED.value
            open_run.finished_at = self.now()
            open_run.error_code = RUN_SUPERSEDED
        run = ConnectorSyncRun(
            connector_id=self.connector_id,
            trigger=SyncTrigger.AGENT.value,
            started_at=self.now(),
            stats={},
        )
        self.session.add(run)
        await self.session.flush()
        await self.session.commit()
        return run

    async def put_document(
        self, run_id: UUID, document: AgentDocument, data: bytes
    ) -> tuple[Material, DocumentChange]:
        run = await self._running(run_id)
        materials = MaterialRepository(self.session)
        existing = await materials.get_by_external_id(
            self.connector_id, document.external_id
        )
        now = self.now()
        change: DocumentChange
        if existing is not None and existing.external_version == document.version:
            existing.synced_at = now
            material, change = existing, "unchanged"
        else:
            material, change = await store_document(
                self.session,
                connector_id=self.connector_id,
                existing=existing,
                document=RemoteDocument(
                    external_id=document.external_id,
                    title=document.title,
                    url=document.path,
                    version=document.version,
                    kind=RemoteDocumentKind.FILE,
                    module="files",
                    path=document.path,
                    filename=document.filename,
                    size=len(data),
                    modified_at=document.modified_at,
                ),
                content=FetchedFile(data=data, filename=document.filename),
                settings=self.settings,
                extractor=self.extractor,
                now=now,
            )
            if material.status is MaterialStatus.PENDING:
                await IngestJobRepository(self.session).enqueue(
                    material.tenant_id, material.id
                )
        await self._apply_access(material, document.allowed_emails)
        self._count(run, change)
        await self.session.commit()
        return material, change

    async def update_access(
        self, run_id: UUID, items: Sequence[tuple[str, Sequence[str]]]
    ) -> tuple[int, list[str]]:
        """Права документов, файлы которых не менялись. Возвращает число
        обновлённых и external_id, которых в kronto нет (агент пришлёт файл)."""
        run = await self._running(run_id)
        materials = MaterialRepository(self.session)
        now = self.now()
        updated, unknown = 0, []
        for external_id, emails in items:
            material = await materials.get_by_external_id(
                self.connector_id, external_id
            )
            if material is None:
                unknown.append(external_id)
                continue
            material.synced_at = now
            await self._apply_access(material, emails)
            updated += 1
        self._bump(run, "seen", updated)
        await self.session.commit()
        return updated, unknown

    async def finish(
        self, run_id: UUID, *, complete: bool, agent_stats: dict[str, Any]
    ) -> ConnectorSyncRun:
        run = await self._running(run_id)
        removed = 0
        if complete:
            removed = await MaterialRepository(
                self.session
            ).delete_by_connector_synced_before(self.connector_id, run.started_at)
        stats = dict(run.stats or {})
        stats["removed"] = removed
        # Счётчики агента (пропуски по формату, размеру, правам) — как есть,
        # только числа: журнал виден админу компании.
        for key, value in agent_stats.items():
            if isinstance(value, int) and not isinstance(value, bool):
                stats[f"agent_{key}"[:64]] = value
        failed = int(stats.get("failed", 0))
        run.stats = stats
        run.status = (
            SyncRunStatus.SUCCEEDED.value
            if complete and not failed
            else SyncRunStatus.PARTIAL.value
        )
        run.finished_at = self.now()
        await self.session.refresh(self.connector)
        self.connector.last_sync_at = run.finished_at
        self.connector.last_error_code = None
        await self.session.commit()
        logger.info(
            "agent_run_finished",
            connector_id=str(self.connector_id),
            run_id=str(run.id),
            complete=complete,
            removed=removed,
        )
        return run

    async def count_failure(self, run_id: UUID) -> None:
        """Документ не принят (формат, размер, пустой текст): в статистику."""
        run = await self._running(run_id)
        self._bump(run, "failed", 1)
        await self.session.commit()

    # --- вспомогательное --------------------------------------------------------------

    async def _apply_access(self, material: Material, emails: Sequence[str]) -> None:
        material.visibility = MaterialVisibility.RESTRICTED.value
        known = await UserRepository(self.session).ids_by_emails(emails)
        await MaterialRepository(self.session).replace_access(
            material.id, known.values()
        )

    async def _open_runs(self) -> list[ConnectorSyncRun]:
        result = await self.session.scalars(
            select(ConnectorSyncRun).where(
                ConnectorSyncRun.connector_id == self.connector_id,
                ConnectorSyncRun.status == SyncRunStatus.RUNNING.value,
            )
        )
        return list(result)

    async def _running(self, run_id: UUID) -> ConnectorSyncRun:
        run = await self.session.get(ConnectorSyncRun, run_id)
        if (
            run is None
            or run.connector_id != self.connector_id
            or run.status != SyncRunStatus.RUNNING.value
        ):
            raise RunNotFoundError(str(run_id))
        return run

    def _count(self, run: ConnectorSyncRun, change: DocumentChange) -> None:
        self._bump(run, "seen", 1)
        if change != "unchanged":
            self._bump(run, change, 1)

    @staticmethod
    def _bump(run: ConnectorSyncRun, key: str, by: int) -> None:
        stats = dict(run.stats or {})
        stats[key] = int(stats.get(key, 0)) + by
        run.stats = stats
