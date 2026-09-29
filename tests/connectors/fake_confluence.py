"""Поддельный Confluence Server для контрактных тестов адаптера.

Формы ответов — по устоявшемуся REST API Server/DC, без сверки с
документацией (недоступна из среды): пространства, страницы с версией и
предками, тело в storage-формате, ограничения чтения по операциям,
участники групп, вложения и их скачивание, текущий пользователь.
Ломается по команде: 429 с Retry-After, 5xx, страница входа вместо JSON.
"""

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx

from corp_ed.core.outbound import OutboundClient
from tests.fake_connector import public_resolver

HOST = "wiki.example.com"
BASE = f"https://{HOST}/"
PAT = "pat-secret"  # noqa: S105 — поддельный сервер
SERVICE_USER = "svc-bot"
SERVICE_PASSWORD = "svc-pass"  # noqa: S105


@dataclass
class FakeConfluence:
    host: str = HOST
    page_limit_cap: int = 50
    spaces: list[dict[str, Any]] = field(default_factory=list)
    pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    bodies: dict[str, str] = field(default_factory=dict)
    restrictions: dict[str, tuple[set[str], set[str]]] = field(default_factory=dict)
    """page id → (usernames, group names) с правом чтения."""
    groups: dict[str, list[str]] = field(default_factory=dict)
    attachments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    files: dict[str, bytes] = field(default_factory=dict)
    hidden_pages: set[str] = field(default_factory=set)
    """Страницы, которые служебной учётке не видны (403 по id, нет в списке)."""
    unreadable_groups: set[str] = field(default_factory=set)
    rate_limit_hits: int = 0
    retry_after: str | None = "1"
    server_errors: int = 0
    login_page: bool = False
    download_content_type: str = "application/octet-stream"
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    auth_log: list[str] = field(default_factory=list)

    @property
    def base(self) -> str:
        return f"https://{self.host}/"

    def client(self) -> OutboundClient:
        return OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
            resolver=public_resolver,
        )

    # --- наполнение ---------------------------------------------------------

    def add_space(self, key: str, name: str, *, space_type: str = "global") -> str:
        self.spaces.append(
            {
                "key": key,
                "name": name,
                "type": space_type,
                "status": "current",
                "_links": {"webui": f"/display/{key}"},
            }
        )
        return key

    def add_page(
        self,
        page_id: str,
        space: str,
        title: str,
        storage: str,
        *,
        parent: str | None = None,
        version: int = 1,
        when: str = "2026-03-01T10:00:00.000+03:00",
        readers: tuple[set[str], set[str]] | None = None,
    ) -> str:
        ancestors: list[dict[str, Any]] = []
        if parent is not None:
            ancestors = [
                *self.pages[parent]["ancestors"],
                {"id": parent, "title": self.pages[parent]["title"]},
            ]
        self.pages[page_id] = {
            "id": page_id,
            "type": "page",
            "status": "current",
            "title": title,
            "space": {"key": space},
            "version": {"number": version, "when": when, "by": {"username": "author"}},
            "ancestors": ancestors,
            "_links": {"webui": f"/display/{space}/{title.replace(' ', '+')}"},
        }
        self.bodies[page_id] = storage
        if readers is not None:
            self.restrictions[page_id] = readers
        return page_id

    def add_attachment(
        self,
        attachment_id: str,
        page_id: str,
        title: str,
        data: bytes,
        *,
        version: int = 1,
        size: int | None = None,
        media_type: str = "application/octet-stream",
    ) -> str:
        self.attachments.setdefault(page_id, []).append(
            {
                "id": attachment_id,
                "type": "attachment",
                "status": "current",
                "title": title,
                "extensions": {
                    "mediaType": media_type,
                    "fileSize": len(data) if size is None else size,
                },
                "version": {"number": version, "when": "2026-03-02T12:00:00.000+03:00"},
                "_links": {
                    "download": (
                        f"/download/attachments/{page_id}/{title}"
                        f"?version={version}&api=v2"
                    ),
                    "webui": f"/pages/viewpageattachments.action?pageId={page_id}",
                },
            }
        )
        self.files[attachment_id] = data
        return attachment_id

    # --- обработка -----------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("host", "") != self.host:
            return httpx.Response(404, text="unknown host")
        path = request.url.path
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        if path.startswith("/download/attachments/"):
            return self._download(request, path)
        if not path.startswith("/rest/api/"):
            return httpx.Response(404, text="not found")
        self.calls.append((path.removeprefix("/rest/api/"), query))
        if self.server_errors > 0:
            self.server_errors -= 1
            return httpx.Response(502, text="bad gateway")
        if self.rate_limit_hits > 0:
            self.rate_limit_hits -= 1
            headers = {"retry-after": self.retry_after} if self.retry_after else {}
            return httpx.Response(429, headers=headers, json={"message": "slow down"})
        if self.login_page:
            return httpx.Response(
                200, headers={"content-type": "text/html"}, text="<html>login</html>"
            )
        user = self._user(request)
        route = path.removeprefix("/rest/api/")
        if route == "user/current":
            if user is None:
                return _json(
                    {
                        "type": "anonymous",
                        "profilePicture": {},
                        "displayName": "Anonymous",
                    }
                )
            return _json({"type": "known", "username": user, "displayName": "Service"})
        if user is None:
            return httpx.Response(401, json={"message": "Unauthorized"})
        return self._route(route, query)

    def _user(self, request: httpx.Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if auth == f"Bearer {PAT}":
            self.auth_log.append("token")
            return SERVICE_USER
        if auth.startswith("Basic "):
            import base64

            try:
                decoded = base64.b64decode(auth.removeprefix("Basic ")).decode()
            except ValueError:
                return None
            if decoded == f"{SERVICE_USER}:{SERVICE_PASSWORD}":
                self.auth_log.append("basic")
                return SERVICE_USER
        return None

    def _route(self, route: str, query: dict[str, str]) -> httpx.Response:
        parts = route.split("/")
        if route == "space":
            wanted_type = query.get("type")
            items = [
                s for s in self.spaces if not wanted_type or s["type"] == wanted_type
            ]
            return self._page(items, query)
        if parts[0] == "space" and len(parts) == 2:
            space = next((s for s in self.spaces if s["key"] == parts[1]), None)
            return (
                _json(space)
                if space
                else httpx.Response(404, json={"message": "No space"})
            )
        if route == "content":
            key = query.get("spaceKey")
            items = [
                p
                for p in self.pages.values()
                if p["space"]["key"] == key and p["id"] not in self.hidden_pages
            ]
            expand = query.get("expand", "")
            items = [self._expand(p, expand) for p in items]
            return self._page(items, query)
        if parts[0] == "content" and len(parts) >= 2:
            content_id = parts[1]
            page = self.pages.get(content_id)
            attachment = self._attachment(content_id)
            if len(parts) == 2:
                if page is not None:
                    if content_id in self.hidden_pages:
                        return httpx.Response(403, json={"message": "Forbidden"})
                    return _json(
                        self._expand(page, query.get("expand", ""), content_id)
                    )
                if attachment is not None:
                    return _json(attachment)
                return httpx.Response(404, json={"message": "No content"})
            if page is None or content_id in self.hidden_pages:
                return httpx.Response(
                    404 if page is None else 403, json={"message": "x"}
                )
            if parts[2:] == ["child", "attachment"]:
                return self._page(list(self.attachments.get(content_id, [])), query)
            if parts[2:] == ["restriction", "byOperation"]:
                users, groups = self.restrictions.get(content_id, (set(), set()))
                return _json(
                    {
                        "read": {
                            "operation": "read",
                            "restrictions": {
                                "user": {
                                    "results": [
                                        {"type": "known", "username": u}
                                        for u in sorted(users)
                                    ],
                                    "size": len(users),
                                },
                                "group": {
                                    "results": [
                                        {"type": "group", "name": g}
                                        for g in sorted(groups)
                                    ],
                                    "size": len(groups),
                                },
                            },
                        },
                        "update": {
                            "operation": "update",
                            "restrictions": {
                                "user": {"results": []},
                                "group": {"results": []},
                            },
                        },
                    }
                )
        if parts[0] == "group" and len(parts) == 3 and parts[2] == "member":
            name = parts[1]
            if name in self.unreadable_groups:
                return httpx.Response(403, json={"message": "Forbidden"})
            if name not in self.groups:
                return httpx.Response(404, json={"message": "No group"})
            return self._page(
                [
                    {"type": "known", "username": u, "displayName": u}
                    for u in self.groups[name]
                ],
                query,
            )
        return httpx.Response(404, json={"message": "No resource"})

    def _expand(
        self, page: dict[str, Any], expand: str, content_id: str | None = None
    ) -> dict[str, Any]:
        item = {k: v for k, v in page.items() if k not in {"ancestors"}}
        wanted = {e.strip() for e in expand.split(",") if e.strip()}
        if "ancestors" in wanted:
            item["ancestors"] = page["ancestors"]
        if "version" not in wanted:
            item.pop("version", None)
        if "body.storage" in wanted and content_id is not None:
            item["body"] = {
                "storage": {
                    "value": self.bodies.get(content_id, ""),
                    "representation": "storage",
                }
            }
        return item

    def _attachment(self, attachment_id: str) -> dict[str, Any] | None:
        for items in self.attachments.values():
            for item in items:
                if item["id"] == attachment_id:
                    return item
        return None

    def _page(
        self, items: list[dict[str, Any]], query: dict[str, str]
    ) -> httpx.Response:
        start = int(query.get("start") or 0)
        limit = min(int(query.get("limit") or 25), self.page_limit_cap)
        chunk = items[start : start + limit]
        payload: dict[str, Any] = {
            "results": chunk,
            "start": start,
            "limit": limit,
            "size": len(chunk),
            "_links": {"base": self.base.rstrip("/"), "context": ""},
        }
        if start + limit < len(items):
            payload["_links"]["next"] = (
                f"/rest/api/x?start={start + limit}&limit={limit}"
            )
        return _json(payload)

    def _download(self, request: httpx.Request, path: str) -> httpx.Response:
        if self._user(request) is None:
            return httpx.Response(401, text="unauthorized")
        _, _, _, page_id, filename = path.split("/", 4)
        for item in self.attachments.get(page_id, []):
            if item["title"] == filename:
                return httpx.Response(
                    200,
                    content=self.files[item["id"]],
                    headers={"content-type": self.download_content_type},
                )
        return httpx.Response(404, text="no attachment")


def _json(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


def sample_confluence() -> FakeConfluence:
    """Два пространства: открытое с ограниченной веткой и закрытое по группе."""
    server = FakeConfluence()
    server.groups["hr-team"] = ["anna", "boris"]
    server.groups["board"] = ["ceo"]
    server.add_space("HR", "Кадры")
    server.add_page(
        "100",
        "HR",
        "Отпуск",
        "<p>Отпуск — 28 календарных дней.</p>"
        '<ac:structured-macro ac:name="info"><ac:rich-text-body>'
        "<p>Заявление за две недели.</p></ac:rich-text-body></ac:structured-macro>"
        '<ac:structured-macro ac:name="toc"/>',
    )
    server.add_page(
        "101",
        "HR",
        "Зарплаты",
        "<p>Ведомость.</p>",
        parent="100",
        readers=({"anna"}, {"hr-team"}),
        version=3,
    )
    server.add_page(
        "102",
        "HR",
        "Премии",
        "<p>Премии по итогам года.</p>",
        parent="101",
        readers=({"boris", "ceo"}, set()),
    )
    server.add_page("103", "HR", "Пустая", "   ")
    server.add_attachment("500", "100", "Правила.docx", b"PK\x03\x04docx")
    server.add_attachment("501", "100", "Схема.png", b"\x89PNG")
    server.add_attachment("502", "100", "Большой.pdf", b"%PDF-", size=100 * 1024 * 1024)
    server.add_attachment("503", "101", "Ведомость.txt", "секретная ведомость".encode())
    server.add_space("BOARD", "Совет")
    server.add_page(
        "200",
        "BOARD",
        "Стратегия",
        "<p>Стратегия на год.</p>",
        readers=(set(), {"board"}),
    )
    server.add_space("ARCHIVE", "Архив", space_type="personal")
    server.add_page("300", "ARCHIVE", "Личное", "<p>Личные заметки.</p>")
    return server
