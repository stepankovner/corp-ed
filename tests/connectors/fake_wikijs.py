"""Поддельный Wiki.js 2.x (GraphQL) для контрактных тестов адаптера.

Формы — по документации и схеме: https://docs.requarks.io/dev/api,
requarks/wiki v2.5.315 `server/graph/schemas/{page,group,user,system}.graphql`,
`server/graph/directives/auth.js` (поле без права — ошибка `Forbidden`),
`server/core/auth.js` (ключ API: права группы ключа; испорченный ключ —
гость; отозванный — 500 из `server/master.js`), `server/helpers/error.js`
(PageNotFound 6003, AuthRequired 1019). Сверены со стендом 2.5.316
(tests/live/wikijs): ошибки полей приходят с HTTP 200 рядом с `data`,
`publishStartDate` пустой — "", `pages.list` без постраничности.

Запросы разбираются по форме, которую шлёт адаптер (не полный GraphQL):
проверка `users.profile` + `system.info`, `users.list`, `groups.list`,
пакеты `g<id>: single(id: <id>)` и `p<id>: single(id: <id>)`,
`pages.list`, `pages.single(id: $id)`.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from corp_ed.core.outbound import OutboundClient
from tests.fake_connector import public_resolver

HOST = "wiki.example.com"
API_KEY = "wjs_full_access"  # noqa: S105 — поддельный сервер
MEMBER_KEY = "wjs_group_key"  # noqa: S105
REVOKED_KEY = "wjs_revoked"  # noqa: S105

READ = ["read:pages", "read:assets", "read:comments"]
_ALIAS = re.compile(r"\b([gp])(\d+): single\(id: (\d+)\)")


def rule(
    match: str, path: str, *, deny: bool = False, locales: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "id": f"{match}-{path}-{deny}",
        "deny": deny,
        "match": match,
        "roles": list(READ),
        "path": path,
        "locales": list(locales),
    }


@dataclass
class FakeWikiJs:
    host: str = HOST
    keys: dict[str, int] = field(default_factory=lambda: {API_KEY: 1, MEMBER_KEY: 3})
    """Ключ → группа ключа (Full Access — 1, Administrators)."""
    api_enabled: bool = True
    users: dict[int, dict[str, Any]] = field(default_factory=dict)
    groups: dict[int, dict[str, Any]] = field(default_factory=dict)
    members: dict[int, set[int]] = field(default_factory=dict)
    """id группы → id пользователей."""
    pages: dict[int, dict[str, Any]] = field(default_factory=dict)
    vanish_after_list: set[int] = field(default_factory=set)
    """Страницы, которые удаляют между pages.list и пакетом single."""
    rate_limited: int = 0
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

    def operations(self) -> list[str]:
        return [name for name, _ in self.calls]

    # --- наполнение ---------------------------------------------------------------

    def add_user(self, user_id: int, login: str, *, active: bool = True) -> int:
        self.users[user_id] = {
            "id": user_id,
            "email": f"{login}@Example.com"
            if login == "anna"
            else f"{login}@example.com",
            "name": login,
            "isSystem": user_id == 2,
            "isActive": active,
            "providerKey": "local",
        }
        return user_id

    def add_group(
        self,
        group_id: int,
        name: str,
        permissions: list[str],
        rules: list[dict[str, Any]],
        members: tuple[int, ...] = (),
    ) -> int:
        self.groups[group_id] = {
            "id": group_id,
            "name": name,
            "isSystem": group_id <= 2,
            "permissions": list(permissions),
            "pageRules": list(rules),
        }
        self.members[group_id] = set(members)
        return group_id

    def add_page(
        self,
        page_id: int,
        path: str,
        title: str,
        text: str,
        *,
        locale: str = "en",
        tags: tuple[str, ...] = (),
        published: bool = True,
        private: bool = False,
        start: str = "",
        end: str = "",
        updated: str = "2026-10-01T10:00:00.000Z",
        render: str | None = None,
        content_type: str = "markdown",
    ) -> int:
        self.pages[page_id] = {
            "id": page_id,
            "path": path,
            "locale": locale,
            "title": title,
            "description": "",
            "isPublished": published,
            "isPrivate": private,
            "privateNS": None,
            "contentType": content_type,
            "createdAt": "2026-01-01T10:00:00.000Z",
            "updatedAt": updated,
            "publishStartDate": start,
            "publishEndDate": end,
            "tags": list(tags),
            "content": f"# {title}\n\n{text}\n",
            "render": (
                render
                if render is not None
                else f'<h1 id="t" class="toc-header"><a class="toc-anchor" '
                f'href="#t">¶</a> {title}</h1>\n<p>{text}</p>\n'
            ),
        }
        return page_id

    # --- обработка ------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("host", "") != self.host:
            return httpx.Response(404, text="unknown host")
        if request.method != "POST" or request.url.path != "/graphql":
            return httpx.Response(404, text="<html>Not Found</html>")
        body = json.loads(request.content or b"{}")
        query = str(body.get("query") or "")
        variables = body.get("variables") or {}
        self.calls.append((_operation(query), variables))
        if self.server_errors > 0:
            self.server_errors -= 1
            return httpx.Response(502, text="bad gateway")
        if self.rate_limited > 0:
            self.rate_limited -= 1
            return httpx.Response(429, headers={"retry-after": "3"}, text="slow down")
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token == REVOKED_KEY or (token in self.keys and not self.api_enabled):
            message = (
                "API Key is invalid or was revoked."
                if token == REVOKED_KEY
                else "API is disabled. You must enable it from the "
                "Administration Area first."
            )
            return httpx.Response(
                500, json={"data": {}, "errors": [{"message": message, "path": []}]}
            )
        group = self.keys.get(token)
        permissions = set(self.groups.get(group or 0, {}).get("permissions", []))
        return self._route(query, variables, group, permissions)

    def _route(
        self,
        query: str,
        variables: dict[str, Any],
        group: int | None,
        permissions: set[str],
    ) -> httpx.Response:
        data: dict[str, Any] = {}
        errors: list[dict[str, Any]] = []
        admin = "manage:system" in permissions

        def deny(*path: str) -> None:
            errors.append(_error("Forbidden", list(path)))

        if "profile" in query:
            if group is None:
                data["users"] = {"profile": None}
                errors.append(
                    _error(
                        "You must be authenticated to access this resource.",
                        ["users", "profile"],
                        1019,
                    )
                )
            else:
                data["users"] = {"profile": {"id": 1}}
            version = "2.5.316" if admin else None
            data["system"] = {"info": {"currentVersion": version}}
            if not admin:
                deny("system", "info", "currentVersion")
        elif query.startswith("{ users { list"):
            data["users"] = {"list": list(self.users.values()) if admin else None}
            if not admin:
                deny("users", "list")
        elif query.startswith("{ groups { list"):
            data["groups"] = {
                "list": [
                    {"id": g["id"], "name": g["name"]} for g in self.groups.values()
                ]
            }
        elif query.startswith("{ groups {"):
            found: dict[str, Any] = {}
            for letter, alias, group_id in _ALIAS.findall(query):
                found[f"{letter}{alias}"] = self._group(int(group_id))
            data["groups"] = found
        elif query.startswith("{ pages { list"):
            data["pages"] = {"list": [self._listed(p) for p in self.pages.values()]}
            for page_id in self.vanish_after_list:
                self.pages.pop(page_id, None)
        elif query.startswith("{ pages {"):
            found = {}
            for letter, alias, page_id in _ALIAS.findall(query):
                name = f"{letter}{alias}"
                page = self.pages.get(int(page_id))
                found[name] = page
                if page is None:
                    errors.append(
                        _error("This page does not exist.", ["pages", name], 6003)
                    )
            data["pages"] = found
        elif "single(id: $id)" in query:
            page = self.pages.get(int(variables.get("id", 0)))
            data["pages"] = {"single": page}
            if page is None:
                errors.append(
                    _error("This page does not exist.", ["pages", "single"], 6003)
                )
        else:
            return httpx.Response(
                400,
                json={
                    "errors": [
                        {
                            "message": "Cannot query field",
                            "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"},
                        }
                    ]
                },
            )
        response: dict[str, Any] = {"data": data}
        if errors:
            response["errors"] = errors
        return httpx.Response(200, json=response)

    def _group(self, group_id: int) -> dict[str, Any] | None:
        group = self.groups.get(group_id)
        if group is None:
            return None
        return {
            **group,
            "users": [
                {"id": u, "email": self.users[u]["email"]}
                for u in sorted(self.members.get(group_id, set()))
                if u in self.users
            ],
        }

    @staticmethod
    def _listed(page: dict[str, Any]) -> dict[str, Any]:
        keys = [
            "id",
            "path",
            "locale",
            "title",
            "description",
            "contentType",
            "isPublished",
            "isPrivate",
            "privateNS",
            "createdAt",
            "updatedAt",
            "tags",
        ]
        return {key: page[key] for key in keys}


def _operation(query: str) -> str:
    if "profile" in query:
        return "check"
    for name in ("users { list", "groups { list", "pages { list"):
        if query.startswith("{ " + name):
            return name.replace(" { ", ".")
    if query.startswith("{ groups {"):
        return "groups.batch"
    if query.startswith("{ pages {"):
        return "pages.batch"
    return "pages.single"


def _error(message: str, path: list[str], code: int | None = None) -> dict[str, Any]:
    exception: dict[str, Any] = {"stacktrace": [f"Error: {message}", "    at x"]}
    if code is not None:
        exception["code"] = code
    return {
        "message": message,
        "locations": [{"line": 1, "column": 3}],
        "path": path,
        "extensions": {"code": "INTERNAL_SERVER_ERROR", "exception": exception},
    }


def sample_wikijs() -> FakeWikiJs:
    """Компания стенда (tests/live/wikijs/seed.py): гостям — только public,
    «Сотрудники» читают всё, кроме hr, reports, тега confidential и
    черновиков на ru; «Кадры» — hr/ (длиннее запрета) и отчёты по REGEX;
    «Подрядчики» — запрет projects. olga отключена."""
    server = FakeWikiJs()
    server.add_user(1, "admin")
    server.add_user(2, "guest")
    for user_id, login in ((3, "ivan"), (4, "anna"), (5, "petr")):
        server.add_user(user_id, login)
    server.add_user(6, "olga", active=False)
    server.add_group(1, "Administrators", ["manage:system"], [], (1,))
    server.add_group(2, "Guests", READ, [rule("START", "public")], (2,))
    server.add_group(
        3,
        "Сотрудники",
        READ,
        [
            rule("START", ""),
            rule("START", "hr", deny=True),
            rule("START", "drafts", deny=True, locales=("ru",)),
            rule("TAG", "confidential", deny=True),
            rule("START", "reports", deny=True),
        ],
        (3, 4, 5, 6),
    )
    server.add_group(
        4,
        "Кадры",
        ["read:pages"],
        [rule("START", "hr/"), rule("REGEX", r"^reports/\d{4}$")],
        (4,),
    )
    server.add_group(5, "Подрядчики", [], [rule("START", "projects", deny=True)], (5,))

    server.add_page(1, "public/welcome", "Добро пожаловать", "Открыто без входа.")
    server.add_page(2, "general/vacation", "Отпуск", "Отпуск — 28 дней.")
    server.add_page(3, "hr/salaries", "Зарплаты", "Оклады — в марте.")
    server.add_page(4, "projects/plan", "План проектов", "Запуск — в мае.")
    server.add_page(
        5, "projects/roadmap", "Дорожная карта", "Слияние.", tags=("confidential",)
    )
    server.add_page(6, "reports/2026", "Отчёт 2026", "Выручка выросла.")
    server.add_page(7, "drafts/idea", "Идея", "Черновик для всех.")
    server.add_page(8, "drafts/idea", "Черновик", "Закрыт.", locale="ru")
    server.add_page(9, "general/otpusk", "Отпуск по-русски", "Заявление.", locale="ru")
    server.add_page(10, "general/hidden", "Неопубликованная", "Нет.", published=False)
    return server


SAMPLE_READERS: dict[int, set[str] | None] = {
    1: None,
    2: {"admin", "ivan", "anna", "petr"},
    3: {"admin", "anna"},
    4: {"admin", "ivan", "anna"},
    5: {"admin"},
    6: {"admin", "anna"},
    7: {"admin", "ivan", "anna", "petr"},
    8: {"admin"},
    9: {"admin", "ivan", "anna", "petr"},
}
"""id страницы → кто читает (None — гости, вся компания). Так же ответил
сам Wiki.js 2.5.316 на стенде (200/403 на показ страницы)."""
