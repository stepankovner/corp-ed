"""Выдуманная компания в Wiki.js 2.x для живой проверки вида `wikijs`.

Всё — через GraphQL (`/graphql`) от имени администратора: завершение
установки (`/finalize`), включение API и два ключа (полный доступ и
ключ группы «Сотрудники» — для проверки, что такой не принимается),
пользователи, группы с правилами страниц, страницы на двух локалях.
Локаль ru добавляется строкой в таблицу `locales` (скачать её стенд не
может: нет выхода к graph.requarks.io), через `docker compose exec`.
Ожидания для tests/live/test_wikijs_live.py — здесь же (EXPECTED).

    WIKIJS_URL=https://127.0.0.1:8470/ DAV_STAND_CA=~/dav-tls/cert.pem \\
        python -m tests.live.wikijs.seed > ~/wikijs-keys.env

Печатает `WIKIJS_TOKEN=…` и `WIKIJS_MEMBER_TOKEN=…` (ключи стенда, в
репозиторий не попадают). Рассчитан на чистый стенд.
"""

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from tests.live.dav_stand import CA, PASSWORD, trust

ADMIN = "admin"
USERS = ("ivan", "maria", "petr", "olga")
"""Сотрудники (почта — <логин>@example.com); admin — администратор Wiki.js,
olga — отключена (isActive = false) и не видит ничего."""
COMPOSE = Path(__file__).with_name("compose.yaml")


@dataclass(frozen=True)
class Rule:
    match: str
    path: str
    deny: bool = False
    locales: tuple[str, ...] = ()


READ = ["read:pages", "read:assets", "read:comments"]
GUEST_RULES = [Rule("START", "public")]
"""Гостям (без входа) — только раздел public: это «вся компания»."""
GROUPS: dict[str, tuple[list[str], tuple[str, ...], list[Rule]]] = {
    # Название → (права группы, участники, правила страниц).
    "Сотрудники": (
        READ,
        ("ivan", "maria", "petr", "olga"),
        [
            Rule("START", ""),
            Rule("START", "hr", deny=True),
            Rule("START", "drafts", deny=True, locales=("ru",)),
            Rule("TAG", "confidential", deny=True),
            Rule("START", "reports", deny=True),
        ],
    ),
    # «hr/» длиннее запрета «hr» у сотрудников — разрешение побеждает;
    # при равной длине победил бы запрет.
    "Кадры": (
        ["read:pages"],
        ("maria",),
        [Rule("START", "hr/"), Rule("REGEX", r"^reports/\d{4}$")],
    ),
    # Прав у группы нет, только запрет: права petr — от «Сотрудников».
    "Подрядчики": ([], ("petr",), [Rule("START", "projects", deny=True)]),
}


@dataclass(frozen=True)
class Page:
    path: str
    title: str
    text: str
    locale: str = "en"
    tags: tuple[str, ...] = ()
    published: bool = True


PAGES = [
    Page("public/welcome", "Добро пожаловать", "Справочник открыт без входа."),
    Page("general/vacation", "Отпуск", "Отпуск — 28 календарных дней."),
    Page("hr/salaries", "Зарплаты", "Оклады пересматриваются в марте."),
    Page("projects/plan", "План проектов", "Запуск — в мае."),
    Page(
        "projects/roadmap",
        "Дорожная карта",
        "Слияние с партнёром во втором квартале.",
        tags=("confidential",),
    ),
    Page("reports/2026", "Отчёт 2026", "Выручка выросла на треть."),
    Page("drafts/idea", "Идея", "Черновик идеи для всех."),
    Page("drafts/idea", "Черновик", "Русский черновик закрыт.", locale="ru"),
    Page("general/otpusk", "Отпуск по-русски", "Заявление — за две недели.", "ru"),
    Page("general/hidden", "Неопубликованная", "Ещё не готово.", published=False),
]

EXPECTED: dict[tuple[str, str], set[str] | None] = {
    ("en", "public/welcome"): None,
    ("en", "general/vacation"): {"admin", "ivan", "maria", "petr"},
    ("en", "hr/salaries"): {"admin", "maria"},
    ("en", "projects/plan"): {"admin", "ivan", "maria"},
    ("en", "projects/roadmap"): {"admin"},
    ("en", "reports/2026"): {"admin", "maria"},
    ("en", "drafts/idea"): {"admin", "ivan", "maria", "petr"},
    ("ru", "drafts/idea"): {"admin"},
    ("ru", "general/otpusk"): {"admin", "ivan", "maria", "petr"},
}
"""(локаль, путь) → кто читает (None — гости, то есть вся компания).
Неопубликованная страница не индексируется; главная home, если её
создать, — тоже None (раздел гостей ""-START здесь сужен до public)."""
TEXT = {(p.locale, p.path): p.text for p in PAGES}
TITLES = {(p.locale, p.path): p.title for p in PAGES}


