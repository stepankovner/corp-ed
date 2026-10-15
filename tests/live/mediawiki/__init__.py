"""Выдуманная компания в MediaWiki для живой проверки вида `mediawiki`.

Вики ставится и наполняется внутри контейнера (stand/install.sh,
stand/seed.sh): сотрудники Ivan, Maria (группы staff, hr) и Petr,
страницы в основном пространстве, «Компания», «Справка», «Участник» и
«Кадры», перенаправление, файлы txt и png, бот-пароли. Здесь — учётные
данные стенда, ожидания тестов и клиент для правок из тестов
(бот-пароль Admin@seed).
"""

from typing import Any

import httpx

from tests.live.dav_stand import CA, trust

BOT = "Admin@kronto"
BOT_PASSWORD = "kronto0stand0admin0bot0pass00001"  # noqa: S105 — стенд на 127.0.0.1
SEED_BOT = "Admin@seed"
SEED_PASSWORD = "kronto0stand0seed00bot0pass00001"  # noqa: S105
NO_READ_BOT = "Petr@kronto"
NO_READ_PASSWORD = "kronto0stand0petr00bot0pass00001"  # noqa: S105

USERS = ("ivan", "maria", "petr")
"""Сотрудники kronto (почта <логин>@example.com); Admin в kronto нет."""
MAPPING = "\n".join(
    [*(f"{login.capitalize()} = {login}@example.com" for login in USERS)]
    + ["Ghost = ghost@example.com"]
)
"""Сопоставление для формы подключения; Ghost в вики нет."""

MAIN_PAGES = {"Заглавная страница", "Отпуск", "Больничный"}
TEXT = {
    "Отпуск": "28 дней",
    "Больничный": "в течение трёх дней",
    "Компания:Правила": "писать коротко",
    "Справка:Поиск": "строка вверху",
}
PUBLIC_READERS = set(USERS)
PRIVATE_READERS = {"ivan", "maria"}
"""На закрытой вики читает только группа staff: у Petr права read нет."""


class WikiApi:
    """Правки стенда из тестов: бот-пароль Admin@seed, cookie сессии."""

    def __init__(self, url: str, ca: str = CA) -> None:
        self.api = url.rstrip("/") + "/api.php"
        self._http = httpx.Client(verify=trust(ca), timeout=60)
        token = self.get(action="query", meta="tokens", type="login")
        result = self.post(
            action="login",
            lgname=SEED_BOT,
            lgpassword=SEED_PASSWORD,
            lgtoken=token["query"]["tokens"]["logintoken"],
        )
        assert result["login"]["result"] == "Success", result
        csrf = self.get(action="query", meta="tokens")
        self._csrf = csrf["query"]["tokens"]["csrftoken"]

    def close(self) -> None:
        self._http.close()

    def get(self, **params: Any) -> dict[str, Any]:
        response = self._http.get(
            self.api, params={**params, "format": "json", "formatversion": "2"}
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data

    def post(self, **params: Any) -> dict[str, Any]:
        response = self._http.post(
            self.api, data={**params, "format": "json", "formatversion": "2"}
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        assert "error" not in data, data["error"]
        return data

    def wikitext(self, title: str) -> str:
        found = self.get(
            action="query",
            prop="revisions",
            titles=title,
            rvprop="content",
            rvslots="main",
        )
        page = found["query"]["pages"][0]
        text: str = page["revisions"][0]["slots"]["main"]["content"]
        return text

    def edit(self, title: str, text: str) -> None:
        self.post(action="edit", title=title, text=text, token=self._csrf, bot="1")

    def delete(self, title: str) -> None:
        self.post(action="delete", title=title, token=self._csrf)

    def block(self, user: str) -> None:
        self.post(action="block", user=user, expiry="infinite", token=self._csrf)

    def unblock(self, user: str) -> None:
        self.post(action="unblock", user=user, token=self._csrf)
