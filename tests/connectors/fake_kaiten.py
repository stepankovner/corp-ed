"""Поддельный Kaiten для контрактных тестов адаптера.

Формы ответов — по developers.kaiten.ru (09.10): `/api/latest`, Bearer
из профиля, дерево `/tree-entities` (только читаемые пользователю
сущности, `levels_count` ≤ 2, `limit`/`offset`; читаемые потомки
нечитаемого узла видны — как «первые читаемые» его ближайшего читаемого
предка), список `/documents` (offset/limit ≤ 100) и документ с `data` в
ProseMirror JSON, карточки `/cards` и `/cards/{id}` с `files`, файл
карточки с ограниченным доступом `/cards/{card_uid}/files/{id}` →
временная подписанная ссылка на другом хосте. 429 —
с `X-RateLimit-Reset` (эпоха UTC).

Список `/documents` отдаёт документы компании без фильтра по правам —
худший случай: что документация об этом молчит, адаптер не должен
считать его списком видимого.
"""

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx

from corp_ed.core.outbound import OutboundClient
from tests.fake_connector import public_resolver

HOST = "company.kaiten.ru"
STORAGE_HOST = "files.kaiten-storage.example.com"
BASE = f"https://{HOST}/"
ANNA_TOKEN = "kaiten-anna-token"  # noqa: S105 — поддельный сервер
BORIS_TOKEN = "kaiten-boris-token"  # noqa: S105
ANNA_ID = 11
BORIS_ID = 12
NOW = 1_800_000_000.0


def pm(*paragraphs: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}
            for text in paragraphs
        ],
    }


