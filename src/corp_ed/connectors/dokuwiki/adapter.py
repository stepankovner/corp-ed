"""Спецификация вида dokuwiki и адаптер (режим organization).

Админ компании включает в DokuWiki удалённый API (`remote`, а в
`remoteuser` — служебную учётку или её группу) и вводит в kronto адрес
вики, токен суперпользователя (Профиль → «Токен авторизации», с Kaos
2024-02-06) или его логин и пароль, и список сотрудников «логин почта».

Почему список: API DokuWiki не отдаёт пользователей ни в одном методе
(`plugin.usermanager` умеет только создать и удалить, `core.whoAmI` —
только себя), а почта — единственный ключ сопоставления с kronto.
Сотрудник, которого нет в списке, не видит в kronto ни одного документа
этой вики.

Права считает сама DokuWiki: для каждого логина из списка
`core.aclCheck(id, login)` (группы сервер берёт из своего бэкенда
авторизации, суперпользователи получают 255) — читает тот, у кого
уровень ≥ 1. Документ всегда RESTRICTED с явным списком почт, «вся
компания» не выставляется даже для `@ALL`. Проверять чужие права
разрешено только суперпользователю — иной ключ не принимается
(`admin_required`).

Число вызовов: в ядре право зависит только от точных правил на сам id
и от правил пространств над ним. Таблица правил (`plugin.acl.listAcls`)
говорит, у каких id есть точные правила: они проверяются поштучно,
остальные — один раз на пространство и логин (медиафайлы пространства
делят результат с его страницами). Без плагина acl — поштучно. Больше
ACL_CHECK_LIMIT вызовов за запуск — остановка с кодом
`acl_checks_limit`, а не частичный индекс: сузить обход полем
«Пространства» или список сотрудников.

Ограничения (в сторону, которую нельзя проверить по API):

- плагины, которые переопределяют проверку прав (событие
  AUTH_ACL_CHECK) по-разному для страниц одного пространства, при
  группировке не учитываются;
- правила с подстановками `%USER%` и `%GROUP%` DokuWiki раскрывает для
  того, кто вызывает API (владелец ключа): личные пространства
  сотрудников им не достаются (в сторону закрытости), а запрет,
  записанный через подстановку, не учитывается — служебной учётке лучше
  быть отдельным суперпользователем;
- логин, удалённый из DokuWiki, но оставшийся в списке, сохраняет права
  `@ALL` и правила на своё имя — список ведёт администратор;
- право на медиафайл проверяется по его id: правило ACL, записанное
  ровно на id файла (сама DokuWiki для файлов смотрит только
  пространство), будет учтено.

Обход: `core.listPages` с глубиной вместо 0 (0 в корне — поисковый
индекс, где нет ещё не проиндексированных страниц) и `core.listMedia`
по всей вики или по пространствам из настройки (с вложенными);
удалённые страницы и файлы исчезают из листинга и
удаляются ядром. Версия — `revision` (время изменения). Страница —
`core.getPageHTML` (HTML без шапки и меню), файл — `core.getMedia`
(base64) поддерживаемых форматов.
"""

import base64
import binascii
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit

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
from corp_ed.connectors.common import Recorder, note_too_large, note_unsupported, to_int
from corp_ed.connectors.dokuwiki.client import BasicAuth, DokuWikiClient, TokenAuth
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

KIND = "dokuwiki"
MODULE_PAGES = "pages"
MODULE_MEDIA = "media"
PAGE_PREFIX = "page:"
MEDIA_PREFIX = "media:"
AUTH_READ = 1
LIST_DEPTH = 64
"""Глубина обхода страниц: любое число > 0 включает обход файлов вместо
поискового индекса; 64 уровня пространств — больше не бывает."""
ACL_CHECK_LIMIT = 20_000
USERS_FIELD_LENGTH = 64 * 1024
BASE64_SLACK = 64 * 1024
_RECORDS = re.compile(r"[\r\n;]+")
_SEPARATORS = re.compile(r"[\s,=]+")

