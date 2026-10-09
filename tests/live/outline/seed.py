"""Выдуманная компания в Outline для живой проверки вида `outline`.

Пользователи появляются в Outline при первом входе (dex, login.mjs);
дальше всё — через API ключом администратора: группа hr (maria, petr),
коллекция «Общая» для всей рабочей области, закрытые «Кадры» (группа hr)
и «Проекты» (ivan), вложенный документ с участником документа (maria).
Ожидания для tests/live/test_outline_live.py — здесь же (EXPECTED).

    OUTLINE_URL=https://127.0.0.1:8447/ OUTLINE_TOKEN=… \\
    DAV_STAND_CA=~/dav-tls/cert.pem \\
        python -m tests.live.outline.seed

Скрипт рассчитан на чистый стенд: коллекции создаются заново.
"""

import os
import sys
from dataclasses import dataclass

import httpx

from tests.live.dav_stand import CA, trust

GROUP = "hr"
USERS = ("ivan", "maria", "petr")
"""Сотрудники (почта — <логин>@example.com); admin — владелец ключа."""


@dataclass(frozen=True)
class Doc:
    collection: str
    title: str
    text: str
    parent: str | None = None
    members: tuple[str, ...] = ()
    """Участники самого документа (documents.add_user)."""


COLLECTIONS = {
    "Общая": "read",
    "Кадры": None,
    "Проекты": None,
}
"""Коллекция → permission (None — закрытая, только участники)."""
COLLECTION_USERS = {"Проекты": ("ivan",)}
COLLECTION_GROUPS = {"Кадры": (GROUP,)}
DOCS = [
    Doc("Общая", "Отпуск", "Отпуск — 28 календарных дней."),
    Doc("Общая", "Заявление", "Заявление пишут за две недели.", parent="Отпуск"),
    Doc("Кадры", "Зарплаты", "Оклады пересматриваются в марте."),
    Doc("Проекты", "План", "Запуск — в мае."),
    Doc("Проекты", "Бюджет", "Бюджет проекта утверждён.", "План", ("maria",)),
]

EXPECTED: dict[str, set[str] | None] = {
    "Отпуск": None,
    "Заявление": None,
    "Зарплаты": {"admin", "maria", "petr"},
    "План": {"admin", "ivan"},
    "Бюджет": {"admin", "ivan", "maria"},
}
"""Название → кто читает (None — вся рабочая область). admin — создатель
закрытых коллекций, он в них участник, как владелец ключа в жизни."""
TEXT = {doc.title: doc.text for doc in DOCS}
PATHS = {
    "Отпуск": "Общая",
    "Заявление": "Общая/Отпуск",
    "Зарплаты": "Кадры",
    "План": "Проекты",
    "Бюджет": "Проекты/План",
}


class OutlineApi:
    def __init__(self, url: str, token: str, ca: str = CA) -> None:
        self.url = url.rstrip("/")
        self._http = httpx.Client(
            verify=trust(ca),
            timeout=60,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def call(self, method: str, **body: object) -> dict[str, object]:
        response = self._http.post(f"{self.url}/api/{method}", json=body)
        if response.status_code != 200:
            raise RuntimeError(
                f"{method}: HTTP {response.status_code} {response.text[:200]}"
            )
        return response.json()  # type: ignore[no-any-return]

    def users(self) -> dict[str, str]:
        """Логин (часть почты до @) → id пользователя Outline."""
        data = self.call("users.list", limit=100)["data"]
        return {str(u["email"]).split("@")[0]: str(u["id"]) for u in data}  # type: ignore[union-attr]

    def group(self, name: str) -> str:
        data = self.call("groups.list", limit=100)["data"]
        groups = data["groups"] if isinstance(data, dict) else data
        return next(str(g["id"]) for g in groups if g["name"] == name)  # type: ignore[union-attr]

    def collection(self, name: str) -> str:
        data = self.call("collections.list", limit=100)["data"]
        return next(str(c["id"]) for c in data if c["name"] == name)  # type: ignore[union-attr]

    def document(self, title: str) -> str:
        data = self.call("documents.search_titles", query=title, limit=25)["data"]
        return next(str(d["id"]) for d in data if d["title"] == title)  # type: ignore[union-attr]


def seed(url: str, token: str) -> None:
    api = OutlineApi(url, token)
    try:
        users = api.users()
        missing = set(USERS) - set(users)
        if missing:
            raise RuntimeError(f"не входили в Outline: {sorted(missing)} (login.mjs)")
        group = str(api.call("groups.create", name=GROUP)["data"]["id"])  # type: ignore[index]
        for login in ("maria", "petr"):
            api.call("groups.add_user", id=group, userId=users[login])
        collections: dict[str, str] = {}
        for name, permission in COLLECTIONS.items():
            created = api.call("collections.create", name=name, permission=permission)
            collections[name] = str(created["data"]["id"])  # type: ignore[index]
        for name, logins in COLLECTION_USERS.items():
            for login in logins:
                api.call(
                    "collections.add_user",
                    id=collections[name],
                    userId=users[login],
                    permission="read",
                )
        for name, groups in COLLECTION_GROUPS.items():
            for _ in groups:
                api.call(
                    "collections.add_group",
                    id=collections[name],
                    groupId=group,
                    permission="read",
                )
        documents: dict[str, str] = {}
        for doc in DOCS:
            body: dict[str, object] = {
                "collectionId": collections[doc.collection],
                "title": doc.title,
                "text": doc.text,
                "publish": True,
            }
            if doc.parent:
                body["parentDocumentId"] = documents[doc.parent]
            created = api.call("documents.create", **body)
            documents[doc.title] = str(created["data"]["id"])  # type: ignore[index]
            for login in doc.members:
                api.call(
                    "documents.add_user",
                    id=documents[doc.title],
                    userId=users[login],
                    permission="read",
                )
    finally:
        api.close()


if __name__ == "__main__":
    address = os.environ.get("OUTLINE_URL", "")
    key = os.environ.get("OUTLINE_TOKEN", "")
    if not address or not key:
        sys.exit("нужны OUTLINE_URL (https://127.0.0.1:8447/) и OUTLINE_TOKEN")
    seed(address, key)
    print(f"коллекций: {len(COLLECTIONS)}, документов: {len(DOCS)}")