@dataclass
class FakeKaiten:
    host: str = HOST
    tokens: dict[str, int] = field(
        default_factory=lambda: {ANNA_TOKEN: ANNA_ID, BORIS_TOKEN: BORIS_ID}
    )
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    readers: dict[str, set[int] | None] = field(default_factory=dict)
    """uid сущности → кто читает (None — все)."""
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    boards: dict[int, set[int] | None] = field(default_factory=dict)
    cards: dict[int, dict[str, Any]] = field(default_factory=dict)
    cards_list_files: bool = False
    """Кладёт ли список карточек их files (документация не обещает)."""
    blobs: dict[str, bytes] = field(default_factory=dict)
    malicious: set[str] = field(default_factory=set)
    tree_missing: bool = False
    unlisted: set[str] = field(default_factory=set)
    """Документы, которых нет в списке `/documents` (есть в дереве)."""
    rate_limit_hits: int = 0
    reset_header: str | None = None
    server_errors: int = 0
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    storage_auth: list[str] = field(default_factory=list)

    @property
    def base(self) -> str:
        return f"https://{self.host}/"

    def client(self) -> OutboundClient:
        return OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
            resolver=public_resolver,
        )

    # --- наполнение -------------------------------------------------------------

    def add_entity(
        self,
        uid: str,
        title: str,
        entity_type: str,
        *,
        parent: str | None = None,
        readers: set[int] | None = None,
        archived: bool = False,
    ) -> str:
        self.entities[uid] = {
            "uid": uid,
            "title": title,
            "entity_type": entity_type,
            "parent_entity_uid": parent,
            "archived": archived,
            "access": "for_everyone" if readers is None else "by_invite",
            "sort_order": len(self.entities),
            "path": uid,
        }
        self.readers[uid] = readers
        return uid

    def add_document(
        self,
        uid: str,
        title: str,
        data: Any,
        *,
        parent: str | None = None,
        readers: set[int] | None = None,
        version: int = 1,
        updated: str = "2026-09-01T10:00:00.000Z",
        archived: bool = False,
    ) -> str:
        self.add_entity(
            uid, title, "document", parent=parent, readers=readers, archived=archived
        )
        self.documents[uid] = {
            "uid": uid,
            "id": str(len(self.documents) + 100),
            "title": title,
            "created": "2026-01-01T10:00:00.000Z",
            "updated": updated,
            "archived": archived,
            "parent_entity_uid": parent,
            "entity_type": "document",
            "access": "for_everyone" if readers is None else "by_invite",
            "version": version,
            "data": data,
        }
        return uid

    def add_card(
        self,
        card_id: int,
        title: str,
        *,
        board: int = 1,
        archived: bool = False,
    ) -> dict[str, Any]:
        card = {
            "id": card_id,
            "uid": f"card-uid-{card_id}",
            "title": title,
            "archived": archived,
            "condition": 2 if archived else 1,
            "board_id": board,
            "board": {"id": board, "title": f"Доска {board}"},
            "updated": "2026-09-02T10:00:00.000Z",
            "files": [],
        }
        self.cards[card_id] = card
        self.boards.setdefault(board, None)
        return card

    def add_file(
        self,
        card_id: int,
        file_id: str,
        name: str,
        data: bytes,
        *,
        restricted: bool = True,
        size: int | None = None,
        deleted: bool = False,
        updated: str = "2026-09-03T10:00:00.000Z",
    ) -> None:
        card = self.cards[card_id]
        item: dict[str, Any] = {
            "id": file_id if restricted else int(file_id),
            "name": name,
            "size": str(len(data) if size is None else size),
            "type": 11 if restricted else 1,
            "deleted": deleted,
            "updated": updated,
            "card_id": card_id,
        }
        if not restricted:
            item["url"] = f"https://{STORAGE_HOST}/public/{file_id}/{name}"
        card["files"].append(item)
        self.blobs[file_id] = data

    # --- обработка ----------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "")
        if host == STORAGE_HOST:
            return self._storage(request)
        if host != self.host:
            return httpx.Response(404, text="unknown host")
        path = request.url.path
        if not path.startswith("/api/latest/"):
            return httpx.Response(404, text="not found")
        route = path.removeprefix("/api/latest/")
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        self.calls.append((route, query))
        if self.server_errors > 0:
            self.server_errors -= 1
            return httpx.Response(503, text="unavailable")
        if self.rate_limit_hits > 0:
            self.rate_limit_hits -= 1
            headers = {"x-ratelimit-remaining": "0"}
            if self.reset_header is not None:
                headers["x-ratelimit-reset"] = self.reset_header
            return httpx.Response(429, headers=headers, text="Too Many Requests")
        auth = request.headers.get("authorization", "")
        user = self.tokens.get(auth.removeprefix("Bearer ")) if auth else None
        if user is None:
            return httpx.Response(401, json="Invalid token")
        return self._route(route, query, user)

    def _route(self, route: str, query: dict[str, str], user: int) -> httpx.Response:
        if route == "users/current":
            return _json({"id": user, "uid": f"user-{user}", "full_name": "Сотрудник"})
        if route == "tree-entities":
            if self.tree_missing:
                return httpx.Response(404)
            return _json(self._tree(query, user))
        if route == "documents":
            offset = int(query.get("offset", 0))
            limit = min(int(query.get("limit", 100)), 100)
            listed = [
                {k: v for k, v in d.items() if k != "data"}
                for d in self.documents.values()
                if d["uid"] not in self.unlisted
            ]
            return _json(listed[offset : offset + limit])
        if route.startswith("documents/"):
            uid = route.removeprefix("documents/")
            document = self.documents.get(uid)
            if document is None:
                return httpx.Response(404)
            if not self._readable(uid, user):
                return httpx.Response(403)
            return _json(document)
        if route == "cards":
            offset = int(query.get("offset", 0))
            limit = min(int(query.get("limit", 100)), 100)
            space = query.get("space_id")
            visible = [
                self._card_listing(card)
                for card in self.cards.values()
                if self._board_readable(card["board_id"], user)
                and (space is None or str(card["board_id"]) == space)
            ]
            return _json(visible[offset : offset + limit])
        parts = route.split("/")
        if len(parts) == 2 and parts[0] == "cards":
            card = self.cards.get(int(parts[1])) if parts[1].isdigit() else None
            if card is None:
                return httpx.Response(404)
            if not self._board_readable(card["board_id"], user):
                return httpx.Response(403)
            return _json(card)
        if len(parts) == 4 and parts[0] == "cards" and parts[2] == "files":
            return self._card_file(parts[1], parts[3], query, user)
        return httpx.Response(404)

    def _readable(self, uid: str, user: int) -> bool:
        entity = self.entities.get(uid)
        if entity is None or entity["archived"]:
            return False
        readers = self.readers.get(uid)
        return readers is None or user in readers

    def _board_readable(self, board: int, user: int) -> bool:
        readers = self.boards.get(board)
        return readers is None or user in readers

    def _nearest_readable(self, uid: str, user: int) -> str | None:
        parent = self.entities[uid]["parent_entity_uid"]
        seen: set[str] = set()
        while parent is not None and parent not in seen:
            seen.add(parent)
            if self._readable(parent, user):
                return str(parent)
            parent = self.entities[parent]["parent_entity_uid"]
        return None

    def _first_readable(self, parent: str | None, user: int) -> list[str]:
        return [
            uid
            for uid in self.entities
            if self._readable(uid, user) and self._nearest_readable(uid, user) == parent
        ]

    def _tree(self, query: dict[str, str], user: int) -> list[dict[str, Any]]:
        parent = query.get("parent_entity_uid")
        if parent is not None and not self._readable(parent, user):
            return []
        levels = min(int(query.get("levels_count", 1)), 2)
        found = self._first_readable(parent, user)
        if levels == 2:
            found += [uid for p in list(found) for uid in self._first_readable(p, user)]
        order = list(self.entities)
        found.sort(key=order.index)
        offset = int(query.get("offset", 0))
        limit = min(int(query.get("limit", 500)), 500)
        return [self.entities[uid] for uid in found[offset : offset + limit]]

    def _card_listing(self, card: dict[str, Any]) -> dict[str, Any]:
        if self.cards_list_files:
            return card
        return {k: v for k, v in card.items() if k != "files"}

    def _card_file(
        self, card_uid: str, file_id: str, query: dict[str, str], user: int
    ) -> httpx.Response:
        card = next((c for c in self.cards.values() if c["uid"] == card_uid), None)
        if card is None:
            return httpx.Response(404)
        if not self._board_readable(card["board_id"], user):
            return httpx.Response(403)
        item = next((f for f in card["files"] if str(f["id"]) == file_id), None)
        if item is None or item["type"] != 11:
            return httpx.Response(404)
        if file_id in self.malicious:
            return httpx.Response(422, json={"message": "malicious file"})
        return _json(
            {
                **item,
                "card_uid": card_uid,
                "entity_type": "card",
                "url": f"https://{STORAGE_HOST}/signed/{file_id}?signature=sig",
            }
        )

    def _storage(self, request: httpx.Request) -> httpx.Response:
        self.storage_auth.append(request.headers.get("authorization", ""))
        parts = request.url.path.split("/")
        if len(parts) < 3:
            return httpx.Response(404)
        kind, file_id = parts[1], parts[2]
        if kind == "signed" and "signature=sig" not in request.url.query.decode():
            return httpx.Response(403)
        data = self.blobs.get(file_id)
        if data is None:
            return httpx.Response(404)
        return httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, content=data
        )