def email(login: str) -> str:
    return f"{login}@example.com"


class WikiApi:
    """GraphQL Wiki.js от имени администратора (JWT входа или API-ключ)."""

    def __init__(self, url: str, ca: str = CA) -> None:
        self.url = url.rstrip("/")
        self._http = httpx.Client(verify=trust(ca), timeout=60)
        self.token = ""

    def close(self) -> None:
        self._http.close()

    def query(self, query: str, /, **variables: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        response = self._http.post(
            f"{self.url}/graphql",
            json={"query": query, "variables": variables},
            headers=headers,
        )
        body = response.json()
        if response.status_code != 200 or body.get("errors"):
            raise RuntimeError(f"HTTP {response.status_code}: {str(body)[:300]}")
        return body["data"]  # type: ignore[no-any-return]

    def mutate(self, query: str, at: tuple[str, ...], /, **variables: Any) -> Any:
        """Мутация с responseResult (at — путь к нему): неуспех — исключение."""
        data: Any = self.query(query, **variables)
        for key in at:
            data = data[key]
        result = data["responseResult"]
        if not result["succeeded"]:
            raise RuntimeError(f"{at}: {result['message']}")
        return data

    def login(self, login: str = ADMIN, password: str = PASSWORD) -> None:
        data = self.mutate(
            """mutation ($u: String!, $p: String!) { authentication {
                 login(username: $u, password: $p, strategy: "local") {
                   responseResult { succeeded message } jwt } } }""",
            ("authentication", "login"),
            u=email(login),
            p=password,
        )
        self.token = data["jwt"]

    def users(self) -> dict[str, int]:
        data = self.query("{ users { list { id email } } }")
        return {u["email"].split("@")[0]: u["id"] for u in data["users"]["list"]}

    def groups(self) -> dict[str, int]:
        data = self.query("{ groups { list { id name } } }")
        return {g["name"]: g["id"] for g in data["groups"]["list"]}

    def group(self, group_id: int) -> dict[str, Any]:
        data = self.query(
            """query ($id: Int!) { groups { single(id: $id) {
                 id name redirectOnLogin permissions
                 pageRules { id deny match roles path locales } } } }""",
            id=group_id,
        )
        return data["groups"]["single"]  # type: ignore[no-any-return]

    def update_group(
        self,
        group_id: int,
        name: str,
        permissions: list[str],
        rules: list[dict[str, Any]],
    ) -> None:
        self.mutate(
            """mutation ($id: Int!, $name: String!, $perms: [String]!,
                         $rules: [PageRuleInput]!) { groups {
                 update(id: $id, name: $name, redirectOnLogin: "/",
                        permissions: $perms, pageRules: $rules) {
                   responseResult { succeeded message } } } }""",
            ("groups", "update"),
            id=group_id,
            name=name,
            perms=permissions,
            rules=rules,
        )

    def pages(self) -> dict[tuple[str, str], int]:
        data = self.query("{ pages { list(limit: 1000) { id path locale } } }")
        return {(p["locale"], p["path"]): p["id"] for p in data["pages"]["list"]}

    def update_page(
        self, page_id: int, title: str, content: str, tags: tuple[str, ...] = ()
    ) -> None:
        """Новый текст страницы. Wiki.js 2.5 переписывает страницу целиком:
        без isPublished она станет неопубликованной, без tags — ошибка
        «Cannot read properties of undefined (reading 'map')», хотя часть
        полей уже сохранена."""
        self.mutate(
            """mutation ($id: Int!, $title: String!, $content: String!,
                         $tags: [String]!) { pages {
                 update(id: $id, title: $title, content: $content, tags: $tags,
                        description: "", isPublished: true, isPrivate: false) {
                   responseResult { succeeded message } } } }""",
            ("pages", "update"),
            id=page_id,
            title=title,
            content=content,
            tags=list(tags),
        )


def rules(items: list[Rule]) -> list[dict[str, Any]]:
    return [
        {
            "id": f"r{n}",
            "deny": rule.deny,
            "match": rule.match,
            "roles": ["read:pages", "read:assets", "read:comments"],
            "path": rule.path,
            "locales": list(rule.locales),
        }
        for n, rule in enumerate(items)
    ]


def finalize(api: WikiApi) -> None:
    """Мастер установки: администратор и адрес сайта. Сервер после него
    перезапускается — ждём, пока GraphQL ответит."""
    http = httpx.Client(verify=trust(CA), timeout=120)
    try:
        for _ in range(60):
            try:
                if http.get(f"{api.url}/").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        response = http.post(
            f"{api.url}/finalize",
            json={
                "adminEmail": email(ADMIN),
                "adminPassword": PASSWORD,
                "adminPasswordConfirm": PASSWORD,
                "siteUrl": api.url,
                "telemetry": False,
            },
        )
        if not response.json().get("ok"):
            raise RuntimeError(f"finalize: {response.text[:300]}")
    finally:
        http.close()
    for _ in range(60):
        try:
            api.login()
            return
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError):
            time.sleep(2)
    raise RuntimeError("Wiki.js не поднялся после установки")


