"""Выдуманная компания в Seafile для живой проверки вида `seafile`.

Пользователи (вход — почта), группа и библиотеки — через REST API Seafile
(api2 и api/v2.1), файлы — по SeafDAV от имени владельца. У ivan
библиотека «Проекты», расшаренная maria, и «Кадры», расшаренная группе
hr (maria, petr); у ivan и maria — свои «Личное» с файлом по одинаковому
пути. Ожидания для tests/live/test_seafile_live.py — здесь же (EXPECTED).

    SEAFILE_URL=https://127.0.0.1:8446/ DAV_STAND_CA=~/dav-tls/cert.pem \\
        python -m tests.live.seafile.seed

Скрипт рассчитан на чистый стенд: библиотеки создаются заново.
"""

import os
import sys
from dataclasses import dataclass

import httpx

from tests.ingest import samples
from tests.live.dav_stand import CA, PASSWORD, DavSeeder, trust

ADMIN = "admin@example.com"
ADMIN_PASSWORD = "Kronto-Admin-1"
"""Администратор из compose.yaml (выдуманный пароль стенда)."""
GROUP = "hr"
USERS = {"ivan": [], "maria": [GROUP], "petr": [GROUP]}
"""Логин (почта — <логин>@example.com) → группы."""
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def email(login: str) -> str:
    return f"{login}@example.com"


@dataclass(frozen=True)
class File:
    owner: str
    path: str
    """От корня SeafDAV: первая часть — библиотека."""
    data: bytes


FILES = [
    File(
        "ivan",
        "Личное/Отпуск.docx",
        samples.docx([("Отпуск Ивана — 28 календарных дней.", None)]),
    ),
    File("ivan", "Личное/Схема.png", PNG),
    File("ivan", "Проекты/План.md", "# План\n\nЗапуск — в мае.\n".encode()),
    File(
        "ivan",
        "Кадры/Регламент.pdf",
        samples.pdf([[("HR policy", 20), ("Probation lasts three months.", 12)]]),
    ),
    File("ivan", "Кадры/Глубже/Анкета.txt", "Анкета нового сотрудника.\n".encode()),
    File(
        "maria",
        "Личное/Отпуск.docx",
        samples.docx([("Мария: отпуск в июле.", None)]),
    ),
]
LIBRARIES = {"ivan": ["Личное", "Проекты", "Кадры"], "maria": ["Личное"]}
SHARES = [("Проекты", "user", "maria"), ("Кадры", "group", GROUP)]
"""Библиотеки ivan: кому открыты (только чтение)."""

EXPECTED: dict[tuple[str, str], set[str]] = {
    ("Отпуск.docx", "Отпуск Ивана"): {"ivan"},
    ("Отпуск.docx", "в июле"): {"maria"},
    ("План.md", "в мае"): {"ivan", "maria"},
    ("Регламент.pdf", "three months"): {"ivan", "maria", "petr"},
    ("Анкета.txt", "нового сотрудника"): {"ivan", "maria", "petr"},
}
"""(название, кусок текста) → кто видит. У Seafile сквозного номера файла
в WebDAV нет: общий файл — копия на каждого, кто его видит, и каждая —
только ему. Схема.png не индексируется (формат)."""


def dav_root(url: str) -> str:
    return f"{url.rstrip('/')}/seafdav/"


