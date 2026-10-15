"""Поддельный BookStack для контрактных тестов адаптера.

Формы — по встроенной документации API (`/api/docs`, примеры ответов —
dev/api/responses в исходниках BookStack) и коду v26.09, сверены со
стендом (tests/live/bookstack): все вызовы — `GET /api/<путь>` с
заголовком `Authorization: Token <id>:<secret>`; списки — `count`
(по умолчанию 100, не больше 500), `offset`, `sort` и `{data, total}`;
ошибки — `{error: {code, message}}`.

- `/api/users` — все пользователи, включая гостя (роль Public);
- `/api/roles` — список, `/api/roles/{id}` — с `permissions` (имена
  системных прав) и `users` (id, name, slug);
- `/api/content-permissions/{type}/{id}` — `owner`, `role_permissions`
  (role_id, view, create, update, delete, role), `fallback_permissions`
  (`inheriting` и права «Все остальные» или null);
- `/api/pages/{id}` — `html` (с подставленными включениями),
  `raw_html`, `markdown`;
- `/api/attachments/{id}` — файл base64 в `content` (у ссылки — адрес).

Токен без «Access System API» или истёкший — 403 на любой вызов; без
`users-manage` / `user-roles-manage` — 403 на пользователей и роли; без
`restrictions-manage-all` — 403 на права содержимого. Лимит — 429 с
Retry-After в секундах (до сброса минутного окна).
"""

import base64
from dataclasses import dataclass, field
from typing import Any

import httpx

from corp_ed.core.outbound import OutboundClient
from tests.fake_connector import public_resolver

HOST = "wiki.example.com"
BASE = f"https://{HOST}/"
ADMIN_TOKEN = ("admin-token-id", "admin-token-secret")  # noqa: S105 — поддельный
EDITOR_TOKEN = ("editor-token-id", "editor-token-secret")  # noqa: S105
"""Токен с «Access System API», но без управления пользователями."""
NO_API_TOKEN = ("noapi-token-id", "noapi-token-secret")  # noqa: S105
STAMP = "2026-09-01T10:00:00.000000Z"


