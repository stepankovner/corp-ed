"""Seafile (SeafDAV, режим per_user): засев Docker-стенда и шаги изменений.

SeafDAV в образе выключен (`enabled = false` в `seafdav.conf`): драйвер
включает его, как это делает админ, и перезапускает сервер. Дальше всё по
REST API Seafile от имени администратора стенда (клиент — из
tests/live/seafile/seed.py) и по SeafDAV для файлов.

Зона манифеста — библиотека администратора с названием зоны, расшаренная
группе `acc-<зона>` только на чтение; в группу входят читатели зоны.
Сотрудник — учётка Seafile с почтой манифеста (вход в SeafDAV — почтой и
паролем). Файлы лежат в `<библиотека>/<подпапка>/<название>.<формат>`.
Общая библиотека у каждого сотрудника — своя «копия» (у SeafDAV нет
сквозного номера файла), поэтому один документ — несколько материалов в
kronto, и каждый виден только своему владельцу.

Отзыв доступа — убрать сотрудника из группы зоны. Перенос в другую
библиотеку — записать файл в неё и удалить из прежней (MOVE между
библиотеками SeafDAV не гарантирует), поэтому адрес документа меняется.
"""

import os
import time

import httpx

from corp_ed.domain.types import ConnectorMode
from tests.acceptance.model import Expected
from tests.acceptance.stand import STANDS, exec_in, restart, wait_ready
from tests.acceptance.systems.base import Creds, Driver, Placement, placements
from tests.acceptance.systems.nextcloud import MovingDav
from tests.live.dav_stand import PASSWORD, trust
from tests.live.seafile.seed import ADMIN, ADMIN_PASSWORD, SeafileApi, dav_root

SEAFDAV_CONF = "/shared/seafile/conf/seafdav.conf"


def group_name(zone: str) -> str:
    return f"acc-{zone}"


class SeafileDriver(Driver):
    mode = ConnectorMode.PER_USER
    modules = ("files",)
    seedable = True
    default_port = 8540

    def _ca(self) -> str:
        return os.environ.get("DAV_STAND_CA", "")

    def _url(self) -> str:
        assert self.creds is not None
        return self.creds.extra["url"]

    def _admin(self) -> SeafileApi:
        return SeafileApi(self._url(), ADMIN, ADMIN_PASSWORD, self._ca())

    def _dav(self) -> MovingDav:
        return MovingDav(dav_root(self._url()), ADMIN, ADMIN_PASSWORD, self._ca())

    def _path(self, zone: str, placement: Placement) -> str:
        library = self.manifest.zones[zone].title
        return f"{library}/{placement.document.folder}/{placement.name}"

    def _enable_seafdav(self) -> None:
        """Включить SeafDAV и перезапустить сервер; ждать, пока он ответит."""
        stand = STANDS["seafile"]
        exec_in(
            stand,
            "seafile",
            "sed",
            "-i",
            "s/^enabled = false$/enabled = true/",
            SEAFDAV_CONF,
        )
        restart(stand, "seafile")
        wait_ready(stand)
        dav = dav_root(self._url())
        deadline = time.monotonic() + 180
        with httpx.Client(verify=trust(self._ca()), timeout=15) as http:
            while time.monotonic() < deadline:
                try:
                    # Выключенный SeafDAV — 502 от nginx, включённый — 401.
                    if http.request("PROPFIND", dav).status_code == 401:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(3)
        raise RuntimeError("SeafDAV не включился за 3 минуты")

    def seed(self, expected: Expected) -> Creds:
        url = self._url()
        self._enable_seafdav()
        admin = self._admin()
        try:
            for employee in self.manifest.employees.values():
                admin.call(
                    "POST",
                    "/api/v2.1/admin/users/",
                    data={
                        "email": employee.email,
                        "password": PASSWORD,
                        "name": employee.login,
                    },
                )
            ids = admin.user_ids()
            for zone_id, zone in self.manifest.zones.items():
                admin.add_group(
                    group_name(zone_id),
                    [ids[login] for login in sorted(expected.members[zone_id])],
                )
                admin.add_library(zone.title)
        finally:
            admin.close()
        dav = self._dav()
        try:
            for placement in placements(expected, self.system):
                dav.put(self._path(placement.zone, placement), placement.content())
        finally:
            dav.close()
        admin = self._admin()
        try:
            for zone_id, zone in self.manifest.zones.items():
                admin.share(zone.title, "group", group_name(zone_id))
        finally:
            admin.close()
        users = {
            e.login: {"login": e.email, "password": PASSWORD}
            for e in self.manifest.employees.values()
        }
        return Creds(config={"server": dav_root(url)}, users=users, extra={"url": url})

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
        admin = self._admin()
        try:
            user = admin.user_ids()[login]
            for zone in zones:
                group = admin.group_id(group_name(zone))
                admin.call("DELETE", f"/api/v2.1/groups/{group}/members/{user}/")
        finally:
            admin.close()

    def move(self, placement: Placement, to_zone: str) -> None:
        dav = self._dav()
        try:
            dav.put(self._path(to_zone, placement), placement.content())
            dav.delete(self._path(placement.zone, placement))
        finally:
            dav.close()
