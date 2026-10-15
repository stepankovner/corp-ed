"""HTTP-клиент API Р7-Офис Корпоративного сервера 2019 (он же API
ONLYOFFICE Community Server / Workspace) поверх OutboundClient.

По документации Р7 (support.r7-office.ru/community_server/api/, 15.10):
адрес — `{портал}/api/2.0/...`, ответ — конверт `{status, response}`
(`status` 0 — успех). Токен — `POST api/2.0/authentication` с
`{userName, password}` → `response.token`; дальше он идёт в заголовке
`Authorization` как есть, без схемы. Токен берётся на запуск, а не
хранится: в подключении лежат логин и пароль администратора. Истёк
посреди запуска (401) — один повторный вход, затем AdapterAuthError.
Включённая двухфакторная проверка (вход отдаёт не токен, а запрос кода)
не поддерживается — нужна учётка без неё.

Списки — общие параметры `count` / `startIndex` («Фильтрация запросов»).
429 и 502–504 повторяются с паузой (Retry-After, иначе RATE_LIMIT_BACKOFF);
лимиты частоты в документации не описаны. Тексты ошибок сервера
(`error.message`) в коды и журнал не попадают.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import structlog

from corp_ed.connectors.base import AdapterAuthError, AdapterError
from corp_ed.connectors.common import Recorder, json_object, redact, retry_after
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError

logger = structlog.get_logger()

USER_AGENT = "corp-ed-connector/1.0"
REQUEST_TIMEOUT = 60.0
DOWNLOAD_TIMEOUT = 120.0
RATE_LIMIT_BACKOFF = (1.0, 2.0, 4.0, 8.0)
PAGE_SIZE = 100
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
# Хвосты адреса API, которые админ мог вставить вместе с адресом портала.
_API_SUFFIXES = ("/api/2.0", "/api")

Sleep = Callable[[float], Awaitable[None]]


def site_root(address: str) -> str:
    """Адрес из настройки → корень портала со слешем на конце."""
    root = address.strip().rstrip("/")
    for suffix in _API_SUFFIXES:
        if root.lower().endswith(suffix):
            root = root[: -len(suffix)]
            break
    return root.rstrip("/") + "/"


class R7Client:
    def __init__(
        self,
        http: OutboundClient,
        *,
        base_url: str,
        login: str,
        password: str,
        sleep: Sleep = asyncio.sleep,
        recorder: Recorder | None = None,
    ) -> None:
        self._http = http
        self._root = site_root(base_url)
        self._login = login
        self._password = password
        self._sleep = sleep
        self._recorder = recorder
        self._token: str | None = None

    @property
    def root(self) -> str:
        """Корень портала (для ссылок на файлы)."""
        return self._root

    async def get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        secret: bool = False,
    ) -> Any:
        """GET api/2.0/{path} → `response` из конверта.

        secret — ответ сам по себе секрет (подписанная ссылка на файл):
        в фикстуру он не записывается.
        """
        if self._token is None:
            await self._authenticate()
        try:
            return await self._call("GET", path, params=params, secret=secret)
        except _ExpiredError:
            # Токен истёк посреди запуска: войти заново один раз.
            await self._authenticate()
            try:
                return await self._call("GET", path, params=params, secret=secret)
            except _ExpiredError as exc:
                raise AdapterAuthError("unauthorized") from exc

    async def get_object(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        data = await self.get(path, params)
        if not isinstance(data, dict):
            raise AdapterError("unexpected_response")
        return data

    async def get_list(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        data = await self.get(path, params)
        if not isinstance(data, list):
            raise AdapterError("unexpected_response")
        return [item for item in data if isinstance(item, dict)]

    async def pages(self, path: str) -> AsyncIterator[dict[str, Any]]:
        """Постраничный список (люди): конец — короткая страница."""
        start = 0
        while True:
            page = await self.get_list(path, {"count": PAGE_SIZE, "startIndex": start})
            for item in page:
                yield item
            if len(page) < PAGE_SIZE:
                return
            start += len(page)

    async def folder_pages(self, folder: str) -> AsyncIterator[dict[str, Any]]:
        """Содержимое папки по страницам: `{files, folders, current, total}`.

        Конец — `startIndex + count >= total`; без `total` или с пустой
        страницей — по длине страницы.
        """
        start = 0
        while True:
            page = await self.get_object(
                f"files/{folder}", {"count": PAGE_SIZE, "startIndex": start}
            )
            yield page
            files = page.get("files")
            folders = page.get("folders")
            size = (len(files) if isinstance(files, list) else 0) + (
                len(folders) if isinstance(folders, list) else 0
            )
            total = page.get("total")
            if size == 0 or size < PAGE_SIZE:
                return
            if isinstance(total, int) and start + size >= total:
                return
            start += size

    async def download(self, link: str, *, max_bytes: int) -> bytes:
        """Файл по подписанной ссылке (`presigneduri`) — без токена.

        Портал за обратным прокси может строить ссылку на свой же адрес
        по http: тогда берётся схема и адрес портала из настройки.
        """
        url = urljoin(self._root, link)
        parts, root = urlsplit(url), urlsplit(self._root)
        if parts.hostname == root.hostname and parts.scheme == "http":
            url = parts._replace(scheme=root.scheme, netloc=root.netloc).geturl()
        if urlsplit(url).scheme != "https":
            raise AdapterError("link_invalid")
        try:
            downloaded = await self._http.download(
                url,
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
        if downloaded.headers.get("content-type", "").lower().startswith("text/html"):
            # Страница входа или ошибки вместо файла: подпись не принята.
            raise AdapterError("download_failed")
        return downloaded.content

    # --- внутреннее ----------------------------------------------------------------

    async def _authenticate(self) -> None:
        try:
            data = await self._call(
                "POST",
                "authentication",
                json={"userName": self._login, "password": self._password},
                authorized=False,
            )
        except _ExpiredError as exc:
            raise AdapterAuthError("unauthorized") from exc
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            if isinstance(data, dict) and (data.get("tfa") or data.get("sms")):
                raise AdapterAuthError("tfa_not_supported")
            raise AdapterAuthError("token_missing")
        self._token = token

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        authorized: bool = True,
        secret: bool = False,
    ) -> Any:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if authorized and self._token:
            headers["Authorization"] = self._token
        body: dict[str, Any] = {"json": dict(json)} if json is not None else {}
        backoff = iter(RATE_LIMIT_BACKOFF)
        url = f"{self._root}api/2.0/{path.lstrip('/')}"
        while True:
            try:
                response = await self._http.request(
                    method,
                    url,
                    params=dict(params or {}),
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=False,
                    **body,
                )
            except OutboundTooLargeError as exc:
                raise AdapterError("response_too_large") from exc
            except httpx.TimeoutException as exc:
                raise AdapterError("timeout", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise AdapterError("network_error", retryable=True) from exc
            status = response.status_code
            if status in _RETRY_STATUSES:
                delay = next(backoff, None)
                if delay is None:
                    code = "rate_limited" if status == 429 else f"http_{status}"
                    raise AdapterError(code, retryable=True)
                wait = retry_after(response.headers.get("retry-after"))
                await self._sleep(wait if wait is not None else delay)
                continue
            envelope = json_object(response)
            if self._recorder is not None:
                recorded = envelope if authorized else _without_token(envelope)
                if secret and recorded is not None:
                    recorded = {**recorded, "response": "<redacted>"}
                self._recorder(
                    path, redact(dict(params or {}), request=True), redact(recorded)
                )
            if status == 401:
                raise _ExpiredError()
            if status == 403:
                raise AdapterError("forbidden")
            if status == 404:
                raise AdapterError("not_found")
            if status != 200 and status != 201:
                raise AdapterError(f"http_{status}", retryable=status >= 500)
            if envelope is None:
                # HTML вместо JSON — страница входа или не тот адрес.
                raise AdapterError("not_json")
            if envelope.get("status") not in (0, None):
                raise AdapterError("r7_error")
            return envelope.get("response")


class _ExpiredError(Exception):
    """401: токен не принят (истёк или неверен)."""


def _without_token(envelope: dict[str, Any] | None) -> Any:
    """Ответ входа без самого токена — для записи фикстур."""
    if envelope is None:
        return None
    response = envelope.get("response")
    if isinstance(response, dict) and "token" in response:
        response = {**response, "token": "<redacted>"}
    return {**envelope, "response": response}
