"""Модуль «База знаний 2.0» (методы note.*, REST 3.0, scope note).

В отличие от классической базы знаний на «Сайтах», документы здесь
отдаются Markdown напрямую: note.collection.list (базы, доступные
пользователю, с курсором) → note.document.tree.list (дерево одной базы)
→ note.document.get (заголовок, markdown, updatedAt). Дерево не несёт
дат, поэтому версия документа известна только из note.document.get —
он вызывается при обходе, а содержимое запоминается на время запуска,
чтобы fetch не ходил за ним второй раз.

Права: методы отдают базы и документы с правом «Просмотр» у
пользователя токена (policyLevel базы, прямой доступ к документу) — в
режиме per_user это и есть видимость. Архивные и удалённые документы в
дерево не попадают.

Не проверено на живом портале (RISKS №32, №36): адрес документа в
интерфейсе в ответах REST не приходит — ссылка ведёт в раздел базы
знаний портала.
"""

from collections.abc import AsyncIterator
from typing import Any

import structlog

from corp_ed.connectors.base import AdapterError, FetchedMarkdown, RemoteDocument
from corp_ed.connectors.bitrix24.client import Bitrix24Client
from corp_ed.domain.types import RemoteDocumentKind

logger = structlog.get_logger()

MODULE_KNOWLEDGE_BASE_V2 = "knowledge_base_v2"
PREFIX = "note:"
PAGE_LIMIT = 50
MAX_DEPTH = 64


class NotesModule:
    def __init__(self, client: Bitrix24Client) -> None:
        self._client = client
        # id документа → markdown, полученный при обходе (один запуск).
        self._markdown: dict[str, str] = {}

    async def walk(self) -> AsyncIterator[RemoteDocument]:
        async for collection in self._collections():
            collection_id = collection.get("id")
            if collection_id is None:
                continue
            name = str(collection.get("name") or f"collection-{collection_id}")
            tree = await self._client.call(
                "note.document.tree.list", {"collectionId": collection_id}, v3=True
            )
            result = tree.get("result") or {}
            if isinstance(result, dict) and result.get("truncated"):
                logger.warning(
                    "bitrix24_notes_tree_truncated", collection_id=collection_id
                )
            items = result.get("items") if isinstance(result, dict) else None
            for node, path in _flatten(items or [], name):
                document = await self._document(node, path)
                if document is not None:
                    yield document

    async def fetch(self, document: RemoteDocument) -> FetchedMarkdown:
        document_id = document.external_id.removeprefix(PREFIX)
        markdown = self._markdown.get(document_id)
        if markdown is None:
            item = await self._get(document_id)
            markdown = str(item.get("markdown") or "")
        if not markdown.strip():
            raise AdapterError("empty_page")
        return FetchedMarkdown(markdown=markdown)

    async def _collections(self) -> AsyncIterator[dict[str, Any]]:
        cursor: dict[str, Any] | None = None
        while True:
            pagination: dict[str, Any] = {"limit": PAGE_LIMIT}
            if cursor is not None:
                pagination["afterCursor"] = cursor
            response = await self._client.call(
                "note.collection.list", {"pagination": pagination}, v3=True
            )
            result = response.get("result") or {}
            items = result.get("items") if isinstance(result, dict) else None
            for item in items or []:
                if isinstance(item, dict):
                    yield item
            cursor = result.get("nextCursor") if isinstance(result, dict) else None
            if not isinstance(cursor, dict) or not items:
                return

    async def _get(self, document_id: str) -> dict[str, Any]:
        response = await self._client.call(
            "note.document.get", {"id": int(document_id)}, v3=True
        )
        result = response.get("result") or {}
        item = result.get("item") if isinstance(result, dict) else None
        if not isinstance(item, dict):
            raise AdapterError("document_not_found")
        return item

    async def _document(self, node: dict[str, Any], path: str) -> RemoteDocument | None:
        document_id = node.get("id")
        if document_id is None:
            return None
        try:
            item = await self._get(str(document_id))
        except AdapterError as exc:
            # Документ исчез или закрыт между деревом и чтением: не наш.
            if exc.retryable or exc.code not in {"error_not_found", "access_denied"}:
                raise
            logger.info("bitrix24_note_skipped", document_id=document_id, code=exc.code)
            return None
        markdown = str(item.get("markdown") or "")
        self._markdown[str(document_id)] = markdown
        return RemoteDocument(
            external_id=f"{PREFIX}{document_id}",
            title=str(item.get("title") or node.get("title") or f"note-{document_id}"),
            url=f"{self._client.portal}knowledge/",
            version=str(item.get("updatedAt") or ""),
            kind=RemoteDocumentKind.PAGE,
            module=MODULE_KNOWLEDGE_BASE_V2,
            path=path,
            size=len(markdown.encode("utf-8")),
        )


def _flatten(
    nodes: list[Any], path: str, depth: int = 0
) -> list[tuple[dict[str, Any], str]]:
    """Дерево → список (узел, путь) в порядке обхода в глубину."""
    if depth > MAX_DEPTH:
        return []
    flat: list[tuple[dict[str, Any], str]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        flat.append((node, path))
        children = node.get("children")
        if isinstance(children, list) and children:
            title = str(node.get("title") or node.get("id"))
            flat.extend(_flatten(children, f"{path}/{title}", depth + 1))
    return flat
