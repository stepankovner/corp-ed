"""Поддельная DokuWiki (JSON-RPC) для контрактных тестов адаптера.

Формы — по документации и коду сервера (2026-07-14c «Mort», 15.10):

- https://www.dokuwiki.org/devel:jsonrpc — `POST /lib/exe/jsonrpc.php`,
  `Content-Type: application/json`, тело `{jsonrpc, id, method, params}`,
  ответ `{jsonrpc, id, result}`; ошибки — `{error: {code, message}}`;
- https://www.dokuwiki.org/devel:remote_api — коды фреймворка: -32603
  без входа (401), -32604 запрещено (403), -32605 API выключен (404);
  нет метода — 400 с -32603 (inc/Remote/Api.php);
- https://www.dokuwiki.org/devel:authentication — токен
  (`Authorization: Bearer` или `X-DokuWiki-Token`) и Basic; неверные
  данные — вызов анонимом;
- https://www.dokuwiki.org/devel:xmlrpc — тот же API по XML-RPC (адаптер
  им не пользуется);
- inc/Remote/ApiCore.php и Response/{Page,Media,User}.php — поля
  `core.listPages` (`id, revision, size, title, permission, hash,
  author`; `depth=0` в корне — только страницы поискового индекса),
  `core.listMedia` (`id, revision, size, permission, isimage, hash,
  author`), `core.whoAmI` (`login, name, mail, groups, isadmin,
  ismanager`), `core.aclCheck` (чужой логин — только суперпользователю,
  группы без параметра — из бэкенда), `core.getPageHTML` (121 — нет
  страницы), `core.getMedia` (base64, 221 — нет файла);
- lib/plugins/acl/remote.php — `plugin.acl.listAcls` (только
  суперпользователю): `{scope: {subject: "level"}}` — уровень строкой,
  как на стенде 15.10; пустая таблица — `[]`.

Права — порт auth_aclcheck_cb (inc/auth.php): суперпользователь — 255;
точные правила на id, затем пространства вверх до `*`; среди
подходящих строк одного уровня — наибольший уровень, но не выше 16.
"""

import base64
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from corp_ed.core.outbound import OutboundClient
from tests.fake_connector import public_resolver

HOST = "wiki.example.com"
BASE = f"https://{HOST}/"
ADMIN_TOKEN = "dw.admin.token"  # noqa: S105 — поддельный сервер
MEMBER_TOKEN = "dw.ivan.token"  # noqa: S105
PASSWORD = "Kronto-Test-1"  # noqa: S105
SUPERUSER_GROUP = "admin"
ENDPOINT = "/lib/exe/jsonrpc.php"