class SeafileApi:
    def __init__(self, url: str, login: str, password: str, ca: str = CA) -> None:
        self.url = url.rstrip("/")
        self._http = httpx.Client(verify=trust(ca), timeout=60)
        response = self._http.post(
            f"{self.url}/api2/auth-token/",
            data={"username": login, "password": password},
        )
        response.raise_for_status()
        self._http.headers["Authorization"] = f"Token {response.json()['token']}"
        self._http.headers["Accept"] = "application/json"

    def close(self) -> None:
        self._http.close()

    def call(self, method: str, path: str, **kwargs: object) -> object:
        response = self._http.request(method, f"{self.url}{path}", **kwargs)  # type: ignore[arg-type]
        response.raise_for_status()
        return response.json() if response.content else None

    def add_user(self, login: str) -> None:
        self.call(
            "POST",
            "/api/v2.1/admin/users/",
            data={"email": email(login), "password": PASSWORD, "name": login},
        )

    def user_ids(self) -> dict[str, str]:
        """Логин → внутренний id Seafile (…@auth.local). С Seafile 11 почта —
        только адрес для входа и писем, группы и шары ждут внутренний id."""
        users = self.call("GET", "/api/v2.1/admin/users/", params={"per_page": 100})
        return {
            str(u["contact_email"]).split("@")[0]: str(u["email"])
            for u in users["data"]  # type: ignore[index]
        }

    def add_group(self, name: str, members: list[str]) -> int:
        group = self.call("POST", "/api/v2.1/groups/", data={"name": name})
        group_id = int(group["id"])  # type: ignore[index]
        for member in members:
            self.call(
                "POST", f"/api/v2.1/groups/{group_id}/members/", data={"email": member}
            )
        return group_id

    def group_id(self, name: str) -> int:
        groups = self.call("GET", "/api/v2.1/groups/")
        return next(int(g["id"]) for g in groups if g["name"] == name)  # type: ignore[union-attr]

    def add_library(self, name: str) -> str:
        repo = self.call("POST", "/api2/repos/", data={"name": name})
        return str(repo["repo_id"])  # type: ignore[index]

    def library(self, name: str) -> str:
        repos = self.call("GET", "/api2/repos/", params={"type": "mine"})
        return next(str(r["id"]) for r in repos if r["name"] == name)  # type: ignore[union-attr]

    def share(self, library: str, kind: str, to: str) -> None:
        """to — внутренний id пользователя или имя группы."""
        target = (
            {"share_type": "user", "username": to}
            if kind == "user"
            else {"share_type": "group", "group_id": str(self.group_id(to))}
        )
        self.call(
            "PUT",
            f"/api2/repos/{self.library(library)}/dir/shared_items/",
            params={"p": "/"},
            data={**target, "permission": "r"},
        )

    def unshare(self, library: str, kind: str, to: str) -> None:
        target = (
            {"share_type": "user", "username": to}
            if kind == "user"
            else {"share_type": "group", "group_id": str(self.group_id(to))}
        )
        self.call(
            "DELETE",
            f"/api2/repos/{self.library(library)}/dir/shared_items/",
            params={"p": "/", **target},
        )


def user_ids(url: str) -> dict[str, str]:
    admin = SeafileApi(url, ADMIN, ADMIN_PASSWORD)
    try:
        return admin.user_ids()
    finally:
        admin.close()


def seed(url: str) -> None:
    admin = SeafileApi(url, ADMIN, ADMIN_PASSWORD)
    try:
        known = admin.user_ids()
        for login in USERS:
            if login not in known:
                admin.add_user(login)
        ids = admin.user_ids()
    finally:
        admin.close()
    owners = {login: SeafileApi(url, email(login), PASSWORD) for login in LIBRARIES}
    try:
        owners["ivan"].add_group(
            GROUP, [ids[login] for login, groups in USERS.items() if GROUP in groups]
        )
        for login, libraries in LIBRARIES.items():
            for name in libraries:
                owners[login].add_library(name)
        for file in FILES:
            dav = DavSeeder(dav_root(url), email(file.owner), PASSWORD)
            try:
                dav.put(file.path, file.data)
            finally:
                dav.close()
        for library, kind, to in SHARES:
            owners["ivan"].share(library, kind, ids[to] if kind == "user" else to)
    finally:
        for api in owners.values():
            api.close()


if __name__ == "__main__":
    address = os.environ.get("SEAFILE_URL", "")
    if not address:
        sys.exit("нужен SEAFILE_URL (https://127.0.0.1:8446/)")
    seed(address)
    print(f"пользователей: {len(USERS)}, файлов: {len(FILES)}, шар: {len(SHARES)}")
