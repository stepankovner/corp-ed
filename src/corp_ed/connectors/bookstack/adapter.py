"""Спецификация вида bookstack и адаптер (режим organization).

Админ компании создаёт в BookStack API-токен (профиль → «API-токены»)
от учётной записи **администратора** (роль Admin; у роли должно быть
право «Access System API») и вводит Token ID, Token Secret и адрес
своей установки. Токену нужны: список пользователей и ролей
(`users-manage`, `user-roles-manage`), права содержимого любой книги,
главы и страницы (`restrictions-manage-all`) и видимость всего
содержимого. Роль Admin видит всё по определению; токен не
администратора индексирует только то, что видит сам, — безопасно, но не
полно.

Обход: пользователи (`/api/users`) и роли (`/api/roles`, состав и
системные права — `/api/roles/{id}`) → книги, главы, страницы (списки
`count`/`offset`) → права содержимого каждой страницы, её главы и книги
(`/api/content-permissions/{type}/{id}`, BookStack v23.01+) → право
«просмотр» для каждого пользователя с почтой (permissions.py). Черновики
(видны только автору) и шаблоны страниц пропускаются; удалённое (в
корзине) API не отдаёт — оно исчезает из листинга и удаляется ядром.

Права. Роль Admin видит всё. Документ «вся компания» — только если есть
роль, в которой состоят все пользователи, и страницу видит каждый
(Directory.everyone); иначе — RESTRICTED со списком почт тех, у кого
право доказано. Гость (пользователь роли Public) не учитывается. Права
уровня, которые API не отдал однозначно (незнакомая форма ответа,
книга или глава недоступна токену), — страница только для роли Admin.
Нет права читать права содержимого (403) — подключение
останавливается (`admin_required`), а не индексирует наугад.

Содержимое страницы — `html` из `/api/pages/{id}` (уже с подставленными
включениями `{{@id}}`), с заголовком страницы; HTML чистит ядро.

Модуль «Вложения страниц»: файлы (`/api/attachments`, без ссылок
`external`) страниц, которые отданы листингом, — с правами своей
страницы. Файл API отдаёт base64 внутри JSON (`/api/attachments/{id}`),
размер до скачивания неизвестен.

Лимит API — 180 запросов в минуту (client.py); на запуск — по запросу на
роль, на каждую страницу, главу и книгу (права) и на каждую скачанную
страницу или файл.
"""

import base64
import binascii
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from html import escape
from pathlib import PurePath
from typing import Any
from urllib.parse import urlsplit

import structlog

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    AdapterOptions,
    FetchedContent,
    FetchedFile,
    FetchedPage,
    RemoteDocument,
)
from corp_ed.connectors.bookstack.client import BookStackClient
from corp_ed.connectors.bookstack.permissions import (
    PUBLIC_ROLE,
    Directory,
    Role,
    Rules,
    parse_rules,
)
from corp_ed.connectors.common import (
    Recorder,
    note_unsupported,
    parse_datetime,
    path_segment,
    to_int,
)
from corp_ed.connectors.registry import (
    AdapterRegistry,
    FieldSpec,
    KindSpec,
    ModuleSpec,
)
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind
from corp_ed.ingest.extract import supported_extensions

logger = structlog.get_logger()

KIND = "bookstack"
MODULE_PAGES = "pages"
MODULE_ATTACHMENTS = "attachments"
PAGE_PREFIX = "page:"
ATTACHMENT_PREFIX = "attachment:"
RESPONSE_SLACK = 64 * 1024
"""Сверх base64 файла в ответе — поля вложения."""

SPEC = KindSpec(
    kind=KIND,
    title="BookStack (вики)",
    mode=ConnectorMode.ORGANIZATION,
    modules=(
        ModuleSpec(MODULE_PAGES, "Книги, главы и страницы"),
        ModuleSpec(MODULE_ATTACHMENTS, "Вложения страниц"),
    ),
    config_fields=(FieldSpec("base_url", "Адрес BookStack (https://wiki.company.ru)"),),
    credential_fields=(
        FieldSpec(
            "token_id",
            "Token ID API-токена администратора BookStack (профиль → API-токены)",
            secret=True,
        ),
        FieldSpec("token_secret", "Token Secret того же токена", secret=True),
    ),
    url_field="base_url",
    extra={"key_owner": "администратор BookStack (роль Admin, «Access System API»)"},
    # Не в каталоге, пока не включён CONNECTOR_PREVIEW_KINDS (стенд —
    # tests/live/bookstack, облака у BookStack нет).
    preview=True,
)


