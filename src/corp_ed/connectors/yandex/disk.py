"""REST Яндекс Диска от имени сотрудника: личный Диск и общие диски организации.

Сверено с документацией REST API Диска (yandex.ru/dev/disk-api/doc/ru/,
28.09):
- все вызовы — GET {api}v1/disk/... с Authorization: OAuth {token};
  листинг папки — resources?path=&limit=&offset=&fields= (_embedded
  есть только у непустых папок; сначала папки, потом файлы);
- общие диски Яндекс 360 — «отдельные облачные хранилища, которые
  принадлежат не конкретному пользователю, а всей организации»: их нет в
  дереве disk:/ сотрудника. Список — virtual-disks/discovery?org_id=
  (доступно сотрудникам, limit до 100), содержимое —
  virtual-disks/resources?path=vd:<vd_hash>:disk:/..., скачивание —
  virtual-disks/resources/download. Параметра fields у них нет;
- скачивание: resources/download?path= → href, по нему — «указав тот же
  OAuth-токен»; дальше 302 на *.storage.yandex.net (токен туда не
  уходит — OutboundClient снимает Authorization при смене хоста);
- 401 → продление токена и один повтор; 429/503 → ожидание; 423
  (технические работы) → повтор позже; 403 → «forbidden» (код Яндекса
  в журнал), вложенная папка пропускается, корень — ошибка; 404 —
  объект исчез.

Что сверить на живом Диске — RISKS №38.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import PurePath
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import structlog

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterError,
    FetchedFile,
    RemoteDocument,
)
from corp_ed.connectors.common import (
    Recorder,
    json_object,
    note_too_large,
    note_unsupported,
    parse_datetime,
    redact,
    to_int,
)
from corp_ed.connectors.yandex.oauth import YandexAuth
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError
from corp_ed.domain.types import RemoteDocumentKind
from corp_ed.ingest.extract import supported_extensions

logger = structlog.get_logger()

USER_AGENT = "corp-ed-connector/1.0"
MODULE_DISK = "disk"
MODULE_SHARED_DISKS = "shared_disks"
PREFIX = "ydisk:"
PAGE_LIMIT = 200
DISCOVERY_LIMIT = 100
MAX_DEPTH = 32
REQUEST_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 120.0
RATE_LIMIT_BACKOFF = (1.0, 2.0, 4.0)
# Ссылки на скачивание, которые называет документация:
# downloader.dst.yandex.ru, затем *.storage.yandex.net. Токен уходит
# только на первый хост, поэтому список узкий.
_ALLOWED_DOWNLOAD_SUFFIXES = (".yandex.ru", ".yandex.net")
_LISTING_FIELDS = (
    "_embedded.items.type,_embedded.items.name,_embedded.items.path,"
    "_embedded.items.modified,_embedded.items.size,_embedded.items.md5,"
    "_embedded.items.resource_id,_embedded.items.public_url,"
    "_embedded.items.share,"
    "_embedded.total,_embedded.limit,_embedded.offset"
)
_SKIPPED_FOLDER_CODES = frozenset({"not_found", "forbidden"})
VIRTUAL_PREFIX = "vd:"

Sleep = Callable[[float], Awaitable[None]]


class YandexDiskClient:
    def __init__(
        self,
        http: OutboundClient,
        *,
        api: str,
        auth: YandexAuth,
        sleep: Sleep = asyncio.sleep,
        recorder: Recorder | None = None,
    ) -> None:
        self._http = http
        self._api = api if api.endswith("/") else api + "/"
        self._auth = auth
        self._sleep = sleep
        self._recorder = recorder

    @property
    def auth(self) -> YandexAuth:
        return self._auth

    @property
    def refreshed_credentials(self) -> Mapping[str, str] | None:
        return self._auth.tokens.as_credentials() if self._auth.refreshed else None

    async def get(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        await self._auth.ensure_fresh()
        refreshed = False
        backoff = iter(RATE_LIMIT_BACKOFF)
        while True:
            try:
                response = await self._http.get(
                    f"{self._api}v1/disk/{path.lstrip('/')}",
                    params=dict(params or {}),
                    headers={
                        "Authorization": self._auth.header,
                        "Accept": "application/json",
                        "User-Agent": USER_AGENT,
                    },
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=False,
                )
            except OutboundTooLargeError as exc:
                raise AdapterError("response_too_large") from exc
            except httpx.TimeoutException as exc:
                raise AdapterError("timeout", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise AdapterError("network_error", retryable=True) from exc
            status = response.status_code
            if status == 401:
                if refreshed:
                    raise AdapterAuthError("unauthorized")
                await self._auth.refresh()
                refreshed = True
                continue
            if status in (429, 503):
                delay = next(backoff, None)
                if delay is None:
                    raise AdapterError("rate_limited", retryable=True)
                await self._sleep(delay)
                continue
            data = json_object(response)
            if self._recorder is not None:
                self._recorder(
                    path, redact(dict(params or {}), request=True), redact(data)
                )
            if status == 200 and data is not None:
                return data
            error = str((data or {}).get("error") or "")
            if status == 403:
                # Причин 403 у Диска несколько (нет прав, Диск только на
                # чтение, пользователь заблокирован), имена в документации
                # не названы — наружу один код, подробность — в журнал.
                logger.info("yandex_disk_forbidden", path=path, error=error[:64])
                raise AdapterError("forbidden")
            if status == 404:
                raise AdapterError("not_found")
            if status == 423:
                raise AdapterError("maintenance", retryable=True)
            code = (error or f"http_{status}").lower()
            raise AdapterError(code[:64], retryable=status >= 500)

    async def download(self, href: str, *, max_bytes: int) -> bytes:
        parts = urlsplit(href)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not host.endswith(_ALLOWED_DOWNLOAD_SUFFIXES):
            raise AdapterError("download_url_foreign")
        try:
            downloaded = await self._http.download(
                href,
                max_bytes=max_bytes,
                headers={
                    "Authorization": self._auth.header,
                    "User-Agent": USER_AGENT,
                    "Accept": "*/*",
                },
                timeout=DOWNLOAD_TIMEOUT,
            )
        except OutboundTooLargeError as exc:
            raise AdapterError("document_too_large") from exc
        except httpx.TimeoutException as exc:
            raise AdapterError("timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise AdapterError("network_error", retryable=True) from exc
        if downloaded.status_code != 200:
            raise AdapterError(
                f"download_http_{downloaded.status_code}",
                retryable=downloaded.status_code >= 500,
            )
        return downloaded.content


def _is_virtual(path: str) -> bool:
    return path.startswith(VIRTUAL_PREFIX)


class YandexDiskModule:
    """Личный Диск сотрудника и общие диски организации."""

    def __init__(
        self,
        client: YandexDiskClient,
        *,
        max_bytes: int,
        org_id: str = "",
    ) -> None:
        self._client = client
        self._max_bytes = max_bytes
        self._org_id = org_id

    async def walk(self) -> AsyncIterator[RemoteDocument]:
        async for document in self._walk("disk:/", "Диск", 0, MODULE_DISK):
            yield document

    async def walk_shared(self) -> AsyncIterator[RemoteDocument]:
        async for disk in self._shared_disks():
            vd_hash = str(disk.get("vd_hash") or "")
            if not vd_hash:
                continue
            permissions = disk.get("permissions") or []
            if isinstance(permissions, list) and "read" not in permissions:
                continue
            name = str(disk.get("name") or vd_hash)
            async for document in self._walk(
                f"{VIRTUAL_PREFIX}{vd_hash}:disk:/",
                f"Общие диски/{name}",
                0,
                MODULE_SHARED_DISKS,
                vd_hash=vd_hash,
            ):
                yield document

    async def fetch(self, document: RemoteDocument, *, max_bytes: int) -> FetchedFile:
        path = document.locator
        if not path:
            raise AdapterError("locator_missing")
        # Размер и имя уже пришли листингом; лишний запрос метаданных на
        # каждый файл — это тысячи вызовов на большом Диске.
        if document.size is not None and document.size > max_bytes:
            raise AdapterError("document_too_large")
        endpoint = (
            "virtual-disks/resources/download"
            if _is_virtual(path)
            else "resources/download"
        )
        link = await self._client.get(endpoint, {"path": path})
        if link.get("templated"):
            # Ссылку-шаблон надо заполнять параметрами — для скачивания
            # документация такого случая не описывает.
            raise AdapterError("download_url_templated")
        href = link.get("href")
        if not isinstance(href, str) or not href:
            raise AdapterError("download_url_missing")
        data = await self._client.download(href, max_bytes=max_bytes)
        return FetchedFile(data=data, filename=document.filename or document.title)

    async def _shared_disks(self) -> AsyncIterator[Mapping[str, Any]]:
        offset = 0
        while True:
            page = await self._client.get(
                "virtual-disks/discovery",
                {"org_id": self._org_id, "limit": DISCOVERY_LIMIT, "offset": offset},
            )
            items = [i for i in page.get("items") or [] if isinstance(i, dict)]
            for item in items:
                yield item
            offset += len(items)
            total = to_int(page.get("total")) or 0
            if not items or offset >= total:
                return

    async def _walk(
        self,
        path: str,
        label: str,
        depth: int,
        module: str,
        *,
        vd_hash: str = "",
    ) -> AsyncIterator[RemoteDocument]:
        if depth > MAX_DEPTH:
            logger.warning("yandex_disk_too_deep", path=label)
            return
        entries: list[dict[str, Any]] = []
        offset = 0
        while True:
            params: dict[str, Any] = {
                "path": path,
                "limit": PAGE_LIMIT,
                "offset": offset,
            }
            if vd_hash:
                endpoint = "virtual-disks/resources"
            else:
                endpoint = "resources"
                params["fields"] = _LISTING_FIELDS
            try:
                page = await self._client.get(endpoint, params)
            except AdapterError as exc:
                if isinstance(exc, AdapterAuthError) or exc.retryable:
                    raise
                # Корень личного Диска недоступен — это не папка, а права
                # приложения или заблокированный Диск: ошибка, а не пропуск.
                # Общий диск без прав — пропуск: у сотрудника его нет.
                if exc.code in _SKIPPED_FOLDER_CODES and (depth > 0 or vd_hash):
                    logger.info("yandex_disk_folder_skipped", path=label, code=exc.code)
                    return
                raise
            embedded = page.get("_embedded") or {}
            items = [i for i in embedded.get("items") or [] if isinstance(i, dict)]
            entries.extend(items)
            total = to_int(embedded.get("total")) or 0
            offset += len(items)
            if not items or offset >= total:
                break
        for entry in entries:
            name = str(entry.get("name") or "")
            entry_path = _qualify(str(entry.get("path") or ""), vd_hash)
            if entry.get("type") == "dir":
                async for document in self._walk(
                    entry_path, f"{label}/{name}", depth + 1, module, vd_hash=vd_hash
                ):
                    yield document
                continue
            if entry.get("type") != "file":
                continue
            found = self._document(entry, entry_path, label, module, vd_hash)
            if found is not None:
                yield found

    def _document(
        self,
        entry: Mapping[str, Any],
        path: str,
        label: str,
        module: str,
        vd_hash: str,
    ) -> RemoteDocument | None:
        name = str(entry.get("name") or "")
        resource_id = str(entry.get("resource_id") or path)
        if PurePath(name).suffix.lower() not in supported_extensions():
            note_unsupported(name, resource_id)
            return None
        size = to_int(entry.get("size"))
        if size is not None and size > self._max_bytes:
            note_too_large(resource_id)
            return None
        modified = str(entry.get("modified") or "")
        url = entry.get("public_url")
        if not isinstance(url, str) or not url:
            url = _folder_url(path, vd_hash)
        return RemoteDocument(
            external_id=f"{PREFIX}{resource_id}",
            title=name,
            url=url,
            version=f"{entry.get('md5') or ''}:{modified}",
            kind=RemoteDocumentKind.FILE,
            module=module,
            path=label,
            locator=path,
            filename=name,
            size=size,
            modified_at=parse_datetime(modified),
        )


def _qualify(path: str, vd_hash: str) -> str:
    """Путь внутри общего диска — в составной форму vd:<hash>:disk:/...

    Документация показывает составные пути в запросах, а в примере
    ответа — относительные («/bar-1»); что приходит на живом общем диске,
    не проверено (RISKS №38), поэтому принимаются все три формы.
    """
    if not vd_hash or _is_virtual(path):
        return path
    if path.startswith("disk:"):
        return f"{VIRTUAL_PREFIX}{vd_hash}:{path}"
    return f"{VIRTUAL_PREFIX}{vd_hash}:disk:/{path.lstrip('/')}"


def _folder_url(path: str, vd_hash: str = "") -> str:
    """Веб-адрес папки файла: у Диска нет прямой ссылки на файл без
    публикации, поэтому источник ответа открывает его папку. Для общего
    диска — его раздел (адрес по документации: метка после vd/)."""
    if vd_hash:
        inner = path.split(":disk:/", 1)[-1]
        folder = inner.rsplit("/", 1)[0] if "/" in inner else ""
        base = f"https://disk.yandex.ru/client/vd/{quote(vd_hash)}"
        return f"{base}/{quote(folder)}" if folder else base
    relative = path.removeprefix("disk:/")
    folder = relative.rsplit("/", 1)[0] if "/" in relative else ""
    return f"https://disk.yandex.ru/client/disk/{quote(folder)}"
