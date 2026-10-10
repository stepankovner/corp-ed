"""Спецификации видов outline и yonote и общий адаптер (режим organization).

Админ компании создаёт в Outline API-ключ от учётной записи
администратора (Настройки → API) и вводит его с адресом своей
установки (пусто — облако). Права Outline зеркалируются так же, как их
считает сам Outline (`documents.users`): документ видят

- все участники рабочей области, кроме гостей, — если у коллекции есть
  `permission` (read, read_write): у нас это «вся компания»;
- иначе — участники коллекции (`collections.memberships`), участники
  групп коллекции (`collections.group_memberships` → `groups.memberships`)
  и участники самого документа (`documents.memberships`,
  `documents.group_memberships`; Outline хранит там и унаследованные от
  родителя права). Нет ответа об участниках документа (403 — ключу
  нельзя менять документ) — только участники коллекции: в сторону
  закрытости.

Почты — из `users.list` (их видит только администратор, заблокированные
не возвращаются). Ключ не администратора не принимается: без почт
закрытые документы не увидит никто, и админ узнал бы об этом не сразу.

Обход: коллекции (`collections.list` — без архивных) → документы
(`documents.list`: без архивных и удалённых, поэтому они исчезают из
листинга и удаляются ядром). Черновики владельца ключа, шаблоны и
документы вне коллекции пропускаются. Версия — `revision` и `updatedAt`.

Владелец ключа видит закрытые коллекции, только если состоит в них —
даже администратор (User.collectionIds в коде Outline): документы
закрытых коллекций без него не индексируются (не «утекают», а просто
не попадают в ответы).

Содержимое: `documents.list` без заголовка x-api-version отдаёт `text`
(Markdown) — он запоминается на время запуска (не больше
MAX_CACHED_BYTES), и fetch не тратит лимит `documents.export` (25 в
минуту); иначе — `documents.export` с Accept JSON → `{data: markdown}`.

Yonote — форк Outline с тем же RPC (yonote.ru/openapi-3.json, 09.10):
отличия — у коллекции `private` вместо `permission`, у пользователя
`isAdmin` вместо `role`, методов участников документа в спецификации
нет (их не спрашиваем). Если в Yonote есть ограничения документа уже
коллекции, которых нет в API, — документ получит права коллекции:
сверить на живой системе (RISKS).
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import structlog

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    AdapterOptions,
    FetchedContent,
    FetchedMarkdown,
    RemoteDocument,
)
from corp_ed.connectors.common import Recorder, parse_datetime, path_segment
from corp_ed.connectors.outline.client import OutlineClient
from corp_ed.connectors.registry import (
    AdapterRegistry,
    FieldSpec,
    KindSpec,
    ModuleSpec,
)
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind

logger = structlog.get_logger()

MODULE_DOCUMENTS = "documents"
PREFIX = "doc:"
DOCUMENTS_LIMIT = 25
"""Страница documents.list: в ней и тексты документов — не больше 25."""
MEMBERS_LIMIT = 100
MAX_CACHED_BYTES = 16 * 1024 * 1024
MAX_PATH_DEPTH = 64
_PUBLIC_PERMISSIONS = frozenset({"read", "read_write"})
_SKIPPED = frozenset({"forbidden", "not_found"})


@dataclass(frozen=True)
class Dialect:
    kind: str
    title: str
    cloud: str
    document_memberships: bool
    preview: bool
    """Вид в каталоге — только после живой проверки."""


# Outline проверен живьём 09.10.2026 на 1.10.1 (tests/live/test_outline_live.py);
# Yonote — нет.
OUTLINE = Dialect("outline", "Outline", "https://app.getoutline.com/", True, False)
YONOTE = Dialect("yonote", "Yonote", "https://app.yonote.ru/", False, True)


def _spec(dialect: Dialect) -> KindSpec:
    host = urlsplit(dialect.cloud).hostname
    return KindSpec(
        kind=dialect.kind,
        title=f"{dialect.title} (база знаний)",
        mode=ConnectorMode.ORGANIZATION,
        modules=(ModuleSpec(MODULE_DOCUMENTS, "Документы коллекций"),),
        config_fields=(
            FieldSpec(
                "base_url",
                f"Адрес {dialect.title} (пусто — облако {host})",
                required=False,
            ),
        ),
        credential_fields=(
            FieldSpec(
                "token",
                f"API-ключ администратора {dialect.title} (Настройки → API)",
                secret=True,
            ),
        ),
        url_field="base_url",
        extra={"key_owner": "администратор, участник закрытых коллекций"},
        preview=dialect.preview,
    )


OUTLINE_SPEC = _spec(OUTLINE)
YONOTE_SPEC = _spec(YONOTE)


class OutlineAdapter:
    def __init__(
        self,
        client: OutlineClient,
        *,
        document_memberships: bool,
        max_bytes: int,
    ) -> None:
        self._client = client
        self._document_memberships = document_memberships
        self._max_bytes = max_bytes
        self._checked = False
        # Кеши на один запуск: почты, состав групп, тексты из листинга.
        self._emails: dict[str, str] = {}
        self._groups: dict[str, frozenset[str]] = {}
        self._texts: dict[str, str] = {}
        self._cached_bytes = 0

    @property
    def root(self) -> str:
        return self._client.root

    async def check(self) -> None:
        info = await self._client.call("auth.info")
        data = info.get("data")
        user = data.get("user") if isinstance(data, dict) else None
        if not isinstance(user, dict) or not user.get("id"):
            raise AdapterAuthError("user_unknown")
        if user.get("role") != "admin" and user.get("isAdmin") is not True:
            raise AdapterAuthError("admin_required")
        self._checked = True

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        if MODULE_DOCUMENTS not in set(modules):
            return
        if not self._checked:
            await self.check()
        self._emails = await self._users()
        collections = {
            str(c["id"]): c
            async for c in self._client.pages("collections.list")
            if c.get("id") and not c.get("archivedAt") and not c.get("deletedAt")
        }
        members: dict[str, frozenset[str] | None] = {}
        documents = [
            d
            async for d in self._client.pages(
                "documents.list",
                {"sort": "createdAt", "direction": "ASC"},
                limit=DOCUMENTS_LIMIT,
            )
            if self._listable(d, collections)
        ]
        titles = {str(d["id"]): str(d.get("title") or "") for d in documents}
        parents = {str(d["id"]): d.get("parentDocumentId") for d in documents}
        for document in documents:
            document_id = str(document["id"])
            collection_id = str(document["collectionId"])
            if collection_id not in members:
                members[collection_id] = await self._collection_readers(
                    collections[collection_id]
                )
            readers = members[collection_id]
            if readers is None:
                visibility = MaterialVisibility.TENANT
                emails: frozenset[str] = frozenset()
            else:
                visibility = MaterialVisibility.RESTRICTED
                users = readers | await self._document_readers(document_id)
                emails = frozenset(self._emails[u] for u in users if u in self._emails)
            self._remember_text(document_id, document.get("text"))
            path = "/".join(
                [
                    str(collections[collection_id].get("name") or ""),
                    *_ancestors(document_id, parents, titles),
                ]
            ).strip("/")
            updated = str(document.get("updatedAt") or "")
            yield RemoteDocument(
                external_id=f"{PREFIX}{document_id}",
                title=titles[document_id] or document_id,
                url=self._web_url(document),
                version=f"{document.get('revision') or ''}:{updated}",
                kind=RemoteDocumentKind.PAGE,
                module=MODULE_DOCUMENTS,
                path=path,
                modified_at=parse_datetime(updated),
                visibility=visibility,
                allowed_emails=emails,
            )

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        if not document.external_id.startswith(PREFIX):
            raise AdapterError("unknown_document")
        document_id = document.external_id.removeprefix(PREFIX)
        text = self._texts.pop(document_id, None)
        if text is None:
            exported = await self._client.call("documents.export", {"id": document_id})
            text = exported.get("data")
            if not isinstance(text, str):
                raise AdapterError("content_missing")
        markdown = _titled(text.strip(), document.title)
        if not markdown.partition("\n")[2].strip():
            # Только заголовок: экспорт пустого документа.
            raise AdapterError("empty_page")
        if len(markdown.encode("utf-8")) > max_bytes:
            raise AdapterError("document_too_large")
        return FetchedMarkdown(markdown=markdown)

    # --- права ---------------------------------------------------------------------

    async def _users(self) -> dict[str, str]:
        emails: dict[str, str] = {}
        async for user in self._client.pages("users.list"):
            email = user.get("email")
            if user.get("id") and isinstance(email, str) and "@" in email:
                if user.get("isSuspended") is True:
                    continue
                emails[str(user["id"])] = email.strip().lower()
        return emails

    async def _collection_readers(
        self, collection: Mapping[str, Any]
    ) -> frozenset[str] | None:
        """None — коллекция открыта рабочей области; иначе id участников."""
        if _public(collection):
            return None
        collection_id = str(collection["id"])
        users = await self._member_ids(
            "collections.memberships", collection_id, "memberships"
        )
        groups = await self._group_ids(
            "collections.group_memberships",
            collection_id,
            # Outline 1.10 (стенд 09.10) — groupMemberships, прежние версии —
            # collectionGroupMemberships.
            ("groupMemberships", "collectionGroupMemberships"),
        )
        for group in groups:
            users |= await self._group_members(group)
        return frozenset(users)

    async def _document_readers(self, document_id: str) -> frozenset[str]:
        if not self._document_memberships:
            return frozenset()
        users = await self._member_ids(
            "documents.memberships", document_id, "memberships"
        )
        groups = await self._group_ids(
            "documents.group_memberships", document_id, "groupMemberships"
        )
        for group in groups:
            users |= await self._group_members(group)
        return frozenset(users)

    async def _member_ids(self, method: str, target: str, key: str) -> set[str]:
        found: set[str] = set()
        try:
            async for membership in self._client.pages(
                method, {"id": target}, key=key, limit=MEMBERS_LIMIT
            ):
                if membership.get("userId"):
                    found.add(str(membership["userId"]))
        except AdapterError as exc:
            self._skippable(exc, method)
        return found

    async def _group_ids(
        self, method: str, target: str, key: str | tuple[str, ...]
    ) -> set[str]:
        found: set[str] = set()
        try:
            async for membership in self._client.pages(
                method, {"id": target}, key=key, limit=MEMBERS_LIMIT
            ):
                if membership.get("groupId"):
                    found.add(str(membership["groupId"]))
        except AdapterError as exc:
            self._skippable(exc, method)
        return found

    async def _group_members(self, group: str) -> frozenset[str]:
        if group not in self._groups:
            self._groups[group] = frozenset(
                await self._member_ids("groups.memberships", group, "groupMemberships")
            )
        return self._groups[group]

    @staticmethod
    def _skippable(exc: AdapterError, method: str) -> None:
        """403/404 на участниках — участников нет (в сторону закрытости)."""
        if isinstance(exc, AdapterAuthError) or exc.retryable:
            raise exc
        if exc.code not in _SKIPPED:
            raise exc
        logger.info("outline_members_unreadable", method=method, code=exc.code)

    # --- документы ---------------------------------------------------------------

    @staticmethod
    def _listable(document: Mapping[str, Any], collections: Mapping[str, Any]) -> bool:
        if not document.get("id") or not document.get("publishedAt"):
            return False
        if document.get("template") is True or document.get("archivedAt"):
            return False
        if document.get("deletedAt"):
            return False
        return str(document.get("collectionId") or "") in collections

    def _remember_text(self, document_id: str, text: Any) -> None:
        if not isinstance(text, str):
            return
        size = len(text.encode("utf-8"))
        if self._cached_bytes + size > MAX_CACHED_BYTES:
            return
        self._texts[document_id] = text
        self._cached_bytes += size

    def _web_url(self, document: Mapping[str, Any]) -> str:
        root = self._client.root
        link = document.get("url")
        if isinstance(link, str) and link:
            url = urljoin(root, link)
            if urlsplit(url).hostname == urlsplit(root).hostname:
                return url
        ident = document.get("urlId") or document.get("id")
        return f"{root}doc/{path_segment(ident)}"


def _public(collection: Mapping[str, Any]) -> bool:
    """Коллекция открыта всей рабочей области (Outline: permission; Yonote
    и старый Outline: private). Незнакомое значение — закрыта."""
    if "permission" in collection:
        return collection.get("permission") in _PUBLIC_PERMISSIONS
    return collection.get("private") is False


def _ancestors(
    document_id: str, parents: Mapping[str, Any], titles: Mapping[str, str]
) -> list[str]:
    chain: list[str] = []
    seen = {document_id}
    parent = parents.get(document_id)
    while parent and str(parent) in titles and len(chain) < MAX_PATH_DEPTH:
        parent = str(parent)
        if parent in seen:
            break
        seen.add(parent)
        chain.append(titles[parent])
        parent = parents.get(parent)
    return list(reversed(chain))


def _titled(markdown: str, title: str) -> str:
    """Заголовок документа первой строкой, если его там ещё нет: экспорт
    ставит его сам, текст из листинга — без него."""
    clean = " ".join(title.split())
    first = markdown.split("\n", 1)[0]
    if not clean or (first.startswith("#") and first.lstrip("#").strip() == clean):
        return markdown
    return f"# {clean}\n\n{markdown}"


def build_adapter(
    spec: KindSpec,
    config: Mapping[str, str],
    credentials: Mapping[str, str],
    http: OutboundClient,
    settings: ConnectorSettings,
    *,
    recorder: Recorder | None = None,
) -> OutlineAdapter:
    dialect = YONOTE if spec.kind == YONOTE.kind else OUTLINE
    token = credentials.get("token", "").strip()
    if not token:
        raise AdapterAuthError("credentials_missing")
    base_url = config.get("base_url", "").strip() or dialect.cloud
    if urlsplit(base_url).scheme != "https":
        raise AdapterConfigError("base_url_invalid")
    return OutlineAdapter(
        OutlineClient(http, base_url=base_url, token=token, recorder=recorder),
        document_memberships=dialect.document_memberships,
        max_bytes=settings.download_limit_bytes,
    )


def register(registry: AdapterRegistry, settings: ConnectorSettings) -> None:
    def factory(
        spec: KindSpec,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
        options: AdapterOptions,
    ) -> OutlineAdapter:
        return build_adapter(
            spec, config, credentials, http, settings, recorder=options.recorder
        )

    registry.register(OUTLINE_SPEC, factory)
    registry.register(YONOTE_SPEC, factory)
