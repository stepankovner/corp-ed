"""Поддельный Outline (и Yonote) для контрактных тестов адаптера.

Формы — по OpenAPI Outline (github.com/outline/openapi, spec3.yml) и
обработчикам сервера (09.10): все методы — `POST /api/<метод>` с JSON,
Bearer API-ключ, страницы `offset`/`limit` (≤ 100) и `pagination` в
ответе, ошибки `{ok: false, error, message}`. `documents.list` без
архивных и удалённых, с черновиками владельца ключа и полем `text`
(Markdown, без x-api-version); `documents.export` с Accept JSON →
`{data: markdown}`. Коллекция открыта рабочей области, если
`permission` — read или read_write; `documents.memberships` и
`documents.group_memberships` требуют права на изменение документа.

Диалект yonote — по yonote.ru/openapi-3.json: у коллекции `private`
вместо `permission`, у пользователя `isAdmin`, методов участников
документа нет (404).

Лимиты: `documents.export` — 25 в минуту, `documents.list` — 100 в
минуту; сверх — 429 с Retry-After в секундах.
"""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from corp_ed.core.outbound import OutboundClient
from tests.fake_connector import public_resolver

HOST = "wiki.example.com"
BASE = f"https://{HOST}/"
API_KEY = "ol_api_admin"  # noqa: S105 — поддельный сервер
MEMBER_KEY = "ol_api_member"  # noqa: S105
ADMIN_ID = "u-admin"


