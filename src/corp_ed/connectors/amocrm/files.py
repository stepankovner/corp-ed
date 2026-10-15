"""Модули файлов сущностей: сделки, контакты, компании.

Права — только из методов «в соответствии с правами пользователя»
(files-api и leads-api, 15.10.2026): список сущностей
`/api/v4/{leads|contacts|companies}` токеном сотрудника, затем у каждой —
связанные файлы `/api/v4/{сущность}/{id}/files` (только file_uuid и id
связи). Сервис файлов (`/v1.0/files`) ограничен scope интеграции, а не
правами пользователя: он нужен лишь для имени, размера, версии и ссылки
на скачивание тех uuid, что уже пришли из сущностей сотрудника. Общий
список сервиса файлов как источник документов не используется никогда.

Один файл, привязанный к нескольким сущностям, — один документ
(`file:<uuid>`); путь и ссылка — первой сущности в обходе. Корзина
(is_trashed, deleted_at), неподдерживаемые форматы и файлы больше
лимита в листинг не попадают.

Стоимость — запрос на каждую сущность: при 4 запросах в секунду 5000
сделок — около 20 минут на сотрудника. Больше MAX_ENTITIES сущностей в
модуле — ошибка too_many_entities, а не обрезанный листинг (обрезанный
ядро сочло бы полным и удалило бы остальное); сузить — полем
«Воронки».
"""

from collections.abc import AsyncIterator, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any

import structlog

from corp_ed.connectors.amocrm.client import AmoClient
from corp_ed.connectors.base import (
    AdapterConfigError,
    AdapterError,
    FetchedFile,
    RemoteDocument,
)
from corp_ed.connectors.common import (
    note_too_large,
    note_unsupported,
    path_segment,
    to_int,
)
from corp_ed.domain.types import RemoteDocumentKind
from corp_ed.ingest.extract import supported_extensions

logger = structlog.get_logger()

MODULE_LEAD_FILES = "lead_files"
MODULE_CONTACT_FILES = "contacts_files"
MODULE_COMPANY_FILES = "companies_files"
PREFIX = "file:"
ENTITY_PAGE = 250
"""Максимум списка сущностей за запрос (leads-api)."""
FILES_PAGE = 50
METADATA_BATCH = 50
MAX_ENTITIES = 5000
MAX_FILE_PAGES = 20
MAX_METADATA_PAGES = 10
# Ошибки на одной сущности, после которых обход продолжается: доступ
# к сделке закрыли или её удалили посреди обхода.
_SKIPPABLE = frozenset({"forbidden", "not_found"})


@dataclass(frozen=True)
class EntityKind:
    module: str
    entity: str
    """Сегмент API и адреса карточки: leads, contacts, companies."""
    section: str


KINDS = (
    EntityKind(MODULE_LEAD_FILES, "leads", "Сделки"),
    EntityKind(MODULE_CONTACT_FILES, "contacts", "Контакты"),
    EntityKind(MODULE_COMPANY_FILES, "companies", "Компании"),
)


@dataclass(frozen=True)
class _Owner:
    """Сущность, через которую файл попал в листинг."""

    kind: EntityKind
    entity_id: int
    name: str