SPEC = KindSpec(
    kind=KIND,
    title="DokuWiki",
    mode=ConnectorMode.ORGANIZATION,
    modules=(
        ModuleSpec(MODULE_PAGES, "Страницы"),
        ModuleSpec(MODULE_MEDIA, "Медиафайлы (docx, doc, xlsx, pptx, pdf, txt, md)"),
    ),
    config_fields=(
        FieldSpec("base_url", "Адрес DokuWiki (https://wiki.company.ru/)"),
        FieldSpec(
            "users",
            "Сотрудники: «логин почта» по одному на строку (или через ;)",
            max_length=USERS_FIELD_LENGTH,
            multiline=True,
        ),
        FieldSpec(
            "namespaces",
            "Пространства через запятую, с вложенными (пусто — вся вики)",
            required=False,
        ),
    ),
    credential_fields=(
        FieldSpec(
            "token",
            "Токен суперпользователя (Профиль → Токен авторизации; DokuWiki 2024+)",
            required=False,
            secret=True,
        ),
        FieldSpec("login", "Логин суперпользователя (вместо токена)", required=False),
        FieldSpec(
            "password",
            "Пароль суперпользователя (без «:» — иначе нужен токен)",
            required=False,
            secret=True,
        ),
    ),
    url_field="base_url",
    config_check=lambda config: _users_error(config.get("users", "")),
    credentials_check=lambda credentials: (
        None
        if credentials.get("token")
        or (credentials.get("login") and credentials.get("password"))
        else "credentials_incomplete"
    ),
    extra={
        "auth": "token или login+password суперпользователя",
        "remote": "в DokuWiki включены remote и remoteuser",
    },
    preview=True,
)


def parse_users(text: str) -> dict[str, str]:
    """Записи «логин почта» → {логин: почта}. Записи — по строкам или
    через «;» (так удобнее в командной строке), внутри — пробелы, запятая
    или «=»; записи с # — комментарии.
    ValueError(код) — пусто, строка не из логина и почты, повтор логина."""
    users: dict[str, str] = {}
    seen: set[str] = set()
    for raw in _RECORDS.split(text):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p for p in _SEPARATORS.split(line) if p]
        emails = [p for p in parts if "@" in p]
        logins = [p for p in parts if "@" not in p]
        if len(parts) != 2 or len(emails) != 1 or len(logins) != 1:
            raise ValueError("users_invalid")
        login, email = logins[0], emails[0].lower()
        local, _, domain = email.partition("@")
        if not local or "." not in domain:
            raise ValueError("users_invalid")
        # Логины DokuWiki обычно без учёта регистра: «Ivan» и «ivan» —
        # один человек, две почты на него — ошибка в списке.
        if login.lower() in seen:
            raise ValueError("users_duplicate")
        seen.add(login.lower())
        users[login] = email
    if not users:
        raise ValueError("users_empty")
    return users


def parse_namespaces(text: str) -> list[str]:
    """«hr, wiki:docs:*» → ["hr", "wiki:docs"]: двоеточия и звёздочка по
    краям не нужны, повторы убираются."""
    found: list[str] = []
    for raw in text.split(","):
        namespace = raw.strip().strip(":*").strip()
        if namespace and namespace not in found:
            found.append(namespace)
    return found


def _users_error(text: str) -> str | None:
    try:
        parse_users(text)
    except ValueError as exc:
        return str(exc)
    return None


