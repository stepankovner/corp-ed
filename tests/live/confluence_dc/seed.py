"""Тестовая компания в Confluence DC для живой проверки адаптера (Р-12 «б»).

Пользователи, группы, два пространства, страницы с ограничениями чтения на
разных уровнях и вложения. Всё выдумано, адреса — example.com. Ожидания
для tests/live/test_confluence_dc_live.py — здесь же (EXPECTED_READERS):
кто по правилам Confluence может прочитать каждую страницу.

    CONFLUENCE_URL=http://127.0.0.1:8090 CONFLUENCE_ADMIN_TOKEN=… \\
        uv run python -m tests.live.confluence_dc.seed

Токен администратора печатает wizard.mjs (мастер первого запуска). Скрипт
рассчитан на чистый Confluence: повторный запуск упадёт на занятых именах.
"""

import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial

import httpx

from tests.ingest import samples

PASSWORD = "Kronto-Test-1"
"""Пароль всех тестовых пользователей (выдуманная компания в контейнере)."""
SERVICE = "svc-kronto"
"""Служебная учётка подключения: токен выдаёт token.mjs."""
ADMIN = "admin"
"""Администратор из мастера: Confluence не даёт ограничить страницу так,
чтобы её автор (здесь — он) потерял доступ, поэтому он есть в каждом
ограничении — как автор страницы в жизни."""
EMAIL_TEMPLATE = "{username}@example.com"

USERS = {
    "alice": ["hr"],
    "bob": ["engineering"],
    "carol": ["hr", "engineering"],
    "dave": [],
    SERVICE: ["hr", "engineering"],
}
"""Пользователь → группы. Служебная учётка — в обеих группах: иначе она
сама не видит закрытые страницы и не может их проиндексировать."""

SPACES = {"HR": "Кадры", "ENG": "Разработка"}
"""Ключ → название. Главную страницу («Кадры Home») Confluence создаёт сам
вместе с пространством: она без ограничений и тоже попадает в индекс."""


@dataclass
class Page:
    title: str
    space: str
    body: str
    parent: str | None = None
    users: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    attachment: str | None = None
    """Имя вложения: .docx или .md — оба формата принимает ingest."""


PAGES = [
    Page(
        "Отпуск",
        "HR",
        "<p>Отпуск — 28 календарных дней.</p>",
        attachment="Заявление.docx",
    ),
    Page(
        "Зарплаты",
        "HR",
        "<p>Оклады пересматриваются в марте.</p>",
        groups=["hr"],
        attachment="Сетка.md",
    ),
    Page("Премии", "HR", "<p>Премия — до 20 % оклада.</p>", parent="Зарплаты"),
    Page(
        "Премия Боба",
        "HR",
        "<p>Бобу — разовая премия.</p>",
        parent="Зарплаты",
        users=["bob", SERVICE],
    ),
    Page(
        "Только Алисе",
        "HR",
        "<p>Служебная учётка этой страницы не видит.</p>",
        users=["alice"],
    ),
    Page("Архитектура", "ENG", "<h2>Схема</h2><p>Монолит и воркер.</p>"),
    Page(
        "Инциденты",
        "ENG",
        "<p>Разбор инцидентов недели.</p>",
        users=["dave"],
        groups=["engineering"],
    ),
]

EXPECTED_READERS: dict[str, frozenset[str] | None] = {
    # None — ограничений нет: документ виден всей компании (пространство
    # выбрал админ подключения, RISKS №37).
    **{f"{name} Home": None for name in SPACES.values()},
    "Отпуск": None,
    "Зарплаты": frozenset({"alice", "carol", SERVICE, ADMIN}),
    # Наследует ограничение родителя.
    "Премии": frozenset({"alice", "carol", SERVICE, ADMIN}),
    # Пересечение по цепочке: bob не в hr — читать не может.
    "Премия Боба": frozenset({SERVICE, ADMIN}),
    "Архитектура": None,
    # Пользователь и группа одного ограничения — объединение.
    "Инциденты": frozenset({"bob", "carol", "dave", SERVICE, ADMIN}),
}
"""«Только Алисе» здесь нет: служебная учётка её не видит, и адаптер её не
должен вернуть."""
HIDDEN_FROM_SERVICE = ("Только Алисе",)


ATTACHMENTS = {
    "Заявление.docx": samples.docx(
        [("Заявление на отпуск подаётся за две недели.", None)]
    ),
    "Сетка.md": "# Сетка окладов\n\nГрейд 1 — 100 000 рублей.\n".encode(),
}


