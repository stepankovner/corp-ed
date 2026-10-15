"""Тестовая компания в Jira Software DC для живой проверки адаптера jira_dc.

Пользователи с почтами, две группы, роль проекта, три проекта с разными
схемами прав, схема уровней безопасности, задачи с комментариями и
вложение. Всё выдумано, адреса — example.com. Ожидания для
tests/live/test_jira_dc_live.py — здесь же (EXPECTED_READERS): кто по
правилам Jira может открыть каждую задачу.

    JIRA_URL=http://127.0.0.1:8510 JIRA_ADMIN_TOKEN=… \\
        uv run python -m tests.live.jira_dc.seed

Порядок: сначала `rest` (пользователи, группы, роль, схемы прав,
проекты), потом security.mjs (схема уровней безопасности — REST её не
создаёт), потом `issues` (задачи, комментарии, вложение); `token
[логин]` печатает персональный токен пользователя (по умолчанию —
служебной учётки). Без аргумента — `rest`. Рассчитан на чистую Jira:
повторный запуск упадёт на занятых именах. Подробно — README.md рядом.
"""

import os
import sys
from dataclasses import dataclass, field
from typing import Any

import httpx

from tests.ingest import samples

PASSWORD = "Kronto-Test-1"
"""Пароль всех тестовых пользователей (выдуманная компания в контейнере)."""
SERVICE = "svc-kronto"
"""Служебная учётка подключения: персональный токен выдаёт token.mjs."""
ADMIN = "admin"
"""Администратор из мастера: автор всех задач. Права проекта действуют и
на администратора Jira, поэтому он есть в каждой схеме прав — как
руководитель проекта в жизни."""
INACTIVE = "erin"
"""Деактивирована после создания: Jira не считает её читателем ничего."""


def email(username: str) -> str:
    return f"{username}@example.com"


USERS = {
    "alice": ["hr"],
    "bob": ["engineering"],
    "carol": ["hr", "engineering"],
    "dave": [],
    INACTIVE: ["hr"],
    SERVICE: ["hr", "engineering", "jira-administrators"],
}
"""Пользователь → группы. Служебная учётка — в обеих группах (иначе она
сама не видит задач) и администратор Jira: кто может открыть задачу,
Jira отвечает только администратору (`/user/permission/search` — 403
обычной учётке, стенд 15.10). Права Browse ей это не добавляет: схемы
прав группу jira-administrators не упоминают."""
EXISTING_GROUPS = {"jira-administrators"}
"""Группы, которые Jira заводит сама при установке."""
RESTRICTED_COMMENT = "Только для администраторов проекта: бюджет отпусков."
"""Комментарий с видимостью «роль Administrators»: служебная учётка его
видит (jira-administrators — участник роли), но в материал он не идёт."""

DEVELOPERS = "Developers"
"""Роль проекта ENG: группа engineering и отдельно dave."""

# Права, нужные администратору, чтобы завести задачи: схема прав
# действует и на него.
_ADMIN_PERMISSIONS = (
    "BROWSE_PROJECTS",
    "CREATE_ISSUES",
    "EDIT_ISSUES",
    "ADD_COMMENTS",
    "CREATE_ATTACHMENTS",
    "SET_ISSUE_SECURITY",
    "MODIFY_REPORTER",
    "ASSIGN_ISSUES",
    "ASSIGNABLE_USER",
    "DELETE_ISSUES",
    "TRANSITION_ISSUES",
    "ADMINISTER_PROJECTS",
)


@dataclass(frozen=True)
class Project:
    key: str
    name: str
    browse: tuple[dict[str, str], ...]
    """Кто получает Browse Projects (holder схемы прав, кроме admin)."""


PROJECTS = (
    # Группа и «Автор задачи» — права зависят от самой задачи.
    Project(
        "HR",
        "Кадры",
        ({"type": "group", "parameter": "hr"}, {"type": "reporter"}),
    ),
    # Роль проекта; часть задач закрыта уровнем безопасности.
    Project("ENG", "Разработка", ({"type": "projectRole", "parameter": "@role"},)),
    # «Любой» (анонимный доступ) — у нас все активные пользователи.
    Project("OPEN", "Общее", ({"type": "anyone"},)),
)

