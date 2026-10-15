"""Спецификация вида mediawiki и адаптер (режим organization).

Админ компании создаёт в вики бот-пароль (Служебная:BotPasswords, права
«Основные» — `basic`; для больших вики можно добавить `highvolume`) от
учётки, которая читает всё нужное, и вводит в kronto адрес api.php, имя
бота (`Логин@бот`) с паролем и сопоставление «логин в вики = почта» для
сотрудников. Почему так с правами — rights.py: прав на страницу в
MediaWiki нет, почт API не отдаёт.

Модули:
- «Страницы» — `generator=allpages` по пространствам имён (без
  перенаправлений) с `prop=revisions|info` и `intestactions=read`:
  страница, которую бот прочитать не может, пропускается. Версия —
  revid последней правки; содержимое — `action=parse` → HTML (FetchedPage).
  Правка шаблона не меняет revid включающих его страниц: такие страницы
  обновятся при следующей своей правке;
- «Файлы» — `generator=allimages` с `prop=imageinfo|info`: файлы
  поддерживаемых форматов, версия — sha1. Только если пространство
  «Файл» синхронизируется (по умолчанию — да, при Lockdown и подобных —
  если админ его перечислил). Скачиваются только с хоста api.php.
  Файлы за img_auth.php (закрытая вики со своей проверкой файлов) не
  берутся: сессия бот-пароля действует только в api.php и rest.php
  (стенд 15.10: 403), а файлового содержимого Action API не отдаёт.

Пространства по умолчанию — содержательные (`content`, обычно только
основное); иначе — список из настройки (номера или названия). Страницы
пространства «Файл» модулем «Страницы» не берутся.

Права документа одни на всю вики: RESTRICTED, почты сопоставленных
логинов, у которых в вики есть право `read` (проверяется при каждом
запуске: снятое право или блокировка убирают доступ без новой версии).
«Вся компания» не ставится никогда — даже открытой без входа вики.
Ни одного читателя — AdapterConfigError("no_readers").

OAuth (owner-only consumer) не выбран: расширение OAuth не входит в
поставку MediaWiki (в образе 1.43 его нет) — бот-пароли есть в ядре.
"""

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
from corp_ed.connectors.common import (
    Recorder,
    note_too_large,
    note_unsupported,
    parse_datetime,
    to_int,
)
from corp_ed.connectors.mediawiki.client import MediaWikiClient, api_endpoint
from corp_ed.connectors.mediawiki.rights import (
    NS_FILE,
    NS_MAIN,
    USERS_BATCH,
    Namespace,
    Readers,
    namespaces_from,
    namespaces_problem,
    parse_user_emails,
    readers_from,
    resolve_namespaces,
    restricting_extensions,
    split_namespaces,
    user_emails_problem,
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

KIND = "mediawiki"
MODULE_PAGES = "pages"
MODULE_FILES = "files"
PAGE_PREFIX = "page:"
FILE_PREFIX = "file:"
PAGES_LIMIT = 50
USER_EMAILS_LENGTH = 256 * 1024
"""Сопоставление на тысячи сотрудников: ~40 байт на строку."""
PARSE_OVERHEAD = 256 * 1024
"""Сверх лимита документа на JSON-обёртку ответа action=parse."""
# Не текст для людей: стили, скрипты, данные модулей.
_CODE_MODELS = frozenset({"css", "sanitized-css", "javascript", "json", "Scribunto"})
_NOT_FOUND = frozenset({"mediawiki_nosuchpageid", "mediawiki_missingtitle"})
_IMG_AUTH = "/img_auth.php"


def _check_config(config: Mapping[str, str]) -> str | None:
    return user_emails_problem(config.get("user_emails", "")) or namespaces_problem(
        config.get("namespaces", "")
    )


def _check_credentials(credentials: Mapping[str, str]) -> str | None:
    # Вход основным паролем через action=login устарел и не ограничен
    # правами бот-пароля: принимаем только имя вида «Логин@бот».
    if "@" not in credentials.get("username", ""):
        return "bot_password_required"
    return None


SPEC = KindSpec(
    kind=KIND,
    title="MediaWiki (своя вики)",
    mode=ConnectorMode.ORGANIZATION,
    modules=(
        ModuleSpec(MODULE_PAGES, "Страницы"),
        ModuleSpec(MODULE_FILES, "Файлы (docx, doc, xlsx, pptx, pdf, txt, md)"),
    ),
    config_fields=(
        FieldSpec("api_url", "Адрес api.php (https://wiki.company.ru/w/api.php)"),
        FieldSpec(
            "user_emails",
            "Сотрудники: «логин в вики = почта» через запятую или по строкам",
            max_length=USER_EMAILS_LENGTH,
            multiline=True,
        ),
        FieldSpec(
            "namespaces",
            "Пространства имён через запятую (пусто — основное); "
            "при Lockdown — обязательно, только открытые всем читателям",
            required=False,
        ),
    ),
    credential_fields=(
        FieldSpec("username", "Имя бот-пароля (Логин@бот, Служебная:BotPasswords)"),
        FieldSpec("password", "Бот-пароль", secret=True),
    ),
    url_field="api_url",
    config_check=_check_config,
    credentials_check=_check_credentials,
    extra={
        "bot_grants": "basic",
        "bot_page": "Special:BotPasswords",
    },
    preview=True,
)


@dataclass(frozen=True)
class _Scope:
    """Что известно после siteinfo и проверки читателей: на один запуск."""

    namespaces: tuple[int, ...]
    names: Mapping[int, Namespace]
    files: bool
    readers: frozenset[str]
    sitename: str


class MediaWikiAdapter:
    def __init__(
        self,
        client: MediaWikiClient,
        *,
        user_emails: Mapping[str, str],
        namespaces: Sequence[str] = (),
        max_bytes: int,
    ) -> None:
        self._client = client
        self._mapping = dict(user_emails)
        self._tokens = tuple(namespaces)
        self._max_bytes = max_bytes
        self._scope: _Scope | None = None

    @property
    def api_url(self) -> str:
        return self._client.api_url

    async def check(self) -> None:
        self._scope = None
        await self._prepare()

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        chosen = set(modules)
        if not chosen & {MODULE_PAGES, MODULE_FILES}:
            return
        scope = await self._prepare()
        if MODULE_PAGES in chosen:
            for ns in scope.namespaces:
                if ns == NS_FILE:
                    continue
                async for document in self._pages(ns, scope):
                    yield document
        if MODULE_FILES in chosen:
            if not scope.files:
                logger.info("mediawiki_files_namespace_not_chosen")
                return
            async for document in self._files(scope):
                yield document

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        if document.external_id.startswith(PAGE_PREFIX):
            return await self._fetch_page(document, max_bytes=max_bytes)
        if document.external_id.startswith(FILE_PREFIX):
            if document.size is not None and document.size > max_bytes:
                raise AdapterError("document_too_large")
            if not document.locator:
                raise AdapterError("locator_missing")
            data = await self._client.download(document.locator, max_bytes=max_bytes)
            return FetchedFile(data=data, filename=document.filename or document.title)
        raise AdapterError("unknown_document")

    # --- права и пространства ----------------------------------------------------

    async def _prepare(self) -> _Scope:
        if self._scope is not None:
            return self._scope
        answer = await self._client.call(
            {
                "action": "query",
                "meta": "siteinfo",
                "siprop": "general|namespaces|namespacealiases|extensions",
            }
        )
        found = answer.get("query")
        info: dict[str, Any] = found if isinstance(found, dict) else {}
        names = namespaces_from(info)
        aliases = [a for a in info.get("namespacealiases") or [] if isinstance(a, dict)]
        restricting = restricting_extensions(info)
        explicit = (
            resolve_namespaces(self._tokens, names, aliases) if self._tokens else None
        )
        if restricting and explicit is None:
            # Права по пространствам есть, но API их не показывает.
            logger.info("mediawiki_namespaces_required", extensions=restricting)
            raise AdapterConfigError("namespaces_required")
        if explicit is not None:
            namespaces = tuple(explicit)
            files = NS_FILE in explicit
        else:
            content = sorted(ns.id for ns in names.values() if ns.content)
            namespaces = tuple(content or [NS_MAIN])
            files = True
        readers = await self._readers()
        if not readers.emails:
            raise AdapterConfigError("no_readers")
        general = info.get("general")
        sitename = general.get("sitename") if isinstance(general, dict) else None
        self._scope = _Scope(
            namespaces=namespaces,
            names=names,
            files=files,
            readers=readers.emails,
            sitename=str(sitename or ""),
        )
        return self._scope

    async def _readers(self) -> Readers:
        users: list[Mapping[str, Any]] = []
        logins = list(self._mapping)
        for start in range(0, len(logins), USERS_BATCH):
            answer = await self._client.call(
                {
                    "action": "query",
                    "list": "users",
                    "ususers": logins[start : start + USERS_BATCH],
                    "usprop": "rights|blockinfo",
                }
            )
            query = answer.get("query")
            found = query.get("users") if isinstance(query, dict) else None
            users.extend(u for u in found or [] if isinstance(u, dict))
        readers = readers_from(users, self._mapping)
        logger.info(
            "mediawiki_readers",
            readers=len(readers.emails),
            missing=readers.missing,
            blocked=readers.blocked,
            no_read=readers.no_read,
        )
        return readers

    # --- страницы -----------------------------------------------------------------

    async def _pages(self, ns: int, scope: _Scope) -> AsyncIterator[RemoteDocument]:
        seen: set[int] = set()
        unreadable = 0
        async for query in self._client.query(
            {
                "generator": "allpages",
                "gapnamespace": ns,
                "gapfilterredir": "nonredirects",
                "gaplimit": PAGES_LIMIT,
                "prop": "revisions|info",
                "rvprop": "ids|timestamp|size",
                "inprop": "url",
                "intestactions": "read",
            }
        ):
            for page in query.get("pages") or []:
                if not isinstance(page, dict):
                    continue
                page_id = to_int(page.get("pageid"))
                if page_id is None or page_id in seen:
                    continue
                if not _readable(page):
                    unreadable += 1
                    seen.add(page_id)
                    continue
                document = self._page(page, page_id, scope)
                if document is not None:
                    seen.add(page_id)
                    yield document
        if unreadable:
            logger.info("mediawiki_pages_unreadable", namespace=ns, count=unreadable)

    def _page(
        self, page: Mapping[str, Any], page_id: int, scope: _Scope
    ) -> RemoteDocument | None:
        if page.get("missing") or page.get("invalid") or page.get("redirect"):
            return None
        if page.get("contentmodel") in _CODE_MODELS:
            return None
        revisions = page.get("revisions")
        revision = revisions[0] if isinstance(revisions, list) and revisions else None
        if not isinstance(revision, dict):
            # Свойства страницы придут в продолжении того же пакета.
            return None
        revid = to_int(revision.get("revid")) or to_int(page.get("lastrevid"))
        if revid is None:
            return None
        title = str(page.get("title") or page_id)
        timestamp = str(revision.get("timestamp") or "")
        return RemoteDocument(
            external_id=f"{PAGE_PREFIX}{page_id}",
            title=title,
            url=self._web_url(page, page_id),
            version=str(revid),
            kind=RemoteDocumentKind.PAGE,
            module=MODULE_PAGES,
            path=self._path(to_int(page.get("ns")), scope),
            modified_at=parse_datetime(timestamp),
            visibility=MaterialVisibility.RESTRICTED,
            allowed_emails=scope.readers,
        )

    async def _fetch_page(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedPage:
        page_id = document.external_id.removeprefix(PAGE_PREFIX)
        try:
            answer = await self._client.call(
                {
                    "action": "parse",
                    "pageid": page_id,
                    "prop": "text|revid",
                    "disablelimitreport": True,
                    "disableeditsection": True,
                    "disabletoc": True,
                },
                max_bytes=max_bytes + PARSE_OVERHEAD,
            )
        except AdapterError as exc:
            if exc.code == "response_too_large":
                raise AdapterError("document_too_large") from exc
            if exc.code in _NOT_FOUND:
                raise AdapterError("not_found") from exc
            if exc.code == "mediawiki_permissiondenied":
                raise AdapterError("forbidden") from exc
            raise
        parsed = answer.get("parse")
        text = parsed.get("text") if isinstance(parsed, dict) else None
        if isinstance(text, dict):
            # formatversion=1: {"*": html}.
            text = text.get("*")
        if not isinstance(text, str) or not text.strip():
            raise AdapterError("empty_page")
        if len(text.encode("utf-8")) > max_bytes:
            raise AdapterError("document_too_large")
        title = " ".join(document.title.split())
        return FetchedPage(html=f"<h1>{escape(title)}</h1>\n{text}")

    # --- файлы ----------------------------------------------------------------------

    async def _files(self, scope: _Scope) -> AsyncIterator[RemoteDocument]:
        seen: set[int] = set()
        behind_img_auth = 0
        supported = supported_extensions()
        async for query in self._client.query(
            {
                "generator": "allimages",
                "gailimit": PAGES_LIMIT,
                "prop": "imageinfo|info",
                "iiprop": "url|size|sha1|mime|timestamp",
                "inprop": "url",
                "intestactions": "read",
            }
        ):
            for page in query.get("pages") or []:
                if not isinstance(page, dict):
                    continue
                page_id = to_int(page.get("pageid"))
                if page_id is None or page_id in seen or not _readable(page):
                    continue
                info = page.get("imageinfo")
                item = info[0] if isinstance(info, list) and info else None
                if not isinstance(item, dict):
                    continue
                seen.add(page_id)
                name = _file_name(str(page.get("title") or ""))
                key = f"{FILE_PREFIX}{page_id}"
                if not name:
                    continue
                if PurePath(name).suffix.lower() not in supported:
                    note_unsupported(name, key)
                    continue
                size = to_int(item.get("size"))
                if size is not None and size > self._max_bytes:
                    note_too_large(key)
                    continue
                link = item.get("url")
                if not isinstance(link, str) or not link:
                    continue
                try:
                    locator = self._client.absolute(link)
                except AdapterError:
                    # Файлы на другом хосте ($wgUploadPath на CDN): cookie
                    # туда не отправляются — такой файл не берём.
                    logger.info("mediawiki_file_foreign_host")
                    continue
                if _IMG_AUTH in urlsplit(locator).path:
                    behind_img_auth += 1
                    continue
                timestamp = str(item.get("timestamp") or "")
                yield RemoteDocument(
                    external_id=key,
                    title=name,
                    url=self._web_url(page, page_id),
                    version=str(item.get("sha1") or timestamp),
                    kind=RemoteDocumentKind.FILE,
                    module=MODULE_FILES,
                    path=self._path(NS_FILE, scope),
                    locator=locator,
                    filename=name,
                    size=size,
                    modified_at=parse_datetime(timestamp),
                    visibility=MaterialVisibility.RESTRICTED,
                    allowed_emails=scope.readers,
                )
        if behind_img_auth:
            # Сессия бот-пароля действует только в api.php и rest.php
            # (BotPasswordSessionProvider): img_auth.php отдаст 403.
            logger.info("mediawiki_files_behind_img_auth", count=behind_img_auth)

    # --- ссылки и пути --------------------------------------------------------------

    def _web_url(self, page: Mapping[str, Any], page_id: int) -> str:
        link = page.get("fullurl")
        if isinstance(link, str) and link:
            try:
                return self._client.absolute(link)
            except AdapterError:
                pass
        index = self._client.api_url.removesuffix("api.php") + "index.php"
        return f"{index}?curid={page_id}"

    @staticmethod
    def _path(ns: int | None, scope: _Scope) -> str:
        namespace = scope.names.get(ns) if ns is not None else None
        name = namespace.name if namespace is not None else ""
        return "/".join(part for part in (scope.sitename, name) if part)


def _readable(page: Mapping[str, Any]) -> bool:
    """intestactions=read: бот может прочитать страницу. Нет ответа —
    не может (в сторону закрытости)."""
    actions = page.get("actions")
    return isinstance(actions, dict) and actions.get("read") is True


def _file_name(title: str) -> str:
    """«Файл:Регламент.txt» → «Регламент.txt»."""
    _, sep, name = title.partition(":")
    return name.strip() if sep else title.strip()


def build_adapter(
    config: Mapping[str, str],
    credentials: Mapping[str, str],
    http: OutboundClient,
    settings: ConnectorSettings,
    *,
    recorder: Recorder | None = None,
) -> MediaWikiAdapter:
    username = credentials.get("username", "").strip()
    password = credentials.get("password", "").strip()
    if not username or not password:
        raise AdapterAuthError("credentials_missing")
    if _check_credentials({"username": username}) is not None:
        raise AdapterAuthError("bot_password_required")
    address = config.get("api_url", "").strip()
    if urlsplit(address).scheme != "https" or not urlsplit(address).hostname:
        raise AdapterConfigError("api_url_invalid")
    mapping = parse_user_emails(config.get("user_emails", ""))
    if namespaces_problem(config.get("namespaces", "")):
        raise AdapterConfigError("namespaces_invalid")
    return MediaWikiAdapter(
        MediaWikiClient(
            http,
            api_url=api_endpoint(address),
            username=username,
            password=password,
            recorder=recorder,
        ),
        user_emails=mapping,
        namespaces=split_namespaces(config.get("namespaces", "")),
        max_bytes=settings.download_limit_bytes,
    )


def register(registry: AdapterRegistry, settings: ConnectorSettings) -> None:
    def factory(
        spec: KindSpec,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
        options: AdapterOptions,
    ) -> MediaWikiAdapter:
        return build_adapter(
            config, credentials, http, settings, recorder=options.recorder
        )

    registry.register(SPEC, factory)
