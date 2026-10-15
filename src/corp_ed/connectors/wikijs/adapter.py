"""Спецификация вида wikijs и адаптер (режим organization).

Админ компании включает API в Wiki.js («Администрирование → API»),
создаёт ключ с полным доступом и вводит его с адресом своей Wiki.js.
Ключ с правами группы (не «Full Access») не принимается: без
`manage:system` Wiki.js не отдаёт группы и почты, а страницы, закрытые
самому ключу, не видны.

Права — как их считает сам Wiki.js при показе страницы (rules.py):
страница, которую читает гость (группа Guests, пользователь id 2), —
«вся компания»; иначе — почты активных пользователей, которым её
открывают правила их групп (`users.list`, `groups.single` → permissions,
pageRules, users). Внимание: в новой установке у Guests есть правило
START "" — все страницы открыты без входа и станут «всей компанией»,
пока администратор Wiki.js его не сузит (так же их видит любой
посетитель сайта). Неизвестный исход REGEX-правила — в сторону
закрытости (rules.py).

Обход: `pages.list` (без постраничности: Wiki.js отдаёт список целиком)
→ без неопубликованных и «личных» (isPrivate), затем окно публикации
(`publishStartDate`/`publishEndDate`) пакетами `pages.single` по
BATCH_PAGES страниц в запросе: вне окна страницу видят только
редакторы (write:pages), и она не индексируется. Все локали; адрес —
`{сайт}{локаль}/{путь}` (работает и без namespacing). Версия —
`updatedAt`.

Содержимое — `render` (готовый HTML любого редактора: Markdown,
визуальный, AsciiDoc); якоря заголовков «¶» вырезаются. Пустой render
(страница ещё не отрисована) — исходник `content`: Markdown или HTML.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from html import escape
from typing import Any
from urllib.parse import quote, urlsplit

import structlog
from bs4 import BeautifulSoup

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    AdapterOptions,
    FetchedContent,
    FetchedMarkdown,
    FetchedPage,
    RemoteDocument,
)
from corp_ed.connectors.common import Recorder, parse_datetime, to_int
from corp_ed.connectors.registry import (
    AdapterRegistry,
    FieldSpec,
    KindSpec,
    ModuleSpec,
)
from corp_ed.connectors.wikijs.client import GraphResult, WikiJsClient
from corp_ed.connectors.wikijs.rules import Group, Page, PageRule, readers
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind

logger = structlog.get_logger()

MODULE_PAGES = "pages"
PREFIX = "page:"
GUEST_ID = 2
"""Гость — пользователь id 2 (server/setup.js, users.getGuestUser)."""
BATCH_PAGES = 50
BATCH_GROUPS = 25
PAGE_NOT_FOUND = 6003
AUTH_REQUIRED = 1019

WIKIJS_SPEC = KindSpec(
    kind="wikijs",
    title="Wiki.js (база знаний)",
    mode=ConnectorMode.ORGANIZATION,
    modules=(ModuleSpec(MODULE_PAGES, "Страницы"),),
    config_fields=(FieldSpec("base_url", "Адрес Wiki.js (https://wiki.company.ru)"),),
    credential_fields=(
        FieldSpec(
            "token",
            "API-ключ Wiki.js с полным доступом (Администрирование → API)",
            secret=True,
        ),
    ),
    url_field="base_url",
    extra={"key_owner": "ключ «Full Access» (группа Administrators)"},
    preview=True,
)

_CHECK = "{ users { profile { id } } system { info { currentVersion } } }"
_USERS = "{ users { list { id email isActive isSystem } } }"
_GROUPS = "{ groups { list { id } } }"
_GROUP_FIELDS = (
    "id permissions users { id } pageRules { id deny match roles path locales }"
)
_PAGES = (
    "{ pages { list(orderBy: ID) { id path locale title isPublished "
    "isPrivate updatedAt tags } } }"
)
_WINDOW_FIELDS = "id publishStartDate publishEndDate"
_SINGLE = (
    "query ($id: Int!) { pages { single(id: $id) { "
    "id title render content contentType } } }"
)


class WikiJsAdapter:
    def __init__(self, client: WikiJsClient, *, max_bytes: int) -> None:
        self._client = client
        self._max_bytes = max_bytes
        self._checked = False
        self.version: str | None = None

    @property
    def root(self) -> str:
        return self._client.root

    async def check(self) -> None:
        result = await self._client.query("check", _CHECK)
        if result.error_at("users", "profile") or not _dig(
            result.data, "users", "profile", "id"
        ):
            # Ключ не принят: Wiki.js обслужил запрос как гостя.
            raise AdapterAuthError("unauthorized")
        version = _dig(result.data, "system", "info", "currentVersion")
        if result.error_at("system") or not isinstance(version, str):
            raise AdapterAuthError("admin_required")
        self.version = version
        self._checked = True

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        if MODULE_PAGES not in set(modules):
            return
        if not self._checked:
            await self.check()
        emails = await self._emails()
        groups, memberships = await self._groups()
        guest = {GUEST_ID: memberships.pop(GUEST_ID, frozenset())}
        members = {u: g for u, g in memberships.items() if u in emails}
        pages = await self._pages()
        now = datetime.now(UTC)
        hidden = await self._outside_window(list(pages), now)
        unsure_pages = 0
        for page_id, page in pages.items():
            if page_id in hidden:
                continue
            target = Page(
                path=str(page["path"]),
                locale=str(page["locale"]),
                tags=frozenset(str(t) for t in page.get("tags") or [] if t),
            )
            public, _ = readers(guest, groups, target)
            if public:
                visibility = MaterialVisibility.TENANT
                allowed: frozenset[str] = frozenset()
            else:
                users, unsure = readers(members, groups, target)
                unsure_pages += 1 if unsure else 0
                visibility = MaterialVisibility.RESTRICTED
                allowed = frozenset(emails[u] for u in users)
            updated = str(page.get("updatedAt") or "")
            folder = target.path.rpartition("/")[0]
            yield RemoteDocument(
                external_id=f"{PREFIX}{page_id}",
                title=str(page.get("title") or "").strip() or target.path,
                url=f"{self.root}{quote(target.locale)}/{quote(target.path)}",
                version=updated,
                kind=RemoteDocumentKind.PAGE,
                module=MODULE_PAGES,
                path="/".join(p for p in (target.locale, folder) if p),
                modified_at=parse_datetime(updated),
                visibility=visibility,
                allowed_emails=allowed,
            )
        if unsure_pages:
            # REGEX-правила, которые не посчитать: часть сотрудников без доступа.
            logger.info("wikijs_regex_rules_unsure", pages=unsure_pages)

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        page_id = to_int(document.external_id.removeprefix(PREFIX))
        if not document.external_id.startswith(PREFIX) or page_id is None:
            raise AdapterError("unknown_document")
        result = await self._client.query("pages.single", _SINGLE, {"id": page_id})
        page = _dig(result.data, "pages", "single")
        error = result.error_at("pages", "single")
        if error is not None and error.code == PAGE_NOT_FOUND:
            raise AdapterError("not_found")
        if not isinstance(page, dict):
            raise AdapterError("content_missing")
        title = str(page.get("title") or document.title)
        render = page.get("render")
        content = page.get("content")
        if isinstance(render, str) and render.strip():
            _limit(render, max_bytes)
            return FetchedPage(html=_titled_html(render, title))
        if isinstance(content, str) and content.strip():
            _limit(content, max_bytes)
            if page.get("contentType") == "html":
                return FetchedPage(html=_titled_html(content, title))
            return FetchedMarkdown(markdown=_titled_markdown(content.strip(), title))
        raise AdapterError("empty_page")

    # --- права -----------------------------------------------------------------

    async def _emails(self) -> dict[int, str]:
        result = await self._client.query("users.list", _USERS)
        found: dict[int, str] = {}
        for user in _list(_dig(result.data, "users", "list")):
            user_id = to_int(user.get("id"))
            email = user.get("email")
            if user_id is None or user_id == GUEST_ID or user.get("isSystem"):
                continue
            if user.get("isActive") is not True:
                continue
            if isinstance(email, str) and "@" in email:
                found[user_id] = email.strip().lower()
        return found

    async def _groups(self) -> tuple[dict[int, Group], dict[int, frozenset[int]]]:
        listed = await self._client.query("groups.list", _GROUPS)
        ids = [
            group_id
            for group in _list(_dig(listed.data, "groups", "list"))
            if (group_id := to_int(group.get("id"))) is not None
        ]
        groups: dict[int, Group] = {}
        members: dict[int, set[int]] = {}
        for chunk in _chunks(ids, BATCH_GROUPS):
            result = await self._batch("groups.single", "groups", chunk, _GROUP_FIELDS)
            for group_id in chunk:
                raw = _dig(result.data, "groups", f"g{group_id}")
                if not isinstance(raw, dict):
                    # Группу удалили между запросами: её участники без её прав.
                    continue
                rules = [PageRule.parse(r) for r in _list(raw.get("pageRules"))]
                groups[group_id] = Group(
                    id=group_id,
                    permissions=frozenset(str(p) for p in raw.get("permissions") or []),
                    rules=tuple(r for r in rules if r is not None),
                    opaque=any(r is None for r in rules),
                )
                for user in _list(raw.get("users")):
                    user_id = to_int(user.get("id"))
                    if user_id is not None:
                        members.setdefault(user_id, set()).add(group_id)
        return groups, {u: frozenset(g) for u, g in members.items()}

    # --- страницы ---------------------------------------------------------------

    async def _pages(self) -> dict[int, dict[str, Any]]:
        result = await self._client.query("pages.list", _PAGES)
        pages: dict[int, dict[str, Any]] = {}
        for page in _list(_dig(result.data, "pages", "list")):
            page_id = to_int(page.get("id"))
            if page_id is None or not page.get("path") or not page.get("locale"):
                continue
            if page.get("isPublished") is not True or page.get("isPrivate") is True:
                continue
            pages[page_id] = page
        return pages

    async def _outside_window(self, ids: Sequence[int], now: datetime) -> set[int]:
        """Страницы вне окна публикации или пропавшие с листинга."""
        hidden: set[int] = set()
        for chunk in _chunks(ids, BATCH_PAGES):
            result = await self._batch("pages.single", "pages", chunk, _WINDOW_FIELDS)
            for page_id in chunk:
                alias = f"p{page_id}"
                page = _dig(result.data, "pages", alias)
                error = result.error_at("pages", alias)
                if error is not None and error.code != PAGE_NOT_FOUND:
                    raise AdapterError("page_unreadable", retryable=True)
                if not isinstance(page, dict) or not _published(page, now):
                    hidden.add(page_id)
        return hidden

    async def _batch(
        self, name: str, root: str, ids: Sequence[int], fields: str
    ) -> GraphResult:
        """Несколько single одним запросом: псевдонимы g<id> / p<id>."""
        letter = root[0]
        body = " ".join(f"{letter}{i}: single(id: {i}) {{ {fields} }}" for i in ids)
        return await self._client.query(name, f"{{ {root} {{ {body} }} }}")


def _published(page: Mapping[str, Any], now: datetime) -> bool:
    """Окно публикации: пусто — без ограничения; дату не разобрать —
    страница скрыта (Wiki.js тоже не сочтёт её опубликованной)."""
    for key, after in (("publishStartDate", False), ("publishEndDate", True)):
        value = page.get(key)
        if value in (None, ""):
            continue
        moment = parse_datetime(value)
        if moment is None:
            return False
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        if (moment < now) if after else (moment > now):
            return False
    return True


def _limit(text: str, max_bytes: int) -> None:
    if len(text.encode("utf-8")) > max_bytes:
        raise AdapterError("document_too_large")


def _titled_html(html: str, title: str) -> str:
    """Без якорей «¶» у заголовков; заголовок страницы — первым h1, если
    его там ещё нет (Markdown Wiki.js обычно начинается с «# Заголовок»)."""
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.select("a.toc-anchor"):
        anchor.decompose()
    first = soup.find(["h1", "h2", "h3"])
    clean = " ".join(title.split())
    if first is not None and " ".join(first.get_text().split()) == clean:
        return str(soup)
    return f"<h1>{escape(clean)}</h1>\n{soup}"


def _titled_markdown(markdown: str, title: str) -> str:
    clean = " ".join(title.split())
    first = markdown.split("\n", 1)[0]
    if not clean or (first.startswith("#") and first.lstrip("#").strip() == clean):
        return markdown
    return f"# {clean}\n\n{markdown}"


def _dig(data: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _list(value: Any) -> list[dict[str, Any]]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _chunks(items: Sequence[int], size: int) -> list[list[int]]:
    return [list(items[start : start + size]) for start in range(0, len(items), size)]


def build_adapter(
    spec: KindSpec,
    config: Mapping[str, str],
    credentials: Mapping[str, str],
    http: OutboundClient,
    settings: ConnectorSettings,
    *,
    recorder: Recorder | None = None,
) -> WikiJsAdapter:
    token = credentials.get("token", "").strip()
    if not token:
        raise AdapterAuthError("credentials_missing")
    base_url = config.get("base_url", "").strip()
    parts = urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname:
        raise AdapterConfigError("base_url_invalid")
    return WikiJsAdapter(
        WikiJsClient(http, base_url=base_url, token=token, recorder=recorder),
        max_bytes=settings.download_limit_bytes,
    )


def register(registry: AdapterRegistry, settings: ConnectorSettings) -> None:
    def factory(
        spec: KindSpec,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
        options: AdapterOptions,
    ) -> WikiJsAdapter:
        return build_adapter(
            spec, config, credentials, http, settings, recorder=options.recorder
        )

    registry.register(WIKIJS_SPEC, factory)
