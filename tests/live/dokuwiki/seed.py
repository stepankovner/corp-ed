"""Выдуманная компания в DokuWiki для живой проверки вида `dokuwiki`.

Пользователи, группы (hr, sales) и ACL заданы файлами conf/ при старте
контейнера; здесь — страницы и медиафайлы через JSON-RPC от имени
суперпользователя admin (Basic). Ожидания для
tests/live/test_dokuwiki_live.py — здесь же (EXPECTED).

    DOKUWIKI_URL=https://127.0.0.1:8490/ DAV_STAND_CA=~/dav-tls/cert.pem \\
        python -m tests.live.dokuwiki.seed

Повторный запуск безопасен: страницы и файлы перезаписываются.
"""

import base64
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from tests.live.dav_stand import CA, PASSWORD, trust

ADMIN = "admin"
USERS = ("ivan", "maria", "petr", "olga")
"""Сотрудники kronto (почта — <логин>@example.com); admin — владелец ключа,
в kronto его нет."""
ALL = {"admin", *USERS}
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "stand_quality" / "docs"

PAGES = {
    "start": "====== Главная ======\nДобро пожаловать в вики компании.",
    "public:vacation": "====== Отпуск ======\nОтпуск — 28 календарных дней.",
    "hr:onboarding": "====== Адаптация ======\nВ первый день — встреча с наставником.",
    "hr:salaries": "====== Зарплаты ======\nОклады пересматриваются в марте.",
    "sales:plan": "====== План продаж ======\nРост выручки на 10% за квартал.",
}
TEXT = {
    "start": "Добро пожаловать в вики компании.",
    "public:vacation": "Отпуск — 28 календарных дней.",
    "hr:onboarding": "В первый день — встреча с наставником.",
    "hr:salaries": "Оклады пересматриваются в марте.",
    "sales:plan": "Рост выручки на 10% за квартал.",
}
TITLES = {
    "start": "Главная",
    "public:vacation": "Отпуск",
    "hr:onboarding": "Адаптация",
    "hr:salaries": "Зарплаты",
    "sales:plan": "План продаж",
}
MEDIA = {
    "hr:otpuska.docx": FIXTURES / "hr_otpuska_i_otguly.docx",
    "public:zakupki.docx": FIXTURES / "zakupki_reglament.docx",
}

EXPECTED: dict[str, set[str]] = {
    "page:start": ALL,
    "page:public:vacation": ALL,
    "page:hr:onboarding": {"admin", "maria", "petr"},
    # petr в группе hr, но страница ему запрещена отдельным правилом.
    "page:hr:salaries": {"admin", "maria"},
    "page:sales:plan": {"admin", "ivan"},
    "media:hr:otpuska.docx": {"admin", "maria", "petr"},
    "media:public:zakupki.docx": ALL,
}
"""external_id → кто из списка читает (логины)."""


class DokuWikiApi:
    def __init__(self, url: str, login: str = ADMIN, ca: str = CA) -> None:
        self.url = url.rstrip("/") + "/lib/exe/jsonrpc.php"
        self._http = httpx.Client(verify=trust(ca), timeout=60, auth=(login, PASSWORD))
        self._id = 0

    def close(self) -> None:
        self._http.close()

    def call(self, method: str, **params: object) -> Any:
        self._id += 1
        response = self._http.post(
            self.url,
            json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
        )
        data = response.json()
        if response.status_code != 200 or data.get("error"):
            raise RuntimeError(f"{method}: HTTP {response.status_code} {data}")
        return data.get("result")

    def save_page(self, page: str, text: str) -> None:
        self.call("core.savePage", page=page, text=text, summary="kronto stand")

    def save_media(self, media: str, data: bytes) -> None:
        encoded = base64.b64encode(data).decode("ascii")
        self.call("core.saveMedia", media=media, base64=encoded, overwrite=True)


def seed(url: str) -> None:
    api = DokuWikiApi(url)
    try:
        for page, text in PAGES.items():
            api.save_page(page, text)
        for media, path in MEDIA.items():
            api.save_media(media, path.read_bytes())
    finally:
        api.close()


if __name__ == "__main__":
    address = os.environ.get("DOKUWIKI_URL", "")
    if not address or not CA:
        sys.exit("нужны DOKUWIKI_URL (https://127.0.0.1:8490/) и DAV_STAND_CA")
    seed(address)
    print(f"страниц: {len(PAGES)}, файлов: {len(MEDIA)}")