class _FailError(Exception):
    def __init__(self, status: int, code: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class FakeDokuWiki:
    host: str = HOST
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    tokens: dict[str, str] = field(
        default_factory=lambda: {ADMIN_TOKEN: "admin", MEMBER_TOKEN: "ivan"}
    )
    acl: list[tuple[str, str, int]] = field(default_factory=list)
    pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    media: dict[str, dict[str, Any]] = field(default_factory=dict)
    indexed: set[str] = field(default_factory=set)
    """Страницы в поисковом индексе: только их отдаёт listPages с depth=0."""
    remote: bool = True
    remoteuser: list[str] = field(default_factory=list)
    """Кому открыт API (логины и @группы); пусто — всем вошедшим."""
    acl_plugin: bool = True
    jsonrpc: bool = True
    """False — DokuWiki до Kaos: файла jsonrpc.php нет."""
    rate_limited: int = 0
    retry_after: str = "3"
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

    def add_user(self, login: str, email: str, *groups: str) -> None:
        self.users[login] = {
            "name": login.title(),
            "mail": email,
            "grps": ["user", *groups],
        }

    def add_page(
        self, page_id: str, html: str, *, revision: int = 1_760_000_000, title: str = ""
    ) -> None:
        self.pages[page_id] = {
            "html": html,
            "revision": revision,
            "title": title or page_id,
        }
        self.indexed.add(page_id)

    def add_media(
        self, media_id: str, data: bytes, *, revision: int = 1_760_000_000
    ) -> None:
        self.media[media_id] = {"data": data, "revision": revision}

    def allow(self, scope: str, subject: str, level: int) -> None:
        self.acl.append((scope, subject, level))

    # --- права ---------------------------------------------------------------------

    def is_admin(self, login: str, groups: list[str]) -> bool:
        return bool(login) and SUPERUSER_GROUP in groups

    def permission(self, item_id: str, login: str, groups: list[str]) -> int:
        if self.is_admin(login, groups):
            return 255
        subjects = {f"@{g}" for g in groups} | {"@ALL"}
        if login:
            subjects.add(login)

        def matched(scope: str) -> int:
            best = -1
            for rule_scope, subject, level in self.acl:
                if rule_scope == scope and subject in subjects:
                    best = max(best, min(level, 16))
            return best

        found = matched(item_id)
        if found > -1:
            return found
        namespace = item_id.rpartition(":")[0]
        path = f"{namespace}:*" if namespace else "*"
        while True:
            found = matched(path)
            if found > -1:
                return found
            if path == "*":
                return 0
            namespace = namespace.rpartition(":")[0]
            path = f"{namespace}:*" if namespace else "*"

    def _groups(self, login: str) -> list[str]:
        user = self.users.get(login)
        return list(user["grps"]) if user else []

    # --- обработка ------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("host", "") != self.host:
            return httpx.Response(404, text="unknown host")
        if request.url.path != ENDPOINT or not self.jsonrpc:
            return httpx.Response(404, text="<html>Not Found</html>")
        if request.method != "POST":
            return _error(405, -32606, "JSON-RPC server only accepts POST requests.")
        content_type = request.headers.get("content-type", "").split(";")[0]
        if content_type.strip().lower() != "application/json":
            return _error(415, -32606, "only accepts application/json requests.")
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            return _error(400, -32700, "JSON-RPC server only accepts valid JSON.")
        method = str(body.get("method", ""))
        params = body.get("params") or {}
        self.calls.append((method, dict(params)))
        if self.server_errors > 0:
            self.server_errors -= 1
            return httpx.Response(502, text="bad gateway")
        if self.rate_limited > 0:
            self.rate_limited -= 1
            return httpx.Response(
                429, headers={"retry-after": self.retry_after}, text="slow down"
            )
        if not self.remote:
            return _error(404, -32605, "JSON-RPC server not enabled.")
        login = self._login(request)
        try:
            result = self._route(method, params, login)
        except _FailError as fail:
            return _error(fail.status, fail.code, fail.message)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": result}
        )

    def _login(self, request: httpx.Request) -> str:
        token = request.headers.get("x-dokuwiki-token", "")
        kind, _, value = request.headers.get("authorization", "").partition(" ")
        if kind == "Bearer" and not token:
            token = value
        if token:
            return self.tokens.get(token, "")
        if kind == "Basic":
            raw = base64.b64decode(value).decode()
            # Как auth_setup: explode(':') без предела — пароль с «:»
            # обрезается.
            parts = raw.split(":")
            login, password = parts[0], parts[1] if len(parts) > 1 else ""
            if login in self.users and password == PASSWORD:
                return login
        return ""

    def _route(self, method: str, params: dict[str, Any], login: str) -> Any:
        if method == "core.getAPIVersion":
            return 14
        if not login:
            raise _FailError(
                401, -32603, f"server error. not authorized to call {method}"
            )
        groups = self._groups(login)
        if self.remoteuser and not (
            login in self.remoteuser or any(f"@{g}" in self.remoteuser for g in groups)
        ):
            raise _FailError(403, -32604, f"server error. forbidden to call {method}")
        admin = self.is_admin(login, groups)
        if method == "core.whoAmI":
            user = self.users[login]
            return {
                "login": login,
                "name": user["name"],
                "mail": user["mail"],
                "groups": groups,
                "isadmin": admin,
                "ismanager": admin,
            }
        if method == "core.aclCheck":
            other = str(params.get("user") or "")
            if not other:
                return self.permission(str(params["page"]), login, groups)
            if other != login and not admin:
                raise _FailError(
                    403, -32604, "forbidden to call the method core.aclCheck"
                )
            other_groups = list(params.get("groups") or []) or self._groups(other)
            return self.permission(str(params["page"]), other, other_groups)
        if method == "core.listPages":
            return self._list_pages(params, login, groups)
        if method == "core.getPageHTML":
            page_id = str(params.get("page", ""))
            if page_id not in self.pages:
                raise _FailError(
                    400, 121, "The requested page (revision) does not exist"
                )
            if self.permission(page_id, login, groups) < 1:
                raise _FailError(403, -32604, "forbidden to call core.getPageHTML")
            return self.pages[page_id]["html"]
        if method == "core.listMedia":
            return [
                {
                    "id": media_id,
                    "revision": item["revision"],
                    "size": len(item["data"]),
                    "permission": self.permission(media_id, login, groups),
                    "isimage": media_id.endswith((".png", ".jpg", ".gif")),
                    "hash": "",
                    "author": "",
                }
                for media_id, item in sorted(self.media.items())
                if self.permission(media_id, login, groups) >= 1
            ]
        if method == "core.getMedia":
            media_id = str(params.get("media", ""))
            if self.permission(media_id, login, groups) < 1:
                raise _FailError(403, -32604, "forbidden to call core.getMedia")
            if media_id not in self.media:
                raise _FailError(400, 221, "The requested media file does not exist")
            return base64.b64encode(self.media[media_id]["data"]).decode("ascii")
        if method == "plugin.acl.listAcls" and self.acl_plugin:
            if not admin:
                raise _FailError(403, -32604, "forbidden to call plugin.acl.listAcls")
            table: dict[str, dict[str, str]] = {}
            for scope, subject, level in self.acl:
                table.setdefault(scope, {})[subject] = str(level)
            return table or []
        raise _FailError(400, -32603, "Method does not exist")

    def _list_pages(
        self, params: dict[str, Any], login: str, groups: list[str]
    ) -> list[dict[str, Any]]:
        namespace = str(params.get("namespace") or "")
        depth = int(params.get("depth", 1))
        listed = []
        for page_id, page in sorted(self.pages.items()):
            if namespace == "" and depth == 0 and page_id not in self.indexed:
                continue
            if namespace and not page_id.startswith(f"{namespace}:"):
                continue
            levels = page_id.count(":") - (namespace.count(":") + 1 if namespace else 0)
            if depth and levels >= depth:
                continue
            permission = self.permission(page_id, login, groups)
            if permission < 1:
                continue
            listed.append(
                {
                    "id": page_id,
                    "revision": page["revision"],
                    "size": len(page["html"]),
                    "title": page["title"],
                    "permission": permission,
                    "hash": "",
                    "author": "",
                }
            )
        return listed


