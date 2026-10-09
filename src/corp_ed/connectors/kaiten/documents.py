"""Модуль «Документы» Kaiten (раздел документов и базы знаний).

Видимость — только дерево `GET /tree-entities`: по документации оно
отдаёт «только сущности, читаемые пользователем», без архивных и
защищённых, а читаемые потомки нечитаемого узла в нём остаются. Список
`GET /documents` о правах молчит — из него берутся только даты и номера
версий (там есть `updated` и `version`, в дереве — нет), и документ,
которого нет в дереве сотрудника, в его листинг не попадает, даже если
список его отдал. Документ из дерева, которого нет в списке, читается
по uid (`GET /documents/{uid}`).

Дерево (BETA) отдаёт не больше двух уровней за запрос (`levels_count`),
страницами по TREE_LIMIT. Узел, про который точно известно, что его
прямые потомки уже пришли (прямой потомок запрошенного родителя), не
запрашивается снова; остальные — запрашиваются: читаемый потомок
нечитаемого узла приходит со своим настоящим родителем, и по ответу не
понять, на каком он уровне. Лишний запрос дешевле пропущенной ветки.

Содержимое — `data` документа в ProseMirror JSON → Markdown
(prosemirror.py). Версия — `version` и `updated` документа.

Адрес документа в интерфейсе в ответах API не приходит: ссылка
`{сайт}/documents/{uid}` — предположение, проверить на живой системе.
"""

from collections.abc import AsyncIterator
from typing import Any

import structlog

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterError,
    FetchedMarkdown,
    RemoteDocument,
)
from corp_ed.connectors.common import parse_datetime, path_segment
from corp_ed.connectors.kaiten.client import KaitenClient
from corp_ed.connectors.kaiten.prosemirror import prosemirror_to_markdown, with_title
from corp_ed.domain.types import RemoteDocumentKind

logger = structlog.get_logger()

MODULE_DOCUMENTS = "documents"
PREFIX = "doc:"
TREE_LIMIT = 500
DOCUMENTS_LIMIT = 100
MAX_ENTITIES = 50_000
MAX_PATH_DEPTH = 64
ENTITY_DOCUMENT = "document"
_SKIPPED = frozenset({"forbidden", "not_found"})


class DocumentsModule:
    def __init__(self, client: KaitenClient) -> None:
        self._client = client

    async def walk(self) -> AsyncIterator[RemoteDocument]:
        entities = await self._tree()
        readable = [
            uid
            for uid, entity in entities.items()
            if entity.get("entity_type") == ENTITY_DOCUMENT
        ]
        if not readable:
            return
        listed = await self._listed()
        for uid in readable:
            meta = listed.get(uid)
            if meta is None:
                meta = await self._info(uid)
                if meta is None:
                    continue
            if meta.get("archived") is True:
                continue
            yield self._document(uid, entities, meta)

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedMarkdown:
        uid = document.external_id.removeprefix(PREFIX)
        item = await self._client.get_object(f"documents/{path_segment(uid)}")
        title = str(item.get("title") or document.title)
        body = prosemirror_to_markdown(item.get("data"))
        if not body.strip():
            raise AdapterError("empty_page")
        markdown = with_title(body, title)
        if len(markdown.encode("utf-8")) > max_bytes:
            raise AdapterError("document_too_large")
        return FetchedMarkdown(markdown=markdown)

    # --- дерево ---------------------------------------------------------------------

    async def _tree(self) -> dict[str, dict[str, Any]]:
        """Все читаемые сущности: uid → сущность."""
        entities: dict[str, dict[str, Any]] = {}
        queue: list[str | None] = [None]
        queried: set[str | None] = set()
        while queue:
            parent = queue.pop()
            if parent in queried:
                continue
            queried.add(parent)
            async for entity in self._level(parent):
                uid = str(entity["uid"])
                if uid in entities:
                    continue
                if len(entities) >= MAX_ENTITIES:
                    logger.warning("kaiten_tree_too_large", limit=MAX_ENTITIES)
                    return entities
                entities[uid] = entity
                # Прямой потомок запрошенного узла пришёл на первом уровне —
                # его собственные потомки уже в ответе. Остальные — нет
                # уверенности: запросить.
                if entity.get("parent_entity_uid") != parent:
                    queue.append(uid)
        return entities

    async def _level(self, parent: str | None) -> AsyncIterator[dict[str, Any]]:
        offset = 0
        while True:
            params: dict[str, Any] = {
                "levels_count": 2,
                "limit": TREE_LIMIT,
                "offset": offset,
            }
            if parent is not None:
                params["parent_entity_uid"] = parent
            try:
                page = await self._client.get_list("tree-entities", params)
            except AdapterError as exc:
                if isinstance(exc, AdapterAuthError) or exc.retryable:
                    raise
                if exc.code == "not_found" and parent is None:
                    # Коробка без дерева (BETA): без него видимость не узнать.
                    raise AdapterError("tree_unavailable") from exc
                if exc.code in _SKIPPED:
                    logger.info("kaiten_tree_node_skipped", code=exc.code)
                    return
                raise
            for entity in page:
                if entity.get("uid") and entity.get("archived") is not True:
                    yield entity
            if len(page) < TREE_LIMIT:
                return
            offset += len(page)

    # --- метаданные документов -------------------------------------------------------

    async def _listed(self) -> dict[str, dict[str, Any]]:
        """Список /documents: uid → метаданные (без права на видимость)."""
        found: dict[str, dict[str, Any]] = {}
        offset = 0
        while True:
            page = await self._client.get_list(
                "documents", {"offset": offset, "limit": DOCUMENTS_LIMIT}
            )
            for item in page:
                if item.get("uid"):
                    found[str(item["uid"])] = item
            if len(page) < DOCUMENTS_LIMIT:
                return found
            offset += len(page)

    async def _info(self, uid: str) -> dict[str, Any] | None:
        try:
            return await self._client.get_object(f"documents/{path_segment(uid)}")
        except AdapterError as exc:
            if isinstance(exc, AdapterAuthError) or exc.retryable:
                raise
            if exc.code not in _SKIPPED:
                raise
            logger.info("kaiten_document_skipped", code=exc.code)
            return None

    def _document(
        self,
        uid: str,
        entities: dict[str, dict[str, Any]],
        meta: dict[str, Any],
    ) -> RemoteDocument:
        title = str(meta.get("title") or entities[uid].get("title") or uid)
        updated = str(meta.get("updated") or "")
        return RemoteDocument(
            external_id=f"{PREFIX}{uid}",
            title=title,
            url=f"{self._client.root}documents/{path_segment(uid)}",
            version=f"{meta.get('version') or ''}:{updated}",
            kind=RemoteDocumentKind.PAGE,
            module=MODULE_DOCUMENTS,
            path=_path(uid, entities),
            modified_at=parse_datetime(updated),
        )


def _path(uid: str, entities: dict[str, dict[str, Any]]) -> str:
    """Заголовки читаемых предков от корня; нечитаемые пропускаются."""
    titles: list[str] = []
    seen = {uid}
    parent = entities[uid].get("parent_entity_uid")
    while parent and parent not in seen and len(titles) < MAX_PATH_DEPTH:
        seen.add(parent)
        entity = entities.get(str(parent))
        if entity is None:
            break
        titles.append(str(entity.get("title") or parent))
        parent = entity.get("parent_entity_uid")
    return "/".join(reversed(titles))
