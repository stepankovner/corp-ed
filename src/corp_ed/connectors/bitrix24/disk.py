"""Модули диска: обход хранилищ и папок, скачивание файлов.

Модуль `disk` — общий диск компании и диски рабочих групп (хранилища
ENTITY_TYPE common и group); `disk_personal` — «Мой диск» самого
сотрудника (user с его ENTITY_ID). Чужие личные диски не читаются
никогда, даже если REST их листит.

Права: disk.storage.getchildren и disk.folder.getchildren отдают
только объекты с правом «Чтение» у пользователя токена (документация)
— в режиме per_user это и есть видимость. Папка без доступа или
удалённая посреди обхода пропускается, а не роняет запуск; ошибки
авторизации, лимитов и сети поднимаются наверх.

Что не скачивается: корзина (DELETED_TYPE ≠ 0), неподдерживаемые
расширения, файлы больше лимита — они не попадают и в листинг, чтобы
не считаться ошибками каждый запуск.
"""

from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from pathlib import PurePath
from typing import Any

import structlog

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    FetchedFile,
    RemoteDocument,
)
from corp_ed.connectors.bitrix24.client import Bitrix24Client
from corp_ed.domain.types import RemoteDocumentKind
from corp_ed.ingest.extract import SUPPORTED_EXTENSIONS

logger = structlog.get_logger()

MODULE_DISK = "disk"
MODULE_DISK_PERSONAL = "disk_personal"
PREFIX = "disk:"
MAX_DEPTH = 32
"""Глубина вложенности папок: защита от цикла в ответах источника."""
# Ошибки метода на одной папке, после которых обход продолжается.
_SKIPPABLE = frozenset({"access_denied", "error_not_found", "error_argument"})


class DiskModule:
    def __init__(
        self, client: Bitrix24Client, *, max_bytes: int, user_id: str | None
    ) -> None:
        self._client = client
        self._max_bytes = max_bytes
        self._user_id = user_id

    async def walk(self, modules: set[str]) -> AsyncIterator[RemoteDocument]:
        wanted: dict[str, str] = {}
        if MODULE_DISK in modules:
            wanted.update({"common": MODULE_DISK, "group": MODULE_DISK})
        if MODULE_DISK_PERSONAL in modules:
            wanted["user"] = MODULE_DISK_PERSONAL
        if not wanted:
            return
        async for storage in self._client.iterate("disk.storage.getlist"):
            entity_type = str(storage.get("ENTITY_TYPE") or "")
            module = wanted.get(entity_type)
            if module is None:
                continue
            if entity_type == "user" and str(storage.get("ENTITY_ID")) != self._user_id:
                continue
            root_id = storage.get("ROOT_OBJECT_ID")
            if root_id is None:
                continue
            name = str(storage.get("NAME") or f"storage-{storage.get('ID')}")
            async for document in self._walk(
                "disk.storage.getchildren",
                str(storage.get("ID")),
                path=name,
                module=module,
                depth=0,
            ):
                yield document

    async def fetch(self, document: RemoteDocument, *, max_bytes: int) -> FetchedFile:
        file_id = document.external_id.removeprefix(PREFIX)
        info = (await self._client.call("disk.file.get", {"id": file_id})).get("result")
        if not isinstance(info, dict):
            raise AdapterError("file_not_found")
        size = _int(info.get("SIZE"))
        if size is not None and size > max_bytes:
            raise AdapterError("document_too_large")
        url = info.get("DOWNLOAD_URL")
        if not isinstance(url, str) or not url:
            raise AdapterError("download_url_missing")
        data = await self._client.download(url, max_bytes=max_bytes)
        filename = str(info.get("NAME") or document.filename or "file")
        return FetchedFile(data=data, filename=filename)

    # --- обход ----------------------------------------------------------------

    async def _walk(
        self, method: str, object_id: str, *, path: str, module: str, depth: int
    ) -> AsyncIterator[RemoteDocument]:
        if depth > MAX_DEPTH:
            logger.warning("bitrix24_disk_too_deep", path=path)
            return
        # Список страницы целиком до рекурсии: вложенный обход внутри
        # постраничного итератора чередовал бы запросы к разным папкам.
        entries: list[dict[str, Any]] = []
        try:
            async for entry in self._client.iterate(method, {"id": object_id}):
                entries.append(entry)
        except AdapterError as exc:
            if (
                isinstance(exc, AdapterAuthError | AdapterConfigError)
                or exc.retryable
                or exc.code not in _SKIPPABLE
            ):
                raise
            logger.info("bitrix24_folder_skipped", path=path, code=exc.code)
            return
        for entry in entries:
            if str(entry.get("DELETED_TYPE") or "0") != "0":
                continue
            entry_type = str(entry.get("TYPE") or "")
            name = str(entry.get("NAME") or "")
            entry_id = entry.get("ID")
            if entry_id is None:
                continue
            if entry_type == "folder":
                async for document in self._walk(
                    "disk.folder.getchildren",
                    str(entry_id),
                    path=f"{path}/{name}",
                    module=module,
                    depth=depth + 1,
                ):
                    yield document
                continue
            if entry_type != "file":
                continue
            found = self._document(entry, path=path, module=module)
            if found is not None:
                yield found

    def _document(
        self, entry: Mapping[str, Any], *, path: str, module: str
    ) -> RemoteDocument | None:
        name = str(entry.get("NAME") or "")
        if PurePath(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
            return None
        size = _int(entry.get("SIZE"))
        if size is not None and size > self._max_bytes:
            return None
        file_id = str(entry.get("ID"))
        updated = str(entry.get("UPDATE_TIME") or "")
        version = f"{entry.get('GLOBAL_CONTENT_VERSION') or ''}:{updated}:{size or ''}"
        url = entry.get("DETAIL_URL")
        if not isinstance(url, str) or not url:
            url = (
                f"{self._client.portal}bitrix/tools/disk/focus.php"
                f"?objectId={file_id}&action=showObjectInGrid&ncc=1"
            )
        return RemoteDocument(
            external_id=f"{PREFIX}{file_id}",
            title=name,
            url=url,
            version=version,
            kind=RemoteDocumentKind.FILE,
            module=module,
            path=path,
            filename=name,
            size=size,
            modified_at=_datetime(updated),
        )


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
