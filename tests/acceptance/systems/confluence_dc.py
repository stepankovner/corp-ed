"""Confluence Data Center (режим organization): засев Docker-стенда и шаги
изменений.

Стенд — образ atlassian/confluence с тестовой лицензией Atlassian
(timebomb на 10 пользователей и 3 часа; ключ лежит вне репозитория,
`~/.kronto-acc/confluence.key`, на стенд его передаёт compose как
ATL_LICENSE_KEY). Мастер первого запуска проходится в браузере
(`tests/live/confluence_dc/wizard.mjs`, Playwright из frontend/node_modules,
CHROMIUM_PATH) и печатает токен администратора. 10 пользователей —
предел лицензии: администратор и восемь сотрудников манифеста (поэтому у
системы в манифесте восемь учёток, а не 18); службной учёткой подключения
служит сам администратор — он есть в каждом ограничении страниц.

Почта в Confluence — `<логин>@example.com`: `.test` он не принимает («Invalid
email address»). Адаптеру это не мешает: он собирает почты читателей по
шаблону `email_template` (`{username}@severny-veter.test`) из имени
пользователя, а не из почты в Confluence.

Зона манифеста — пространство Confluence (ключ — зона заглавными буквами)
и группа `acc-<зона>`; в группу входят читатели зоны. Права Confluence на
уровне пространства REST не отдаёт, поэтому права зоны выражены ограничением
чтения КАЖДОЙ страницы: «читает группа зоны» (плюс администратор — Confluence
не даёт ограничить страницу так, чтобы её автор потерял доступ).
Главную страницу пространства драйвер тоже закрывает группой зоны: иначе
она без ограничений и в kronto «видна всей компании». Открытых страниц без
ограничений нет — даже у зоны `public` группа из всех: отзыв доступа
(выход из группы) должен быть виден и для общих документов, а
страница без ограничений в kronto — «вся компания», а не члены группы.

Отзыв доступа — убрать пользователя из группы зоны; правка — PUT
страницы с новой версией; удаление — страница в корзину. Перенос
страницы REST Confluence 10.2 не даёт (`PUT …/content/{id}/move/append/{id}` —
404, проверено на стенде), поэтому документ создаётся заново в пространстве другой
зоны с ограничением её группы, прежняя страница удаляется: у документа
меняется адрес, как при переносе в интерфейсе (ключ страницы тот же, id —
новый).
"""

import html
import os
import re
import shutil
import subprocess
import sys
from functools import partial

from corp_ed.domain.types import ConnectorMode
from tests.acceptance.model import Expected
from tests.acceptance.stand import ROOT
from tests.acceptance.systems.base import Creds, Driver, Placement, placements
from tests.acceptance.systems.outline import node_dir
from tests.live.confluence_dc.seed import Page, Seeder
from tests.live.dav_stand import PASSWORD

WIZARD = "tests/live/confluence_dc/wizard.mjs"


def space_key(zone: str) -> str:
    return zone.upper()


def group_name(zone: str) -> str:
    return f"acc-{zone}"


def _inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)


def to_storage(markdown: str) -> str:
    """Markdown исходников → формат хранения Confluence (XHTML): заголовки,
    абзацы, списки, таблицы, жирный."""
    out: list[str] = []
    state = {"list": "", "table": False, "header": False}

    def close() -> None:
        if state["list"]:
            out.append(f"</{state['list']}>")
            state["list"] = ""
        if state["table"]:
            out.append("</tbody></table>")
            state["table"] = False
            state["header"] = False

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if state["list"]:
                close()
            if not state["table"]:
                out.append("<table><tbody>")
                state["table"] = True
            tag = "td" if state["header"] else "th"
            state["header"] = True
            row = "".join(f"<{tag}>{_inline(c)}</{tag}>" for c in cells)
            out.append(f"<tr>{row}</tr>")
            continue
        item = re.match(r"(?:- |\d+\. )(.*)", line)
        if item:
            kind = "ul" if line.startswith("- ") else "ol"
            if state["table"]:
                close()
            if state["list"] != kind:
                close()
                out.append(f"<{kind}>")
                state["list"] = kind
            out.append(f"<li>{_inline(item.group(1))}</li>")
            continue
        close()
        if line.startswith("## "):
            out.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line:
            out.append(f"<p>{_inline(line)}</p>")
    close()
    return "\n".join(out) + "\n"


