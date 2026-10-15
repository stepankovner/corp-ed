"""BookStack (режим organization): засев Docker-стенда и шаги изменений.

Зона манифеста — книга BookStack со своей ролью `acc-<зона>`: право на
просмотр книги дано только этой роли, остальным запрещено (как «Кадры» в
tests/live/bookstack/seed.py). Подпапка документа — глава книги. Сотрудник
входит в роли тех зон, где он читатель. Все клиенты — из существующего
seed.py стенда (не меняется); здесь добавлено ожидание при 429: лимит API
BookStack — 180 запросов в минуту, а засев и опрос листинга его упираются.

Отзыв доступа — снять роль зоны; перенос в закрытую папку — переместить
страницу в главу книги другой зоны (страница наследует её права).
"""

import os
import time
from typing import Any

from corp_ed.domain.types import ConnectorMode
from tests.acceptance.model import Expected
from tests.acceptance.systems.base import (
    Creds,
    Driver,
    Placement,
    placements,
)
from tests.live.bookstack.seed import (
    DEFAULT_ADMIN,
    BookStackApi,
    BookStackWeb,
    Grant,
)
from tests.live.dav_stand import PASSWORD

MAX_WAIT = 90.0


class PatientApi(BookStackApi):
    """BookStackApi, который при 429 ждёт Retry-After и повторяет."""

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        for _ in range(8):
            response = self._http.request(method, f"{self.url}/api/{path}", **kwargs)
            if response.status_code == 429:
                wait = min(float(response.headers.get("Retry-After", "20")), MAX_WAIT)
                time.sleep(wait + 1)
                continue
            if response.status_code not in (200, 201, 204):
                raise RuntimeError(
                    f"{method} {path}: HTTP {response.status_code} "
                    f"{response.text[:300]}"
                )
            return response.json() if response.content else None
        raise RuntimeError(f"{method} {path}: лимит запросов не снялся")


def role_name(zone: str) -> str:
    return f"acc-{zone}"


class BookStackDriver(Driver):
    mode = ConnectorMode.ORGANIZATION
    modules = ("pages",)
    seedable = True
    default_port = 8520

    def _ca(self) -> str:
        return os.environ.get("DAV_STAND_CA", "")

    def _api(self) -> PatientApi:
        assert self.creds and self.creds.organization
        return PatientApi(
            self.creds.extra["url"],
            self.creds.organization["token_id"],
            self.creds.organization["token_secret"],
            self._ca(),
        )

    # --- засев --------------------------------------------------------------

    def seed(self, expected: Expected) -> Creds:
        assert self.creds is not None
        url = self.creds.extra["url"]
        manifest = self.manifest
        web = BookStackWeb(url, self._ca())
        try:
            web.login(*DEFAULT_ADMIN)
            token = web.api_token(1)
        finally:
            web.close()
        api = PatientApi(url, *token, self._ca())
        try:
            roles = {}
            for zone in manifest.zones:
                created = api.call(
                    "POST",
                    "roles",
                    json={"display_name": role_name(zone), "permissions": []},
                )
                roles[zone] = int(created["id"])
            for employee in manifest.employees.values():
                api.call(
                    "POST",
                    "users",
                    json={
                        "name": employee.name,
                        "email": employee.email,
                        "password": PASSWORD,
                        "roles": [
                            roles[z]
                            for z, members in expected.members.items()
                            if employee.login in members
                        ],
                    },
                )
            books = {}
            for zone_id, zone in manifest.zones.items():
                book = api.call("POST", "books", json={"name": zone.title})
                books[zone_id] = int(book["id"])
                api.grant(
                    "book", books[zone_id], Grant({role_name(zone_id): True}, False)
                )
            chapters: dict[tuple[str, str], int] = {}
            for placement in placements(expected, self.system):
                key = (placement.zone, placement.document.folder)
                if key not in chapters:
                    chapter = api.call(
                        "POST",
                        "chapters",
                        json={"book_id": books[key[0]], "name": key[1]},
                    )
                    chapters[key] = int(chapter["id"])
                api.call(
                    "POST",
                    "pages",
                    json={
                        "chapter_id": chapters[key],
                        "name": placement.document.title,
                        "markdown": placement.text(),
                    },
                )
        finally:
            api.close()
        return Creds(
            config={"base_url": url},
            organization={"token_id": token[0], "token_secret": token[1]},
            extra={"url": url},
        )

    # --- шаги ---------------------------------------------------------------

    def _page(self, api: PatientApi, placement: Placement) -> int:
        return int(api.find("pages", placement.document.title)["id"])

    def edit(self, placement: Placement) -> None:
        api = self._api()
        try:
            api.call(
                "PUT",
                f"pages/{self._page(api, placement)}",
                json={"markdown": placement.text()},
            )
        finally:
            api.close()

    def delete(self, placement: Placement) -> None:
        api = self._api()
        try:
            api.call("DELETE", f"pages/{self._page(api, placement)}")
        finally:
            api.close()

    def revoke(self, login: str, zones: list[str]) -> None:
        api = self._api()
        try:
            users = api.users()
            roles = api.roles()
            user_id = users[login]
            current = api.call("GET", f"users/{user_id}")["roles"]
            drop = {roles[role_name(z)] for z in zones}
            kept = [int(r["id"]) for r in current if int(r["id"]) not in drop]
            api.call("PUT", f"users/{user_id}", json={"roles": kept})
        finally:
            api.close()

    def move(self, placement: Placement, to_zone: str) -> None:
        api = self._api()
        try:
            book_name = self.manifest.zones[to_zone].title
            book = int(api.find("books", book_name)["id"])
            folder = placement.document.folder
            found = [
                c
                for c in api.listing("chapters")
                if c["name"] == folder and int(c["book_id"]) == book
            ]
            if found:
                chapter = int(found[0]["id"])
            else:
                chapter = int(
                    api.call(
                        "POST", "chapters", json={"book_id": book, "name": folder}
                    )["id"]
                )
            api.call(
                "PUT",
                f"pages/{self._page(api, placement)}",
                json={"chapter_id": chapter},
            )
        finally:
            api.close()