class Seeder:
    def __init__(self, base_url: str, token: str) -> None:
        headers = {"Authorization": f"Bearer {token}", "X-Atlassian-Token": "no-check"}
        root = self.root = base_url.rstrip("/")
        self.http = httpx.Client(
            base_url=f"{root}/rest/api", headers=headers, timeout=60
        )
        self.rpc = httpx.Client(
            base_url=f"{root}/rpc/json-rpc/confluenceservice-v2",
            headers=headers,
            timeout=60,
        )
        self.legacy = False
        """7.x: пользователей и группы REST не ведёт (появилось в 8.x) — JSON-RPC."""
        self.ids: dict[str, str] = {}

    def _check(self, response: httpx.Response) -> httpx.Response:
        if response.status_code >= 300:
            raise RuntimeError(
                f"{response.request.method} {response.request.url.path}: "
                f"HTTP {response.status_code} {response.text[:300]}"
            )
        return response

    def _admin(
        self, rest: Callable[[], httpx.Response], method: str, *params: object
    ) -> None:
        """Управление пользователями: REST (8.x+), а где его нет — JSON-RPC."""
        if not self.legacy:
            response = rest()
            if response.status_code != 404:
                self._check(response)
                return
            self.legacy = True
        response = self._check(self.rpc.post(f"/{method}", json=list(params)))
        answer = response.json() if response.content else None
        if isinstance(answer, dict) and answer.get("error"):
            raise RuntimeError(f"{method}: {answer['error']}")

    def users(self) -> None:
        for group in sorted({g for groups in USERS.values() for g in groups}):
            self._admin(
                partial(
                    self.http.post,
                    "/admin/group",
                    json={"type": "group", "name": group},
                ),
                "addGroup",
                group,
            )
        for username, groups in USERS.items():
            email = EMAIL_TEMPLATE.replace("{username}", username)
            self._admin(
                partial(
                    self.http.post,
                    "/admin/user",
                    json={
                        "userName": username,
                        "fullName": username.capitalize(),
                        "email": email,
                        "password": PASSWORD,
                        "notifyViaEmail": False,
                    },
                ),
                "addUser",
                {"name": username, "fullname": username.capitalize(), "email": email},
                PASSWORD,
            )
            for group in groups:
                self.join(username, group)

    def spaces(self) -> None:
        for key, name in SPACES.items():
            self._check(self.http.post("/space", json={"key": key, "name": name}))

    def pages(self) -> None:
        for page in PAGES:
            self.create_page(page)

    def create_page(self, page: Page) -> str:
        data: dict[str, object] = {
            "type": "page",
            "title": page.title,
            "space": {"key": page.space},
            "body": {"storage": {"value": page.body, "representation": "storage"}},
        }
        if page.parent:
            data["ancestors"] = [{"id": self.ids[page.parent]}]
        created = self._check(self.http.post("/content", json=data)).json()
        page_id = self.ids[page.title] = str(created["id"])
        if page.users or page.groups:
            self.restrict(page_id, users=page.users, groups=page.groups)
        if page.attachment:
            self._attach(page_id, page.attachment)
        return page_id

    # --- правки поверх засеянного: тест отзыва прав ---------------------------

    def page_id(self, space: str, title: str) -> str:
        found = self._check(
            self.http.get("/content", params={"spaceKey": space, "title": title})
        ).json()["results"]
        return str(found[0]["id"])

    def restrict(
        self, page_id: str, *, users: Sequence[str] = (), groups: Sequence[str] = ()
    ) -> None:
        self._read_restriction(
            page_id,
            users=[{"type": "known", "username": u} for u in [*users, ADMIN]],
            groups=[{"type": "group", "name": g} for g in groups],
        )

    def unrestrict(self, page_id: str) -> None:
        # DELETE …/restriction в Confluence 10 — 405; пустой PUT снимает.
        self._read_restriction(page_id, users=[], groups=[])

    def _read_restriction(
        self, page_id: str, *, users: list[dict[str, str]], groups: list[dict[str, str]]
    ) -> None:
        body = [{"operation": "read", "restrictions": {"user": users, "group": groups}}]
        response = self.http.put(f"/content/{page_id}/restriction", json=body)
        if response.status_code == 405:
            # 7.x: запись ограничений — только в experimental (чтение — в api).
            response = self.http.put(
                f"{self.root}/rest/experimental/content/{page_id}/restriction",
                json=body,
            )
        self._check(response)

    def join(self, username: str, group: str) -> None:
        self._admin(
            partial(self.http.put, f"/user/{username}/group/{group}"),
            "addUserToGroup",
            username,
            group,
        )

    def leave(self, username: str, group: str) -> None:
        self._admin(
            partial(self.http.delete, f"/user/{username}/group/{group}"),
            "removeUserFromGroup",
            username,
            group,
        )

    def trash(self, page_id: str) -> None:
        self._check(self.http.delete(f"/content/{page_id}"))

    def trash_and_purge(self, page_id: str) -> None:
        """Удалить насовсем — и из корзины; уже удалённую — только из неё."""
        current = self.http.get(f"/content/{page_id}")
        if current.status_code == 200:
            self.trash(page_id)
        self._check(
            self.http.delete(f"/content/{page_id}", params={"status": "trashed"})
        )

    def close(self) -> None:
        self.http.close()
        self.rpc.close()

    def _attach(self, page_id: str, name: str) -> None:
        self._check(
            self.http.post(
                f"/content/{page_id}/child/attachment",
                files={"file": (name, ATTACHMENTS[name])},
            )
        )


def main() -> int:
    base_url = os.environ.get("CONFLUENCE_URL", "http://127.0.0.1:8090")
    token = os.environ.get("CONFLUENCE_ADMIN_TOKEN")
    if not token:
        print("нужен CONFLUENCE_ADMIN_TOKEN (печатает wizard.mjs)", file=sys.stderr)
        return 64
    seeder = Seeder(base_url, token)
    seeder.users()
    seeder.spaces()
    seeder.pages()
    print(f"готово: {len(USERS)} пользователей, {len(PAGES)} страниц")
    return 0


if __name__ == "__main__":
    sys.exit(main())