class EntityFilesModule:
    def __init__(
        self,
        client: AmoClient,
        *,
        max_bytes: int,
        pipelines: Collection[int] = (),
    ) -> None:
        self._client = client
        self._max_bytes = max_bytes
        self._pipelines = frozenset(pipelines)

    async def walk(self, modules: Collection[str]) -> AsyncIterator[RemoteDocument]:
        seen: set[str] = set()
        for kind in KINDS:
            if kind.module not in modules:
                continue
            pending: dict[str, _Owner] = {}
            async for owner in self._entities(kind):
                for uuid in await self._entity_files(owner):
                    if uuid not in seen:
                        seen.add(uuid)
                        pending[uuid] = owner
                if len(pending) >= METADATA_BATCH:
                    async for document in self._documents(pending):
                        yield document
                    pending = {}
            if pending:
                async for document in self._documents(pending):
                    yield document

    async def fetch(self, document: RemoteDocument, *, max_bytes: int) -> FetchedFile:
        uuid = document.external_id.removeprefix(PREFIX)
        try:
            info = await self._client.drive(f"files/{path_segment(uuid)}")
        except AdapterError as exc:
            if exc.code == "not_found":
                raise AdapterError("file_not_found") from exc
            raise
        if info is None or _trashed(info):
            raise AdapterError("file_not_found")
        size = to_int(info.get("size"))
        if size is not None and size > max_bytes:
            raise AdapterError("document_too_large")
        links = info.get("_links")
        href = _href(links, "download_version") or _href(links, "download")
        if href is None:
            raise AdapterError("download_url_missing")
        data = await self._client.download(href, max_bytes=max_bytes)
        return FetchedFile(data=data, filename=_filename(info) or document.title)

    # --- обход ----------------------------------------------------------------

    async def _entities(self, kind: EntityKind) -> AsyncIterator[_Owner]:
        """Сущности, которые видит сотрудник, постранично по 250."""
        count = 0
        page = 1
        while True:
            try:
                data = await self._client.api(
                    kind.entity, {"limit": ENTITY_PAGE, "page": page}
                )
            except AdapterError as exc:
                if exc.code != "forbidden":
                    raise
                # Аккаунт отвечает — значит, у сотрудника нет права
                # смотреть этот раздел вовсе.
                await self._confirm_forbidden()
                logger.info("amocrm_section_forbidden", entity=kind.entity)
                return
            items = _embedded(data, kind.entity)
            for item in items:
                entity_id = to_int(item.get("id"))
                if entity_id is None or item.get("is_deleted"):
                    continue
                if (
                    kind.entity == "leads"
                    and self._pipelines
                    and to_int(item.get("pipeline_id")) not in self._pipelines
                ):
                    continue
                count += 1
                if count > MAX_ENTITIES:
                    raise AdapterError("too_many_entities")
                name = str(item.get("name") or "").strip() or f"№{entity_id}"
                yield _Owner(kind, entity_id, name)
            if not items or _href((data or {}).get("_links"), "next") is None:
                return
            page += 1

    async def _entity_files(self, owner: _Owner) -> list[str]:
        """uuid файлов сущности; курсор — before_id (id связи)."""
        path = f"{owner.kind.entity}/{owner.entity_id}/files"
        found: list[str] = []
        before: int | None = None
        for _ in range(MAX_FILE_PAGES):
            params: dict[str, Any] = {"limit": FILES_PAGE}
            if before is not None:
                params["before_id"] = before
            try:
                data = await self._client.api(path, params)
            except AdapterError as exc:
                if exc.retryable or exc.code not in _SKIPPABLE:
                    raise
                if exc.code == "forbidden":
                    await self._confirm_forbidden()
                logger.info(
                    "amocrm_entity_skipped",
                    entity=owner.kind.entity,
                    entity_id=owner.entity_id,
                    code=exc.code,
                )
                return found
            links = _embedded(data, "files")
            ids: list[int] = []
            for link in links:
                uuid = link.get("file_uuid")
                if isinstance(uuid, str) and uuid:
                    found.append(uuid)
                link_id = to_int(link.get("id"))
                if link_id is not None:
                    ids.append(link_id)
            if len(links) < FILES_PAGE or not ids:
                return found
            lowest = min(ids)
            if before is not None and lowest >= before:
                # Курсор не движется — ответ не тот, что в документации.
                return found
            before = lowest
        logger.warning(
            "amocrm_entity_files_truncated",
            entity=owner.kind.entity,
            entity_id=owner.entity_id,
        )
        raise AdapterError("too_many_entity_files")

    async def _confirm_forbidden(self) -> None:
        """403 на раздел или сущность — нет прав у сотрудника, только если
        сам аккаунт отвечает. Заблокированный аккаунт отвечает 403 на всё:
        пустой из-за этого листинг ядро сочло бы полным и удалило бы
        материалы."""
        try:
            await self._client.api("account")
        except AdapterError as exc:
            if exc.code == "forbidden":
                raise AdapterConfigError("account_forbidden") from exc
            raise

    async def _documents(
        self, pending: Mapping[str, _Owner]
    ) -> AsyncIterator[RemoteDocument]:
        """Сведения о файлах пачкой из сервиса файлов — только эти uuid."""
        params: dict[str, Any] = {
            "filter[uuid]": ",".join(pending),
            "limit": len(pending),
        }
        for page in range(1, MAX_METADATA_PAGES + 1):
            data = await self._client.drive("files", {**params, "page": page})
            for info in _embedded(data, "files"):
                uuid = info.get("uuid")
                owner = pending.get(uuid) if isinstance(uuid, str) else None
                if owner is None or _trashed(info):
                    # Чужой uuid в ответе — не наш документ.
                    continue
                document = self._document(str(uuid), info, owner)
                if document is not None:
                    yield document
            if _href((data or {}).get("_links"), "next") is None:
                return

    def _document(
        self, uuid: str, info: Mapping[str, Any], owner: _Owner
    ) -> RemoteDocument | None:
        name = _filename(info)
        if not name or PurePath(name).suffix.lower() not in supported_extensions():
            note_unsupported(name or uuid, uuid)
            return None
        size = to_int(info.get("size"))
        if size is not None and size > self._max_bytes:
            note_too_large(uuid)
            return None
        updated = to_int(info.get("updated_at"))
        version = f"{info.get('version_uuid') or ''}:{updated or ''}:{size or ''}"
        return RemoteDocument(
            external_id=f"{PREFIX}{uuid}",
            title=name,
            url=f"{self._client.account}{owner.kind.entity}/detail/{owner.entity_id}",
            version=version,
            kind=RemoteDocumentKind.FILE,
            module=owner.kind.module,
            path=f"{owner.kind.section}/{owner.name}",
            filename=name,
            size=size,
            modified_at=(
                datetime.fromtimestamp(updated, UTC) if updated is not None else None
            ),
        )


def _embedded(data: Mapping[str, Any] | None, key: str) -> list[dict[str, Any]]:
    embedded = (data or {}).get("_embedded")
    items = embedded.get(key) if isinstance(embedded, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _href(links: Any, key: str) -> str | None:
    link = links.get(key) if isinstance(links, dict) else None
    href = link.get("href") if isinstance(link, dict) else None
    return href if isinstance(href, str) and href else None


def _trashed(info: Mapping[str, Any]) -> bool:
    return bool(info.get("is_trashed")) or info.get("deleted_at") is not None


def _filename(info: Mapping[str, Any]) -> str:
    """Имя файла; без расширения в имени — из metadata.extension."""
    name = str(info.get("name") or "").strip()
    metadata = info.get("metadata")
    extension = metadata.get("extension") if isinstance(metadata, dict) else None
    if name and not PurePath(name).suffix and isinstance(extension, str) and extension:
        name = f"{name}.{extension.lstrip('.')}"
    return name
