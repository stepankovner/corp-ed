"""Выдуманная компания в Nextcloud для живой проверки вида `nextcloud`.

Пользователи и группа — через OCS Provisioning API администратора,
файлы — по WebDAV от имени владельца, общий доступ — через OCS Share API
от имени ivan. Тем же кодом засевается ownCloud 10 (у него те же OCS и
WebDAV): tests/live/owncloud/.

    NEXTCLOUD_URL=https://127.0.0.1:8444/ DAV_STAND_CA=~/dav-tls/cert.pem \\
        python -m tests.live.nextcloud.seed

Ожидания для tests/live/test_nextcloud_live.py — здесь же (EXPECTED):
кто видит каждый файл. Скрипт рассчитан на чистый стенд: повторный
запуск не создаёт пользователей заново, но шарит файлы ещё раз —
только на чистом стенде.
"""

import os
import sys
from dataclasses import dataclass

import httpx

from tests.ingest import samples
from tests.live.dav_stand import CA, PASSWORD, DavSeeder, trust

ADMIN = "admin"
ADMIN_PASSWORD = "Kronto-Admin-1"
"""Администратор из compose.yaml (выдуманный пароль стенда)."""
GROUP = "hr"
USERS = {"ivan": [], "maria": [GROUP], "petr": [GROUP]}
"""Пользователь → группы. ivan — владелец общих файлов, в группу не входит."""

SHARE_USER = 0
SHARE_GROUP = 1
PERMISSION_READ = 1


@dataclass(frozen=True)
class File:
    owner: str
    path: str
    data: bytes


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

FILES = [
    File(
        "ivan",
        "Документы/Отпуск.docx",
        samples.docx([("Отпуск Ивана — 28 календарных дней.", None)]),
    ),
    File("ivan", "Документы/Схема.png", PNG),
    File("ivan", "Проекты/План.md", "# План\n\nЗапуск — в мае.\n".encode()),
    File(
        "ivan",
        "Кадры/Регламент.pdf",
        samples.pdf([[("HR policy", 20), ("Probation lasts three months.", 12)]]),
    ),
    File("ivan", "Кадры/Глубже/Анкета.txt", "Анкета нового сотрудника.\n".encode()),
    File("ivan", "Общий.txt", "Общий файл: телефоны офиса.\n".encode()),
    File(
        "maria",
        "Документы/Отпуск.docx",
        samples.docx([("Мария: отпуск в июле.", None)]),
    ),
]

SHARES = [
    ("ivan", "Проекты", SHARE_USER, "maria"),
    ("ivan", "Кадры", SHARE_GROUP, GROUP),
    # Один файл — и пользователю, и его группе: у maria он один, не два.
    ("ivan", "Общий.txt", SHARE_USER, "maria"),
    ("ivan", "Общий.txt", SHARE_GROUP, GROUP),
]

EXPECTED: dict[tuple[str, str], set[str]] = {
    ("Отпуск.docx", "Отпуск Ивана"): {"ivan"},
    ("Отпуск.docx", "в июле"): {"maria"},
    ("План.md", "в мае"): {"ivan", "maria"},
    ("Регламент.pdf", "three months"): {"ivan", "maria", "petr"},
    ("Анкета.txt", "нового сотрудника"): {"ivan", "maria", "petr"},
    ("Общий.txt", "телефоны"): {"ivan", "maria", "petr"},
}
"""(название, кусок текста) → кто видит. Общий файл Nextcloud — один
материал на всех, у кого он есть (сквозной oc:fileid). Схема.png не
индексируется (формат)."""


def dav_root(url: str, user: str) -> str:
    return f"{url.rstrip('/')}/remote.php/dav/files/{user}/"


class Ocs:
    """OCS API Nextcloud и ownCloud 10 (JSON, заголовок OCS-APIRequest)."""

    def __init__(self, url: str, login: str, password: str, ca: str = CA) -> None:
        self.url = url.rstrip("/")
        self._http = httpx.Client(
            auth=(login, password),
            verify=trust(ca),
            timeout=120,
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def call(
        self,
        method: str,
        endpoint: str,
        *,
        exists: int | None = None,
        **data: object,
    ) -> dict[str, object]:
        """exists — код OCS «уже есть»: повторный запуск seed не падает."""
        response = self._http.request(
            method,
            f"{self.url}/ocs/v1.php/{endpoint}",
            params={"format": "json"},
            data=data,
        )
        response.raise_for_status()
        body = response.json()["ocs"]
        status = body["meta"]["statuscode"]
        if status not in (100, 200, exists):
            raise RuntimeError(
                f"OCS {endpoint}: {status} {body['meta'].get('message')}"
            )
        return body.get("data") or {}  # type: ignore[no-any-return]

    def add_group(self, group: str) -> None:
        self.call("POST", "cloud/groups", exists=102, groupid=group)

    def add_user(self, user: str, password: str, groups: list[str]) -> None:
        self.call("POST", "cloud/users", exists=102, userid=user, password=password)
        for group in groups:
            self.call("POST", f"cloud/users/{user}/groups", groupid=group)

    def share(self, path: str, share_type: int, to: str) -> str:
        data = self.call(
            "POST",
            "apps/files_sharing/api/v1/shares",
            path=f"/{path}",
            shareType=share_type,
            shareWith=to,
            permissions=PERMISSION_READ,
        )
        return str(data["id"])

    def shares(self, path: str) -> list[dict[str, object]]:
        response = self._http.get(
            f"{self.url}/ocs/v1.php/apps/files_sharing/api/v1/shares",
            params={"format": "json", "path": f"/{path}"},
        )
        response.raise_for_status()
        return response.json()["ocs"]["data"]  # type: ignore[no-any-return]

    def unshare(self, share_id: str) -> None:
        self.call("DELETE", f"apps/files_sharing/api/v1/shares/{share_id}")


def app_password(url: str, user: str, password: str = PASSWORD, ca: str = CA) -> str:
    """Пароль приложения Nextcloud из пароля входа (core/getapppassword) —
    то, что сотрудник создаёт в «Настройки → Безопасность»."""
    with httpx.Client(auth=(user, password), verify=trust(ca), timeout=60) as http:
        response = http.get(
            f"{url.rstrip('/')}/ocs/v2.php/core/getapppassword",
            params={"format": "json"},
            headers={"OCS-APIRequest": "true"},
        )
        response.raise_for_status()
        return str(response.json()["ocs"]["data"]["apppassword"])


def seed(url: str, root: object = dav_root) -> None:
    admin = Ocs(url, ADMIN, ADMIN_PASSWORD)
    try:
        admin.add_group(GROUP)
        for user, groups in USERS.items():
            admin.add_user(user, PASSWORD, groups)
    finally:
        admin.close()
    for file in FILES:
        owner = DavSeeder(root(url, file.owner), file.owner, PASSWORD)  # type: ignore[operator]
        try:
            owner.put(file.path, file.data)
        finally:
            owner.close()
    ocs = Ocs(url, "ivan", PASSWORD)
    try:
        for _, path, share_type, to in SHARES:
            ocs.share(path, share_type, to)
    finally:
        ocs.close()


if __name__ == "__main__":
    address = os.environ.get("NEXTCLOUD_URL", "")
    if not address:
        sys.exit("нужен NEXTCLOUD_URL (https://127.0.0.1:8444/)")
    seed(address)
    print(f"пользователей: {len(USERS)}, файлов: {len(FILES)}, шар: {len(SHARES)}")
