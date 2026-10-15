"""HTTP-клиент публичного Web API ELMA365 поверх OutboundClient.

По api.elma365.com/ru/public-api (спецификация `api-v1-doc.json`, 15.10):
адрес `https://<компания>.elma365.ru/pub/v1` (у коробки — свой домен
компании), заголовок `Authorization: Bearer <токен>`, только JSON, тело
запроса — не больше 50 МБ. Ответы — объект с `success` и, при
`success: false`, текстом `error`: текст сервера не попадает ни в код,
ни в журнал.

Лимит облака (elma365.com/ru/help/saas-standard-limits.html): на компанию
90 + 5 × число лицензий запросов к Web API за 60 секунд; сверх него 429 с
Retry-After. Ждём столько, сколько сказано (не больше MAX_RETRY_AFTER),
без заголовка — по RATE_LIMIT_BACKOFF; 502–504 повторяются так же.

Ошибки: 401 — токен не принят; 403 — «недостаточно прав» на файл или
папку (что с ним делать, решает модуль: пропуск или остановка гранта);
400 — неверные параметры, 404 — объекта нет. Файл скачивается по
временной ссылке из `get-link` без токена сотрудника.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from corp_ed.connectors.base import AdapterAuthError, AdapterError
from corp_ed.connectors.common import Recorder, json_object, redact, retry_after
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError

USER_AGENT = "corp-ed-connector/1.0"
REQUEST_TIMEOUT = 60.0
DOWNLOAD_TIMEOUT = 120.0
RATE_LIMIT_BACKOFF = (2.0, 5.0, 10.0, 20.0, 30.0)
MAX_RETRY_AFTER = 60.0
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
# Хвосты адреса API, которые админ мог вставить вместе с адресом компании.
_API_SUFFIXES = ("/pub/v1", "/pub")
# Ключи ответа со ссылкой на скачивание: подпись в её параметрах — тот же
# секрет, в фикстуру она не пишется.
_LINK_KEYS = frozenset({"link"})

Sleep = Callable[[float], Awaitable[None]]


def site_root(address: str) -> str:
    """Адрес из настройки → корень сайта компании со слешем на конце."""
    root = address.strip().rstrip("/")
    for suffix in _API_SUFFIXES:
        if root.lower().endswith(suffix):
            root = root[: -len(suffix)]
            break
    return root.rstrip("/") + "/"


def _without_link_query(value: Any) -> Any:
    """Ссылки на скачивание без параметров (подписи) — для фикстур."""
    if isinstance(value, dict):
        return {
            key: (
                urlunsplit(urlsplit(item)._replace(query="", fragment=""))
                if str(key).lower() in _LINK_KEYS and isinstance(item, str)
                else _without_link_query(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_without_link_query(item) for item in value]
    return value


class Elma365Client:
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
        self._api = f"{self._root}pub/v1/"
        self._token = token
        self._sleep = sleep
        self._recorder = recorder

    @property
    def root(self) -> str:
        """Корень сайта компании (для ссылок на папки)."""
        return self._root

    async def get(self, path: str) -> dict[str, Any]:
        return await self._call("GET", path, None)

    async def post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        return await self._call("POST", path, body)

    async def _call(
        self, method: str, path: str, body: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """{method} /pub/v1/{path} → объект ответа с success = true."""
        backoff = iter(RATE_LIMIT_BACKOFF)
        kwargs: dict[str, Any] = {} if body is None else {"json": dict(body)}
        while True:
            try:
                response = await self._http.request(
                    method,
                    f"{self._api}{path.lstrip('/')}",
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Accept": "application/json",
                        "User-Agent": USER_AGENT,
                    },
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=False,
                    **kwargs,
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
                await self._sleep(min(wait or delay, MAX_RETRY_AFTER))
                continue
            data = json_object(response)
            if self._recorder is not None:
                self._recorder(
                    f"{method} {path}",
                    redact(dict(body or {}), request=True),
                    _without_link_query(redact(data)),
                )
            if status == 200:
                if data is None:
                    # HTML вместо JSON — страница входа или не тот адрес.
                    raise AdapterError("not_json")
                if data.get("success") is False:
                    raise AdapterError("api_error")
                return data
            if status == 401:
                raise AdapterAuthError("unauthorized")
            if status == 403:
                raise AdapterError("forbidden")
            if status == 404:
                raise AdapterError("not_found")
            if status == 400:
                raise AdapterError("bad_request")
            raise AdapterError(f"http_{status}", retryable=status >= 500)

    async def download(self, link: str, *, max_bytes: int) -> bytes:
        """Файл по временной ссылке из get-link — без токена сотрудника."""
        url = urljoin(self._root, link)
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
            # Страница входа или ошибки вместо файла: ссылка истекла.
            raise AdapterError("download_failed")
        return downloaded.content