class ConfluenceDriver(Driver):
    mode = ConnectorMode.ORGANIZATION
    modules = ("pages",)
    seedable = True
    default_port = 8543

    def _url(self) -> str:
        assert self.creds is not None
        return self.creds.extra["url"].rstrip("/")

    def _seeder(self) -> Seeder:
        assert self.creds and self.creds.organization
        return Seeder(self._url(), self.creds.organization["token"])

    def _wizard(self) -> str:
        """Мастер первого запуска в браузере; результат — токен админа."""
        node = shutil.which("node") or "node"
        result = subprocess.run(
            [node, str(ROOT / WIZARD)],
            cwd=node_dir(),
            env={**os.environ, "CONFLUENCE_URL": self._url()},
            capture_output=True,
            text=True,
            check=False,
            timeout=1200,
        )
        if result.returncode != 0:
            sys.stderr.write(result.stdout[-1000:])
            raise RuntimeError(f"мастер Confluence: {result.stderr[-400:]}")
        for line in reversed(result.stdout.splitlines()):
            if line.startswith("ADMIN_TOKEN="):
                return line.split("=", 1)[1].strip()
        raise RuntimeError("мастер Confluence не напечатал ADMIN_TOKEN")

    def seed(self, expected: Expected) -> Creds:
        url = self._url()
        token = self._wizard()
        seeder = Seeder(url, token)
        try:
            for zone in self.manifest.zones:
                seeder._admin(  # noqa: SLF001 — клиент стенда из tests/live
                    partial(
                        seeder.http.post,
                        "/admin/group",
                        json={"type": "group", "name": group_name(zone)},
                    ),
                    "addGroup",
                    group_name(zone),
                )
            for employee in self.manifest.employees.values():
                if employee.login not in self.system.accounts:
                    continue
                seeder._admin(  # noqa: SLF001
                    partial(
                        seeder.http.post,
                        "/admin/user",
                        json={
                            "userName": employee.login,
                            "fullName": employee.name,
                            "email": f"{employee.login}@example.com",
                            "password": PASSWORD,
                            "notifyViaEmail": False,
                        },
                    ),
                    "addUser",
                    {
                        "name": employee.login,
                        "fullname": employee.name,
                        "email": f"{employee.login}@example.com",
                    },
                    PASSWORD,
                )
                for zone, members in expected.members.items():
                    if employee.login in members:
                        seeder.join(employee.login, group_name(zone))
            for zone_id, zone in self.manifest.zones.items():
                key = space_key(zone_id)
                seeder._check(  # noqa: SLF001
                    seeder.http.post("/space", json={"key": key, "name": zone.title})
                )
                home = seeder._check(  # noqa: SLF001
                    seeder.http.get(f"/space/{key}", params={"expand": "homepage"})
                ).json()["homepage"]["id"]
                seeder.restrict(str(home), groups=[group_name(zone_id)])
            for placement in placements(expected, self.system):
                self._create(seeder, placement, placement.zone)
        finally:
            seeder.close()
        return Creds(
            config={
                "base_url": url,
                "spaces": ",".join(space_key(z) for z in self.manifest.zones),
                "email_template": "{username}@" + self.manifest.domain,
            },
            organization={"token": token},
            extra={"url": url},
        )

    @staticmethod
    def _create(seeder: Seeder, placement: Placement, zone: str) -> str:
        return seeder.create_page(
            Page(
                title=placement.document.title,
                space=space_key(zone),
                body=to_storage(placement.text()),
                groups=[group_name(zone)],
            )
        )

    # --- шаги ---------------------------------------------------------------

    def _page_id(self, seeder: Seeder, zone: str, placement: Placement) -> str:
        return seeder.page_id(space_key(zone), placement.document.title)

    def edit(self, placement: Placement) -> None:
        seeder = self._seeder()
        try:
            page = self._page_id(seeder, placement.zone, placement)
            version = seeder._check(  # noqa: SLF001
                seeder.http.get(f"/content/{page}", params={"expand": "version"})
            ).json()["version"]["number"]
            seeder._check(  # noqa: SLF001
                seeder.http.put(
                    f"/content/{page}",
                    json={
                        "type": "page",
                        "title": placement.document.title,
                        "version": {"number": int(version) + 1},
                        "body": {
                            "storage": {
                                "value": to_storage(placement.text()),
                                "representation": "storage",
                            }
                        },
                    },
                )
            )
        finally:
            seeder.close()

    def delete(self, placement: Placement) -> None:
        seeder = self._seeder()
        try:
            seeder.trash(self._page_id(seeder, placement.zone, placement))
        finally:
            seeder.close()

    def revoke(self, login: str, zones: list[str]) -> None:
        seeder = self._seeder()
        try:
            for zone in zones:
                seeder.leave(login, group_name(zone))
        finally:
            seeder.close()

    def move(self, placement: Placement, to_zone: str) -> None:
        seeder = self._seeder()
        try:
            old = self._page_id(seeder, placement.zone, placement)
            self._create(seeder, placement, to_zone)
            seeder.trash(old)
        finally:
            seeder.close()
