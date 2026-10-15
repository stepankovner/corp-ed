"""Модуль «Файлы» ELMA365: файлы поддерживаемых форматов из папок раздела.

Обход по токену сотрудника (api.elma365.com/ru/public-api, раздел
«Работа с файлами», 15.10):

1. `POST /disk/directory/list` (`active: true`, `from`/`size` ≤ 10 000) —
   все папки: `__id`, `__name`, родитель `directory`, `parentsList`.
   Папки-ссылки (непустой `target`) пропускаются — иначе повторы и петли;
   корень — папка без родителя, в путь не входит.
2. Для каждой папки `GET /disk/directory/{id}` — 403 «Недостаточно прав
   для доступа к директории» или 404 → папка пропускается.
3. `POST /disk/files/search` с `filter.directory` — файлы папки
   (`__id`, `__name`, `hash`, `version`, `__updatedAt`; размера и папки
   в модели нет), конец — короткая страница.
4. Для каждого файла поддерживаемого формата `GET /disk/file/{id}` —
   403 «Недостаточно прав для доступа к файлу» или 404 → файл
   пропускается.

Почему шаги 2 и 4: в режиме per_user ядро скачивает документ один раз, а
видимым делает его каждому, в чьём листинге он оказался. Документация
не говорит, что списки папок и поиск файлов отфильтрованы по правам
владельца токена, — 403 описан только у запросов одного файла, папки и
ссылки на скачивание. Поэтому список — только из того, что прошло
поштучную проверку. Цена — запрос на каждую папку и каждый файл
поддерживаемого формата на каждого сотрудника, а лимит облака — 90 + 5 ×
лицензий запросов в минуту на компанию: большой раздел обходится за
несколько запусков (бюджет ядра), «Папки» в настройке сужают обход.
Ослабить проверку можно после живой сверки, если окажется, что поиск
сам режет чужое.

Скачивание: `GET /disk/file/{id}/get-link` (те же 403/404) → `Link`,
временная ссылка; токен по ней не отправляется. Размер до скачивания
неизвестен — лимит держит потоковое скачивание.

Ссылка на папку в интерфейсе `{сайт}/files/{id}` — предположение (в
документации API адресов интерфейса нет), проверить на живой системе.
"""

from collections.abc import AsyncIterator, Sequence
from pathlib import PurePath
from typing import Any

import structlog

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterError,
    FetchedFile,
    RemoteDocument,
)
from corp_ed.connectors.common import (
    note_unsupported,
    parse_datetime,
    path_segment,
)
from corp_ed.connectors.elma365.client import Elma365Client
from corp_ed.domain.types import RemoteDocumentKind
from corp_ed.ingest.extract import supported_extensions

logger = structlog.get_logger()

MODULE_FILES = "files"
PREFIX = "file:"
PAGE_SIZE = 1000
MAX_DIRECTORIES = 20_000
MAX_DEPTH = 64
_SKIPPED = frozenset({"forbidden", "not_found"})


