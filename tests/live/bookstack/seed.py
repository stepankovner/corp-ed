"""Выдуманная компания в BookStack для живой проверки вида `bookstack`.

Первый токен (администратора) API выдать не может — его создаёт форма
профиля; скрипт входит в BookStack как администратор по умолчанию
(admin@admin.com / password, так ставится BookStack) и заполняет ту же
форму. Дальше всё через API: почта администратора, роли, пользователи,
книги, главы, страницы, права содержимого, вложения — и токены
сотрудников (администратор может выпустить их за любого): тесты
сверяют права адаптера с тем, что BookStack отдаёт самому сотруднику.

    BOOKSTACK_URL=https://127.0.0.1:8460/ DAV_STAND_CA=~/dav-tls/cert.pem \\
        python -m tests.live.bookstack.seed ~/bookstack-stand.json

В файл (вне репозитория) пишутся токены: admin — для подключения, у
остальных — для сверки. Скрипт рассчитан на чистый стенд.

Компания: все — в роли «Сотрудники» (без системного права просмотра:
только «Access System API»); ivan ещё и «Редакторы» (просмотр всего по
умолчанию), maria — «Кадры», olga — «Авторы» (просмотр только своих
страниц), petr — только «Сотрудники».
"""

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from tests.live.dav_stand import CA, PASSWORD, trust

DEFAULT_ADMIN = ("admin@admin.com", "password")
ADMIN_EMAIL = "admin@example.com"
USERS = ("ivan", "maria", "petr", "olga")
"""Сотрудники (почта — <логин>@example.com); admin — владелец токена."""
EVERYONE = frozenset({"admin", *USERS})

ROLES: dict[str, tuple[str, ...]] = {
    "Сотрудники": ("access-api",),
    "Редакторы": (
        "access-api",
        "book-view-all",
        "chapter-view-all",
        "page-view-all",
    ),
    "Кадры": ("access-api",),
    "Авторы": ("access-api", "book-view-all", "page-view-own"),
}
MEMBERS: dict[str, tuple[str, ...]] = {
    "admin": ("Сотрудники",),
    "ivan": ("Сотрудники", "Редакторы"),
    "maria": ("Сотрудники", "Кадры"),
    "petr": ("Сотрудники",),
    "olga": ("Сотрудники", "Авторы"),
}
"""Логин → роли сверх Admin (у admin она уже есть)."""


@dataclass(frozen=True)
class Grant:
    """Права содержимого: роль → «просмотр»; fallback — «Все остальные»."""

    roles: dict[str, bool] = field(default_factory=dict)
    fallback: bool | None = None


@dataclass(frozen=True)
class Page:
    book: str
    name: str
    text: str
    chapter: str | None = None
    grant: Grant | None = None
    owner: str | None = None


BOOKS: dict[str, Grant | None] = {
    # Общая книга: «Сотрудники» (все) — явное право.
    "Общая": Grant({"Сотрудники": True}),
    # Закрытая книга для роли «Кадры».
    "Кадры": Grant({"Кадры": True}, fallback=False),
    # Без прав содержимого: системные права ролей.
    "Проекты": None,
}
CHAPTERS = {"Командировки": "Общая"}
PAGES = [
    Page("Общая", "Отпуск", "Отпуск — 28 календарных дней."),
    Page("Общая", "Суточные", "Суточные — 700 рублей.", chapter="Командировки"),
    # Переопределение на странице общей книги: только «Кадры».
    Page(
        "Общая",
        "Премии",
        "Премии выплачиваются раз в квартал.",
        grant=Grant({"Кадры": True}, fallback=False),
    ),
    Page("Кадры", "Зарплаты", "Оклады пересматриваются в марте."),
    # Страница открыта всем, хотя её книга закрыта.
    Page(
        "Кадры",
        "Оргструктура",
        "Отделов в компании четыре.",
        grant=Grant({"Сотрудники": True}),
    ),
    # Явный запрет «Сотрудникам» перевешивает их право, кроме «Кадров»
    # (явное право по книге).
    Page(
        "Кадры",
        "Увольнения",
        "Обходной лист — за три дня.",
        grant=Grant({"Сотрудники": False}),
    ),
    Page("Проекты", "План", "Запуск — в мае."),
    # «Своё»: olga видит только свои страницы.
    Page("Проекты", "Заметки автора", "Черновые мысли о запуске.", owner="olga"),
    # Явный запрет «Сотрудникам» перевешивает «Редакторов» у ivan.
    Page(
        "Проекты",
        "Смета",
        "Смета согласована.",
        grant=Grant({"Сотрудники": False}),
    ),
]
EXPECTED: dict[str, frozenset[str] | None] = {
    "Отпуск": None,
    "Суточные": None,
    "Премии": frozenset({"admin", "maria"}),
    "Зарплаты": frozenset({"admin", "maria"}),
    "Оргструктура": None,
    "Увольнения": frozenset({"admin", "maria"}),
    "План": frozenset({"admin", "ivan"}),
    "Заметки автора": frozenset({"admin", "ivan", "olga"}),
    "Смета": frozenset({"admin"}),
}
"""Название → кто видит (None — все: роль «Сотрудники» и право у всех)."""
TEXT = {page.name: page.text for page in PAGES}
PATHS = {
    page.name: "/".join(p for p in (page.book, page.chapter) if p) for page in PAGES
}
ATTACHMENTS = {
    "памятка.txt": ("Отпуск", "Памятка: заявление за две недели."),
    "ведомость.txt": ("Зарплаты", "Ведомость за сентябрь."),
}
"""Файл → (страница, текст); ещё одно вложение-ссылка не индексируется."""