def _json(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


def sample_kaiten() -> FakeKaiten:
    """Дерево: открытая папка, закрытая ветка Анны глубже двух уровней,
    нечитаемая Борису папка с открытым документом внутри, архивный
    документ. Карточки: файлы с ограниченным доступом и старые, лишний
    формат, слишком большой, удалённый; доска, закрытая Борису."""
    server = FakeKaiten()
    server.add_entity("g-hr", "Кадры", "document_group")
    server.add_document(
        "d-vacation", "Отпуск", pm("Отпуск — 28 календарных дней."), parent="g-hr"
    )
    server.add_entity(
        "g-closed", "Закрытое", "document_group", parent="g-hr", readers={ANNA_ID}
    )
    server.add_document(
        "d-salary", "Зарплаты", pm("Ведомость."), parent="g-closed", readers={ANNA_ID}
    )
    server.add_entity(
        "g-deep", "Глубже", "document_group", parent="g-closed", readers={ANNA_ID}
    )
    server.add_document(
        "d-bonus",
        "Премии",
        pm("Премии по итогам года."),
        parent="g-deep",
        readers={ANNA_ID},
    )
    server.add_entity(
        "g-hidden", "Только Анна", "document_group", parent="g-hr", readers={ANNA_ID}
    )
    server.add_document(
        "d-inside", "Открытый внутри", pm("Виден всем."), parent="g-hidden"
    )
    server.add_document("d-old", "Архив", pm("Старое."), archived=True)
    server.add_document("d-root", "Памятка", pm("Корневой документ."))

    server.add_card(501, "Онбординг")
    server.add_file(501, "f-guide", "Гайд.txt", "Первый день: пропуск.".encode())
    server.add_file(
        501,
        "7001",
        "Заметки.md",
        "# Заметки\n\nСтарый файл.".encode(),
        restricted=False,
    )
    server.add_file(501, "f-pic", "Схема.png", b"\x89PNG")
    server.add_file(501, "f-big", "Большой.pdf", b"%PDF-", size=100 * 1024 * 1024)
    server.add_file(501, "f-gone", "Удалён.txt", b"x", deleted=True)
    server.add_card(502, "Бюджет", board=2)
    server.boards[2] = {ANNA_ID}
    server.add_file(502, "f-budget", "Бюджет.txt", "Смета на год.".encode())
    server.add_card(503, "Старая карточка", archived=True)
    server.add_file(503, "f-archived", "Архив.txt", b"old")
    return server