SECURITY_SCHEME = "Kronto Секретность"
SECURITY_LEVELS = {
    "Руководство": ["carol", SERVICE, ADMIN],
    "Без службы": ["carol", ADMIN],
}
"""Уровни безопасности ENG и их участники (пользователи). Создаёт их
security.mjs: REST DC схем уровней не создаёт."""


@dataclass
class Issue:
    project: str
    summary: str
    description: str
    comments: list[str | tuple[str, str]] = field(default_factory=list)
    """Текст или (текст, роль проекта, которой виден комментарий)."""
    reporter: str = ADMIN
    level: str | None = None
    attachment: str | None = None


ISSUES = [
    Issue(
        "HR",
        "Отпуск",
        "Отпуск — *28* календарных дней.",
        comments=[
            "Заявление — за две недели.",
            "Перенос — по согласованию.",
            (RESTRICTED_COMMENT, "Administrators"),
        ],
        attachment="Заявление.docx",
    ),
    Issue("HR", "Справка для Дэйва", "Справка 2-НДФЛ готова.", reporter="dave"),
    Issue("ENG", "Архитектура", "h2. Схема\n\nМонолит и воркер."),
    Issue(
        "ENG",
        "Инцидент с данными",
        "Разбор утечки тестовых данных.",
        comments=["Доступ закрыт до разбора."],
        level="Руководство",
    ),
    Issue("ENG", "Оценка руководителя", "Только для carol.", level="Без службы"),
    Issue("OPEN", "Регламент", "Рабочий день — с 10 до 19."),
]

ACTIVE = sorted({ADMIN, *USERS} - {INACTIVE})
EXPECTED_READERS: dict[str, frozenset[str]] = {
    # Группа hr (erin деактивирована) и автор — admin.
    "Отпуск": frozenset({"alice", "carol", SERVICE, ADMIN}),
    # Автор задачи — dave: «Reporter» в схеме прав даёт ему эту задачу.
    "Справка для Дэйва": frozenset({"alice", "carol", "dave", SERVICE, ADMIN}),
    # Роль Developers: группа engineering и dave.
    "Архитектура": frozenset({"bob", "carol", "dave", SERVICE, ADMIN}),
    # Уровень безопасности сужает роль до своих участников.
    "Инцидент с данными": frozenset({"carol", SERVICE, ADMIN}),
    # «Любой» — все активные пользователи.
    "Регламент": frozenset(ACTIVE),
}
"""«Оценки руководителя» здесь нет: служебная учётка её не видит, и адаптер
её не должен вернуть."""
HIDDEN_FROM_SERVICE = ("Оценка руководителя",)

ATTACHMENTS = {
    "Заявление.docx": samples.docx(
        [("Заявление на отпуск подаётся за две недели.", None)]
    ),
}