@dataclass
class FakeOutline:
    host: str = HOST
    dialect: str = "outline"
    keys: dict[str, str] = field(
        default_factory=lambda: {API_KEY: ADMIN_ID, MEMBER_KEY: "u-anna"}
    )
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    collections: dict[str, dict[str, Any]] = field(default_factory=dict)
    collection_members: dict[str, list[str]] = field(default_factory=dict)
    collection_groups: dict[str, list[str]] = field(default_factory=dict)
    groups: dict[str, list[str]] = field(default_factory=dict)
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    texts: dict[str, str] = field(default_factory=dict)
    document_members: dict[str, list[str]] = field(default_factory=dict)
    document_groups: dict[str, list[str]] = field(default_factory=dict)
    locked_documents: set[str] = field(default_factory=set)
    """Ключу нельзя менять документ — участники документа: 403."""
    list_text: bool = True
    rate_limited: dict[str, int] = field(default_factory=dict)
    """метод → сколько раз ответить 429."""
    retry_after: str = "7"
    server_errors: int = 0
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    @property
    def base(self) -> str:
        return f"https://{self.host}/"

    def client(self) -> OutboundClient:
        return OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
            resolver=public_resolver,
        )

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    # --- наполнение ---------------------------------------------------------------

    def add_user(
        self, user_id: str, email: str, *, role: str = "member", suspended: bool = False
    ) -> str:
        self.users[user_id] = {
            "id": user_id,
            "name": user_id,
            "email": email,
            "role": role,
            "isAdmin": role == "admin",
            "isSuspended": suspended,
        }
        return user_id

    def add_collection(
        self, collection_id: str, name: str, *, public: bool, archived: bool = False
    ) -> str:
        collection: dict[str, Any] = {
            "id": collection_id,
            "name": name,
            "url": f"/collection/{collection_id}",
            "archivedAt": "2026-05-01T00:00:00.000Z" if archived else None,
        }
        if self.dialect == "yonote":
            collection["private"] = not public
        else:
            collection["permission"] = "read" if public else None
        self.collections[collection_id] = collection
        return collection_id

    def add_document(
        self,
        document_id: str,
        collection_id: str | None,
        title: str,
        text: str,
        *,
        parent: str | None = None,
        updated: str = "2026-09-01T10:00:00.000Z",
        revision: int = 1,
        draft: bool = False,
        template: bool = False,
    ) -> str:
        self.documents[document_id] = {
            "id": document_id,
            "urlId": f"u{document_id}",
            "url": f"/doc/{title.lower()}-u{document_id}",
            "title": title,
            "collectionId": collection_id,
            "parentDocumentId": parent,
            "revision": revision,
            "createdAt": "2026-01-01T10:00:00.000Z",
            "updatedAt": updated,
            "publishedAt": None if draft else "2026-01-01T10:00:00.000Z",
            "archivedAt": None,
            "deletedAt": None,
            "template": template,
        }
        self.texts[document_id] = text
        return document_id

    # --- обработка ------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("host", "") != self.host:
            return httpx.Response(404, text="unknown host")
        path = request.url.path
        if request.method != "POST" or not path.startswith("/api/"):
            return httpx.Response(404, text="not found")
        method = path.removeprefix("/api/")
        body = _body(request)
        self.calls.append((method, body))
        if self.server_errors > 0:
            self.server_errors -= 1
            return httpx.Response(502, text="bad gateway")
        if self.rate_limited.get(method, 0) > 0:
            self.rate_limited[method] -= 1
            return httpx.Response(
                429,
                headers={"retry-after": self.retry_after},
                json={"ok": False, "error": "rate_limit_exceeded", "status": 429},
            )
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        actor = self.keys.get(token)
        if actor is None:
            return _error(401, "authentication_required", "Authentication required")
        return self._route(method, body, actor, request)

    def _route(
        self, method: str, body: dict[str, Any], actor: str, request: httpx.Request
    ) -> httpx.Response:
        admin = self.users.get(actor, {}).get("role") == "admin"
        if method == "auth.info":
            user = dict(self.users[actor])
            if self.dialect == "yonote":
                user.pop("role")
            return _ok({"user": user, "team": {"id": "team", "name": "Компания"}})
        if method == "users.list":
            listed = [
                self._user(u, with_email=admin)
                for u in self.users.values()
                if not u["isSuspended"]
            ]
            return self._page(body, listed)
        if method == "collections.list":
            listed = [c for c in self.collections.values() if not c["archivedAt"]]
            return self._page(body, listed)
        if method == "collections.memberships":
            ids = self.collection_members.get(body.get("id", ""), [])
            return self._memberships(body, ids, "memberships")
        if method == "collections.group_memberships":
            groups = self.collection_groups.get(body.get("id", ""), [])
            return self._groups_page(body, groups, "collectionGroupMemberships")
        if method == "groups.memberships":
            if body.get("id") not in self.groups:
                return _error(404, "not_found", "Not found")
            ids = self.groups[body["id"]]
            return self._memberships(body, ids, "groupMemberships")
        if method == "documents.list":
            return self._page(body, self._listed_documents(actor))
        if method in {"documents.memberships", "documents.group_memberships"}:
            if self.dialect == "yonote":
                return _error(404, "not_found", "Resource not found")
            document_id = body.get("id", "")
            if document_id not in self.documents:
                return _error(404, "not_found", "Not found")
            if document_id in self.locked_documents:
                return _error(403, "authorization_error", "Authorization error")
            if method == "documents.memberships":
                ids = self.document_members.get(document_id, [])
                return self._memberships(body, ids, "memberships")
            groups = self.document_groups.get(document_id, [])
            return self._groups_page(body, groups, "groupMemberships")
        if method == "documents.export":
            document = self.documents.get(body.get("id", ""))
            if document is None or document["deletedAt"]:
                return _error(404, "not_found", "Not found")
            if "application/json" not in request.headers.get("accept", ""):
                return httpx.Response(200, text="zip?")
            text = f"# {document['title']}\n\n{self.texts[document['id']]}"
            return _ok(text)
        return _error(404, "not_found", "Resource not found")

    def _user(self, user: dict[str, Any], *, with_email: bool) -> dict[str, Any]:
        shown = dict(user)
        if not with_email:
            shown.pop("email")
        if self.dialect == "yonote":
            shown.pop("role")
        return shown

    def _listed_documents(self, actor: str) -> list[dict[str, Any]]:
        listed = []
        for document in self.documents.values():
            if document["archivedAt"] or document["deletedAt"]:
                continue
            if document["publishedAt"] is None and actor != ADMIN_ID:
                continue
            collection = document["collectionId"]
            if collection is not None and collection not in self.collections:
                continue
            shown = dict(document)
            if self.list_text:
                shown["text"] = self.texts[document["id"]]
            listed.append(shown)
        return listed

    def _page(self, body: dict[str, Any], items: list[Any]) -> httpx.Response:
        offset, limit = _bounds(body)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "status": 200,
                "data": items[offset : offset + limit],
                "pagination": {"offset": offset, "limit": limit},
            },
        )

    def _memberships(
        self, body: dict[str, Any], ids: list[str], key: str
    ) -> httpx.Response:
        offset, limit = _bounds(body)
        chosen = ids[offset : offset + limit]
        return _ok(
            {
                key: [{"id": f"m-{i}", "userId": i} for i in chosen],
                "users": [
                    self._user(self.users[i], with_email=False)
                    for i in chosen
                    if i in self.users
                ],
            },
            offset=offset,
            limit=limit,
        )

    def _groups_page(
        self, body: dict[str, Any], groups: list[str], key: str
    ) -> httpx.Response:
        offset, limit = _bounds(body)
        chosen = groups[offset : offset + limit]
        return _ok(
            {
                key: [{"id": f"gm-{g}", "groupId": g} for g in chosen],
                "groups": [{"id": g, "name": g} for g in chosen],
            },
            offset=offset,
            limit=limit,
        )