class _Gone:
    """Элемента уже нет: content-permissions ответил 404."""


GONE = _Gone()


@dataclass(frozen=True)
class _Access:
    visibility: MaterialVisibility
    emails: frozenset[str]


class BookStackAdapter:
    def __init__(self, client: BookStackClient, *, max_bytes: int) -> None:
        self._client = client
        self._max_bytes = max_bytes
        self._checked = False
        # Права уровней на один запуск: книга и глава общие для страниц.
        self._rules: dict[str, Rules | None] = {}

    @property
    def root(self) -> str:
        return self._client.root

    async def check(self) -> None:
        try:
            await self._client.get("books", {"count": 1})
        except AdapterError as exc:
            if exc.code == "forbidden":
                # Токен истёк или у роли нет «Access System API».
                raise AdapterAuthError("api_access_denied") from exc
            raise
        for path in ("users", "roles"):
            try:
                await self._client.get(path, {"count": 1})
            except AdapterError as exc:
                if exc.code == "forbidden":
                    raise AdapterAuthError("admin_required") from exc
                if exc.code == "not_found":
                    # Роли и права содержимого появились в API в v23.x —
                    # старая установка их не отдаёт.
                    raise AdapterConfigError("bookstack_version_unsupported") from exc
                raise
        self._checked = True

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        chosen = set(modules)
        if not chosen & {MODULE_PAGES, MODULE_ATTACHMENTS}:
            return
        if not self._checked:
            await self.check()
        directory = await self._directory()
        books = {_id(b): b async for b in self._client.pages("books") if _id(b)}
        chapters = {_id(c): c async for c in self._client.pages("chapters") if _id(c)}
        listed = [p async for p in self._client.pages("pages") if _listable(p)]
        access: dict[int, _Access] = {}
        paths: dict[int, str] = {}
        for page in listed:
            page_id = int(page["id"])
            found = await self._access(page, directory)
            if found is None:
                continue
            access[page_id] = found
            book = books.get(_id(page, "book_id"), {})
            chapter = chapters.get(_id(page, "chapter_id"), {})
            paths[page_id] = "/".join(
                str(part.get("name") or "").strip()
                for part in (book, chapter)
                if part.get("name")
            )
            if MODULE_PAGES not in chosen:
                continue
            updated = str(page.get("updated_at") or "")
            yield RemoteDocument(
                external_id=f"{PAGE_PREFIX}{page_id}",
                title=str(page.get("name") or "").strip() or str(page_id),
                url=self._page_url(page, book),
                version=f"{page.get('revision_count') or ''}:{updated}",
                kind=RemoteDocumentKind.PAGE,
                module=MODULE_PAGES,
                path=paths[page_id],
                modified_at=parse_datetime(updated),
                visibility=found.visibility,
                allowed_emails=found.emails,
            )
        if MODULE_ATTACHMENTS not in chosen:
            return
        titles = {int(p["id"]): str(p.get("name") or "").strip() for p in listed}
        async for attachment in self._client.pages("attachments"):
            parent = to_int(attachment.get("uploaded_to"))
            if parent is None or parent not in access:
                continue
            document = self._attachment(
                attachment,
                access[parent],
                "/".join(p for p in (paths[parent], titles[parent]) if p),
            )
            if document is not None:
                yield document

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        if document.external_id.startswith(PAGE_PREFIX):
            return await self._fetch_page(
                document.external_id.removeprefix(PAGE_PREFIX), document, max_bytes
            )
        if document.external_id.startswith(ATTACHMENT_PREFIX):
            return await self._fetch_attachment(
                document.external_id.removeprefix(ATTACHMENT_PREFIX),
                document,
                max_bytes,
            )
        raise AdapterError("unknown_document")

    # --- права ---------------------------------------------------------------------

    async def _directory(self) -> Directory:
        """Пользователи с почтой, роли с составом и системными правами.

        Гость (пользователь, у которого единственная роль — Public) —
        не сотрудник: его почта не попадает в права и в «всех
        пользователей»."""
        emails: dict[int, str] = {}
        async for user in self._client.pages("users"):
            email = user.get("email")
            user_id = to_int(user.get("id"))
            if user_id is not None and isinstance(email, str) and "@" in email:
                emails[user_id] = email.strip().lower()
        roles: dict[int, Role] = {}
        async for listed in self._client.pages("roles"):
            role_id = to_int(listed.get("id"))
            if role_id is None:
                continue
            role = await self._role(role_id, listed)
            roles[role_id] = role
        user_roles: dict[int, set[int]] = {}
        for role in roles.values():
            for user_id in role.users:
                user_roles.setdefault(user_id, set()).add(role.id)
        public = {r.id for r in roles.values() if r.system_name == PUBLIC_ROLE}
        for user_id, held in user_roles.items():
            if public and held <= public:
                emails.pop(user_id, None)
        return Directory(
            emails=emails,
            roles=roles,
            user_roles={u: frozenset(r) for u, r in user_roles.items()},
        )

    async def _role(self, role_id: int, listed: Mapping[str, Any]) -> Role:
        system_name = str(listed.get("system_name") or "")
        try:
            data = await self._client.get(f"roles/{path_segment(role_id)}")
        except AdapterError as exc:
            if exc.code == "forbidden":
                raise AdapterAuthError("admin_required") from exc
            if exc.code != "not_found":
                raise
            # Роль удалили по ходу запуска: ни прав, ни участников.
            return Role(role_id, system_name, False, False, frozenset())
        permissions = data.get("permissions")
        names = (
            {p for p in permissions if isinstance(p, str)}
            if isinstance(permissions, list)
            else set()
        )
        users = data.get("users")
        members = frozenset(
            user_id
            for user in (users if isinstance(users, list) else [])
            if isinstance(user, dict)
            and (user_id := to_int(user.get("id"))) is not None
        )
        return Role(
            id=role_id,
            system_name=str(data.get("system_name") or system_name),
            # Незнакомая форма прав — как без прав: просмотр «по умолчанию»
            # только сужается (явный запрет max() не отменяет).
            view_all="page-view-all" in names,
            view_own="page-view-own" in names,
            users=members,
        )

    async def _access(
        self, page: Mapping[str, Any], directory: Directory
    ) -> _Access | None:
        """Кто видит страницу; None — страницы уже нет (404 на её права)."""
        page_id = int(page["id"])
        own = await self._level("page", page_id)
        if isinstance(own, _Gone):
            return None
        chain: list[Rules | None] = [own]
        chapter_id = to_int(page.get("chapter_id"))
        if chapter_id:
            chain.append(await self._cached_level("chapter", chapter_id))
        book_id = to_int(page.get("book_id"))
        chain.append(await self._cached_level("book", book_id) if book_id else None)
        known = [rules for rules in chain if isinstance(rules, Rules)]
        if len(known) != len(chain):
            logger.info("bookstack_rules_unknown", page=page_id)
            readers = directory.admins
        else:
            readers = directory.readers(known, to_int(page.get("owned_by")))
        if directory.everyone(readers):
            return _Access(MaterialVisibility.TENANT, frozenset())
        return _Access(MaterialVisibility.RESTRICTED, directory.emails_of(readers))

    async def _cached_level(self, kind: str, entity_id: int) -> Rules | None:
        key = f"{kind}:{entity_id}"
        if key not in self._rules:
            found = await self._level(kind, entity_id)
            self._rules[key] = None if isinstance(found, _Gone) else found
        return self._rules[key]

    async def _level(self, kind: str, entity_id: int) -> Rules | None | _Gone:
        """Права уровня; None — форма ответа незнакома; GONE — 404."""
        try:
            data = await self._client.get(
                f"content-permissions/{kind}/{path_segment(entity_id)}"
            )
        except AdapterError as exc:
            if exc.code == "forbidden":
                # Без права «Управлять правами» на этот элемент права не
                # прочитать: останавливаемся, а не угадываем.
                raise AdapterAuthError("admin_required") from exc
            if exc.code == "not_found":
                return GONE
            raise
        rules = parse_rules(data)
        if rules is None:
            logger.info("bookstack_rules_unparsed", kind=kind)
        return rules

    # --- документы ---------------------------------------------------------------

    def _page_url(self, page: Mapping[str, Any], book: Mapping[str, Any]) -> str:
        book_slug = page.get("book_slug") or book.get("slug")
        slug = page.get("slug")
        if book_slug and slug:
            return (
                f"{self.root}books/{path_segment(book_slug)}/page/{path_segment(slug)}"
            )
        # Короткая ссылка BookStack: /link/{id} ведёт на страницу.
        return f"{self.root}link/{path_segment(page['id'])}"

    def _attachment(
        self, attachment: Mapping[str, Any], access: _Access, path: str
    ) -> RemoteDocument | None:
        attachment_id = to_int(attachment.get("id"))
        if attachment_id is None or not _uploaded(attachment):
            return None
        filename = _filename(attachment)
        if filename is None:
            return None
        key = f"{ATTACHMENT_PREFIX}{attachment_id}"
        if PurePath(filename).suffix.lower() not in supported_extensions():
            note_unsupported(filename, key)
            return None
        updated = str(attachment.get("updated_at") or "")
        return RemoteDocument(
            external_id=key,
            title=filename,
            url=f"{self.root}attachments/{attachment_id}",
            version=updated,
            kind=RemoteDocumentKind.FILE,
            module=MODULE_ATTACHMENTS,
            path=path,
            filename=filename,
            modified_at=parse_datetime(updated),
            visibility=access.visibility,
            allowed_emails=access.emails,
        )

    async def _fetch_page(
        self, page_id: str, document: RemoteDocument, max_bytes: int
    ) -> FetchedPage:
        try:
            page = await self._client.get(f"pages/{path_segment(page_id)}")
        except AdapterError as exc:
            if exc.code == "response_too_large":
                raise AdapterError("document_too_large") from exc
            raise
        html = page.get("html")
        if not isinstance(html, str) or not html.strip():
            raise AdapterError("empty_page")
        if len(html.encode("utf-8")) > max_bytes:
            raise AdapterError("document_too_large")
        title = str(page.get("name") or document.title).strip()
        return FetchedPage(html=f"<h1>{escape(title)}</h1>\n{html}")

    async def _fetch_attachment(
        self, attachment_id: str, document: RemoteDocument, max_bytes: int
    ) -> FetchedFile:
        try:
            data = await self._client.get(
                f"attachments/{path_segment(attachment_id)}",
                max_bytes=max_bytes * 4 // 3 + RESPONSE_SLACK,
            )
        except AdapterError as exc:
            if exc.code == "response_too_large":
                raise AdapterError("document_too_large") from exc
            raise
        if not _uploaded(data):
            raise AdapterError("attachment_external")
        content = data.get("content")
        if not isinstance(content, str):
            raise AdapterError("content_missing")
        try:
            raw = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AdapterError("content_invalid") from exc
        if len(raw) > max_bytes:
            raise AdapterError("document_too_large")
        return FetchedFile(data=raw, filename=document.filename or document.title)


