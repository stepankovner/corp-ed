"""Яндекс Вики (Яндекс 360 для бизнеса) от имени сотрудника.

По документации публичного API Вики (yandex.ru/support/wiki/ru/api-ref/,
28.09; живой организацией не проверено — RISKS №43):
- база https://api.wiki.yandex.net/v1, заголовки Authorization: OAuth
  <токен Яндекс ID> и X-Org-Id: <идентификатор организации>; право
  приложения wiki:read. Запросы идут от имени сотрудника и видят только
  доступные ему страницы — видимость как у Диска (per_user);
- обход: pages/descendants?slug=&include_self=true&page_size=100&cursor=
  → {results:[{id, slug}], next_cursor}. Способа перечислить всю Вики
  без корня в документации нет, поэтому корни — разделы из настройки
  подключения (по умолчанию homepage);
- метаданные: pages/{id}?fields=attributes,breadcrumbs,redirect —
  title, slug, page_type (page|grid|wysiwyg|template),
  attributes.modified_at и is_draft. Версии в ответе нет — версия
  документа = modified_at;
- содержимое: pages/{id}?fields=content — строка в разметке YFM
  (Markdown-диалект) или старой вики-разметки. Динамические таблицы
  (grid) — отдельные ресурсы, пропускаются;
- ошибки — JSON {error_code, debug_message}; 403 FORCED_SYNC_REQUIRED —
  сотрудник ни разу не открывал Вики в браузере.
"""

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any

import httpx
import structlog

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterError,
    FetchedMarkdown,
    RemoteDocument,
)
from corp_ed.connectors.common import Recorder, json_object, parse_datetime, redact
from corp_ed.connectors.yandex.oauth import YandexAuth
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import RemoteDocumentKind

logger = structlog.get_logger()

USER_AGENT = "corp-ed-connector/1.0"
MODULE_WIKI = "wiki"
PREFIX = "ywiki:"
WEB_BASE = "https://wiki.yandex.ru/"
DEFAULT_ROOTS = ("homepage",)
PAGE_SIZE = 100
REQUEST_TIMEOUT = 30.0
RATE_LIMIT_BACKOFF = (1.0, 2.0, 4.0)
# Динамические таблицы и шаблоны — не текст для ответов.
_SKIPPED_TYPES = frozenset({"grid", "template"})
_SKIPPED_CODES = frozenset({"not_found", "forbidden"})

Sleep = Callable[[float], Awaitable[None]]


