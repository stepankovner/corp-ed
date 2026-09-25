"""Спецификация вида confluence и адаптер (режим organization).

Обход: пространства (список из config или все глобальные текущие) →
страницы `status=current` с версией и предками → для каждой —
эффективные читатели: ограничения чтения самой страницы и каждого
предка (пользователи и группы, группы раскрываются
`/group/{name}/member`), пересечение по цепочке; ни одного
ограничения — документ виден компании (пространство выбрал админ,
значит, его смотрит вся компания — RISKS №37). Вложения наследуют
читателей страницы.

Личности: Confluence Server не отдаёт почту через REST — почта
собирается из имени пользователя по шаблону `email_template`
(`{username}` по умолчанию, то есть логин и есть почта; для LDAP с
короткими логинами — `{username}@company.ru`).
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from pathlib import PurePath
from typing import Any

import structlog

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    FetchedContent,
    FetchedFile,
    FetchedPage,
    RemoteDocument,
)
from corp_ed.connectors.confluence.client import (
    GROUP_LIMIT,
    BasicAuth,
    ConfluenceClient,
    TokenAuth,
)
from corp_ed.connectors.confluence.storage import storage_to_html
from corp_ed.connectors.registry import (
    AdapterRegistry,
    FieldSpec,
    KindSpec,
    ModuleSpec,
)
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind
from corp_ed.ingest.extract import SUPPORTED_EXTENSIONS

logger = structlog.get_logger()

KIND = "confluence"
MODULE_PAGES = "pages"
MODULE_ATTACHMENTS = "attachments"
PAGE_PREFIX = "page:"
ATTACHMENT_PREFIX = "att:"
DEFAULT_EMAIL_TEMPLATE = "{username}"
_CONTENT_EXPAND = "version,ancestors,space"
_RESTRICTION_EXPAND = "read.restrictions.user,read.restrictions.group"
# Ошибки одной страницы или группы, после которых обход продолжается.
_SKIPPABLE = frozenset({"forbidden", "not_found"})

SPEC = KindSpec(
    kind=KIND,
    title="Confluence Server / Data Center",
    mode=ConnectorMode.ORGANIZATION,
    modules=(
        ModuleSpec(MODULE_PAGES, "Страницы"),
        ModuleSpec(MODULE_ATTACHMENTS, "Вложения страниц (docx, pdf, txt, md)"),
    ),
    config_fields=(
        FieldSpec("base_url", "Адрес Confluence (https://wiki.company.ru/)"),
        FieldSpec("spaces", "Ключи пространств через запятую (пусто — все)", False),
        FieldSpec(
            "email_template",
            "Почта из логина: {username}@company.ru (пусто — логин и есть почта)",
            False,
        ),
    ),
    credential_fields=(
        FieldSpec("token", "Персональный токен доступа (DC 7.9+)", False, True),
        FieldSpec("username", "Логин служебной учётной записи", False),
        FieldSpec("password", "Пароль служебной учётной записи", False, True),
    ),
    url_field="base_url",
    extra={"auth": "token или username+password"},
)


class ConfluenceAdapter:
    def __init__(
        self,
        client: ConfluenceClient,
        *,
        spaces: Sequence[str],
        email_template: str,
        max_bytes: int,
    ) -> None:
        self._client = client
        self._spaces = [s.strip() for s in spaces if s.strip()]
        self._email_template = email_template or DEFAULT_EMAIL_TEMPLATE
        self._max_bytes = max_bytes
        # Кеши на один запуск: ограничения страниц и состав групп.
        self._restrictions: dict[str, frozenset[str] | None] = {}
        self._groups: dict[str, frozenset[str]] = {}

    async def check(self) -> None:
        user = await self._client.get("user/current")
        if user.get("type") == "anonymous" or not user.get("username"):
            raise AdapterAuthError("anonymous")

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        wanted = set(modules)
        with_attachments = MODULE_ATTACHMENTS in wanted
        async for space in self._iter_spaces():
            key = str(space.get("key") or "")
            name = str(space.get("name") or key)
            if not key:
                continue
            listing = self._client.paginate(
                "content",
                {
                    "type": "page",
                    "spaceKey": key,
                    "status": "current",
                    "expand": _CONTENT_EXPAND,
                },
            )
            async for page in listing:
                page_id = str(page.get("id") or "")
                if not page_id:
                    continue
                try:
                    readers = await self._readers(page)
                except AdapterError as exc:
                    if exc.retryable or isinstance(exc, AdapterAuthError):
                        raise
                    if exc.code not in _SKIPPABLE:
                        raise
                    logger.info(
                        "confluence_page_skipped", page_id=page_id, code=exc.code
                    )
                    continue
                path = "/".join([name, *_ancestor_titles(page)])
                visibility, emails = self._access(readers)
                if MODULE_PAGES in wanted:
                    yield RemoteDocument(
                        external_id=f"{PAGE_PREFIX}{page_id}",
                        title=str(page.get("title") or page_id),
                        url=self._webui(page),
                        version=_version(page),
                        kind=RemoteDocumentKind.PAGE,
                        module=MODULE_PAGES,
                        path=path,
                        modified_at=_when(page),
                        visibility=visibility,
                        allowed_emails=emails,
                    )
                if with_attachments:
                    page_path = f"{path}/{page.get('title') or page_id}"
                    async for attachment in self._attachments(page_id):
                        document = self._attachment_document(
                            attachment, page_path, visibility, emails
                        )
                        if document is not None:
                            yield document

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        if document.external_id.startswith(PAGE_PREFIX):
            page_id = document.external_id.removeprefix(PAGE_PREFIX)
            page = await self._client.get(
                f"content/{page_id}", {"expand": "body.storage,version"}
            )
            body = page.get("body", {}).get("storage", {}).get("value")
            if not isinstance(body, str) or not body.strip():
                raise AdapterError("empty_page")
            return FetchedPage(
                html=storage_to_html(str(page.get("title") or document.title), body)
            )
        if document.external_id.startswith(ATTACHMENT_PREFIX):
            attachment_id = document.external_id.removeprefix(ATTACHMENT_PREFIX)
            info = await self._client.get(
                f"content/{attachment_id}", {"expand": "version"}
            )
            size = _int(info.get("extensions", {}).get("fileSize"))
            if size is not None and size > max_bytes:
                raise AdapterError("document_too_large")
            link = info.get("_links", {}).get("download")
            if not isinstance(link, str) or not link:
                raise AdapterError("download_url_missing")
            data = await self._client.download(link, max_bytes=max_bytes)
            return FetchedFile(
                data=data,
                filename=str(info.get("title") or document.filename or "file"),
            )
        raise AdapterError("unknown_document")

    # --- пространства и вложения ---------------------------------------------------

    async def _iter_spaces(self) -> AsyncIterator[dict[str, Any]]:
        if self._spaces:
            for key in self._spaces:
                try:
                    yield await self._client.get(f"space/{key}")
                except AdapterError as exc:
                    if exc.retryable or isinstance(exc, AdapterAuthError):
                        raise
                    if exc.code in _SKIPPABLE:
                        # Пространство недоступно учётной записи или удалено:
                        # админ увидит это по нулю документов, а не по сбою.
                        logger.warning(
                            "confluence_space_skipped", key=key, code=exc.code
                        )
                        continue
                    raise
            return
        async for space in self._client.paginate(
            "space", {"type": "global", "status": "current"}
        ):
            yield space

    async def _attachments(self, page_id: str) -> AsyncIterator[dict[str, Any]]:
        listing = self._client.paginate(
            f"content/{page_id}/child/attachment", {"expand": "version"}
        )
        try:
            async for attachment in listing:
                yield attachment
        except AdapterError as exc:
            if exc.retryable or isinstance(exc, AdapterAuthError):
                raise
            if exc.code not in _SKIPPABLE:
                raise
            logger.info(
                "confluence_attachments_skipped", page_id=page_id, code=exc.code
            )

    def _attachment_document(
        self,
        attachment: Mapping[str, Any],
        page_path: str,
        visibility: MaterialVisibility,
        emails: frozenset[str],
    ) -> RemoteDocument | None:
        title = str(attachment.get("title") or "")
        if PurePath(title).suffix.lower() not in SUPPORTED_EXTENSIONS:
            return None
        size = _int(attachment.get("extensions", {}).get("fileSize"))
        if size is not None and size > self._max_bytes:
            return None
        attachment_id = str(attachment.get("id") or "")
        if not attachment_id:
            return None
        return RemoteDocument(
            external_id=f"{ATTACHMENT_PREFIX}{attachment_id}",
            title=title,
            url=self._webui(attachment),
            version=_version(attachment),
            kind=RemoteDocumentKind.FILE,
            module=MODULE_ATTACHMENTS,
            path=page_path,
            filename=title,
            size=size,
            modified_at=_when(attachment),
            visibility=visibility,
            allowed_emails=emails,
        )

    # --- права ------------------------------------------------------------------------

    async def _readers(self, page: Mapping[str, Any]) -> frozenset[str] | None:
        """Кто может читать страницу: пересечение ограничений её и предков.
        None — ограничений нет ни на одном уровне."""
        chain = [str(a.get("id")) for a in page.get("ancestors") or [] if a.get("id")]
        chain.append(str(page["id"]))
        readers: frozenset[str] | None = None
        for page_id in chain:
            restricted = await self._restriction(page_id)
            if restricted is None:
                continue
            readers = restricted if readers is None else readers & restricted
        return readers

    async def _restriction(self, page_id: str) -> frozenset[str] | None:
        if page_id in self._restrictions:
            return self._restrictions[page_id]
        data = await self._client.get(
            f"content/{page_id}/restriction/byOperation",
            {"expand": _RESTRICTION_EXPAND},
        )
        read = data.get("read") or {}
        restrictions = read.get("restrictions") or {}
        users = {
            str(u.get("username"))
            for u in (restrictions.get("user") or {}).get("results") or []
            if isinstance(u, dict) and u.get("username")
        }
        groups = [
            str(g.get("name"))
            for g in (restrictions.get("group") or {}).get("results") or []
            if isinstance(g, dict) and g.get("name")
        ]
        result: frozenset[str] | None
        if not users and not groups:
            result = None
        else:
            for group in groups:
                users |= await self._members(group)
            result = frozenset(users)
        self._restrictions[page_id] = result
        return result

    async def _members(self, group: str) -> frozenset[str]:
        if group in self._groups:
            return self._groups[group]
        members: set[str] = set()
        try:
            async for user in self._client.paginate(
                f"group/{group}/member", limit=GROUP_LIMIT
            ):
                if user.get("username"):
                    members.add(str(user["username"]))
        except AdapterError as exc:
            if exc.retryable or isinstance(exc, AdapterAuthError):
                raise
            if exc.code not in _SKIPPABLE:
                raise
            # Группу не видно служебной учётке: её участники прав не получат.
            logger.warning("confluence_group_unreadable", group=group, code=exc.code)
        self._groups[group] = frozenset(members)
        return self._groups[group]

    def _access(
        self, readers: frozenset[str] | None
    ) -> tuple[MaterialVisibility, frozenset[str]]:
        if readers is None:
            return MaterialVisibility.TENANT, frozenset()
        emails = frozenset(
            self._email_template.replace("{username}", username) for username in readers
        )
        return MaterialVisibility.RESTRICTED, emails

    def _webui(self, item: Mapping[str, Any]) -> str:
        link = item.get("_links", {}).get("webui")
        if isinstance(link, str) and link:
            try:
                return self._client.absolute(link)
            except AdapterError:
                pass
        return f"{self._client.base_url}pages/viewpage.action?pageId={item.get('id')}"


def _ancestor_titles(page: Mapping[str, Any]) -> list[str]:
    return [
        str(a.get("title"))
        for a in page.get("ancestors") or []
        if isinstance(a, dict) and a.get("title")
    ]


def _version(item: Mapping[str, Any]) -> str:
    version = item.get("version") or {}
    return f"{version.get('number') or ''}:{version.get('when') or ''}"


def _when(item: Mapping[str, Any]) -> datetime | None:
    value = (item.get("version") or {}).get("when")
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_adapter(
    config: Mapping[str, str],
    credentials: Mapping[str, str],
    http: OutboundClient,
    settings: ConnectorSettings,
) -> ConfluenceAdapter:
    base_url = config.get("base_url", "")
    if not base_url:
        raise AdapterConfigError("base_url_missing")
    auth: TokenAuth | BasicAuth
    if credentials.get("token"):
        auth = TokenAuth(credentials["token"])
    elif credentials.get("username") and credentials.get("password"):
        auth = BasicAuth(credentials["username"], credentials["password"])
    else:
        raise AdapterAuthError("credentials_missing")
    template = config.get("email_template", "").strip() or DEFAULT_EMAIL_TEMPLATE
    if "{username}" not in template:
        raise AdapterConfigError("email_template_invalid")
    return ConfluenceAdapter(
        ConfluenceClient(http, base_url=base_url, auth=auth),
        spaces=config.get("spaces", "").split(","),
        email_template=template,
        max_bytes=settings.max_document_bytes,
    )


def register(registry: AdapterRegistry, settings: ConnectorSettings) -> None:
    def factory(
        spec: KindSpec,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
    ) -> ConfluenceAdapter:
        return build_adapter(config, credentials, http, settings)

    registry.register(SPEC, factory)