@dataclass
class FakeBookStack:
    host: str = HOST
    users: dict[int, dict[str, Any]] = field(default_factory=dict)
    roles: dict[int, dict[str, Any]] = field(default_factory=dict)
    role_users: dict[int, list[int]] = field(default_factory=dict)
    books: dict[int, dict[str, Any]] = field(default_factory=dict)
    chapters: dict[int, dict[str, Any]] = field(default_factory=dict)
    pages: dict[int, dict[str, Any]] = field(default_factory=dict)
    html: dict[int, str] = field(default_factory=dict)
    attachments: dict[int, dict[str, Any]] = field(default_factory=dict)
    files: dict[int, bytes] = field(default_factory=dict)
    permissions: dict[str, dict[str, Any]] = field(default_factory=dict)
    """"page:5" → ответ content-permissions (по умолчанию — наследование)."""
    tokens: dict[tuple[str, str], str] = field(
        default_factory=lambda: {
            ADMIN_TOKEN: "admin",
            EDITOR_TOKEN: "editor",
            NO_API_TOKEN: "noapi",
        }
    )
    hidden_from_token: set[str] = field(default_factory=set)
    """"book:1" — элемент не виден владельцу токена (404 на права)."""
    locked: set[str] = field(default_factory=set)
    """"page:5" — у токена нет права управлять правами элемента (403)."""
    no_roles_api: bool = False
    """Старый BookStack (до v23.x): /api/roles — 404."""
    rate_limited: dict[str, int] = field(default_factory=dict)
    """путь → сколько раз ответить 429."""
    retry_after: str = "7"
    server_errors: int = 0
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    _next_id: int = 1

    @property
    def base(self) -> str:
        return f"https://{self.host}/"

    def client(self) -> OutboundClient:
        return OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
            resolver=public_resolver,
        )

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]

    # --- наполнение ---------------------------------------------------------------

    def _id(self) -> int:
        # BookStack v25+ ведёт id книг, глав и страниц одной
        # последовательностью (стенд 15.10): у нас так же.
        self._next_id += 1
        return self._next_id

    def add_user(self, user_id: int, email: str, *roles: int) -> int:
        self.users[user_id] = {
            "id": user_id,
            "name": email.split("@")[0].capitalize(),
            "email": email,
            "slug": email.split("@")[0],
            "created_at": STAMP,
            "updated_at": STAMP,
            "external_auth_id": "",
            "last_activity_at": None,
        }
        for role in roles:
            self.role_users.setdefault(role, []).append(user_id)
        return user_id

    def add_role(
        self, role_id: int, name: str, *permissions: str, system_name: str = ""
    ) -> int:
        self.roles[role_id] = {
            "id": role_id,
            "display_name": name,
            "description": "",
            "system_name": system_name,
            "external_auth_id": "",
            "mfa_enforced": False,
            "created_at": STAMP,
            "updated_at": STAMP,
            "permissions": sorted(permissions),
        }
        self.role_users.setdefault(role_id, [])
        return role_id

    def add_book(self, name: str) -> int:
        book_id = self._id()
        self.books[book_id] = {
            "id": book_id,
            "name": name,
            "slug": name.lower(),
            "description": "",
            "created_at": STAMP,
            "updated_at": STAMP,
            "created_by": 1,
            "updated_by": 1,
            "owned_by": 1,
        }
        return book_id

    def add_chapter(self, book_id: int, name: str) -> int:
        chapter_id = self._id()
        self.chapters[chapter_id] = {
            "id": chapter_id,
            "book_id": book_id,
            "name": name,
            "slug": name.lower(),
            "description": "",
            "priority": 1,
            "created_at": STAMP,
            "updated_at": STAMP,
            "created_by": 1,
            "updated_by": 1,
            "owned_by": 1,
            "book_slug": self.books[book_id]["slug"],
        }
        return chapter_id

    def add_page(
        self,
        book_id: int,
        name: str,
        html: str,
        *,
        chapter_id: int | None = None,
        owner: int = 1,
        draft: bool = False,
        template: bool = False,
        revisions: int = 1,
        updated: str = STAMP,
    ) -> int:
        page_id = self._id()
        self.pages[page_id] = {
            "id": page_id,
            "book_id": book_id,
            "chapter_id": chapter_id,
            "name": name,
            "slug": name.lower(),
            "priority": len(self.pages),
            "draft": draft,
            "template": template,
            "created_at": STAMP,
            "updated_at": updated,
            "created_by": 1,
            "updated_by": 1,
            "owned_by": owner,
            "revision_count": revisions,
            "editor": "wysiwyg",
            "book_slug": self.books[book_id]["slug"],
        }
        self.html[page_id] = html
        return page_id

    def add_attachment(
        self,
        page_id: int,
        name: str,
        data: bytes | None,
        *,
        extension: str | None = None,
        link: str = "",
    ) -> int:
        attachment_id = len(self.attachments) + 1
        self.attachments[attachment_id] = {
            "id": attachment_id,
            "name": name,
            "extension": extension
            if extension is not None
            else name.rpartition(".")[2]
            if "." in name
            else "",
            "uploaded_to": page_id,
            "external": data is None,
            "order": attachment_id,
            "created_at": STAMP,
            "updated_at": STAMP,
            "created_by": 1,
            "updated_by": 1,
        }
        if data is not None:
            self.files[attachment_id] = data
        else:
            self.files[attachment_id] = link.encode()
        return attachment_id

    def grant(
        self,
        kind: str,
        entity_id: int,
        roles: dict[int, bool] | None = None,
        fallback: bool | None = None,
    ) -> None:
        """Права содержимого элемента; fallback None — наследование."""
        self.permissions[f"{kind}:{entity_id}"] = {
            "owner": {"id": 1, "name": "Admin", "slug": "admin"},
            "role_permissions": [
                {
                    "role_id": role_id,
                    "view": view,
                    "create": False,
                    "update": False,
                    "delete": False,
                    "role": {
                        "id": role_id,
                        "display_name": self.roles[role_id]["display_name"],
                    },
                }
                for role_id, view in (roles or {}).items()
            ],
            "fallback_permissions": {
                "inheriting": fallback is None,
                "view": fallback,
                "create": None if fallback is None else False,
                "update": None if fallback is None else False,
                "delete": None if fallback is None else False,
            },
        }

    # --- обработка ------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("host", "") != self.host:
            return httpx.Response(404, text="unknown host")
        path = request.url.path
        if request.method != "GET" or not path.startswith("/api/"):
            return httpx.Response(404, text="not found")
        route = path.removeprefix("/api/")
        params = dict(request.url.params)
        self.calls.append((route, params))
        if self.server_errors > 0:
            self.server_errors -= 1
            return httpx.Response(502, text="bad gateway")
        if self.rate_limited.get(route, 0) > 0:
            self.rate_limited[route] -= 1
            return httpx.Response(
                429,
                headers={"retry-after": self.retry_after},
                json=_error(429, "Too Many Attempts.").json(),
            )
        actor = self._actor(request.headers.get("authorization", ""))
        if actor is None:
            return _error(401, "The API token could not be found")
        if actor == "noapi":
            return _error(
                403, "The owner of the used API token does not have permission"
            )
        return self._route(route, params, actor)

    def _actor(self, header: str) -> str | None:
        if not header.startswith("Token "):
            return None
        token_id, _, secret = header.removeprefix("Token ").partition(":")
        return self.tokens.get((token_id, secret))

    def _route(self, route: str, params: dict[str, str], actor: str) -> httpx.Response:
        admin = actor == "admin"
        parts = route.split("/")
        if route == "books":
            return _listing(params, list(self.books.values()))
        if route == "chapters":
            return _listing(params, list(self.chapters.values()))
        if route == "pages":
            # Черновики видит только их автор; в подделке автор — admin.
            listed = [
                p for p in self.pages.values() if not p["draft"] or p["owned_by"] == 1
            ]
            return _listing(params, listed)
        if route == "attachments":
            listed = [
                a for a in self.attachments.values() if a["uploaded_to"] in self.pages
            ]
            return _listing(params, listed)
        if parts[0] in {"users", "roles"}:
            if not admin:
                return _error(
                    403, "You do not have permission to access the requested page."
                )
            if parts[0] == "roles" and self.no_roles_api:
                return _error(404, "Not found")
            if route == "users":
                return _listing(params, list(self.users.values()))
            if route == "roles":
                listed = [
                    {
                        **{k: v for k, v in r.items() if k != "permissions"},
                        "users_count": len(self.role_users[r["id"]]),
                        "permissions_count": len(r["permissions"]),
                    }
                    for r in self.roles.values()
                ]
                return _listing(params, listed)
            if parts[0] == "roles" and len(parts) == 2:
                role = self.roles.get(_int(parts[1]))
                if role is None:
                    return _error(404, "Not found")
                return httpx.Response(
                    200,
                    json={
                        **role,
                        "users": [
                            {
                                "id": u,
                                "name": self.users[u]["name"],
                                "slug": self.users[u]["slug"],
                            }
                            for u in self.role_users[role["id"]]
                            if u in self.users
                        ],
                    },
                )
        if parts[0] == "content-permissions" and len(parts) == 3:
            key = f"{parts[1]}:{parts[2]}"
            store = {
                "book": self.books,
                "chapter": self.chapters,
                "page": self.pages,
            }.get(parts[1])
            if store is None or _int(parts[2]) not in store:
                return _error(404, "The requested resource could not be found.")
            if key in self.hidden_from_token:
                return _error(404, "The requested resource could not be found.")
            if not admin or key in self.locked:
                return _error(
                    403, "You do not have permission to access the requested page."
                )
            if key not in self.permissions:
                return httpx.Response(
                    200,
                    json={
                        "owner": {"id": 1, "name": "Admin", "slug": "admin"},
                        "role_permissions": [],
                        "fallback_permissions": {
                            "inheriting": True,
                            "view": None,
                            "create": None,
                            "update": None,
                            "delete": None,
                        },
                    },
                )
            return httpx.Response(200, json=self.permissions[key])
        if parts[0] == "pages" and len(parts) == 2:
            page = self.pages.get(_int(parts[1]))
            if page is None:
                return _error(404, "Page not found")
            html = self.html[page["id"]]
            return httpx.Response(
                200,
                json={
                    **page,
                    "html": html,
                    "raw_html": html,
                    "markdown": "",
                    "created_by": {"id": 1, "name": "Admin", "slug": "admin"},
                    "updated_by": {"id": 1, "name": "Admin", "slug": "admin"},
                    "owned_by": {"id": page["owned_by"], "name": "x", "slug": "x"},
                    "tags": [],
                },
            )
        if parts[0] == "attachments" and len(parts) == 2:
            attachment = self.attachments.get(_int(parts[1]))
            if attachment is None:
                return _error(404, "Not found")
            data = self.files[attachment["id"]]
            content = (
                data.decode()
                if attachment["external"]
                else base64.b64encode(data).decode("ascii")
            )
            return httpx.Response(
                200,
                json={
                    **attachment,
                    "links": {"html": "<a>x</a>", "markdown": "[x](x)"},
                    "content": content,
                },
            )
        return _error(404, "Route not found")