class YandexWikiClient:
    def __init__(
        self,
        http: OutboundClient,
        *,
        api: str,
        org_id: str,
        auth: YandexAuth,
        sleep: Sleep = asyncio.sleep,
        recorder: Recorder | None = None,
    ) -> None:
        self._http = http
        self._api = api if api.endswith("/") else api + "/"
        self._org_id = org_id
        self._auth = auth
        self._sleep = sleep
        self._recorder = recorder

    async def get(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        await self._auth.ensure_fresh()
        refreshed = False
        backoff = iter(RATE_LIMIT_BACKOFF)
        while True:
            try:
                response = await self._http.get(
                    f"{self._api}v1/{path.lstrip('/')}",
                    params=dict(params or {}),
                    headers={
                        "Authorization": self._auth.header,
                        "X-Org-Id": self._org_id,
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
                await self._auth.refresh()
                refreshed = True
                continue
            if status in (429, 502, 503, 504):
                delay = next(backoff, None)
                if delay is None:
                    raise AdapterError("rate_limited", retryable=True)
                await self._sleep(delay)
                continue
            data = json_object(response)
            if self._recorder is not None:
                self._recorder(
                    f"wiki/{path}",
                    redact(dict(params or {}), request=True),
                    redact(data),
                )
            if status == 200 and data is not None:
                return data
            error = str((data or {}).get("error_code") or "").upper()
            if status == 403 and error == "FORCED_SYNC_REQUIRED":
                # «Please authenticate user via frontend first»: грант
                # сотрудника, а не приложение — ему открыть Вики.
                raise AdapterAuthError("wiki_login_required")
            if status == 403:
                logger.info("yandex_wiki_forbidden", path=path, error=error[:64])
                raise AdapterError("forbidden")
            if status == 404:
                raise AdapterError("not_found")
            code = (error or f"http_{status}").lower()
            raise AdapterError(f"wiki_{code}"[:64], retryable=status >= 500)


class YandexWikiModule:
    def __init__(
        self,
        client: YandexWikiClient,
        *,
        roots: Sequence[str],
        max_bytes: int,
    ) -> None:
        self._client = client
        self._roots = tuple(roots) or DEFAULT_ROOTS
        self._max_bytes = max_bytes

    async def walk(self) -> AsyncIterator[RemoteDocument]:
        seen: set[str] = set()
        for root in self._roots:
            async for page_id in self._descendants(root):
                if page_id in seen:
                    continue
                seen.add(page_id)
                document = await self._document(page_id)
                if document is not None:
                    yield document

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedMarkdown:
        if not document.locator:
            raise AdapterError("locator_missing")
        page = await self._client.get(
            f"pages/{document.locator}",
            {"fields": "content", "raise_on_redirect": "true"},
        )
        content = page.get("content")
        if not isinstance(content, str):
            raise AdapterError("content_missing")
        if len(content.encode("utf-8")) > max_bytes:
            raise AdapterError("document_too_large")
        return FetchedMarkdown(markdown=normalize_wiki_markup(content))

    async def _descendants(self, root: str) -> AsyncIterator[str]:
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "slug": root,
                "include_self": "true",
                "page_size": PAGE_SIZE,
            }
            if cursor:
                params["cursor"] = cursor
            try:
                page = await self._client.get("pages/descendants", params)
            except AdapterError as exc:
                if (
                    not isinstance(exc, AdapterAuthError)
                    and not exc.retryable
                    and exc.code in _SKIPPED_CODES
                ):
                    logger.info("yandex_wiki_root_skipped", root=root, code=exc.code)
                    return
                raise
            for item in page.get("results") or []:
                page_id = item.get("id") if isinstance(item, dict) else None
                if isinstance(page_id, int | str) and str(page_id):
                    yield str(page_id)
            next_cursor = page.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return
            cursor = next_cursor

    async def _document(self, page_id: str) -> RemoteDocument | None:
        try:
            page = await self._client.get(
                f"pages/{page_id}", {"fields": "attributes,breadcrumbs,redirect"}
            )
        except AdapterError as exc:
            if (
                not isinstance(exc, AdapterAuthError)
                and not exc.retryable
                and exc.code in _SKIPPED_CODES
            ):
                return None
            raise
        if page.get("redirect"):
            return None
        if str(page.get("page_type") or "") in _SKIPPED_TYPES:
            return None
        attributes = page.get("attributes") or {}
        if not isinstance(attributes, dict) or attributes.get("is_draft"):
            return None
        slug = str(page.get("slug") or "").strip("/")
        title = str(page.get("title") or slug or page_id)
        modified = str(attributes.get("modified_at") or "")
        return RemoteDocument(
            external_id=f"{PREFIX}{page_id}",
            title=title,
            url=f"{WEB_BASE}{slug}/" if slug else WEB_BASE,
            version=modified or slug,
            kind=RemoteDocumentKind.PAGE,
            module=MODULE_WIKI,
            path=_breadcrumbs(page.get("breadcrumbs")) or slug,
            locator=page_id,
            modified_at=parse_datetime(modified),
        )


def parse_roots(value: str) -> tuple[str, ...]:
    """Разделы Вики из настройки: slug через запятую или перенос строки;
    вставленные целиком адреса wiki.yandex.ru тоже принимаются."""
    roots: list[str] = []
    for raw in re.split(r"[,\n]", value or ""):
        slug = raw.strip()
        for prefix in ("https://wiki.yandex.ru/", "http://wiki.yandex.ru/"):
            slug = slug.removeprefix(prefix)
        slug = slug.strip("/")
        if slug and slug not in roots:
            roots.append(slug)
    return tuple(roots)


def _breadcrumbs(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    titles = [
        str(item.get("title"))
        for item in value
        if isinstance(item, dict) and item.get("title")
    ]
    return " / ".join(titles)


_FILE_DIRECTIVE = re.compile(r"\{%\s*file\b[^%]*?name=\"([^\"]*)\"[^%]*%\}")
_DIRECTIVE = re.compile(r"\{%.*?%\}", re.DOTALL)
_DYNAMIC_BLOCK = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_TABLE = re.compile(r"#\|(.*?)\|#", re.DOTALL)
_ROW = re.compile(r"\|\|(.*?)\|\|", re.DOTALL)


def normalize_wiki_markup(content: str) -> str:
    """Разметка Вики → Markdown для конвейера.

    Директивы YFM ({% cut %}, {% note %}, …) снимаются, содержимое внутри
    остаётся; вложенный файл — его имя. Динамические блоки старой
    разметки ({{…}}) — не текст документа. Многострочные таблицы
    (#| || ячейка | ячейка || |#) — обычные таблицы Markdown, иначе
    нарезка разрежет строку таблицы пополам.
    """
    text = _FILE_DIRECTIVE.sub(lambda m: m.group(1), content)
    text = _DIRECTIVE.sub("", text)
    text = _DYNAMIC_BLOCK.sub("", text)
    text = _TABLE.sub(lambda m: _table(m.group(1)), text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def _table(body: str) -> str:
    rows = [
        [" ".join(cell.split()) for cell in row.split("|")]
        for row in _ROW.findall(body)
    ]
    rows = [row for row in rows if any(row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    return "\n" + "\n".join(lines) + "\n"
