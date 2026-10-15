"""Outline (своя установка, режим organization): засев Docker-стенда и
шаги изменений.

Outline пускает только через внешний вход, поэтому стенд — вместе с dex
(OIDC): в нём статические учётки сотрудников манифеста (почта и общий
пароль стенда), конфиг — `tests/acceptance/stands/outline-dex.yaml` (его
собирает `dex_config`, тест сверяет файл с манифестом) через override
compose. Outline заводит пользователя при первом входе, поэтому драйвер
входит за каждого в браузере (`tests/live/outline/login.mjs`, Playwright
из frontend/node_modules, браузер — CHROMIUM_PATH): первым — администратор
(владелец API-ключа), дальше сотрудники.

Зона манифеста — закрытая коллекция Outline со своей группой
`acc-<зона>` (право чтения); в группу входят читатели зоны. Открытой зоны
нет: даже `public` — коллекция группы из всех, чтобы отзыв доступа
(выход из группы) был виден и для общих документов, — открытая всей
рабочей области коллекция в kronto «видна всей компании», а не тем, кто ещё
состоит в Outline. Документ — страница Markdown в коллекции зоны без
вложенности (подпапок нет). Приветственную коллекцию Outline драйвер
удаляет: она открыта всей рабочей области, а сверка её не касается.

Отзыв доступа — убрать сотрудника из группы зоны (`groups.remove_user`);
перенос — `documents.move` в коллекцию другой зоны (документ тот же,
меняются только права); правка — `documents.update`; удаление —
`documents.delete`.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

from corp_ed.domain.types import ConnectorMode
from tests.acceptance.model import Expected, Manifest
from tests.acceptance.stand import ROOT
from tests.acceptance.systems.base import Creds, Driver, Placement, placements
from tests.live.dav_stand import PASSWORD
from tests.live.outline.seed import OutlineApi

ADMIN_EMAIL = "admin@severny-veter.test"
HASH = "$2a$10$G9Ykml3or6/faJOGCwyFQ.7eCR7ZHYZ4O/2NLAUYR79GlX/KBk5qi"
"""bcrypt от пароля стенда (Kronto-Test-1) — тот же, что в
tests/live/outline/dex.yaml; пароль выдуманный."""
DEX_FILE = ROOT / "tests/acceptance/stands/outline-dex.yaml"
LOGIN = ROOT / "tests/live/outline/login.mjs"


def group_name(zone: str) -> str:
    return f"acc-{zone}"


def dex_config(manifest: Manifest, port: int = 8542) -> str:
    """Конфиг dex: клиент Outline и статические учётки сотрудников."""
    people = [
        ("admin", ADMIN_EMAIL),
        *((e.login, e.email) for e in manifest.employees.values()),
    ]
    lines = [
        "# dex для стенда приёмки Outline: сотрудники манифеста (почта и общий",
        "# пароль стенда Kronto-Test-1). Собирается из манифеста: см.",
        "# tests/acceptance/systems/outline.py (dex_config), файл сверяет тест.",
        "issuer: http://127.0.0.1:5556/dex",
        "storage:",
        "  type: memory",
        "web:",
        "  http: 0.0.0.0:5556",
        "oauth2:",
        "  skipApprovalScreen: true",
        "staticClients:",
        "  - id: outline",
        "    secret: outline-stand-client-secret",
        "    name: Outline",
        "    redirectURIs:",
        f"      - https://127.0.0.1:{port}/auth/oidc.callback",
        "enablePasswordDB: true",
        "staticPasswords:",
    ]
    for index, (login, email) in enumerate(people, start=1):
        lines += [
            f"  - email: {email}",
            f"    username: {login}",
            f'    userID: "00000000-0000-0000-0000-{index:012d}"',
            f'    hash: "{HASH}"',
        ]
    return "\n".join(lines) + "\n"


def node_dir() -> Path:
    """Папка frontend/ с node_modules (Playwright): OUTLINE_NODE_DIR или
    первая найденная от корня репозитория вверх (в worktree своих
    node_modules нет — они в основном репозитории)."""
    explicit = os.environ.get("OUTLINE_NODE_DIR")
    if explicit:
        return Path(explicit)
    for base in (ROOT, *ROOT.parents):
        if (base / "frontend/node_modules/playwright").is_dir():
            return base / "frontend"
    raise RuntimeError("нет frontend/node_modules (npm ci) — задайте OUTLINE_NODE_DIR")


def body_of(text: str) -> str:
    """Текст страницы без первой строки «# Заголовок»: заголовок Outline
    берёт из поля title."""
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).lstrip("\n") + "\n"


class OutlineDriver(Driver):
    mode = ConnectorMode.ORGANIZATION
    modules = ("documents",)
    seedable = True
    default_port = 8542

    def _ca(self) -> str:
        return os.environ.get("DAV_STAND_CA", "")

    def _url(self) -> str:
        assert self.creds is not None
        return self.creds.extra["url"]

    def _api(self) -> OutlineApi:
        assert self.creds and self.creds.organization
        return OutlineApi(self._url(), self.creds.organization["token"], self._ca())

    def _login(self, email: str, *flags: str) -> str:
        """Войти в Outline через dex в браузере; с --api-key — ключ API.
        Браузерный вход изредка срывается (токен CSRF ещё не выставлен) —
        до трёх попыток."""
        node = shutil.which("node") or "node"
        last = ""
        for attempt in range(3):
            result = subprocess.run(
                [node, str(LOGIN), self._url(), email, PASSWORD, *flags],
                cwd=node_dir(),
                capture_output=True,
                text=True,
                check=False,
                timeout=240,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            last = result.stderr[-300:]
            time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"вход {email} в Outline: {last}")

    def seed(self, expected: Expected) -> Creds:
        url = self._url()
        # Первый вошедший становится администратором рабочей области.
        key = self._login(ADMIN_EMAIL, "--api-key")
        for employee in self.manifest.employees.values():
            self._login(employee.email)
        api = OutlineApi(url, key, self._ca())
        try:
            welcome = [
                str(item["id"])
                for item in api.call("collections.list", limit=100)["data"]  # type: ignore[union-attr]
            ]
            users = api.users()
            collections: dict[str, str] = {}
            for zone_id, zone in self.manifest.zones.items():
                group = str(
                    api.call("groups.create", name=group_name(zone_id))["data"]["id"]
                )  # type: ignore[index]
                for login in sorted(expected.members[zone_id]):
                    api.call("groups.add_user", id=group, userId=users[login])
                created = api.call(
                    "collections.create", name=zone.title, permission=None
                )
                collections[zone_id] = str(created["data"]["id"])  # type: ignore[index]
                api.call(
                    "collections.add_group",
                    id=collections[zone_id],
                    groupId=group,
                    permission="read",
                )
            # Последнюю коллекцию Outline удалить не даёт: после зон — можно.
            for collection_id in welcome:
                api.call("collections.delete", id=collection_id)
            for placement in placements(expected, self.system):
                api.call(
                    "documents.create",
                    collectionId=collections[placement.zone],
                    title=placement.document.title,
                    text=body_of(placement.text()),
                    publish=True,
                )
        finally:
            api.close()
        return Creds(
            config={"base_url": url},
            organization={"token": key},
            extra={"url": url},
        )

    # --- шаги ---------------------------------------------------------------

    def _document(self, api: OutlineApi, title: str) -> str:
        return api.document(title)

    def _collection(self, api: OutlineApi, zone: str) -> str:
        return api.collection(self.manifest.zones[zone].title)

    def edit(self, placement: Placement) -> None:
        api = self._api()
        try:
            api.call(
                "documents.update",
                id=self._document(api, placement.document.title),
                text=body_of(placement.text()),
                publish=True,
            )
        finally:
            api.close()

    def delete(self, placement: Placement) -> None:
        api = self._api()
        try:
            api.call(
                "documents.delete", id=self._document(api, placement.document.title)
            )
        finally:
            api.close()

    def revoke(self, login: str, zones: list[str]) -> None:
        api = self._api()
        try:
            user = api.users()[login]
            for zone in zones:
                api.call(
                    "groups.remove_user", id=api.group(group_name(zone)), userId=user
                )
        finally:
            api.close()

    def move(self, placement: Placement, to_zone: str) -> None:
        api = self._api()
        try:
            api.call(
                "documents.move",
                id=self._document(api, placement.document.title),
                collectionId=self._collection(api, to_zone),
            )
        finally:
            api.close()
