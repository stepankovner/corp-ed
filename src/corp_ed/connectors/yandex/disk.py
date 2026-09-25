"""REST Яндекс Диска от имени сотрудника: обход папок и скачивание.

Все вызовы — GET {api}v1/disk/... с заголовком Authorization: OAuth
{token}. Листинг папки — resources?path=&limit=&offset= с
_embedded.items (type dir|file, name, path, modified, size, md5,
resource_id); скачивание — resources/download?path= → href на
downloader.disk.yandex.ru (подписанная ссылка, без токена). Общие
папки Яндекс 360 смонтированы в диск сотрудника и приходят тем же
листингом — это и есть его видимость.

401 → продление токена и один повтор, 429/503 → retryable, 404 —
объект исчез (пропуск). Ссылки на скачивание принимаются только на
доменах Яндекса.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
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
from corp_ed.connectors.bitrix24.oauth import TokenSet
from corp_ed.connectors.yandex.oauth import YandexOAuth
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError
from corp_ed.domain.types import RemoteDocumentKind
from corp_ed.ingest.extract import SUPPORTED_EXTENSIONS

logger = structlog.get_logger()

USER_AGENT = "corp-ed-connector/1.0"
MODULE_DISK = "disk"
PREFIX = "ydisk:"
PAGE_LIMIT = 200
MAX_DEPTH = 32
REQUEST_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 120.0
RATE_LIMIT_BACKOFF = (1.0, 2.0, 4.0)
_ALLOWED_DOWNLOAD_SUFFIXES = (".yandex.ru", ".yandex.net", ".yandexcloud.net")
_LISTING_FIELDS = (
    "_embedded.items.type,_embedded.items.name,_embedded.items.path,"
    "_embedded.items.modified,_embedded.items.size,_embedded.items.md5,"
    "_embedded.items.resource_id,_embedded.items.public_url,"
    "_embedded.total,_embedded.limit,_embedded.offset"
)

Sleep = Callable[[float], Awaitable[None]]


@dataclass
class OAuthTokens:
    tokens: TokenSet
    oauth: YandexOAuth
    refreshed: bool = False


class YandexDiskClient:
    def __init__(
        self,
        http: OutboundClient,
        *,
        api: str,
        auth: OAuthTokens,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http
        self._api = api if api.endswith("/") else api + "/"
        self._auth = auth
        self._sleep = sleep

    @property
    def refreshed_credentials(self) -> Mapping[str, str] | None:
        return self._auth.tokens.as_credentials() if self._auth.refreshed else None

    async def get(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        refreshed = False
        backoff = iter(RATE_LIMIT_BACKOFF)
        while True:
            try:
                response = await self._http.get(
                    f"{self._api}v1/disk/{path.lstrip('/')}",
                    params=dict(params or {}),
                    headers={
                        "Authorization": f"OAuth {self._auth.tokens.access_token}",
                        "Accept": "application/json",
                        "User-Agent": USER_AGENT,
                    },
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=False,
                )
            except httpx.TimeoutException as exc:
                raise AdapterError("timeout", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise AdapterError("network_error", retryable=True) from exc
            status = response.status_code
            if status == 401:
                if refreshed:
                    raise AdapterAuthError("unauthorized")
                await self._refresh()
                refreshed = True
                continue
            if status in (429, 503):
                delay = next(backoff, None)
                if delay is None:
                    raise AdapterError("rate_limited", retryable=True)
                await self._sleep(delay)
                continue
            data = _json(response)
            if status == 200 and data is not None:
                return data
            if status == 403:
                raise AdapterError(
                    str((data or {}).get("error") or "forbidden").lower()
                )
            if status == 404:
                raise AdapterError("not_found")
            code = str((data or {}).get("error") or f"http_{status}").lower()
            raise AdapterError(code[:64], retryable=status >= 500)

    async def download(self, href: str, *, max_bytes: int) -> bytes:
        host = (urlsplit(href).hostname or "").lower()
        if urlsplit(href).scheme != "https" or not host.endswith(
            _ALLOWED_DOWNLOAD_SUFFIXES
        ):
            raise AdapterError("download_url_foreign")
        try:
            # Ссылка подписана: токен ей не нужен и на чужой хост не уходит.
            downloaded = await self._http.download(
                href,
                max_bytes=max_bytes,
                headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
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

    async def _refresh(self) -> None:
        self._auth.tokens = await self._auth.oauth.refresh(
            self._auth.tokens.refresh_token
        )
        self._auth.refreshed = True


class YandexDiskModule:
    def __init__(self, client: YandexDiskClient, *, max_bytes: int) -> None:
        self._client = client
        self._max_bytes = max_bytes

    async def walk(self) -> AsyncIterator[RemoteDocument]:
        async for document in self._walk("disk:/", "Диск", 0):
            yield document

    async def fetch(self, document: RemoteDocument, *, max_bytes: int) -> FetchedFile:
        path = document.locator
        if not path:
            raise AdapterError("locator_missing")
        info = await self._client.get(
            "resources", {"path": path, "fields": "size,name"}
        )
        size = _int(info.get("size"))
        if size is not None and size > max_bytes:
            raise AdapterError("document_too_large")
        link = await self._client.get("resources/download", {"path": path})
        href = link.get("href")
        if not isinstance(href, str) or not href:
            raise AdapterError("download_url_missing")
        data = await self._client.download(href, max_bytes=max_bytes)
        return FetchedFile(
            data=data, filename=str(info.get("name") or document.filename or "file")
        )

    async def _walk(
        self, path: str, label: str, depth: int
    ) -> AsyncIterator[RemoteDocument]:
        if depth > MAX_DEPTH:
            logger.warning("yandex_disk_too_deep", path=label)
            return
        entries: list[dict[str, Any]] = []
        offset = 0
        while True:
            try:
                page = await self._client.get(
                    "resources",
                    {
                        "path": path,
                        "limit": PAGE_LIMIT,
                        "offset": offset,
                        "fields": _LISTING_FIELDS,
                    },
                )
            except AdapterError as exc:
                if isinstance(exc, AdapterAuthError) or exc.retryable:
                    raise
                if exc.code in {"not_found", "forbidden", "diskforbiddenerror"}:
                    logger.info("yandex_disk_folder_skipped", path=label, code=exc.code)
                    return
                raise
            embedded = page.get("_embedded") or {}
            items = [i for i in embedded.get("items") or [] if isinstance(i, dict)]
            entries.extend(items)
            total = _int(embedded.get("total")) or 0
            offset += len(items)
            if not items or offset >= total:
                break
        for entry in entries:
            name = str(entry.get("name") or "")
            entry_path = str(entry.get("path") or "")
            if entry.get("type") == "dir":
                async for document in self._walk(
                    entry_path, f"{label}/{name}", depth + 1
                ):
                    yield document
                continue
            if entry.get("type") != "file":
                continue
            found = self._document(entry, label)
            if found is not None:
                yield found

    def _document(self, entry: Mapping[str, Any], label: str) -> RemoteDocument | None:
        name = str(entry.get("name") or "")
        if PurePath(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
            return None
        size = _int(entry.get("size"))
        if size is not None and size > self._max_bytes:
            return None
        path = str(entry.get("path") or "")
        resource_id = str(entry.get("resource_id") or path)
        modified = str(entry.get("modified") or "")
        url = entry.get("public_url")
        if not isinstance(url, str) or not url:
            url = _folder_url(path)
        return RemoteDocument(
            external_id=f"{PREFIX}{resource_id}",
            title=name,
            url=url,
            version=f"{entry.get('md5') or ''}:{modified}",
            kind=RemoteDocumentKind.FILE,
            module=MODULE_DISK,
            path=label,
            locator=path,
            filename=name,
            size=size,
            modified_at=_datetime(modified),
        )


def _folder_url(path: str) -> str:
    """Веб-адрес папки файла: у Диска нет прямой ссылки на файл без
    публикации, поэтому источник ответа открывает его папку."""
    folder = (
        path.removeprefix("disk:/").rsplit("/", 1)[0]
        if "/" in path.removeprefix("disk:/")
        else ""
    )
    return f"https://disk.yandex.ru/client/disk/{quote(folder)}"


def _json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None
