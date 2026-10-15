"""DokuWiki (режим organization): засев Docker-стенда и шаги изменений.

Пользователи и ACL в DokuWiki — файлы `conf/users.auth.php` и
`conf/acl.auth.php`, а не API, поэтому после запуска стенда драйвер
пишет их в контейнер заново (стенд читает их при каждом запросе): сотрудники
манифеста, группа `acc_<зона>` на каждую зону и правило «группе зоны —
чтение пространства зоны». Страницы — JSON-RPC от имени суперпользователя
`admin` (клиент из существующего seed.py). Название страницы — шифр
документа в нижнем регистре (`sales:sv-sal-001`): идентификаторы DokuWiki —
латиница, русское название лежит в заголовке.

Отзыв доступа — убрать группу зоны из строки пользователя; перенос в
закрытую папку — записать страницу в пространство другой зоны и удалить
прежнюю (пустой текст удаляет страницу).
"""

import os
import re

from corp_ed.domain.types import ConnectorMode
from tests.acceptance.model import Expected
from tests.acceptance.stand import STANDS, exec_in
from tests.acceptance.systems.base import Creds, Driver, Placement, placements
from tests.live.dav_stand import PASSWORD
from tests.live.dokuwiki.seed import ADMIN, DokuWikiApi

# bcrypt от пароля стенда (Kronto-Test-1) — тот же, что в
# tests/live/dokuwiki/conf/users.auth.php; пароль выдуманный.
PASSWORD_HASH = "$2y$12$QyALOR5T1uR7TgwvejeMGu3O/0TCJsKK7iasE1V2lte1EA6ESCN8O"
USERS_FILE = "/storage/conf/users.auth.php"
ACL_FILE = "/storage/conf/acl.auth.php"


def group_name(zone: str) -> str:
    return f"acc_{zone}"


def page_id(zone: str, doc_id: str) -> str:
    return f"{zone}:{doc_id.lower()}"


def to_dokuwiki(markdown: str) -> str:
    """Markdown исходников → разметка DokuWiki (заголовки, списки, таблицы)."""
    lines: list[str] = []
    header_done = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            lines.append(f"===== {line[3:]} =====")
        elif line.startswith("# "):
            lines.append(f"====== {line[2:]} ======")
        elif line.startswith("- "):
            lines.append(f"  * {line[2:]}")
        elif re.match(r"\d+\. ", line):
            lines.append(f"  - {line.split('. ', 1)[1]}")
        elif line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if not header_done:
                lines.append("^ " + " ^ ".join(cells) + " ^")
                header_done = True
            else:
                lines.append("| " + " | ".join(cells) + " |")
        else:
            header_done = False
            lines.append(line)
            # Строки подряд склеивались бы в один абзац.
            if line:
                lines.append("")
    return "\n".join(lines) + "\n"


class DokuWikiDriver(Driver):
    mode = ConnectorMode.ORGANIZATION
    modules = ("pages",)
    seedable = True
    default_port = 8529

    def _ca(self) -> str:
        return os.environ.get("DAV_STAND_CA", "")

    def _url(self) -> str:
        assert self.creds is not None
        return self.creds.extra["url"]

    def _api(self) -> DokuWikiApi:
        return DokuWikiApi(self._url(), ADMIN, self._ca())

    def _stand(self):  # type: ignore[no-untyped-def]
        return STANDS["dokuwiki"]

    def _write(self, path: str, text: str) -> None:
        exec_in(
            self._stand(),
            "dokuwiki",
            "sh",
            "-c",
            f"cat > {path}",
            input_text=text,
        )

    def _users_text(self, groups: dict[str, list[str]]) -> str:
        lines = [f"{ADMIN}:{PASSWORD_HASH}:Администратор:admin@example.com:admin,user"]
        for employee in self.manifest.employees.values():
            member = ",".join(["user", *groups.get(employee.login, [])])
            lines.append(
                f"{employee.login}:{PASSWORD_HASH}:{employee.name.replace(':', ' ')}"
                f":{employee.email}:{member}"
            )
        return "\n".join(lines) + "\n"

    def _acl_text(self) -> str:
        lines = ["*\t@ALL\t0", "*\t@user\t0"]
        lines += [f"{zone}:*\t@{group_name(zone)}\t1" for zone in self.manifest.zones]
        return "\n".join(lines) + "\n"

    def seed(self, expected: Expected) -> Creds:
        url = self._url()
        groups = {
            employee.login: [
                group_name(zone)
                for zone, members in expected.members.items()
                if employee.login in members
            ]
            for employee in self.manifest.employees.values()
        }
        self._write(USERS_FILE, self._users_text(groups))
        self._write(ACL_FILE, self._acl_text())
        api = self._api()
        try:
            for placement in placements(expected, self.system):
                api.save_page(
                    page_id(placement.zone, placement.document.id),
                    to_dokuwiki(placement.text()),
                )
        finally:
            api.close()
        users = "\n".join(
            f"{e.login} {e.email}" for e in self.manifest.employees.values()
        )
        return Creds(
            config={"base_url": url, "users": users},
            organization={"login": ADMIN, "password": PASSWORD},
            extra={"url": url},
        )

    # --- шаги ---------------------------------------------------------------

    def edit(self, placement: Placement) -> None:
        api = self._api()
        try:
            api.save_page(
                page_id(placement.zone, placement.document.id),
                to_dokuwiki(placement.text()),
            )
        finally:
            api.close()

    def delete(self, placement: Placement) -> None:
        api = self._api()
        try:
            api.save_page(page_id(placement.zone, placement.document.id), "")
        finally:
            api.close()

    def revoke(self, login: str, zones: list[str]) -> None:
        text = exec_in(self._stand(), "dokuwiki", "cat", USERS_FILE)
        out = []
        for line in text.splitlines():
            parts = line.split(":")
            if len(parts) >= 5 and parts[0] == login:
                drop = {group_name(z) for z in zones}
                parts[-1] = ",".join(g for g in parts[-1].split(",") if g not in drop)
                line = ":".join(parts)
            out.append(line)
        self._write(USERS_FILE, "\n".join(out) + "\n")

    def move(self, placement: Placement, to_zone: str) -> None:
        api = self._api()
        try:
            api.save_page(
                page_id(to_zone, placement.document.id), to_dokuwiki(placement.text())
            )
            api.save_page(page_id(placement.zone, placement.document.id), "")
        finally:
            api.close()
