"""Поддельная Jira Software Server / Data Center для контрактных тестов.

Формы ответов — по REST API v2 Jira Server
(https://docs.atlassian.com/software/jira/docs/api/REST/10.3.0/ — разделы
api/2/search, api/2/issue, api/2/issue/{issueIdOrKey}/comment,
api/2/user/permission/search, api/2/project, api/2/project/{key}/
permissionscheme, api/2/myself, api/2/attachment) и сверены с живой
Jira 10.3.26 (стенд tests/live/jira_dc/, 15.10):

- поиск пользователей с правом принимает только старый ключ права
  `BROWSE` (BROWSE_PROJECTS — 400), нужен `issueKey` или `projectKey`,
  отдаёт активных пользователей с почтой и учитывает уровень
  безопасности и автора задачи; не администратору — 403 с пустыми
  errorMessages; страницы — после проверки прав, конец — пустая;
- схема прав проекта не администратору — 403;
- описание и комментарии в renderedFields — HTML; дата комментария там
  «2 minutes ago», ISO — в fields; комментарий с visibility видит
  администратор, обычный пользователь — нет;
- `content` вложения — абсолютный адрес /secure/attachment/{id}/{имя};
  без токена — 302 на login.jsp.

Права считаются как в Jira: держатели Browse из схемы проекта (anyone,
applicationRole, group, user, projectRole, projectLead, reporter,
assignee) и участники уровня безопасности задачи. Ломается по команде:
429 с Retry-After, 5xx, страница входа вместо JSON.
"""

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote

import httpx

from corp_ed.core.outbound import OutboundClient
from tests.fake_connector import public_resolver

HOST = "jira.example.com"
ADMIN_TOKEN = "pat-admin"  # noqa: S105 — поддельный сервер
MEMBER_TOKEN = "pat-member"  # noqa: S105
_PROJECT_JQL = re.compile(
    r'^project = "([A-Z][A-Z0-9_]*)"(?: AND \((.*)\))?( ORDER BY .*)?$'
)