def add_locale(code: str, name: str, native: str) -> None:
    """Строка локали, как её пишет Wiki.js (даты — ISO-строкой)."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    sql = (
        'INSERT INTO locales (code, strings, "isRTL", name, "nativeName", '
        '"availability", "createdAt", "updatedAt") VALUES '
        f"('{code}', '{{}}', false, '{name}', '{native}', 100, "
        f"'{stamp}', '{stamp}') ON CONFLICT (code) DO NOTHING"
    )
    subprocess.run(  # noqa: S603 — фиксированные аргументы, стенд
        [  # noqa: S607
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "wikijs",
            "wiki",
            "-c",
            sql,
        ],
        # DAV_TLS_DIR — из окружения, как при `up`: без него compose не
        # разберёт файл.
        check=True,
        capture_output=True,
    )


def seed(url: str, *, install: bool = True) -> tuple[str, str]:
    api = WikiApi(url)
    try:
        if install:
            finalize(api)
        api.login()
        add_locale("ru", "Russian", "Русский")
        for login in USERS:
            api.mutate(
                """mutation ($e: String!, $n: String!, $p: String!) { users {
                     create(email: $e, name: $n, passwordRaw: $p,
                            providerKey: "local", groups: [],
                            mustChangePassword: false, sendWelcomeEmail: false) {
                       responseResult { succeeded message } } } }""",
                ("users", "create"),
                e=email(login),
                n=login,
                p=PASSWORD,
            )
        users = api.users()
        api.mutate(
            """mutation ($id: Int!) { users { deactivate(id: $id) {
                 responseResult { succeeded message } } } }""",
            ("users", "deactivate"),
            id=users["olga"],
        )
        api.update_group(2, "Guests", READ, rules(GUEST_RULES))
        for name, (permissions, members, items) in GROUPS.items():
            created = api.mutate(
                """mutation ($n: String!) { groups { create(name: $n) {
                     responseResult { succeeded message } group { id } } } }""",
                ("groups", "create"),
                n=name,
            )
            group_id = created["group"]["id"]
            api.update_group(group_id, name, permissions, rules(items))
            for login in members:
                api.mutate(
                    """mutation ($g: Int!, $u: Int!) { groups {
                         assignUser(groupId: $g, userId: $u) {
                           responseResult { succeeded message } } } }""",
                    ("groups", "assignUser"),
                    g=group_id,
                    u=users[login],
                )
        for page in PAGES:
            api.mutate(
                """mutation ($content: String!, $locale: String!, $path: String!,
                             $title: String!, $tags: [String]!, $pub: Boolean!) {
                     pages { create(content: $content, description: "",
                       editor: "markdown", isPublished: $pub, isPrivate: false,
                       locale: $locale, path: $path, tags: $tags, title: $title) {
                         responseResult { succeeded message } } } }""",
                ("pages", "create"),
                content=f"# {page.title}\n\n{page.text}\n",
                locale=page.locale,
                path=page.path,
                title=page.title,
                tags=list(page.tags),
                pub=page.published,
            )
        api.mutate(
            """mutation { authentication { setApiState(enabled: true) {
                 responseResult { succeeded message } } } }""",
            ("authentication", "setApiState"),
        )
        keys = []
        for name, full, group in (
            ("kronto", True, None),
            ("kronto-member", False, api.groups()["Сотрудники"]),
        ):
            created = api.mutate(
                """mutation ($n: String!, $f: Boolean!, $g: Int) { authentication {
                     createApiKey(name: $n, expiration: "1y", fullAccess: $f,
                                  group: $g) {
                       responseResult { succeeded message } key } } }""",
                ("authentication", "createApiKey"),
                n=name,
                f=full,
                g=group,
            )
            keys.append(created["key"])
        return keys[0], keys[1]
    finally:
        api.close()


if __name__ == "__main__":
    address = os.environ.get("WIKIJS_URL", "")
    if not address or not CA:
        sys.exit("нужны WIKIJS_URL (https://127.0.0.1:8470/) и DAV_STAND_CA")
    admin_key, member_key = seed(address, install="--no-install" not in sys.argv)
    print(f"WIKIJS_TOKEN={admin_key}")
    print(f"WIKIJS_MEMBER_TOKEN={member_key}")