def email(login: str) -> str:
    return ADMIN_EMAIL if login == "admin" else f"{login}@example.com"


class BookStackWeb:
    """Вход в BookStack и форма выпуска API-токена (сессия и CSRF)."""

    def __init__(self, url: str, ca: str = CA) -> None:
        self.url = url.rstrip("/")
        self._http = httpx.Client(verify=trust(ca), timeout=60, follow_redirects=True)

    def close(self) -> None:
        self._http.close()

    def login(self, login: str, password: str) -> None:
        token = _csrf(self._http.get(f"{self.url}/login").text)
        response = self._http.post(
            f"{self.url}/login",
            data={"_token": token, "email": login, "password": password},
        )
        if "/login" in str(response.url):
            raise RuntimeError(f"вход {login} не принят")

    def api_token(self, user_id: int, name: str = "kronto") -> tuple[str, str]:
        """Новый токен пользователя → (Token ID, Token Secret)."""
        form = f"{self.url}/api-tokens/{user_id}/create"
        token = _csrf(self._http.get(form).text)
        page = self._http.post(form, data={"_token": token, "name": name}).text
        found_id = re.search(r'name="token_id"[^>]*value="([^"]+)"', page) or re.search(
            r'value="([^"]+)"[^>]*name="token_id"', page
        )
        secret = re.search(r'readonly="readonly" value="([^"]+)"', page)
        if not found_id or not secret:
            raise RuntimeError(f"токен для пользователя {user_id} не выпущен")
        return found_id.group(1), secret.group(1)


def _csrf(html: str) -> str:
    found = re.search(r'name="_token" value="([^"]+)"', html)
    if not found:
        raise RuntimeError("нет CSRF-токена на странице")
    return found.group(1)


