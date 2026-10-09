"""Модуль «Вложения карточек» Kaiten: файлы поддерживаемых форматов.

Обход: `GET /cards` (offset/limit ≤ 100; по пространствам из настройки —
`space_id`) → карточки, которые отдаёт API по токену сотрудника; архивные
пропускаются. Список карточек `files` по документации не содержит —
тогда они читаются из `GET /cards/{id}` (запрос на карточку; 403/404 —
карточка недоступна, пропуск). Если список их всё же отдал — без
лишних запросов.

Файлы двух видов (developers.kaiten.ru/restricted-access-files-migration):
- `type = 11` — с ограниченным доступом: постоянной ссылки нет,
  `GET /cards/{card_uid}/files/{id}` после проверки прав отдаёт
  временную подписанную ссылку (`url`), её берут прямо перед
  скачиванием и не хранят; 422 — файл помечен как вредоносный;
- старые (`type = 1`, числовой id) — с постоянной публичной `url`.
Токен сотрудника по ссылке на файл не отправляется ни в каком случае.

Ссылка на карточку `{сайт}/{id}` — предположение (короткая ссылка
интерфейса), проверить на живой системе.
"""

from collections.abc import AsyncIterator, Sequence
from pathlib import PurePath
from typing import Any

import structlog

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterError,
    FetchedFile,
    RemoteDocument,
)
from corp_ed.connectors.common import (
    note_too_large,
    note_unsupported,
    parse_datetime,
    path_segment,
    to_int,
)
from corp_ed.connectors.kaiten.client import KaitenClient
from corp_ed.domain.types import RemoteDocumentKind
from corp_ed.ingest.extract import supported_extensions

logger = structlog.get_logger()

MODULE_CARD_FILES = "card_files"
PREFIX = "file:"
CARDS_LIMIT = 100
RESTRICTED_TYPE = 11
_RESTRICTED_LOCATOR = "r:"
_PUBLIC_LOCATOR = "u:"
_SKIPPED = frozenset({"forbidden", "not_found"})


class CardFilesModule:
    def __init__(
        self, client: KaitenClient, *, max_bytes: int, spaces: Sequence[str] = ()
    ) -> None:
        self._client = client
        self._max_bytes = max_bytes
        self._spaces = [s.strip() for s in spaces if s.strip()]

    async def walk(self) -> AsyncIterator[RemoteDocument]:
        seen: set[str] = set()
        async for card in self._cards():
            files = card.get("files")
            if not isinstance(files, list):
                detail = await self._card(card)
                files = detail.get("files") if detail is not None else None
                if detail is not None:
                    card = {**card, **detail}
            for item in files if isinstance(files, list) else []:
                if not isinstance(item, dict):
                    continue
                document = self._document(card, item)
                if document is not None and document.external_id not in seen:
                    seen.add(document.external_id)
                    yield document

    async def fetch(self, document: RemoteDocument, *, max_bytes: int) -> FetchedFile:
        if document.size is not None and document.size > max_bytes:
            raise AdapterError("document_too_large")
        locator = document.locator
        if locator.startswith(_RESTRICTED_LOCATOR):
            card_uid, _, file_id = locator.removeprefix(_RESTRICTED_LOCATOR).partition(
                "/"
            )
            meta = await self._client.get_object(
                f"cards/{path_segment(card_uid)}/files/{path_segment(file_id)}"
            )
            size = to_int(meta.get("size"))
            if size is not None and size > max_bytes:
                raise AdapterError("document_too_large")
            link = meta.get("url")
        elif locator.startswith(_PUBLIC_LOCATOR):
            link = locator.removeprefix(_PUBLIC_LOCATOR)
        else:
            raise AdapterError("locator_missing")
        if not isinstance(link, str) or not link:
            raise AdapterError("download_url_missing")
        data = await self._client.download(link, max_bytes=max_bytes)
        return FetchedFile(data=data, filename=document.filename or document.title)

    async def _cards(self) -> AsyncIterator[dict[str, Any]]:
        scopes: list[dict[str, Any]] = (
            [{"space_id": space} for space in self._spaces] if self._spaces else [{}]
        )
        for scope in scopes:
            offset = 0
            while True:
                page = await self._client.get_list(
                    "cards", {**scope, "offset": offset, "limit": CARDS_LIMIT}
                )
                for card in page:
                    if card.get("id") is None:
                        continue
                    if card.get("archived") is True or card.get("condition") == 2:
                        continue
                    yield card
                if len(page) < CARDS_LIMIT:
                    break
                offset += len(page)

    async def _card(self, card: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return await self._client.get_object(f"cards/{path_segment(card['id'])}")
        except AdapterError as exc:
            if isinstance(exc, AdapterAuthError) or exc.retryable:
                raise
            if exc.code not in _SKIPPED:
                raise
            logger.info("kaiten_card_skipped", code=exc.code)
            return None

    def _document(
        self, card: dict[str, Any], item: dict[str, Any]
    ) -> RemoteDocument | None:
        file_id = item.get("id")
        name = item.get("name")
        if file_id is None or not isinstance(name, str) or not name.strip():
            return None
        if item.get("deleted") is True:
            return None
        key = str(file_id)
        if PurePath(name).suffix.lower() not in supported_extensions():
            note_unsupported(name, key)
            return None
        size = to_int(item.get("size"))
        if size is not None and size > self._max_bytes:
            note_too_large(key)
            return None
        if to_int(item.get("type")) == RESTRICTED_TYPE:
            card_uid = card.get("uid")
            if not card_uid:
                return None
            locator = f"{_RESTRICTED_LOCATOR}{card_uid}/{key}"
        else:
            url = item.get("url")
            if not isinstance(url, str) or not url:
                return None
            locator = f"{_PUBLIC_LOCATOR}{url}"
        updated = str(item.get("updated") or "")
        board = card.get("board")
        board_title = board.get("title") if isinstance(board, dict) else None
        card_title = str(card.get("title") or card["id"])
        return RemoteDocument(
            external_id=f"{PREFIX}{key}",
            title=name,
            url=f"{self._client.root}{path_segment(card['id'])}",
            version=f"{updated}:{size if size is not None else ''}",
            kind=RemoteDocumentKind.FILE,
            module=MODULE_CARD_FILES,
            path="/".join(str(p) for p in (board_title, card_title) if p),
            locator=locator,
            filename=name,
            size=size,
            modified_at=parse_datetime(updated),
        )
