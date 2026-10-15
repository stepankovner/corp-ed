"""Wiki.js 2.x (режим organization): засев Docker-стенда и шаги изменений.

Всё через GraphQL от имени администратора (клиент и мастер установки —
из tests/live/wikijs/seed.py, он не меняется). Зона манифеста — группа
`acc-<зона>` с одним правилом «читать страницы, чей путь начинается с
`<зона>/`»; сотрудник входит в группы тех зон, где он читатель. У группы
гостей правил нет: открытых всем страниц в стенде нет, «вся компания» —
это группа зоны `public`, в которой все. Страница лежит по пути
`<зона>/<шифр в нижнем регистре>`, название — русское из манифеста.

Одна локаль (`en`): русскую в стенд не скачать (нет выхода к
graph.requarks.io), а для проверки прав она не нужна.

Отзыв доступа — снять пользователя с группы зоны (`unassignUser`); перенос
в закрытую папку — `pages.move` в путь другой зоны.
"""

import os

from corp_ed.domain.types import ConnectorMode
from tests.acceptance.model import Expected
from tests.acceptance.systems.base import Creds, Driver, Placement, placements
from tests.live.dav_stand import PASSWORD
from tests.live.wikijs.seed import (
    ADMIN,
    READ,
    Rule,
    WikiApi,
    finalize,
    rules,
)

LOCALE = "en"


def group_name(zone: str) -> str:
    return f"acc-{zone}"


def page_path(zone: str, doc_id: str) -> str:
    return f"{zone}/{doc_id.lower()}"


class WikiJsDriver(Driver):
    mode = ConnectorMode.ORGANIZATION
    modules = ("pages",)
    seedable = True
    default_port = 8527

    def _ca(self) -> str:
        return os.environ.get("DAV_STAND_CA", "")

    def _url(self) -> str:
        assert self.creds is not None
        return self.creds.extra["url"]

    def _api(self) -> WikiApi:
        """Клиент с ключом администратора: вход паролем в Wiki.js ограничен
        пятью попытками в минуту, а шагов много."""
        assert self.creds and self.creds.organization
        api = WikiApi(self._url(), self._ca())
        api.token = self.creds.organization["token"]
        return api

    def seed(self, expected: Expected) -> Creds:
        url = self._url()
        api = WikiApi(url, self._ca())
        try:
            # finalize берёт сертификат из dav_stand.CA (DAV_STAND_CA или
            # ~/.kronto-acc/tls, см. tests/acceptance/__init__.py).
            finalize(api)
            api.login()
            api.update_group(2, "Guests", READ, rules([]))
            group_ids: dict[str, int] = {}
            for zone in self.manifest.zones:
                created = api.mutate(
                    """mutation ($n: String!) { groups { create(name: $n) {
                         responseResult { succeeded message } group { id } } } }""",
                    ("groups", "create"),
                    n=group_name(zone),
                )
                group_ids[zone] = created["group"]["id"]
                api.update_group(
                    group_ids[zone],
                    group_name(zone),
                    READ,
                    rules([Rule("START", f"{zone}/")]),
                )
            for employee in self.manifest.employees.values():
                api.mutate(
                    """mutation ($e: String!, $n: String!, $p: String!) {
                         users { create(email: $e, name: $n, passwordRaw: $p,
                           providerKey: "local", groups: [],
                           mustChangePassword: false, sendWelcomeEmail: false) {
                           responseResult { succeeded message } } } }""",
                    ("users", "create"),
                    e=employee.email,
                    n=employee.name,
                    p=PASSWORD,
                )
            users = {
                u["email"].split("@")[0]: u["id"]
                for u in api.query("{ users { list { id email } } }")["users"]["list"]
            }
            for zone, members in expected.members.items():
                for login in sorted(members):
                    self._assign(api, group_ids[zone], users[login])
            for placement in placements(expected, self.system):
                api.mutate(
                    """mutation ($content: String!, $path: String!,
                                 $title: String!, $locale: String!) {
                         pages { create(content: $content, description: "",
                           editor: "markdown", isPublished: true, isPrivate: false,
                           locale: $locale, path: $path, tags: [], title: $title) {
                             responseResult { succeeded message } } } }""",
                    ("pages", "create"),
                    content=placement.text(),
                    path=page_path(placement.zone, placement.document.id),
                    title=placement.document.title,
                    locale=LOCALE,
                )
            api.mutate(
                """mutation { authentication { setApiState(enabled: true) {
                     responseResult { succeeded message } } } }""",
                ("authentication", "setApiState"),
            )
            key = api.mutate(
                """mutation { authentication { createApiKey(name: "kronto",
                     expiration: "1y", fullAccess: true, group: null) {
                       responseResult { succeeded message } key } } }""",
                ("authentication", "createApiKey"),
            )["key"]
        finally:
            api.close()
        return Creds(
            config={"base_url": url},
            organization={"token": key},
            extra={"url": url, "admin": ADMIN},
        )

    @staticmethod
    def _assign(api: WikiApi, group_id: int, user_id: int) -> None:
        api.mutate(
            """mutation ($g: Int!, $u: Int!) { groups { assignUser(
                 groupId: $g, userId: $u) {
                   responseResult { succeeded message } } } }""",
            ("groups", "assignUser"),
            g=group_id,
            u=user_id,
        )

    # --- шаги ---------------------------------------------------------------

    def _page_id(self, api: WikiApi, zone: str, placement: Placement) -> int:
        return api.pages()[(LOCALE, page_path(zone, placement.document.id))]

    def edit(self, placement: Placement) -> None:
        api = self._api()
        try:
            api.update_page(
                self._page_id(api, placement.zone, placement),
                placement.document.title,
                placement.text(),
            )
        finally:
            api.close()

    def delete(self, placement: Placement) -> None:
        api = self._api()
        try:
            api.mutate(
                """mutation ($id: Int!) { pages { delete(id: $id) {
                     responseResult { succeeded message } } } }""",
                ("pages", "delete"),
                id=self._page_id(api, placement.zone, placement),
            )
        finally:
            api.close()

    def revoke(self, login: str, zones: list[str]) -> None:
        api = self._api()
        try:
            users = {
                u["email"].split("@")[0]: u["id"]
                for u in api.query("{ users { list { id email } } }")["users"]["list"]
            }
            groups = api.groups()
            for zone in zones:
                api.mutate(
                    """mutation ($g: Int!, $u: Int!) { groups { unassignUser(
                         groupId: $g, userId: $u) {
                           responseResult { succeeded message } } } }""",
                    ("groups", "unassignUser"),
                    g=groups[group_name(zone)],
                    u=users[login],
                )
        finally:
            api.close()

    def move(self, placement: Placement, to_zone: str) -> None:
        api = self._api()
        try:
            api.mutate(
                """mutation ($id: Int!, $path: String!, $locale: String!) {
                     pages { move(id: $id, destinationPath: $path,
                       destinationLocale: $locale) {
                         responseResult { succeeded message } } } }""",
                ("pages", "move"),
                id=self._page_id(api, placement.zone, placement),
                path=page_path(to_zone, placement.document.id),
                locale=LOCALE,
            )
        finally:
            api.close()
