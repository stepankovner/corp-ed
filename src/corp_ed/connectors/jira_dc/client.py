"""HTTP-клиент REST Jira Server / Data Center (API v2) поверх OutboundClient.

Авторизация — персональный токен доступа (Bearer, Jira 8.14+). Ответы:
JSON-объект или список (`/project`, `/user/permission/search`); ошибки —
`{errorMessages, errors}`, в код идёт только статус: текст сервера
локализован и может содержать имена. 429 (лимит DC 8.6+) и 503 (узел
стартует, переиндексация) — ожидание по Retry-After, до
len(RATE_LIMIT_BACKOFF) попыток. Скачивание вложений — только с хоста
Jira, потоково с обрывом по лимиту.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from corp_ed.connectors.base import AdapterAuthError, AdapterError
from corp_ed.connectors.common import Recorder, redact, retry_after
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError

USER_AGENT = "corp-ed-connector/1.0"
REQUEST_TIMEOUT = 60.0
DOWNLOAD_TIMEOUT = 120.0
RATE_LIMIT_BACKOFF = (1.0, 2.0, 4.0, 8.0)
MAX_RETRY_AFTER = 30.0
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_API = "rest/api/2/"

Sleep = Callable[[float], Awaitable[None]]


def site_root(address: str) -> str:
    """Адрес из настройки → корень Jira со слешем (контекстный путь вроде
    /jira сохраняется); хвост /rest/api/2 срезается."""
    root = address.strip().rstrip("/")
    for suffix in ("/rest/api/2", "/rest/api/latest"):
        if root.lower().endswith(suffix):
            root = root[: -len(suffix)]
    return root.rstrip("/") + "/"


class JiraClient:
    def __init__(
        self,
        http: OutboundClient,
        *,
        base_url: str,
        token: str,
        sleep: Sleep = asyncio.sleep,
        recorder: Recorder | None = None,
    ) -> None:
        self._http = http
        self._root = site_root(base_url)
        self._host = (urlsplit(self._root).hostname or "").lower()
        self._token = token
        self._sleep = sleep
        self._recorder = recorder

    @property
    def root(self) -> str:
        return self._root

    def absolute(self, link: str) -> str:
        """Ссылка из ответа (content вложения) → абсолютная на хосте Jira.
        Чужой хост в ответе не принимается: туда не уйдёт токен."""
        url = urljoin(self._root, link)
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or (
            (parts.hostname or "").lower() != self._host
        ):
            raise AdapterError("link_foreign")
        return url

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """GET /rest/api/2/{path}: объект или список JSON."""
        url = f"{self._root}{_API}{path.lstrip('/')}"
        query = dict(params or {})
        backoff = iter(RATE_LIMIT_BACKOFF)
        while True:
            try:
                response = await self._http.get(
                    url,
                    params=query,
                    headers=self._headers(accept="application/json"),
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
            if status in _RETRY_STATUSES:
                delay = next(backoff, None)
                if delay is None:
                    code = "rate_limited" if status == 429 else f"http_{status}"
                    raise AdapterError(code, retryable=True)
                await self._sleep(_wait(response, delay))
                continue
            data = _json(response)
            if self._recorder is not None:
                self._recorder(path, redact(query, request=True), redact(data))
            return _parse(status, data)

    async def download(self, link: str, *, max_bytes: int) -> bytes:
        url = self.absolute(link)
        try:
            downloaded = await self._http.download(
                url,
                max_bytes=max_bytes,
                headers=self._headers(accept="*/*"),
                timeout=DOWNLOAD_TIMEOUT,
                same_host=True,
            )
        except OutboundTooLargeError as exc:
            raise AdapterError("document_too_large") from exc
        except httpx.TimeoutException as exc:
            raise AdapterError("timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise AdapterError("network_error", retryable=True) from exc
        status = downloaded.status_code
        if status == 401:
            raise AdapterAuthError("unauthorized")
        if status != 200:
            raise AdapterError(
                f"download_http_{status}", retryable=status in _RETRY_STATUSES
            )
        if downloaded.headers.get("content-type", "").lower().startswith("text/html"):
            # Страница входа вместо файла (Jira уводит на login.jsp на том
            # же хосте): токен не принят для скачивания или нет прав.
            raise AdapterError("download_failed")
        return downloaded.content

    def _headers(self, *, accept: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": accept,
            "User-Agent": USER_AGENT,
            # Защита от XSRF Jira на GET не срабатывает, но заголовок —
            # обычай клиентов REST Atlassian; без него часть прокси SSO
            # считает запрос браузерным.
            "X-Atlassian-Token": "no-check",
        }


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _parse(status: int, data: Any) -> Any:
    if status == 401:
        raise AdapterAuthError("unauthorized")
    if status == 200:
        if not isinstance(data, dict | list):
            # HTML вместо JSON — страница входа SSO или не тот адрес.
            raise AdapterError("not_json")
        return data
    if status == 403:
        raise AdapterError("forbidden")
    if status == 404:
        raise AdapterError("not_found")
    if status == 400:
        raise AdapterError("bad_request")
    if 300 <= status < 400:
        # Редирект на вход SSO или на другой адрес Jira — не API.
        raise AdapterError("redirected")
    raise AdapterError(f"http_{status}", retryable=status >= 500)


def _wait(response: httpx.Response, default: float) -> float:
    seconds = retry_after(response.headers.get("retry-after"))
    return min(seconds if seconds is not None else default, MAX_RETRY_AFTER)
