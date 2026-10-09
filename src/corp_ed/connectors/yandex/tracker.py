"""Яндекс Трекер от имени сотрудника: задачи, комментарии, вложения.

По документации API Трекера (yandex.ru/support/tracker/ru/, 09.10.2026;
живой организацией не проверено — модуль скрыт до проверки):
- база https://api.tracker.yandex.net/v3, Authorization: OAuth <токен
  Яндекс ID>, право приложения tracker:read; «запросы выполняются от имени
  пользователя Трекера» — видимость как у Диска и Вики (per_user);
- организация — X-Org-ID, если к Трекеру привязана организация Яндекс 360
  для бизнеса, и X-Cloud-Org-ID, если организация Yandex Identity Hub
  (Yandex Cloud): у такой идентификатор не числовой, поэтому отдельное поле;
- очереди: GET queues?perPage&page, число страниц — X-Total-Pages;
- задачи очереди: POST issues/_search {"queue": KEY} — относительная
  пагинация без потолка в 10 000: perPage и id, следующая страница —
  в заголовке Link (rel="next"; из него берётся только id — на адрес из
  ответа запрос не уходит); expand=attachments — вложения в той же выдаче;
- комментарии: GET issues/{id}/comments?perPage&id, следующая страница —
  тоже в Link;
- вложения: content — ссылка на скачивание на api.tracker.yandex.net, с
  тем же токеном; токен уходит только на этот хост.

Документ — задача: Markdown с заголовком, статусом, описанием (YFM, как у
Вики) и комментариями. Версия — номер версии задачи, updatedAt и счётчики
комментариев: меняет ли новый комментарий version или updatedAt, в
документации не сказано (проверить живьём); правка текста комментария без
изменения задачи не заметна. Вложение — отдельный документ-файл.

Ошибки: 401 — продление токена и один повтор; 429 и 5xx шлюза — ожидание
(Retry-After) и повтор; 403/404 очереди — пропуск очереди (нет доступа или
нет такой), 403 списка очередей — у сотрудника нет Трекера: модуль пуст.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import PurePath
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import structlog

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterError,
    FetchedFile,
    FetchedMarkdown,
    RemoteDocument,
)
from corp_ed.connectors.common import (
    Recorder,
    note_too_large,
    note_unsupported,
    path_segment,
    redact,
    retry_after,
    safe_code,
    to_int,
)
from corp_ed.connectors.yandex.oauth import YandexAuth
from corp_ed.connectors.yandex.wiki import normalize_wiki_markup
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError
from corp_ed.domain.types import RemoteDocumentKind
from corp_ed.ingest.extract import supported_extensions

logger = structlog.get_logger()

USER_AGENT = "corp-ed-connector/1.0"
MODULE_TRACKER = "tracker"
PREFIX = "ytracker:"
FILE_PREFIX = "ytracker-file:"
WEB_BASE = "https://tracker.yandex.ru/"
PAGE_SIZE = 100
MAX_PAGES = 10_000
"""Страниц одной выдачи — защита от зацикленной пагинации."""
MAX_COMMENTS = 2_000
REQUEST_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 120.0
RATE_LIMIT_BACKOFF = (1.0, 2.0, 4.0)
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_SKIPPED_CODES = frozenset({"forbidden", "not_found"})

Sleep = Callable[[float], Awaitable[None]]


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class YandexTrackerClient:
    def __init__(
        self,
        http: OutboundClient,
        *,
        api: str,
        auth: YandexAuth,
        org_id: str = "",
        cloud_org_id: str = "",
        recorder: Recorder | None = None,
    ) -> None:
        self._http = http
        self._api = api if api.endswith("/") else api + "/"
        self._host = (urlsplit(self._api).hostname or "").lower()
        self._auth = auth
        # Организация Identity Hub важнее: если админ её указал, Трекер
        # привязан к ней, а не к организации Яндекс 360.
        self._org_header = (
            ("X-Cloud-Org-ID", cloud_org_id) if cloud_org_id else ("X-Org-ID", org_id)
        )
        self._recorder = recorder

    def _headers(self) -> dict[str, str]:
        name, value = self._org_header
        return {
            "Authorization": self._auth.header,
            name: value,
            "Accept": "application/json",
            "Accept-Language": "ru",
            "User-Agent": USER_AGENT,
        }

    async def call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> tuple[Any, httpx.Headers]:
        """Вызов API: (разобранный JSON, заголовки ответа)."""
        await self._auth.ensure_fresh()
        refreshed = False
        backoff = iter(RATE_LIMIT_BACKOFF)
        while True:
            try:
                response = await self._http.request(
                    method,
                    f"{self._api}v3/{path.lstrip('/')}",
                    params=dict(params or {}),
                    json=dict(body) if body is not None else None,
                    headers=self._headers(),
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
            if status in _RETRY_STATUSES:
                delay = next(backoff, None)
                if delay is None:
                    code = "rate_limited" if status == 429 else f"http_{status}"
                    raise AdapterError(code, retryable=True)
                await _sleep(retry_after(response.headers.get("retry-after")) or delay)
                continue
            try:
                data: Any = response.json()
            except ValueError:
                data = None
            if self._recorder is not None:
                self._recorder(
                    f"tracker/{path}",
                    redact({**dict(params or {}), **dict(body or {})}, request=True),
                    redact(data),
                )
            if status == 200 and data is not None:
                return data, response.headers
            if status == 403:
                # Текст ошибки Трекера — в errorMessages на языке
                # пользователя: наружу один код.
                raise AdapterError("forbidden")
            if status == 404:
                raise AdapterError("not_found")
            raise AdapterError(
                safe_code(f"http_{status}", prefix="tracker_"), retryable=status >= 500
            )

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        """Вложение по ссылке content: только https на хосте API — иначе
        токен сотрудника ушёл бы туда, куда указал ответ."""
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").lower() != self._host:
            raise AdapterError("download_url_foreign")
        await self._auth.ensure_fresh()
        refreshed = False
        while True:
            try:
                downloaded = await self._http.download(
                    url,
                    max_bytes=max_bytes,
                    headers={**self._headers(), "Accept": "*/*"},
                    timeout=DOWNLOAD_TIMEOUT,
                )
            except OutboundTooLargeError as exc:
                raise AdapterError("document_too_large") from exc
            except httpx.TimeoutException as exc:
                raise AdapterError("timeout", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise AdapterError("network_error", retryable=True) from exc
            if downloaded.status_code == 401 and not refreshed:
                await self._auth.refresh()
                refreshed = True
                continue
            if downloaded.status_code != 200:
                raise AdapterError(
                    f"download_http_{downloaded.status_code}",
                    retryable=downloaded.status_code >= 500,
                )
            return downloaded.content


class YandexTrackerModule:
    def __init__(
        self,
        client: YandexTrackerClient,
        *,
        queues: Sequence[str],
        max_bytes: int,
    ) -> None:
        self._client = client
        self._queues = tuple(queues)
        self._max_bytes = max_bytes

    async def walk(self) -> AsyncIterator[RemoteDocument]:
        queues = (
            [(key, key) for key in self._queues]
            if self._queues
            else await self._all_queues()
        )
        for key, name in queues:
            async for document in self._queue(key, name):
                yield document

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedFile | FetchedMarkdown:
        if not document.locator:
            raise AdapterError("locator_missing")
        if document.external_id.startswith(FILE_PREFIX):
            if document.size is not None and document.size > max_bytes:
                raise AdapterError("document_too_large")
            data = await self._client.download(document.locator, max_bytes=max_bytes)
            return FetchedFile(data=data, filename=document.filename or document.title)
        issue, _ = await self._client.call(
            "GET", f"issues/{path_segment(document.locator)}"
        )
        if not isinstance(issue, dict):
            raise AdapterError("tracker_bad_response")
        comments = [c async for c in self._comments(document.locator)]
        markdown = render_issue(issue, comments)
        if len(markdown.encode("utf-8")) > max_bytes:
            raise AdapterError("document_too_large")
        return FetchedMarkdown(markdown=markdown)

    async def _all_queues(self) -> list[tuple[str, str]]:
        queues: list[tuple[str, str]] = []
        page = 1
        while page <= MAX_PAGES:
            try:
                data, headers = await self._client.call(
                    "GET", "queues", params={"perPage": PAGE_SIZE, "page": page}
                )
            except AdapterError as exc:
                if _skippable(exc):
                    # У сотрудника нет Трекера (или не та организация):
                    # модуль пуст, остальные модули идут как шли.
                    logger.info("yandex_tracker_unavailable", code=exc.code)
                    return []
                raise
            items = (
                [q for q in data if isinstance(q, dict)]
                if isinstance(data, list)
                else []
            )
            for queue in items:
                key = str(queue.get("key") or "")
                if key:
                    queues.append((key, str(queue.get("name") or key)))
            total = to_int(headers.get("x-total-pages")) or 1
            if not items or page >= total:
                break
            page += 1
        return queues

    async def _queue(self, key: str, name: str) -> AsyncIterator[RemoteDocument]:
        params: dict[str, Any] = {"perPage": PAGE_SIZE, "expand": "attachments"}
        for _ in range(MAX_PAGES):
            try:
                data, headers = await self._client.call(
                    "POST", "issues/_search", params=params, body={"queue": key}
                )
            except AdapterError as exc:
                if _skippable(exc):
                    logger.info("yandex_tracker_queue_skipped", code=exc.code)
                    return
                raise
            issues = (
                [i for i in data if isinstance(i, dict)]
                if isinstance(data, list)
                else []
            )
            for issue in issues:
                for document in self._documents(issue, name):
                    yield document
            next_id = _next_param(headers.get("link"), "id")
            if not issues or not next_id or next_id == params.get("id"):
                return
            params["id"] = next_id

    async def _comments(self, issue_id: str) -> AsyncIterator[dict[str, Any]]:
        params: dict[str, Any] = {"perPage": PAGE_SIZE}
        count = 0
        for _ in range(MAX_PAGES):
            data, headers = await self._client.call(
                "GET",
                f"issues/{path_segment(issue_id)}/comments",
                params=params,
            )
            comments = (
                [c for c in data if isinstance(c, dict)]
                if isinstance(data, list)
                else []
            )
            for comment in comments:
                count += 1
                if count > MAX_COMMENTS:
                    return
                yield comment
            next_id = _next_param(headers.get("link"), "id")
            if not comments or not next_id or next_id == str(params.get("id", "")):
                return
            params["id"] = next_id

    def _documents(
        self, issue: Mapping[str, Any], queue_name: str
    ) -> list[RemoteDocument]:
        issue_id = str(issue.get("id") or "")
        key = str(issue.get("key") or "")
        if not issue_id or not key:
            return []
        queue = issue.get("queue") if isinstance(issue.get("queue"), dict) else {}
        label = f"Трекер / {_display(queue) or queue_name}"
        url = f"{WEB_BASE}{key}"
        version = ":".join(
            str(issue.get(name) or 0)
            for name in (
                "version",
                "updatedAt",
                "commentWithoutExternalMessageCount",
                "commentWithExternalMessageCount",
            )
        )
        documents = [
            RemoteDocument(
                external_id=f"{PREFIX}{issue_id}",
                title=f"{key}: {issue.get('summary') or ''}".strip().rstrip(":"),
                url=url,
                version=version,
                kind=RemoteDocumentKind.PAGE,
                module=MODULE_TRACKER,
                path=label,
                locator=issue_id,
                modified_at=None,
            )
        ]
        for attachment in issue.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            document = self._attachment(issue_id, url, f"{label} / {key}", attachment)
            if document is not None:
                documents.append(document)
        return documents

    def _attachment(
        self, issue_id: str, url: str, label: str, attachment: Mapping[str, Any]
    ) -> RemoteDocument | None:
        att_id = str(attachment.get("id") or "")
        name = str(attachment.get("name") or "")
        content = attachment.get("content")
        if not att_id or not name or not isinstance(content, str):
            return None
        external_id = f"{FILE_PREFIX}{issue_id}:{att_id}"
        if PurePath(name).suffix.lower() not in supported_extensions():
            note_unsupported(name, external_id)
            return None
        size = to_int(attachment.get("size"))
        if size is not None and size > self._max_bytes:
            note_too_large(external_id)
            return None
        return RemoteDocument(
            external_id=external_id,
            title=name,
            url=url,
            # Вложение не редактируется: новый файл — новый id.
            version=f"{att_id}:{size or ''}",
            kind=RemoteDocumentKind.FILE,
            module=MODULE_TRACKER,
            path=label,
            locator=content,
            filename=name,
            size=size,
        )


def render_issue(
    issue: Mapping[str, Any], comments: Sequence[Mapping[str, Any]]
) -> str:
    """Задача → Markdown для конвейера (сырой HTML вырежет ядро)."""
    key = str(issue.get("key") or "")
    summary = str(issue.get("summary") or "").strip()
    lines = [f"# {key}: {summary}" if summary else f"# {key}", ""]
    facts = [
        ("Статус", _display(issue.get("status"))),
        ("Тип", _display(issue.get("type"))),
        ("Очередь", _display(issue.get("queue"))),
        ("Автор", _display(issue.get("createdBy"))),
        ("Исполнитель", _display(issue.get("assignee"))),
        ("Создана", _date(issue.get("createdAt"))),
        ("Обновлена", _date(issue.get("updatedAt"))),
    ]
    lines.append(" · ".join(f"{name}: {value}" for name, value in facts if value))
    description = str(issue.get("description") or "").strip()
    if description:
        lines += ["", "## Описание", "", normalize_wiki_markup(description).strip()]
    texts = [
        (comment, str(comment.get("text") or "").strip())
        for comment in comments
        if str(comment.get("text") or "").strip()
    ]
    if texts:
        lines += ["", "## Комментарии"]
        for comment, text in texts:
            author = _display(comment.get("createdBy")) or "Без автора"
            when = _date(comment.get("createdAt"))
            lines += ["", f"**{author}**" + (f", {when}" if when else ""), ""]
            lines.append(normalize_wiki_markup(text).strip())
    return "\n".join(lines).strip() + "\n"


def parse_queues(value: str) -> tuple[str, ...]:
    """Ключи очередей из настройки: через запятую или перенос строки."""
    keys: list[str] = []
    for raw in value.replace("\n", ",").split(","):
        key = raw.strip().upper()
        if key and key not in keys:
            keys.append(key)
    return tuple(keys)


def _skippable(exc: AdapterError) -> bool:
    return (
        not isinstance(exc, AdapterAuthError)
        and not exc.retryable
        and exc.code in _SKIPPED_CODES
    )


def _next_param(link: str | None, name: str) -> str | None:
    """Значение параметра из ссылки rel="next" заголовка Link."""
    if not link:
        return None
    for part in link.split(","):
        target, _, rest = part.partition(";")
        if 'rel="next"' not in rest.replace(" ", "") and "rel=next" not in rest:
            continue
        query = urlsplit(target.strip().strip("<>")).query
        values = parse_qs(query).get(name)
        return values[0] if values else None
    return None


def _display(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("display") or value.get("key") or "").strip()
    return ""


def _date(value: Any) -> str:
    return str(value)[:10] if isinstance(value, str) and value else ""
