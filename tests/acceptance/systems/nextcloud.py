"""Nextcloud и ownCloud 10 (режим per_user, пароль приложения): засев
Docker-стенда и шаги изменений.

Зона манифеста — папка администратора стенда, расшаренная группе
`acc-<зона>` только на чтение; в группу входят читатели зоны. Файлы лежат в
`<зона>/<подпапка>/<название>.<формат>`: докумет в формате из манифеста
(docx, xlsx, pdf…), собранный `corpus/build.py`. Сотрудник видит общую
папку у себя в корне, а kronto получает по нему один материал на файл
(сквозной oc:fileid) — права берутся из того, что видит этот сотрудник.

Отзыв доступа — убрать сотрудника из группы зоны; перенос в закрытую
папку — WebDAV MOVE между папками зон (номер файла не меняется).
Вход сотрудника kronto — пароль приложения, выпущенный из пароля входа.
"""

import os
import subprocess
import sys
from collections.abc import Callable

from corp_ed.domain.types import ConnectorMode
from tests.acceptance.model import Expected
from tests.acceptance.systems.base import Creds, Driver, Placement, placements
from tests.live.dav_stand import PASSWORD, DavSeeder
from tests.live.nextcloud.seed import (
    ADMIN,
    ADMIN_PASSWORD,
    SHARE_GROUP,
    Ocs,
    dav_root,
)
from tests.live.nextcloud.seed import app_password as nextcloud_app_password
from tests.live.owncloud.seed import app_password as owncloud_app_password


def group_name(zone: str) -> str:
    return f"acc-{zone}"


class MovingDav(DavSeeder):
    """DavSeeder с переносом (MOVE): в существующем нет."""

    def move(self, source: str, target: str) -> None:
        folder = target.rpartition("/")[0]
        if folder:
            self.mkdir(folder)
        response = self._http.request(
            "MOVE",
            self.url(source),
            headers={"Destination": self.url(target), "Overwrite": "T"},
        )
        if response.status_code not in (201, 204):
            response.raise_for_status()


class OcFamilyDriver(Driver):
    mode = ConnectorMode.PER_USER
    modules = ("files",)
    seedable = True
    family = "nextcloud"
    app_password: Callable[..., str]
    service = "nextcloud"

    def _ca(self) -> str:
        return os.environ.get("DAV_STAND_CA", "")

    def _url(self) -> str:
        assert self.creds is not None
        return self.creds.extra["url"]

    def _path(self, zone: str, placement: Placement) -> str:
        zone_title = self.manifest.zones[zone].title
        return f"{zone_title}/{placement.document.folder}/{placement.name}"

    def _dav(self) -> MovingDav:
        url = self._url()
        return MovingDav(dav_root(url, ADMIN), ADMIN, ADMIN_PASSWORD, self._ca())

    def _ocs(self) -> Ocs:
        return Ocs(self._url(), ADMIN, ADMIN_PASSWORD, self._ca())

    def _disable_skeleton(self) -> None:
        """Файлы-образцы, которые сервер кладёт новому пользователю
        (Readme, руководство в PDF на мегабайты), в проверке не нужны: без
        них синхронизация 18 сотрудников не качает 18 копий руководства."""
        from tests.acceptance.stand import STANDS, exec_in

        stand = STANDS.get(self.system.id)
        if stand is None:
            return
        user = "www-data" if self.family == "nextcloud" else None
        command = (["php", "occ"] if self.family == "nextcloud" else ["occ"]) + [
            "config:system:set",
            "skeletondirectory",
            "--value=",
        ]
        try:
            exec_in(stand, self.service, *command, user=user)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            print(f"файлы-образцы не выключены: {exc}", file=sys.stderr)

    def seed(self, expected: Expected) -> Creds:
        url = self._url()
        self._disable_skeleton()
        ocs = self._ocs()
        try:
            for zone in self.manifest.zones:
                ocs.add_group(group_name(zone))
            for employee in self.manifest.employees.values():
                groups = [
                    group_name(z)
                    for z, members in expected.members.items()
                    if employee.login in members
                ]
                ocs.add_user(employee.login, PASSWORD, groups)
        finally:
            ocs.close()
        dav = self._dav()
        try:
            for zone in self.manifest.zones.values():
                dav.mkdir(zone.title)
            for placement in placements(expected, self.system):
                dav.put(self._path(placement.zone, placement), placement.content())
        finally:
            dav.close()
        ocs = self._ocs()
        try:
            for zone_id, zone in self.manifest.zones.items():
                ocs.share(zone.title, SHARE_GROUP, group_name(zone_id))
        finally:
            ocs.close()
        users = {
            login: {
                "login": login,
                "password": type(self).app_password(url, login, PASSWORD, self._ca()),
            }
            for login in self.manifest.employees
        }
        return Creds(
            config={"server": url},
            users=users,
            extra={"url": url},
        )

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
        ocs = self._ocs()
        try:
            for zone in zones:
                ocs.call(
                    "DELETE", f"cloud/users/{login}/groups", groupid=group_name(zone)
                )
        finally:
            ocs.close()

    def move(self, placement: Placement, to_zone: str) -> None:
        dav = self._dav()
        try:
            dav.move(
                self._path(placement.zone, placement), self._path(to_zone, placement)
            )
        finally:
            dav.close()


class NextcloudDriver(OcFamilyDriver):
    family = "nextcloud"
    service = "nextcloud"
    default_port = 8521
    app_password = staticmethod(nextcloud_app_password)  # type: ignore[assignment]


class OwnCloudDriver(OcFamilyDriver):
    family = "owncloud"
    service = "owncloud"
    default_port = 8522
    app_password = staticmethod(owncloud_app_password)  # type: ignore[assignment]