@dataclass
class FakeJira:
    host: str = HOST
    context: str = ""
    """Контекстный путь установки (`/jira`): пусто — корень хоста."""
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    groups: dict[str, set[str]] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)
    admins: set[str] = field(default_factory=set)
    projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    issues: dict[str, dict[str, Any]] = field(default_factory=dict)
    files: dict[str, bytes] = field(default_factory=dict)
    search_cap: int = 50
    """Сколько задач Jira отдаёт на страницу, сколько бы ни просили."""
    users_cap: int = 1000
    inline_comments: int = 1000
    """Сколько комментариев отдаёт сама задача (дальше — /comment)."""
    rate_limit_hits: int = 0
    retry_after: str = "2"
    server_errors: int = 0
    login_page: bool = False
    foreign_content: bool = False
    """content вложения указывает на чужой хост."""
    hidden_schemes: set[str] = field(default_factory=set)
    """Проекты, чью схему прав токену не показывают (403)."""
    vanished: set[str] = field(default_factory=set)
    """Задачи, удалённые между поиском и вопросом о читателях (404)."""
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    @property
    def base(self) -> str:
        return f"https://{self.host}{self.context}/"

    def client(self) -> OutboundClient:
        return OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
            resolver=public_resolver,
        )

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]

    # --- наполнение ---------------------------------------------------------------

    def add_user(
        self,
        name: str,
        *,
        groups: tuple[str, ...] = (),
        email: str | None = "",
        active: bool = True,
        token: str | None = None,
        admin: bool = False,
    ) -> str:
        self.users[name] = {
            "name": name,
            "key": f"JIRAUSER{10000 + len(self.users)}",
            "displayName": name.capitalize(),
            "emailAddress": f"{name}@example.com" if email == "" else email,
            "active": active,
        }
        for group in groups:
            self.groups.setdefault(group, set()).add(name)
        if token:
            self.tokens[token] = name
        if admin:
            self.admins.add(name)
        return name

    def add_project(
        self,
        key: str,
        name: str,
        browse: list[dict[str, str]],
        *,
        lead: str = "admin",
        roles: dict[str, dict[str, set[str]]] | None = None,
        levels: dict[str, dict[str, Any]] | None = None,
        project_admins: set[str] | None = None,
    ) -> str:
        self.projects[key] = {
            "id": str(10000 + len(self.projects)),
            "key": key,
            "name": name,
            "archived": False,
            "lead": lead,
            "browse": browse,
            "roles": roles or {},
            "levels": levels or {},
            "project_admins": project_admins or set(),
        }
        return key

    def add_issue(
        self,
        project: str,
        summary: str,
        description: str = "",
        *,
        reporter: str = "admin",
        assignee: str | None = None,
        level: str | None = None,
        comments: list[tuple[str, str, dict[str, str] | None]] | None = None,
        updated: str = "2026-10-01T10:00:00.000+0000",
    ) -> str:
        number = 1 + sum(1 for i in self.issues.values() if i["project"] == project)
        issue_id = str(20000 + len(self.issues))
        key = f"{project}-{number}"
        self.issues[issue_id] = {
            "id": issue_id,
            "key": key,
            "project": project,
            "summary": summary,
            "description": description,
            "reporter": reporter,
            "assignee": assignee,
            "level": level,
            "created": f"2026-09-01T10:{len(self.issues):02d}:00.000+0000",
            "updated": updated,
            "comments": [
                {
                    "id": str(30000 + n),
                    "author": author,
                    "body": body,
                    "visibility": visibility,
                    "created": "2026-09-02T12:00:00.000+0000",
                }
                for n, (author, body, visibility) in enumerate(comments or [])
            ],
            "attachments": [],
        }
        return key

    def add_comment(self, key: str, author: str, body: str) -> None:
        issue = self.issue_by_key(key)
        issue["comments"].append(
            {
                "id": str(31000 + len(issue["comments"])),
                "author": author,
                "body": body,
                "visibility": None,
                "created": "2026-09-03T12:00:00.000+0000",
            }
        )

    def add_attachment(
        self, key: str, filename: str, data: bytes, *, size: int | None = None
    ) -> str:
        attachment_id = str(40000 + len(self.files))
        self.issue_by_key(key)["attachments"].append(
            {
                "id": attachment_id,
                "filename": filename,
                "size": len(data) if size is None else size,
                "created": "2026-09-04T09:00:00.000+0000",
                "mimeType": "application/octet-stream",
            }
        )
        self.files[attachment_id] = data
        return attachment_id

    def issue_by_key(self, key: str) -> dict[str, Any]:
        return next(i for i in self.issues.values() if i["key"] == key)

    # --- права, как их считает Jira ------------------------------------------------

    def can_browse(self, name: str, issue: dict[str, Any]) -> bool:
        user = self.users.get(name)
        if user is None or not user["active"]:
            return False
        project = self.projects[issue["project"]]
        if not any(self._holds(name, h, project, issue) for h in project["browse"]):
            return False
        level = issue["level"]
        if level is None:
            return True
        members = project["levels"][level]
        return (
            name in members.get("users", set())
            or bool(self._groups_of(name) & members.get("groups", set()))
            or (members.get("reporter", False) and issue["reporter"] == name)
        )

    def _holds(
        self,
        name: str,
        holder: dict[str, str],
        project: dict[str, Any],
        issue: dict[str, Any],
    ) -> bool:
        kind = holder["type"]
        parameter = holder.get("parameter", "")
        if kind in {"anyone", "applicationRole"}:
            return True
        if kind == "group":
            return name in self.groups.get(parameter, set())
        if kind == "user":
            return self.users[name]["key"] == parameter
        if kind == "projectRole":
            role = project["roles"].get(parameter, {})
            return name in role.get("users", set()) or bool(
                self._groups_of(name) & role.get("groups", set())
            )
        if kind == "projectLead":
            return project["lead"] == name
        if kind == "reporter":
            return issue["reporter"] == name
        if kind == "assignee":
            return issue["assignee"] == name
        return False

    def _groups_of(self, name: str) -> set[str]:
        return {group for group, members in self.groups.items() if name in members}

    def _administers(self, actor: str, project: str) -> bool:
        return actor in self.admins or actor in self.projects[project]["project_admins"]

    # --- обработка ------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        # OutboundClient ходит на закреплённый IP: имя хоста — в Host.
        if request.headers.get("host", "").split(":")[0] != self.host:
            return httpx.Response(404, text="unknown host")
        path = unquote(request.url.path)
        if not path.startswith(self.context + "/"):
            return httpx.Response(404, text="no context")
        path = path.removeprefix(self.context)
        query = dict(request.url.params)
        self.calls.append((path, query))
        if self.rate_limit_hits > 0:
            self.rate_limit_hits -= 1
            return httpx.Response(
                429, headers={"Retry-After": self.retry_after}, text="Too many"
            )
        if self.server_errors > 0:
            self.server_errors -= 1
            return httpx.Response(503, text="<html>maintenance</html>")
        actor = self.tokens.get(
            request.headers.get("authorization", "").removeprefix("Bearer ")
        )
        if path.startswith("/secure/attachment/"):
            return self._download(actor, path)
        if actor is None:
            return _error(401, "You are not authenticated.")
        if self.login_page:
            return httpx.Response(
                200, headers={"content-type": "text/html"}, text="<html>login</html>"
            )
        if not path.startswith("/rest/api/2/"):
            return _error(404, "not found")
        return self._route(actor, path.removeprefix("/rest/api/2/"), query)

    def _route(self, actor: str, route: str, query: dict[str, str]) -> httpx.Response:
        if route == "myself":
            return _json(self._user(actor))
        if route == "project":
            return _json(
                [
                    self._project(p)
                    for p in self.projects.values()
                    if self._sees_project(actor, p["key"])
                ]
            )
        if match := re.fullmatch(r"project/([^/]+)/permissionscheme", route):
            key = match.group(1)
            if key not in self.projects:
                return _error(404, f"No project could be found with key '{key}'.")
            if not self._administers(actor, key) or key in self.hidden_schemes:
                return _error(403, "You cannot edit the configuration of this project.")
            return _json(self._scheme(key))
        if match := re.fullmatch(r"project/([^/]+)", route):
            key = match.group(1)
            if key not in self.projects or not self._sees_project(actor, key):
                return _error(404, f"No project could be found with key '{key}'.")
            return _json(self._project(self.projects[key]))
        if route == "search":
            return self._search(actor, query)
        if route == "user/permission/search":
            return self._permission_search(actor, query)
        if match := re.fullmatch(r"issue/([^/]+)/comment", route):
            return self._comments(actor, match.group(1), query)
        if match := re.fullmatch(r"issue/([^/]+)", route):
            return self._issue(actor, match.group(1), query)
        if match := re.fullmatch(r"attachment/([^/]+)", route):
            for issue in self.issues.values():
                for attachment in issue["attachments"]:
                    if attachment["id"] == match.group(1) and self.can_browse(
                        actor, issue
                    ):
                        return _json(self._attachment(issue, attachment))
            return _error(404, "The attachment does not exist")
        return _error(404, "null for uri")

    def _sees_project(self, actor: str, key: str) -> bool:
        return actor in self.admins or any(
            self.can_browse(actor, issue)
            for issue in self.issues.values()
            if issue["project"] == key
        )

    def _search(self, actor: str, query: dict[str, str]) -> httpx.Response:
        match = _PROJECT_JQL.match(query.get("jql", ""))
        if match is None:
            return _error(400, "Error in JQL Query: unsupported in fake")
        key, extra = match.group(1), match.group(2)
        if key not in self.projects:
            return _error(
                400, f"The value '{key}' does not exist for the field 'project'."
            )
        if extra is not None and "invalid" in extra:
            return _error(400, "Error in JQL Query: Expecting either a value")
        found = sorted(
            (
                i
                for i in self.issues.values()
                if i["project"] == key and self.can_browse(actor, i)
            ),
            key=lambda i: (i["created"], i["key"]),
        )
        start = int(query.get("startAt", 0))
        limit = min(int(query.get("maxResults", 50)), self.search_cap)
        wanted = set(query.get("fields", "").split(","))
        return _json(
            {
                "startAt": start,
                "maxResults": limit,
                "total": len(found),
                "issues": [
                    {
                        "id": issue["id"],
                        "key": issue["key"],
                        "self": f"{self.base}rest/api/2/issue/{issue['id']}",
                        "fields": {
                            k: v for k, v in self._fields(issue).items() if k in wanted
                        },
                        "renderedFields": None,
                    }
                    for issue in found[start : start + limit]
                ],
            }
        )

    def _permission_search(self, actor: str, query: dict[str, str]) -> httpx.Response:
        if query.get("permissions") != "BROWSE":
            return _error(
                400, f"Invalid permission name specified: '{query.get('permissions')}'"
            )
        if "username" not in query:
            return _error(400, "The username query parameter was not provided")
        key = query.get("issueKey")
        if not key:
            return _error(400, "No project or issue key was provided")
        issue = next((i for i in self.issues.values() if i["key"] == key), None)
        if issue is None or key in self.vanished:
            return _error(404, "The issue no longer exists.")
        if not self._administers(actor, issue["project"]):
            return httpx.Response(403, json={"errorMessages": [], "errors": {}})
        needle = query["username"].lower()
        allowed = [
            self._user(name)
            for name in sorted(self.users)
            if self.can_browse(name, issue)
            and (
                name.startswith(needle)
                or str(self.users[name]["emailAddress"] or "").startswith(needle)
            )
        ]
        start = int(query.get("startAt", 0))
        limit = min(int(query.get("maxResults", 50)), self.users_cap)
        return _json(allowed[start : start + limit])

    def _issue(self, actor: str, ident: str, query: dict[str, str]) -> httpx.Response:
        issue = self.issues.get(ident) or next(
            (i for i in self.issues.values() if i["key"] == ident), None
        )
        if issue is None or not self.can_browse(actor, issue):
            return _error(404, "Issue Does Not Exist")
        fields = self._fields(issue, actor=actor)
        wanted = set(query.get("fields", "*all").split(","))
        if "*all" not in wanted:
            fields = {k: v for k, v in fields.items() if k in wanted}
        body: dict[str, Any] = {
            "id": issue["id"],
            "key": issue["key"],
            "fields": fields,
        }
        if "renderedFields" in query.get("expand", ""):
            comments = self._visible(actor, issue)[: self.inline_comments]
            body["renderedFields"] = {
                "description": issue["description"] or None,
                "comment": {
                    "comments": [
                        {**self._comment(c), "body": c["body"], "created": "Just now"}
                        for c in comments
                    ],
                    "maxResults": self.inline_comments,
                    "total": len(self._visible(actor, issue)),
                    "startAt": 0,
                },
            }
        return _json(body)

    def _comments(
        self, actor: str, ident: str, query: dict[str, str]
    ) -> httpx.Response:
        issue = self.issues.get(ident) or next(
            (i for i in self.issues.values() if i["key"] == ident), None
        )
        if issue is None or not self.can_browse(actor, issue):
            return _error(404, "Issue Does Not Exist")
        visible = self._visible(actor, issue)
        start = int(query.get("startAt", 0))
        limit = min(int(query.get("maxResults", 50)), 100)
        rendered = "renderedBody" in query.get("expand", "")
        return _json(
            {
                "startAt": start,
                "maxResults": limit,
                "total": len(visible),
                "comments": [
                    {
                        **self._comment(c),
                        **({"renderedBody": c["body"]} if rendered else {}),
                    }
                    for c in visible[start : start + limit]
                ],
            }
        )

    def _download(self, actor: str | None, path: str) -> httpx.Response:
        match = re.fullmatch(r"/secure/attachment/(\d+)/(.+)", path)
        if actor is None:
            return httpx.Response(
                302,
                headers={"location": f"{self.base}login.jsp?permissionViolation=true"},
            )
        if match is None or match.group(1) not in self.files:
            return httpx.Response(404, text="<html>not found</html>")
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=self.files[match.group(1)],
        )

    # --- формы ответов ---------------------------------------------------------------

    def _user(self, name: str) -> dict[str, Any]:
        user = self.users[name]
        shown = {
            "self": f"{self.base}rest/api/2/user?username={name}",
            "key": user["key"],
            "name": name,
            "displayName": user["displayName"],
            "active": user["active"],
            "timeZone": "Etc/UTC",
        }
        if user["emailAddress"]:
            shown["emailAddress"] = user["emailAddress"]
        return shown

    def _project(self, project: dict[str, Any]) -> dict[str, Any]:
        return {
            "self": f"{self.base}rest/api/2/project/{project['id']}",
            "id": project["id"],
            "key": project["key"],
            "name": project["name"],
            "projectTypeKey": "software",
            "archived": project["archived"],
        }

    def _scheme(self, key: str) -> dict[str, Any]:
        project = self.projects[key]
        permissions = [
            {"id": 100 + n, "holder": dict(h), "permission": "BROWSE_PROJECTS"}
            for n, h in enumerate(project["browse"])
        ]
        permissions.append(
            {
                "id": 999,
                "holder": {"type": "projectRole", "parameter": "10002"},
                "permission": "ADMINISTER_PROJECTS",
            }
        )
        return {"id": 10000, "name": f"Схема {key}", "permissions": permissions}

    def _fields(
        self, issue: dict[str, Any], *, actor: str | None = None
    ) -> dict[str, Any]:
        project = self.projects[issue["project"]]
        level = issue["level"]
        visible = self._visible(actor, issue) if actor else []
        return {
            "summary": issue["summary"],
            "created": issue["created"],
            "updated": issue["updated"],
            "project": {
                "id": project["id"],
                "key": project["key"],
                "name": project["name"],
            },
            "issuetype": {"id": "10101", "name": "Task", "subtask": False},
            "status": {"id": "1", "name": "To Do"},
            "reporter": self._user(issue["reporter"]),
            "assignee": self._user(issue["assignee"]) if issue["assignee"] else None,
            "security": (
                {"id": level, "name": project["levels"][level]["name"]}
                if level
                else None
            ),
            "description": issue["description"] or None,
            "comment": {
                "comments": [self._comment(c) for c in visible[: self.inline_comments]],
                "maxResults": self.inline_comments,
                "total": len(visible),
                "startAt": 0,
            },
            "attachment": [self._attachment(issue, a) for a in issue["attachments"]],
        }

    def _visible(
        self, actor: str | None, issue: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Комментарии, которые видит actor: с visibility — только админ."""
        return [
            c
            for c in issue["comments"]
            if c["visibility"] is None or (actor is not None and actor in self.admins)
        ]

    def _comment(self, comment: dict[str, Any]) -> dict[str, Any]:
        shown = {
            "id": comment["id"],
            "author": self._user(comment["author"]),
            # Сырое тело — вики-разметка; HTML — только в renderedFields.
            "body": re.sub(r"<[^>]+>", "", comment["body"]),
            "created": comment["created"],
        }
        if comment["visibility"]:
            shown["visibility"] = comment["visibility"]
        return shown

    def _attachment(
        self, issue: dict[str, Any], attachment: dict[str, Any]
    ) -> dict[str, Any]:
        host = "evil.example.net" if self.foreign_content else self.host
        context = "" if self.foreign_content else self.context
        name = quote(attachment["filename"])
        return {
            "self": f"{self.base}rest/api/2/attachment/{attachment['id']}",
            **attachment,
            "content": f"https://{host}{context}/secure/attachment/{attachment['id']}/{name}",
        }


def _json(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"errorMessages": [message], "errors": {}})


def sample_jira() -> FakeJira:
    """Три проекта, как на живом стенде: HR — группа и автор задачи, ENG —
    роль проекта и уровни безопасности, OPEN — «любой»."""
    server = FakeJira()
    server.add_user("admin", admin=True, token=ADMIN_TOKEN)
    server.add_user("alice", groups=("hr",))
    server.add_user("bob", groups=("engineering",), token=MEMBER_TOKEN)
    server.add_user("carol", groups=("hr", "engineering"))
    server.add_user("dave")
    server.add_user("erin", groups=("hr",), active=False)
    server.add_user("nomail", groups=("hr",), email=None)
    admin_key = server.users["admin"]["key"]
    server.add_project(
        "HR",
        "Кадры",
        [
            {"type": "group", "parameter": "hr"},
            {"type": "reporter"},
            {"type": "user", "parameter": admin_key},
        ],
    )
    server.add_project(
        "ENG",
        "Разработка",
        [
            {"type": "projectRole", "parameter": "10100"},
            {"type": "user", "parameter": admin_key},
        ],
        roles={"10100": {"groups": {"engineering"}, "users": {"dave"}}},
        levels={
            "10000": {"name": "Руководство", "users": {"carol", "admin"}},
            "10001": {"name": "Без админа", "users": {"carol"}},
        },
    )
    server.add_project(
        "OPEN", "Общее", [{"type": "anyone"}, {"type": "user", "parameter": admin_key}]
    )
    server.add_issue(
        "HR",
        "Отпуск",
        "<p>Отпуск — <b>28</b> дней.</p>",
        comments=[
            ("alice", "<p>Заявление — за две недели.</p>", None),
            (
                "admin",
                "<p>Бюджет отпусков.</p>",
                {"type": "role", "value": "Administrators"},
            ),
        ],
    )
    server.add_attachment("HR-1", "Заявление.md", "# Заявление\n\nТекст.".encode())
    server.add_attachment("HR-1", "схема.exe", b"MZ")
    server.add_issue("HR", "Справка для Дэйва", "<p>Готова.</p>", reporter="dave")
    server.add_issue("ENG", "Архитектура", "<h2>Схема</h2><p>Монолит.</p>")
    server.add_issue("ENG", "Инцидент", "<p>Разбор.</p>", level="10000")
    server.add_issue("ENG", "Оценка", "<p>Только carol.</p>", level="10001")
    server.add_issue("OPEN", "Регламент", "<p>С 10 до 19.</p>", assignee="bob")
    return server