class DokuWikiAdapter:
    def __init__(
        self,
        client: DokuWikiClient,
        *,
        users: Mapping[str, str],
        max_bytes: int,
        namespaces: Sequence[str] = (),
        acl_check_limit: int = ACL_CHECK_LIMIT,
    ) -> None:
        self._client = client
        self._users = dict(users)
        # Пусто — корень (вся вики).
        self._namespaces = list(namespaces) or [""]
        self._max_bytes = max_bytes
        self._acl_check_limit = acl_check_limit
        self._checked = False
        # На один запуск: id с точными правилами (None — таблицы нет,
        # проверка поштучно) и читатели по ключу группировки.
        self._exact: frozenset[str] | None = None
        self._readers: dict[str, frozenset[str]] = {}
        self._acl_checks = 0

    @property
    def root(self) -> str:
        return self._client.root

    async def check(self) -> None:
        try:
            me = await self._client.call("core.whoAmI")
        except AdapterError as exc:
            if exc.code == "method_missing":
                # Вики до Kaos: нового API (core.*) нет.
                raise AdapterConfigError("dokuwiki_too_old") from exc
            raise
        if not isinstance(me, dict) or not me.get("login"):
            raise AdapterAuthError("user_unknown")
        if me.get("isadmin") is not True:
            raise AdapterAuthError("admin_required")
        self._checked = True

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        wanted = set(modules)
        if not wanted & {MODULE_PAGES, MODULE_MEDIA}:
            return
        if not self._checked:
            await self.check()
        self._exact = await self._exact_scopes()
        self._readers = {}
        self._acl_checks = 0
        # Вложенные пространства из настройки дали бы документ дважды.
        seen: set[str] = set()
        if MODULE_PAGES in wanted:
            for namespace in self._namespaces:
                pages = await self._client.call(
                    "core.listPages", {"namespace": namespace, "depth": LIST_DEPTH}
                )
                for page in _items(pages):
                    if _once(seen, PAGE_PREFIX, page):
                        document = await self._page(page)
                        if document is not None:
                            yield document
        if MODULE_MEDIA in wanted:
            for namespace in self._namespaces:
                files = await self._client.call(
                    "core.listMedia",
                    {"namespace": namespace, "pattern": "", "depth": 0},
                )
                for item in _items(files):
                    if _once(seen, MEDIA_PREFIX, item):
                        document = await self._media(item)
                        if document is not None:
                            yield document

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        if document.external_id.startswith(PAGE_PREFIX):
            page_id = document.external_id.removeprefix(PAGE_PREFIX)
            html = await self._client.call("core.getPageHTML", {"page": page_id})
            if not isinstance(html, str):
                raise AdapterError("content_missing")
            if not html.strip():
                raise AdapterError("empty_page")
            if len(html.encode("utf-8")) > max_bytes:
                raise AdapterError("document_too_large")
            return FetchedPage(html=html)
        if document.external_id.startswith(MEDIA_PREFIX):
            media_id = document.external_id.removeprefix(MEDIA_PREFIX)
            if document.size is not None and document.size > max_bytes:
                raise AdapterError("document_too_large")
            try:
                encoded = await self._client.call(
                    "core.getMedia",
                    {"media": media_id},
                    max_bytes=max_bytes * 4 // 3 + BASE64_SLACK,
                )
            except AdapterError as exc:
                if exc.code == "response_too_large":
                    raise AdapterError("document_too_large") from exc
                raise
            if not isinstance(encoded, str):
                raise AdapterError("content_missing")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise AdapterError("content_invalid") from exc
            if len(data) > max_bytes:
                raise AdapterError("document_too_large")
            return FetchedFile(
                data=data, filename=document.filename or _basename(media_id)
            )
        raise AdapterError("unknown_document")

    # --- документы ---------------------------------------------------------------

    async def _page(self, page: Mapping[str, Any]) -> RemoteDocument | None:
        page_id = page.get("id")
        if not isinstance(page_id, str) or not page_id:
            return None
        revision = to_int(page.get("revision"))
        title = str(page.get("title") or "").strip() or page_id
        emails = await self._allowed(page_id)
        return RemoteDocument(
            external_id=f"{PAGE_PREFIX}{page_id}",
            title=title,
            url=f"{self.root}doku.php?id={quote(page_id, safe=':')}",
            version=str(revision or ""),
            kind=RemoteDocumentKind.PAGE,
            module=MODULE_PAGES,
            path=_path(page_id),
            size=to_int(page.get("size")),
            modified_at=_moment(revision),
            visibility=MaterialVisibility.RESTRICTED,
            allowed_emails=emails,
        )

    async def _media(self, item: Mapping[str, Any]) -> RemoteDocument | None:
        media_id = item.get("id")
        if not isinstance(media_id, str) or not media_id:
            return None
        filename = _basename(media_id)
        if PurePosixPath(filename).suffix.lower() not in supported_extensions():
            note_unsupported(filename, media_id)
            return None
        size = to_int(item.get("size"))
        if size is not None and size > self._max_bytes:
            note_too_large(media_id)
            return None
        revision = to_int(item.get("revision"))
        emails = await self._allowed(media_id)
        return RemoteDocument(
            external_id=f"{MEDIA_PREFIX}{media_id}",
            title=filename,
            url=f"{self.root}lib/exe/fetch.php?media={quote(media_id, safe=':')}",
            version=str(revision or ""),
            kind=RemoteDocumentKind.FILE,
            module=MODULE_MEDIA,
            path=_path(media_id),
            filename=filename,
            size=size,
            modified_at=_moment(revision),
            visibility=MaterialVisibility.RESTRICTED,
            allowed_emails=emails,
        )

    # --- права ---------------------------------------------------------------------

    async def _exact_scopes(self) -> frozenset[str] | None:
        """id, на которые в ACL есть точные правила; None — таблицы нет
        (плагин acl выключен): тогда каждый документ проверяется сам."""
        try:
            table = await self._client.call("plugin.acl.listAcls")
        except AdapterError as exc:
            if isinstance(exc, AdapterAuthError | AdapterConfigError):
                raise
            if exc.code != "method_missing":
                raise
            logger.info("dokuwiki_acl_table_unavailable")
            return None
        if isinstance(table, list) and not table:
            # Пустой массив PHP приходит списком: правил нет совсем.
            return frozenset()
        if not isinstance(table, dict):
            raise AdapterError("acl_table_invalid")
        return frozenset(str(scope) for scope in table)

    async def _allowed(self, item_id: str) -> frozenset[str]:
        key = self._group_key(item_id)
        cached = self._readers.get(key)
        if cached is not None:
            return cached
        emails: set[str] = set()
        for login, email in self._users.items():
            if self._acl_checks >= self._acl_check_limit:
                raise AdapterConfigError("acl_checks_limit")
            self._acl_checks += 1
            level = to_int(
                await self._client.call(
                    "core.aclCheck", {"page": item_id, "user": login, "groups": []}
                )
            )
            if level is None:
                raise AdapterError("acl_invalid")
            if level >= AUTH_READ:
                emails.add(email)
        readers = frozenset(emails)
        self._readers[key] = readers
        return readers

    def _group_key(self, item_id: str) -> str:
        """Ключ кеша прав: сам id, если на него есть точное правило (или
        таблицы правил нет), иначе пространство — в ядре DokuWiki право
        такого id равно праву пространства (auth_aclcheck_cb)."""
        if self._exact is None or item_id in self._exact:
            return f"id:{item_id}"
        namespace, _, _ = item_id.rpartition(":")
        return f"ns:{namespace}"