def _error(status: int, code: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


def sample_dokuwiki() -> FakeDokuWiki:
    """Компания: admin (суперпользователь), ivan (sales), maria и petr
    (hr), olga без групп. Открытое `public`, закрытые для групп `hr` и
    `sales`, страница `hr:salaries` с запретом для petr, медиафайлы в
    `hr` и `public`, страница вне индекса."""
    server = FakeDokuWiki()
    server.add_user("admin", "admin@example.com", SUPERUSER_GROUP)
    server.add_user("ivan", "Ivan@Example.com", "sales")
    server.add_user("maria", "maria@example.com", "hr")
    server.add_user("petr", "petr@example.com", "hr")
    server.add_user("olga", "olga@example.com")
    server.allow("*", "@ALL", 0)
    server.allow("*", "@user", 1)
    server.allow("public:*", "@ALL", 1)
    server.allow("hr:*", "@user", 0)
    server.allow("hr:*", "@hr", 8)
    server.allow("hr:salaries", "petr", 0)
    server.allow("sales:*", "@user", 0)
    server.allow("sales:*", "@sales", 8)

    server.add_page(
        "start", "<h1>Главная</h1><p>Добро пожаловать.</p>", title="Главная"
    )
    server.add_page(
        "public:vacation", "<h1>Отпуск</h1><p>Отпуск — 28 дней.</p>", title="Отпуск"
    )
    server.add_page(
        "hr:onboarding", "<h1>Адаптация</h1><p>Первый день.</p>", title="Адаптация"
    )
    server.add_page(
        "hr:salaries", "<h1>Зарплаты</h1><p>Оклады в марте.</p>", title="Зарплаты"
    )
    server.add_page("sales:plan", "<h1>План продаж</h1><p>Рост 10%.</p>")
    server.add_page("public:fresh", "<h1>Новая</h1><p>Ещё не в индексе.</p>")
    server.indexed.discard("public:fresh")
    server.add_media("hr:policy.pdf", b"%PDF-1.4 policy")
    server.add_media("public:guide.pdf", b"%PDF-1.4 guide")
    server.add_media("public:logo.png", b"\x89PNG logo")
    return server