def _id(item: Mapping[str, Any], key: str = "id") -> int | None:
    return to_int(item.get(key))


def _listable(page: Mapping[str, Any]) -> bool:
    """Опубликованная страница, не шаблон. Черновик API отдаёт только его
    автору (владельцу токена) — не документ компании."""
    if _id(page) is None:
        return False
    # Флаги — по истинности: в JSON они могут прийти и 0/1.
    return not page.get("draft") and not page.get("template")


def _uploaded(attachment: Mapping[str, Any]) -> bool:
    """Загруженный файл, а не ссылка (`external`)."""
    return "external" in attachment and not attachment["external"]


def _filename(attachment: Mapping[str, Any]) -> str | None:
    """Имя вложения — подпись, которую дал автор; расширение — отдельно."""
    name = str(attachment.get("name") or "").strip().replace("/", "_")
    extension = str(attachment.get("extension") or "").strip().lower().lstrip(".")
    if not name and not extension:
        return None
    if extension and PurePath(name).suffix.lower() != f".{extension}":
        name = f"{name or 'file'}.{extension}"
    return name


def build_adapter(
    spec: KindSpec,
    config: Mapping[str, str],
    credentials: Mapping[str, str],
    http: OutboundClient,
    settings: ConnectorSettings,
    *,
    recorder: Recorder | None = None,
) -> BookStackAdapter:
    token_id = credentials.get("token_id", "").strip()
    token_secret = credentials.get("token_secret", "").strip()
    if not token_id or not token_secret:
        raise AdapterAuthError("credentials_missing")
    base_url = config.get("base_url", "").strip()
    parts = urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname:
        raise AdapterConfigError("base_url_invalid")
    return BookStackAdapter(
        BookStackClient(
            http,
            base_url=base_url,
            token_id=token_id,
            token_secret=token_secret,
            recorder=recorder,
        ),
        max_bytes=settings.download_limit_bytes,
    )


def register(registry: AdapterRegistry, settings: ConnectorSettings) -> None:
    def factory(
        spec: KindSpec,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
        options: AdapterOptions,
    ) -> BookStackAdapter:
        return build_adapter(
            spec, config, credentials, http, settings, recorder=options.recorder
        )

    registry.register(SPEC, factory)