class BookStackApi:
    def __init__(self, url: str, token_id: str, secret: str, ca: str = CA) -> None:
        self.url = url.rstrip("/")
        self._http = httpx.Client(
            verify=trust(ca),
            timeout=60,
            headers={
                "Authorization": f"Token {token_id}:{secret}",
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        self._http.close()

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._http.request(method, f"{self.url}/api/{path}", **kwargs)
        if response.status_code not in (200, 201, 204):
            raise RuntimeError(
                f"{method} {path}: HTTP {response.status_code} {response.text[:300]}"
            )
        return response.json() if response.content else None

    def listing(self, path: str) -> list[dict[str, Any]]:
        data = self.call("GET", path, params={"count": 500})
        return list(data["data"])

    def find(self, path: str, name: str, key: str = "name") -> dict[str, Any]:
        return next(item for item in self.listing(path) if item[key] == name)

    def users(self) -> dict[str, int]:
        """Логин (часть почты до @) → id; admin — по своей почте."""
        found = {}
        for user in self.listing("users"):
            login = str(user["email"]).split("@")[0]
            found["admin" if user["email"] == ADMIN_EMAIL else login] = int(user["id"])
        return found

    def roles(self) -> dict[str, int]:
        return {str(r["display_name"]): int(r["id"]) for r in self.listing("roles")}

    def grant(self, kind: str, entity_id: int, grant: Grant) -> None:
        roles = self.roles()
        body: dict[str, Any] = {
            "role_permissions": [
                {
                    "role_id": roles[name],
                    "view": view,
                    "create": False,
                    "update": False,
                    "delete": False,
                }
                for name, view in grant.roles.items()
            ],
            "fallback_permissions": (
                {"inheriting": True}
                if grant.fallback is None
                else {
                    "inheriting": False,
                    "view": grant.fallback,
                    "create": False,
                    "update": False,
                    "delete": False,
                }
            ),
        }
        self.call("PUT", f"content-permissions/{kind}/{entity_id}", json=body)


def seed(url: str, out: Path) -> None:
    web = BookStackWeb(url)
    try:
        web.login(*DEFAULT_ADMIN)
        admin_id = 1
        admin_token = web.api_token(admin_id)
        api = BookStackApi(url, *admin_token)
        try:
            api.call("PUT", f"users/{admin_id}", json={"email": ADMIN_EMAIL})
            for name, permissions in ROLES.items():
                api.call(
                    "POST",
                    "roles",
                    json={"display_name": name, "permissions": list(permissions)},
                )
            roles = api.roles()
            admin_roles = [roles["Admin"], *(roles[r] for r in MEMBERS["admin"])]
            api.call("PUT", f"users/{admin_id}", json={"roles": admin_roles})
            for login in USERS:
                api.call(
                    "POST",
                    "users",
                    json={
                        "name": login.capitalize(),
                        "email": email(login),
                        "password": PASSWORD,
                        "roles": [roles[r] for r in MEMBERS[login]],
                    },
                )
            users = api.users()
            books = {}
            for name, grant in BOOKS.items():
                books[name] = int(api.call("POST", "books", json={"name": name})["id"])
                if grant is not None:
                    api.grant("book", books[name], grant)
            chapters = {
                name: int(
                    api.call(
                        "POST", "chapters", json={"book_id": books[book], "name": name}
                    )["id"]
                )
                for name, book in CHAPTERS.items()
            }
            pages = {}
            for page in PAGES:
                parent = (
                    {"chapter_id": chapters[page.chapter]}
                    if page.chapter
                    else {"book_id": books[page.book]}
                )
                created = api.call(
                    "POST",
                    "pages",
                    json={**parent, "name": page.name, "html": f"<p>{page.text}</p>"},
                )
                pages[page.name] = int(created["id"])
                if page.grant is not None:
                    api.grant("page", pages[page.name], page.grant)
                if page.owner is not None:
                    api.call(
                        "PUT",
                        f"content-permissions/page/{pages[page.name]}",
                        json={"owner_id": users[page.owner]},
                    )
            for filename, (page_name, text) in ATTACHMENTS.items():
                api.call(
                    "POST",
                    "attachments",
                    data={"name": filename, "uploaded_to": str(pages[page_name])},
                    files={"file": (filename, text.encode(), "text/plain")},
                )
            api.call(
                "POST",
                "attachments",
                json={
                    "name": "Сайт компании",
                    "uploaded_to": pages["Отпуск"],
                    "link": "https://example.com/",
                },
            )
            tokens = {"admin": admin_token}
            for login in USERS:
                tokens[login] = web.api_token(users[login])
        finally:
            api.close()
    finally:
        web.close()
    out.write_text(
        json.dumps(
            {
                "url": url,
                "tokens": {k: {"id": i, "secret": s} for k, (i, s) in tokens.items()},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    out.chmod(0o600)


def main() -> int:
    url = os.environ.get("BOOKSTACK_URL", "")
    if not url or not CA or len(sys.argv) != 2:
        print(
            "нужны BOOKSTACK_URL, DAV_STAND_CA и путь к файлу токенов",
            file=sys.stderr,
        )
        return 2
    out = Path(sys.argv[1]).expanduser()
    seed(url, out)
    print(f"засеяно: {url}; токены — {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