class FilesModule:
    def __init__(
        self,
        client: Elma365Client,
        *,
        folders: Sequence[str] = (),
        page_size: int = PAGE_SIZE,
    ) -> None:
        self._client = client
        self._folders = frozenset(f.strip().lower() for f in folders if f.strip())
        self._page_size = page_size

    async def check(self) -> None:
        """Токен принят и раздел «Файлы» ему доступен."""
        await self._listing(
            "disk/directory/list", {"active": True, "from": 0, "size": 1}
        )

    async def walk(self) -> AsyncIterator[RemoteDocument]:
        directories = await self._directories()
        paths = {key: self._path(key, directories) for key in directories}
        seen: set[str] = set()
        for key in sorted(directories, key=lambda k: (paths[k] or "", k)):
            if not self._wanted(key, directories):
                continue
            if await self._probe(f"disk/directory/{path_segment(key)}") is None:
                logger.info("elma365_directory_skipped")
                continue
            async for item in self._files(key):
                document = await self._document(item, key, paths[key] or "")
                if document is not None and document.external_id not in seen:
                    seen.add(document.external_id)
                    yield document

    async def fetch(self, document: RemoteDocument, *, max_bytes: int) -> FetchedFile:
        file_id = document.locator or document.external_id.removeprefix(PREFIX)
        meta = await self._client.get(f"disk/file/{path_segment(file_id)}/get-link")
        link = meta.get("Link")
        if not isinstance(link, str) or not link:
            raise AdapterError("download_url_missing")
        data = await self._client.download(link, max_bytes=max_bytes)
        return FetchedFile(data=data, filename=document.filename or document.title)

    # --- обход --------------------------------------------------------------------

    async def _listing(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Список папок: 403 здесь — токену закрыт весь раздел, грант стоит."""
        try:
            return await self._client.post(path, body)
        except AdapterError as exc:
            if exc.code == "forbidden" and not isinstance(exc, AdapterAuthError):
                raise AdapterAuthError("forbidden") from exc
            raise

    async def _directories(self) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        offset = 0
        while True:
            data = await self._listing(
                "disk/directory/list",
                {"active": True, "from": offset, "size": self._page_size},
            )
            result = data.get("result")
            page = result.get("result") if isinstance(result, dict) else None
            if not isinstance(page, list):
                raise AdapterError("unexpected_response")
            for item in page:
                if not isinstance(item, dict) or not item.get("__id"):
                    continue
                if item.get("target") or item.get("__deletedAt"):
                    continue
                found[str(item["__id"]).lower()] = item
            if len(found) > MAX_DIRECTORIES:
                logger.warning("elma365_tree_too_large", limit=MAX_DIRECTORIES)
                raise AdapterError("tree_too_large")
            offset += len(page)
            total = result.get("total") if isinstance(result, dict) else None
            if len(page) < self._page_size or (
                isinstance(total, int) and offset >= total
            ):
                return found

    async def _files(self, directory: str) -> AsyncIterator[dict[str, Any]]:
        offset = 0
        while True:
            try:
                data = await self._client.post(
                    "disk/files/search",
                    {
                        "filter": {"directory": directory},
                        "from": offset,
                        "size": self._page_size,
                    },
                )
            except AdapterError as exc:
                # Поиск ограничен одной папкой: 403 — закрыта она, а не весь
                # раздел; пропускается папка, грант живёт.
                if isinstance(exc, AdapterAuthError) or exc.code != "forbidden":
                    raise
                logger.info("elma365_directory_search_skipped")
                return
            page = data.get("files")
            if page is None:
                return
            if not isinstance(page, list):
                raise AdapterError("unexpected_response")
            for item in page:
                if isinstance(item, dict) and item.get("__id"):
                    yield item
            if len(page) < self._page_size:
                return
            offset += len(page)

    async def _probe(self, path: str) -> dict[str, Any] | None:
        """Запрос одного объекта по токену сотрудника; 403/404 — None."""
        try:
            return await self._client.get(path)
        except AdapterError as exc:
            if isinstance(exc, AdapterAuthError) or exc.retryable:
                raise
            if exc.code not in _SKIPPED:
                raise
            return None

    async def _document(
        self, item: dict[str, Any], directory: str, path: str
    ) -> RemoteDocument | None:
        key = str(item["__id"]).lower()
        name = item.get("__name")
        if not isinstance(name, str) or not name.strip():
            return None
        if PurePath(name).suffix.lower() not in supported_extensions():
            note_unsupported(name, key)
            return None
        checked = await self._probe(f"disk/file/{path_segment(key)}")
        meta = checked.get("file") if checked is not None else None
        if not isinstance(meta, dict):
            logger.info("elma365_file_skipped")
            return None
        meta = {**item, **meta}
        name = str(meta.get("__name") or name)
        updated = str(meta.get("__updatedAt") or "")
        return RemoteDocument(
            external_id=f"{PREFIX}{key}",
            title=name,
            url=f"{self._client.root}files/{path_segment(directory)}",
            version=f"{meta.get('version', '')}:{meta.get('hash', '')}:{updated}",
            kind=RemoteDocumentKind.FILE,
            module=MODULE_FILES,
            path=path,
            locator=key,
            filename=name,
            modified_at=parse_datetime(updated),
        )

    # --- дерево -------------------------------------------------------------------

    def _chain(self, key: str, directories: dict[str, dict[str, Any]]) -> list[str]:
        """Папка и её предки до корня (сама папка — первой)."""
        chain: list[str] = []
        current: str | None = key
        while current is not None and current in directories:
            if current in chain or len(chain) >= MAX_DEPTH:
                break
            chain.append(current)
            parent = directories[current].get("directory")
            current = str(parent).lower() if parent else None
        return chain

    def _path(self, key: str, directories: dict[str, dict[str, Any]]) -> str:
        names = [
            str(directories[k].get("__name") or "")
            for k in reversed(self._chain(key, directories))
            if directories[k].get("directory")
        ]
        return "/".join(name for name in names if name)

    def _wanted(self, key: str, directories: dict[str, dict[str, Any]]) -> bool:
        """Входит ли папка в «Папки» из настройки (пусто — все)."""
        if not self._folders:
            return True
        parents = directories[key].get("parentsList")
        ancestors = (
            {str(p).lower() for p in parents} if isinstance(parents, list) else set()
        )
        ancestors.update(self._chain(key, directories))
        return bool(ancestors & self._folders)