def _int(value: str) -> int:
    return int(value) if value.isdigit() else -1


def _listing(params: dict[str, str], items: list[dict[str, Any]]) -> httpx.Response:
    count = max(min(int(params.get("count") or 100), 500), 1)
    offset = max(int(params.get("offset") or 0), 0)
    key = params.get("sort", "").lstrip("+-") or "name"
    ordered = sorted(items, key=lambda item: str(item.get(key, "")).zfill(12))
    if params.get("sort", "").startswith("-"):
        ordered.reverse()
    return httpx.Response(
        200, json={"data": ordered[offset : offset + count], "total": len(items)}
    )


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": message}})


ADMIN_ROLE = 1
EDITOR_ROLE = 2
PUBLIC_ROLE = 3
STAFF_ROLE = 10
HR_ROLE = 11
AUTHOR_ROLE = 12


def sample_bookstack() -> FakeBookStack:
    """Компания как на стенде: роль «Сотрудники» у всех, «Кадры», «Авторы»
    (только свои страницы), Editor с правом просмотра всего, гость."""
    server = FakeBookStack()
    server.add_role(
        ADMIN_ROLE, "Admin", "access-api", "page-view-all", system_name="admin"
    )
    server.add_role(
        EDITOR_ROLE,
        "Editor",
        "access-api",
        "book-view-all",
        "chapter-view-all",
        "page-view-all",
    )
    server.add_role(PUBLIC_ROLE, "Public", "page-view-all", system_name="public")
    server.add_role(STAFF_ROLE, "Сотрудники", "access-api")
    server.add_role(HR_ROLE, "Кадры", "access-api")
    server.add_role(AUTHOR_ROLE, "Авторы", "access-api", "page-view-own")
    server.add_user(1, "admin@example.com", ADMIN_ROLE, STAFF_ROLE)
    server.add_user(2, "guest@example.com", PUBLIC_ROLE)
    server.add_user(3, "Ivan@Example.com", STAFF_ROLE, EDITOR_ROLE)
    server.add_user(4, "maria@example.com", STAFF_ROLE, HR_ROLE)
    server.add_user(5, "petr@example.com", STAFF_ROLE)
    server.add_user(6, "olga@example.com", STAFF_ROLE, AUTHOR_ROLE)

    common = server.add_book("Общая")
    server.grant("book", common, {STAFF_ROLE: True})
    server.add_page(common, "Отпуск", "<p>Отпуск — 28 дней.</p>")
    trips = server.add_chapter(common, "Командировки")
    server.add_page(common, "Суточные", "<p>700 рублей.</p>", chapter_id=trips)
    bonus = server.add_page(common, "Премии", "<p>Раз в квартал.</p>")
    server.grant("page", bonus, {HR_ROLE: True}, fallback=False)

    hr = server.add_book("Кадры")
    server.grant("book", hr, {HR_ROLE: True}, fallback=False)
    server.add_page(hr, "Зарплаты", "<p>Оклады — в марте.</p>")
    chart = server.add_page(hr, "Оргструктура", "<p>Четыре отдела.</p>")
    server.grant("page", chart, {STAFF_ROLE: True})
    leave = server.add_page(hr, "Увольнения", "<p>Обходной лист.</p>")
    server.grant("page", leave, {STAFF_ROLE: False})

    projects = server.add_book("Проекты")
    server.add_page(projects, "План", "<p>Запуск — в мае.</p>")
    server.add_page(projects, "Заметки", "<p>Мысли.</p>", owner=6)
    estimate = server.add_page(projects, "Смета", "<p>Согласована.</p>")
    server.grant("page", estimate, {STAFF_ROLE: False})
    server.add_page(projects, "Черновик", "<p>Не опубликован.</p>", draft=True)
    server.add_page(projects, "Шаблон", "<p>Шаблон.</p>", template=True)
    return server