def _body(request: httpx.Request) -> dict[str, Any]:
    try:
        data = json.loads(request.content or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _bounds(body: dict[str, Any]) -> tuple[int, int]:
    offset = int(body.get("offset") or 0)
    limit = min(int(body.get("limit") or 25), 100)
    return offset, limit


def _ok(data: Any, *, offset: int = 0, limit: int = 25) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "ok": True,
            "status": 200,
            "data": data,
            "pagination": {"offset": offset, "limit": limit},
        },
    )


def _error(status: int, error: str, message: str) -> httpx.Response:
    return httpx.Response(
        status, json={"ok": False, "status": status, "error": error, "message": message}
    )


def sample_outline(dialect: str = "outline") -> FakeOutline:
    """Открытая коллекция, закрытая с участником и группой, документ с
    отдельным участником, черновик, шаблон, документ без коллекции."""
    server = FakeOutline(dialect=dialect)
    server.add_user(ADMIN_ID, "admin@example.com", role="admin")
    server.add_user("u-anna", "Anna@Example.com")
    server.add_user("u-boris", "boris@example.com")
    server.add_user("u-vera", "vera@example.com")
    server.add_user("u-gone", "gone@example.com", suspended=True)
    server.groups["g-finance"] = ["u-boris", "u-gone"]

    server.add_collection("c-hr", "Кадры", public=True)
    server.add_document("d-vacation", "c-hr", "Отпуск", "Отпуск — 28 дней.")
    server.add_document(
        "d-sick",
        "c-hr",
        "Больничный",
        "Справка в течение трёх дней.",
        parent="d-vacation",
    )
    server.add_collection("c-fin", "Финансы", public=False)
    server.collection_members["c-fin"] = [ADMIN_ID, "u-anna"]
    server.collection_groups["c-fin"] = ["g-finance"]
    server.add_document("d-budget", "c-fin", "Бюджет", "Смета на год.")
    server.add_document("d-bonus", "c-fin", "Премии", "Премии по итогам года.")
    server.document_members["d-bonus"] = ["u-vera"]
    server.add_document("d-draft", "c-hr", "Черновик", "Не опубликован.", draft=True)
    server.add_document("d-template", "c-hr", "Шаблон", "Шаблон.", template=True)
    server.add_document("d-loose", None, "Без коллекции", "Отдельно.")
    server.add_collection("c-old", "Архив", public=True, archived=True)
    return server