class Seeder:
    def __init__(self, base_url: str, token: str) -> None:
        self.root = base_url.rstrip("/")
        self.http = httpx.Client(
            base_url=f"{self.root}/rest/api/2",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Atlassian-Token": "no-check",
            },
            timeout=120,
        )

    def _check(self, response: httpx.Response) -> Any:
        if response.status_code >= 300:
            raise RuntimeError(
                f"{response.request.method} {response.request.url.path}: "
                f"HTTP {response.status_code} {response.text[:400]}"
            )
        return response.json() if response.content else None

    def users(self) -> None:
        created = {g for names in USERS.values() for g in names} - EXISTING_GROUPS
        for group in sorted(created):
            self._check(self.http.post("/group", json={"name": group}))
        for username, groups in USERS.items():
            self._check(
                self.http.post(
                    "/user",
                    json={
                        "name": username,
                        "password": PASSWORD,
                        "emailAddress": email(username),
                        "displayName": username.capitalize(),
                    },
                )
            )
            for group in groups:
                self.join(username, group)
        self._check(
            self.http.put(
                "/user", params={"username": INACTIVE}, json={"active": False}
            )
        )

    def join(self, username: str, group: str) -> None:
        self._check(
            self.http.post(
                "/group/user", params={"groupname": group}, json={"name": username}
            )
        )

    def leave(self, username: str, group: str) -> None:
        self._check(
            self.http.delete(
                "/group/user", params={"groupname": group, "username": username}
            )
        )

    def role(self) -> str:
        found = self._check(
            self.http.post("/role", json={"name": DEVELOPERS, "description": "Kronto"})
        )
        return str(found["id"])

    def projects(self) -> None:
        role = self.role()
        for project in PROJECTS:
            holders = [
                {**h, "parameter": role} if h.get("parameter") == "@role" else h
                for h in project.browse
            ]
            permissions = [
                {"holder": h, "permission": "BROWSE_PROJECTS"} for h in holders
            ] + [
                {"holder": {"type": "user", "parameter": ADMIN}, "permission": p}
                for p in _ADMIN_PERMISSIONS
            ]
            scheme = self._check(
                self.http.post(
                    "/permissionscheme",
                    json={"name": f"Kronto {project.key}", "permissions": permissions},
                )
            )
            self._check(
                self.http.post(
                    "/project",
                    json={
                        "key": project.key,
                        "name": project.name,
                        "projectTypeKey": "software",
                        "projectTemplateKey": (
                            "com.pyxis.greenhopper.jira:basic-software-development-template"
                        ),
                        "lead": ADMIN,
                        "permissionScheme": scheme["id"],
                    },
                )
            )
            if project.key == "ENG":
                self._check(
                    self.http.post(
                        f"/project/ENG/role/{role}",
                        json={"group": ["engineering"], "user": ["dave"]},
                    )
                )

    def levels(self) -> dict[str, str]:
        """Имя уровня безопасности ENG → id (схема привязана security.mjs)."""
        scheme = self._check(self.http.get("/project/ENG/issuesecuritylevelscheme"))
        found = self._check(self.http.get(f"/issuesecurityschemes/{scheme['id']}"))
        return {str(level["name"]): str(level["id"]) for level in found["levels"]}

    def issues(self) -> dict[str, str]:
        """Завести задачи; вернуть заголовок → ключ."""
        levels = self.levels()
        keys: dict[str, str] = {}
        for issue in ISSUES:
            fields: dict[str, Any] = {
                "project": {"key": issue.project},
                "summary": issue.summary,
                "description": issue.description,
                "issuetype": {"name": "Task"},
                "reporter": {"name": issue.reporter},
            }
            if issue.level:
                fields["security"] = {"id": levels[issue.level]}
            created = self._check(self.http.post("/issue", json={"fields": fields}))
            key = keys[issue.summary] = str(created["key"])
            for comment in issue.comments:
                body: dict[str, Any] = {"body": comment}
                if isinstance(comment, tuple):
                    body = {
                        "body": comment[0],
                        "visibility": {"type": "role", "value": comment[1]},
                    }
                self._check(self.http.post(f"/issue/{key}/comment", json=body))
            if issue.attachment:
                self._check(
                    self.http.post(
                        f"/issue/{key}/attachments",
                        files={
                            "file": (issue.attachment, ATTACHMENTS[issue.attachment])
                        },
                    )
                )
        return keys


def user_token(base_url: str, username: str, password: str = PASSWORD) -> str:
    """Персональный токен пользователя: в 10.3 Basic в REST включён, и
    токен выдаётся без браузера (POST /rest/pat/latest/tokens)."""
    response = httpx.post(
        f"{base_url.rstrip('/')}/rest/pat/latest/tokens",
        auth=(username, password),
        json={"name": f"kronto-{username}", "expirationDuration": 1},
        headers={"X-Atlassian-Token": "no-check"},
        timeout=60,
    )
    if response.status_code not in {200, 201}:
        raise RuntimeError(f"токен {username}: HTTP {response.status_code}")
    return str(response.json()["rawToken"])


def main() -> None:
    url = os.environ.get("JIRA_URL", "http://127.0.0.1:8510")
    stage = sys.argv[1] if len(sys.argv) > 1 else "rest"
    if stage == "token":
        print(user_token(url, sys.argv[2] if len(sys.argv) > 2 else SERVICE))
        return
    token = os.environ.get("JIRA_ADMIN_TOKEN", "")
    if not token:
        sys.exit("нужен JIRA_ADMIN_TOKEN (печатает wizard.mjs)")
    seeder = Seeder(url, token)
    if stage not in {"rest", "users", "projects", "issues"}:
        sys.exit(f"неизвестный шаг {stage}: rest, issues или token")
    if stage in {"rest", "users"}:
        seeder.users()
    if stage in {"rest", "projects"}:
        seeder.projects()
    if stage == "issues":
        for summary, key in seeder.issues().items():
            print(f"{key}\t{summary}")


if __name__ == "__main__":
    main()