def _items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise AdapterError("listing_invalid")
    return [item for item in value if isinstance(item, dict)]


def _once(seen: set[str], prefix: str, item: Mapping[str, Any]) -> bool:
    key = f"{prefix}{item.get('id')}"
    if key in seen:
        return False
    seen.add(key)
    return True


def _basename(item_id: str) -> str:
    return item_id.rpartition(":")[2] or item_id


def _path(item_id: str) -> str:
    """Пространство имён для человека: «hr:docs:plan» → «hr/docs»."""
    return item_id.rpartition(":")[0].replace(":", "/")


def _moment(revision: int | None) -> datetime | None:
    if not revision or revision <= 0:
        return None
    try:
        return datetime.fromtimestamp(revision, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def build_adapter(
    spec: KindSpec,
    config: Mapping[str, str],
    credentials: Mapping[str, str],
    http: OutboundClient,
    settings: ConnectorSettings,
    *,
    recorder: Recorder | None = None,
) -> DokuWikiAdapter:
    token = credentials.get("token", "").strip()
    login = credentials.get("login", "").strip()
    password = credentials.get("password", "")
    auth: TokenAuth | BasicAuth
    if token:
        auth = TokenAuth(token)
    elif login and password:
        auth = BasicAuth(login, password)
    else:
        raise AdapterAuthError("credentials_missing")
    base_url = config.get("base_url", "").strip()
    if urlsplit(base_url).scheme != "https" or not urlsplit(base_url).hostname:
        raise AdapterConfigError("base_url_invalid")
    try:
        users = parse_users(config.get("users", ""))
    except ValueError as exc:
        raise AdapterConfigError(str(exc)) from exc
    return DokuWikiAdapter(
        DokuWikiClient(http, base_url=base_url, auth=auth, recorder=recorder),
        users=users,
        max_bytes=settings.download_limit_bytes,
        namespaces=parse_namespaces(config.get("namespaces", "")),
    )


def register(registry: AdapterRegistry, settings: ConnectorSettings) -> None:
    def factory(
        spec: KindSpec,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
        options: AdapterOptions,
    ) -> DokuWikiAdapter:
        return build_adapter(
            spec, config, credentials, http, settings, recorder=options.recorder
        )

    registry.register(SPEC, factory)
