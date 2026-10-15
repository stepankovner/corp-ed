"""Сетевой диск по WebDAV (Apache mod_dav, как NAS) — режим per_user:
засев Docker-стенда и шаги изменений.

Права в Apache — не API, а конфигурация. Стенд из tests/live/webdav
поднимается как есть; после запуска драйвер заводит в htpasswd
сотрудников манифеста и дописывает свой `acc.conf`: для каждой зоны папка
`/dav/files/<зона>` с правилом `Require user admin <читатели зоны>`, и
перечитывает конфигурацию (`apachectl -k graceful`). Файлы кладёт
`admin` по WebDAV (клиент и перенос — из общих помощников набора). У WebDAV
нет сквозного номера файла: у каждого сотрудника копия — отдельный документ,
сверка идёт по шифру внутри текста, так что один документ — это несколько
материалов, и каждый видит только свои.

Отзыв доступа — убрать логин из `Require user` зоны; перенос — WebDAV MOVE
между папками зон.
"""

import os
import re

from corp_ed.domain.types import ConnectorMode
from tests.acceptance.model import Expected
from tests.acceptance.stand import STANDS, exec_in
from tests.acceptance.systems.base import Creds, Driver, Placement, placements
from tests.acceptance.systems.nextcloud import MovingDav
from tests.live.dav_stand import PASSWORD

ADMIN = "admin"
CONF = "/usr/local/apache2/conf/acc.conf"
HTTPD = "/usr/local/apache2/conf/httpd.conf"
USERS = "/usr/local/apache2/users"
REQUIRE = re.compile(
    r'(<Directory "/dav/files/(?P<zone>\w+)">\s*Require user )(?P<users>[^\n]*)'
)


class WebDavDriver(Driver):
    mode = ConnectorMode.PER_USER
    modules = ("files",)
    seedable = True
    default_port = 8523

    def _ca(self) -> str:
        return os.environ.get("DAV_STAND_CA", "")

    def _url(self) -> str:
        assert self.creds is not None
        return self.creds.extra["url"]

    def _stand(self):  # type: ignore[no-untyped-def]
        return STANDS["webdav"]

    def _dav(self) -> MovingDav:
        return MovingDav(self._url() + "dav/", ADMIN, PASSWORD, self._ca())

    @staticmethod
    def _path(zone: str, placement: Placement) -> str:
        return f"{zone}/{placement.document.folder}/{placement.name}"

    def _conf(self, members: dict[str, list[str]]) -> str:
        blocks = []
        for zone, logins in members.items():
            users = " ".join([ADMIN, *logins])
            blocks.append(
                f'<Directory "/dav/files/{zone}">\n'
                f"    Require user {users}\n"
                "</Directory>"
            )
        return "\n".join(blocks) + "\n"

    def _install(self, conf: str) -> None:
        stand = self._stand()
        exec_in(stand, "webdav", "sh", "-c", f"cat > {CONF}", input_text=conf)
        exec_in(
            stand,
            "webdav",
            "sh",
            "-c",
            f"grep -q acc.conf {HTTPD} || echo 'Include conf/acc.conf' >> {HTTPD}; "
            "apachectl -k graceful",
        )

    def seed(self, expected: Expected) -> Creds:
        url = self._url()
        stand = self._stand()
        logins = [ADMIN, *self.manifest.employees]
        exec_in(
            stand,
            "webdav",
            "sh",
            "-c",
            "; ".join(f"htpasswd -b {USERS} {login} {PASSWORD}" for login in logins),
        )
        members = {zone: sorted(m) for zone, m in expected.members.items()}
        self._install(self._conf(members))
        dav = self._dav()
        try:
            for placement in placements(expected, self.system):
                dav.put(self._path(placement.zone, placement), placement.content())
        finally:
            dav.close()
        users = {
            login: {"login": login, "password": PASSWORD}
            for login in self.manifest.employees
        }
        return Creds(config={"server": url + "dav/"}, users=users, extra={"url": url})

    # --- шаги ---------------------------------------------------------------

    def edit(self, placement: Placement) -> None:
        dav = self._dav()
        try:
            dav.put(self._path(placement.zone, placement), placement.content())
        finally:
            dav.close()

    def delete(self, placement: Placement) -> None:
        dav = self._dav()
        try:
            dav.delete(self._path(placement.zone, placement))
        finally:
            dav.close()

    def revoke(self, login: str, zones: list[str]) -> None:
        conf = exec_in(self._stand(), "webdav", "cat", CONF)

        def drop(found: re.Match[str]) -> str:
            if found.group("zone") not in zones:
                return found.group(0)
            kept = [u for u in found.group("users").split() if u != login]
            return found.group(1) + " ".join(kept)

        self._install(REQUIRE.sub(drop, conf))

    def move(self, placement: Placement, to_zone: str) -> None:
        dav = self._dav()
        try:
            dav.move(
                self._path(placement.zone, placement), self._path(to_zone, placement)
            )
        finally:
            dav.close()
