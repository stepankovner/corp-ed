"""Спецификация вида confluence и адаптер (режим organization).

Обход: пространства (список из config или все глобальные текущие) →
страницы `status=current` с версией и предками → для каждой —
эффективные читатели: ограничения чтения самой страницы и каждого
предка (пользователи и группы, группы раскрываются
`/group/{name}/member`), пересечение по цепочке; ни одного
ограничения — документ виден компании (пространство выбрал админ,
значит, его смотрит вся компания — RISKS №37). Вложения наследуют
читателей страницы.

Состав групп в 7.x и 8.x REST отдаёт только администраторам: обычной
учётке — 401 при рабочем токене (живая проверка 01.10 на 7.19.30 и
8.5.31; 10.2 отдаёт). Тогда он собирается обратным ходом: все
пользователи (CQL `type=user`) → группы каждого (`user/memberof`) — один
раз на запуск и не больше DIRECTORY_LIMIT пользователей; сверх лимита
группы не раскрываются (права не выдаются — в сторону закрытости).

Личности: 7.x и 8.x не отдают почту через REST даже администратору
(10.x отдаёт, но адаптер для всех версий берёт шаблон) — почта
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
    path_segment,
    to_int,
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
from corp_ed.ingest.extract import supported_extensions

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
DIRECTORY_LIMIT = 2000
"""Обратный ход по составу групп (7.x, 8.x) — запрос на каждого пользователя:
2 000 — минуты на запуск; больше — права администратора Confluence
служебной учётке (тогда состав читается напрямую) — DEPLOY.md."""

SPEC = KindSpec(
    kind=KIND,
    title="Confluence Server / Data Center",
    mode=ConnectorMode.ORGANIZATION,
    modules=(
        ModuleSpec(MODULE_PAGES, "Страницы"),
        ModuleSpec(
            MODULE_ATTACHMENTS, "Вложения страниц (docx, doc, xlsx, pptx, pdf, txt, md)"
        ),
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
        FieldSpec(
            "token",
            "Персональный токен доступа (7.9+; в 10.x — только он)",
            False,
            True,
        ),
        FieldSpec("username", "Логин служебной учётной записи (до 10.x)", False),
        FieldSpec("password", "Пароль служебной учётной записи", False, True),
    ),
    url_field="base_url",
    config_check=lambda config: (
        "email_template_invalid"
        if config.get("email_template", "").strip()
        and "{username}" not in config["email_template"]
        else None
    ),
    credentials_check=lambda credentials: (
        None
        if credentials.get("token")
        or (credentials.get("username") and credentials.get("password"))
        else "credentials_incomplete"
    ),
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
        self._directory_groups: dict[str, frozenset[str]] | None = None

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
                f"content/{path_segment(page_id)}", {"expand": "body.storage,version"}
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
                f"content/{path_segment(attachment_id)}", {"expand": "version"}
            )
            size = to_int(info.get("extensions", {}).get("fileSize"))
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
                    yield await self._client.get(f"space/{path_segment(key)}")
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
            f"content/{path_segment(page_id)}/child/attachment", {"expand": "version"}
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
        attachment_id = str(attachment.get("id") or "")
        if PurePath(title).suffix.lower() not in supported_extensions():
            note_unsupported(title, attachment_id)
            return None
        size = to_int(attachment.get("extensions", {}).get("fileSize"))
        if size is not None and size > self._max_bytes:
            note_too_large(attachment_id)
            return None
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
            f"content/{path_segment(page_id)}/restriction/byOperation",
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
        members: frozenset[str]
        try:
            members = frozenset(
                [
                    str(user["username"])
                    async for user in self._client.paginate(
                        f"group/{path_segment(group)}/member", limit=GROUP_LIMIT
                    )
                    if user.get("username")
                ]
            )
        except AdapterAuthError:
            # 7.x и 8.x: состав группы — только администраторам, обычной учётке
            # 401 при рабочем токене (check прошёл). Настоящий отзыв токена
            # не потеряется: обратный ход получит тот же 401 и поднимет его.
            members = (await self._directory()).get(group, frozenset())
        except AdapterError as exc:
            if exc.retryable or exc.code not in _SKIPPABLE:
                raise
            # Группу не видно служебной учётке: её участники прав не получат.
            logger.warning("confluence_group_unreadable", group=group, code=exc.code)
            members = frozenset()
        self._groups[group] = members
        return members

    async def _directory(self) -> dict[str, frozenset[str]]:
        """Состав всех групп обратным ходом: пользователи → их группы."""
        if self._directory_groups is not None:
            return self._directory_groups
        groups: dict[str, set[str]] = {}
        users = 0
        try:
            async for found in self._client.paginate(
                "search", {"cql": "type=user"}, limit=GROUP_LIMIT
            ):
                user = found.get("user")
                username = user.get("username") if isinstance(user, dict) else None
                if not username:
                    continue
                users += 1
                if users > DIRECTORY_LIMIT:
                    logger.warning(
                        "confluence_directory_too_large", limit=DIRECTORY_LIMIT
                    )
                    groups = {}
                    break
                async for group in self._client.paginate(
                    "user/memberof", {"username": username}, limit=GROUP_LIMIT
                ):
                    if group.get("name"):
                        groups.setdefault(str(group["name"]), set()).add(str(username))
        except AdapterError as exc:
            if (
                exc.retryable
                or isinstance(exc, AdapterAuthError)
                or exc.code not in _SKIPPABLE
            ):
                raise
            logger.warning("confluence_directory_unreadable", code=exc.code)
            groups = {}
        logger.info("confluence_groups_from_directory", users=users, groups=len(groups))
        self._directory_groups = {
            name: frozenset(members) for name, members in groups.items()
        }
        return self._directory_groups

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


def build_adapter(
    config: Mapping[str, str],
    credentials: Mapping[str, str],
    http: OutboundClient,
    settings: ConnectorSettings,
    *,
    recorder: Recorder | None = None,
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
        ConfluenceClient(http, base_url=base_url, auth=auth, recorder=recorder),
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
        options: AdapterOptions,
    ) -> ConfluenceAdapter:
        return build_adapter(
            config, credentials, http, settings, recorder=options.recorder
        )

    registry.register(SPEC, factory)
