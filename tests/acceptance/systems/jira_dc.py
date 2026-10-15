"""Jira Software Data Center (режим organization): засев Docker-стенда и
шаги изменений.

Стенд — образ atlassian/jira-software с тестовой лицензией Atlassian
(timebomb на 10 пользователей и 3 часа; ключ лежит вне репозитория,
`~/.kronto-acc/jira.key`, его вводит мастер первого запуска
`tests/live/jira_dc/wizard.mjs`: Playwright из frontend/node_modules,
CHROMIUM_PATH). Контейнеру нужно 4 ГБ памяти (`mem_limit` в compose). Мастер
печатает токен администратора; он же — токен подключения (адаптеру нужен
администратор Jira: кто видит задачу, Jira отвечает только ему). 10
пользователей — предел лицензии: администратор и восемь сотрудников
манифеста (в манифесте у системы восемь учёток, а не 18).

Зона манифеста — проект Jira (ключ — зона заглавными буквами) с СВОЕЙ
схемой прав: Browse Projects — группе `acc-<зона>` (читатели зоны) и
администратору (автор всех задач и руководитель проектов: схема прав
действует и на него, а ограничить так, чтобы он потерял доступ, нельзя).
Открытых («Любой») схем нет: даже у зоны `public` группа из всех, чтобы
отзыв доступа (выход из группы) был виден и для общих документов, а
«Любой» в kronto — все активные пользователи Jira, а не члены группы.
Документ — задача Task; описание — викиразметка Jira, переведённая из
Markdown исходника; название задачи — название документа.

Уровни безопасности в этом драйвере не используются: схему уровней REST
Jira DC не создаёт (только браузером, `security.mjs`), а права зоны
выражены схемой прав проекта; уровни проверены в `tests/live/jira_dc`.

Почта в Jira — `<логин>@severny-veter.test`, если Jira её принимает; иначе
`<логин>@example.com`, и тогда почты сотрудников kronto берутся с того же
домена (`extra.email_domain`) — адаптер отдаёт права по почтам из Jira.

Отзыв доступа — убрать пользователя из группы зоны. Правка — PUT описания
задачи; удаление — DELETE задачи. Перенос задачи между проектами REST Jira
DC не даёт (только «Переместить» в интерфейсе), поэтому задача создаётся
заново в проекте другой зоны, прежняя удаляется: ключ новый, как у
перемещённой в интерфейсе.
"""

import os
import re
import shutil
import subprocess
import sys
from typing import Any

from corp_ed.domain.types import ConnectorMode
from tests.acceptance.model import Expected
from tests.acceptance.stand import ROOT
from tests.acceptance.systems.base import Creds, Driver, Placement, placements
from tests.acceptance.systems.outline import node_dir
from tests.live.dav_stand import PASSWORD
from tests.live.jira_dc.seed import _ADMIN_PERMISSIONS, ADMIN, Seeder

WIZARD = "tests/live/jira_dc/wizard.mjs"
LICENSE_FILE = "~/.kronto-acc/jira.key"
TEMPLATE = "com.pyxis.greenhopper.jira:basic-software-development-template"


def project_key(zone: str) -> str:
    """Ключ проекта — зона заглавными. Короткие зоны (hr, it) получают
    суффикс: Jira превращает в ссылку на задачу любой текст вида `ХХ-001`
    для существующего проекта, и «SV-HR-001» в тексте документа читался бы
    как `SV-` и ссылка на HR-1 — шифр документа терялся."""
    key = zone.upper()
    return key if len(key) > 2 else f"{key}DOC"


def group_name(zone: str) -> str:
    return f"acc-{zone}"


def _inline(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)


def to_jira_markup(markdown: str) -> str:
    """Markdown исходников → викиразметка Jira: заголовки, списки,
    таблицы, жирный."""
    out: list[str] = []
    header_done = False
    in_table = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            in_table = True
            joined = [_inline(c) for c in cells]
            if not header_done:
                out.append("||" + "||".join(joined) + "||")
                header_done = True
            else:
                out.append("|" + "|".join(joined) + "|")
            continue
        if in_table:
            out.append("")
            in_table = header_done = False
        if line.startswith("### "):
            out.append(f"h3. {_inline(line[4:])}")
        elif line.startswith("## "):
            out.append(f"h2. {_inline(line[3:])}")
        elif line.startswith("# "):
            out.append(f"h1. {_inline(line[2:])}")
        elif line.startswith("- "):
            out.append(f"* {_inline(line[2:])}")
        elif re.match(r"\d+\. ", line):
            out.append(f"# {_inline(line.split('. ', 1)[1])}")
        else:
            out.append(_inline(line))
            if line:
                out.append("")
    return "\n".join(out) + "\n"


class JiraDriver(Driver):
    mode = ConnectorMode.ORGANIZATION
    modules = ("issues",)
    seedable = True
    default_port = 8544

    def _url(self) -> str:
        assert self.creds is not None
        return self.creds.extra["url"].rstrip("/")

    def _seeder(self) -> Seeder:
        assert self.creds and self.creds.organization
        return Seeder(self._url(), self.creds.organization["token"])

    def _wizard(self) -> str:
        """Мастер первого запуска в браузере; результат — токен админа."""
        node = shutil.which("node") or "node"
        license_key = os.path.expanduser(LICENSE_FILE)
        with open(license_key, encoding="utf-8") as handle:
            key = handle.read().strip()
        result = subprocess.run(
            [node, str(ROOT / WIZARD)],
            cwd=node_dir(),
            env={**os.environ, "JIRA_URL": self._url(), "JIRA_LICENSE": key},
            capture_output=True,
            text=True,
            check=False,
            timeout=1800,
        )
        if result.returncode != 0:
            sys.stderr.write(result.stdout[-1000:])
            raise RuntimeError(f"мастер Jira: {result.stderr[-400:]}")
        for line in reversed(result.stdout.splitlines()):
            if line.startswith("ADMIN_TOKEN="):
                return line.split("=", 1)[1].strip()
        raise RuntimeError("мастер Jira не напечатал ADMIN_TOKEN")

    def _create_user(self, seeder: Seeder, login: str, name: str, domain: str) -> str:
        body = {
            "name": login,
            "password": PASSWORD,
            "emailAddress": f"{login}@{domain}",
            "displayName": name,
        }
        return str(seeder.http.post("/user", json=body).status_code)

    def seed(self, expected: Expected) -> Creds:
        url = self._url()
        token = self._wizard()
        seeder = Seeder(url, token)
        domain = self.manifest.domain
        try:
            for zone in self.manifest.zones:
                seeder._check(  # noqa: SLF001 — клиент стенда из tests/live
                    seeder.http.post("/group", json={"name": group_name(zone)})
                )
            people = [
                e
                for e in self.manifest.employees.values()
                if e.login in self.system.accounts
            ]
            # Домен `.test` Jira может не принять — тогда почты на example.com.
            if (
                self._create_user(seeder, people[0].login, people[0].name, domain)
                != "201"
            ):
                domain = "example.com"
                code = self._create_user(
                    seeder, people[0].login, people[0].name, domain
                )
                if code != "201":
                    raise RuntimeError(f"пользователь {people[0].login}: HTTP {code}")
            for employee in people[1:]:
                code = self._create_user(seeder, employee.login, employee.name, domain)
                if code != "201":
                    raise RuntimeError(f"пользователь {employee.login}: HTTP {code}")
            for employee in people:
                for zone, members in expected.members.items():
                    if employee.login in members:
                        seeder.join(employee.login, group_name(zone))
            for zone_id, zone in self.manifest.zones.items():
                permissions: list[dict[str, Any]] = [
                    {
                        "holder": {"type": "group", "parameter": group_name(zone_id)},
                        "permission": "BROWSE_PROJECTS",
                    }
                ] + [
                    {"holder": {"type": "user", "parameter": ADMIN}, "permission": p}
                    for p in _ADMIN_PERMISSIONS
                ]
                scheme = seeder._check(  # noqa: SLF001
                    seeder.http.post(
                        "/permissionscheme",
                        json={"name": f"acc {zone_id}", "permissions": permissions},
                    )
                )
                seeder._check(  # noqa: SLF001
                    seeder.http.post(
                        "/project",
                        json={
                            "key": project_key(zone_id),
                            "name": zone.title,
                            "projectTypeKey": "software",
                            "projectTemplateKey": TEMPLATE,
                            "lead": ADMIN,
                            "permissionScheme": scheme["id"],
                        },
                    )
                )
            for placement in placements(expected, self.system):
                self._create(seeder, placement, placement.zone)
        finally:
            seeder.http.close()
        extra = {"url": url}
        if domain != self.manifest.domain:
            extra["email_domain"] = domain
        return Creds(
            config={
                "base_url": url,
                "projects": ",".join(project_key(z) for z in self.manifest.zones),
            },
            organization={"token": token},
            extra=extra,
        )

    @staticmethod
    def _create(seeder: Seeder, placement: Placement, zone: str) -> str:
        created = seeder._check(  # noqa: SLF001
            seeder.http.post(
                "/issue",
                json={
                    "fields": {
                        "project": {"key": project_key(zone)},
                        "summary": placement.document.title,
                        "description": to_jira_markup(placement.text()),
                        "issuetype": {"name": "Task"},
                    }
                },
            )
        )
        return str(created["key"])

    # --- шаги ---------------------------------------------------------------

    @staticmethod
    def _issue(seeder: Seeder, zone: str, placement: Placement) -> str:
        found = seeder._check(  # noqa: SLF001
            seeder.http.get(
                "/search",
                params={
                    "jql": f'project = "{project_key(zone)}"',
                    "fields": "summary",
                    "maxResults": 200,
                },
            )
        )
        for issue in found["issues"]:
            if issue["fields"]["summary"] == placement.document.title:
                return str(issue["key"])
        raise RuntimeError(f"нет задачи «{placement.document.title}» в {zone}")

    def edit(self, placement: Placement) -> None:
        seeder = self._seeder()
        try:
            key = self._issue(seeder, placement.zone, placement)
            seeder._check(  # noqa: SLF001
                seeder.http.put(
                    f"/issue/{key}",
                    json={"fields": {"description": to_jira_markup(placement.text())}},
                )
            )
        finally:
            seeder.http.close()

    def delete(self, placement: Placement) -> None:
        seeder = self._seeder()
        try:
            key = self._issue(seeder, placement.zone, placement)
            seeder._check(seeder.http.delete(f"/issue/{key}"))  # noqa: SLF001
        finally:
            seeder.http.close()

    def revoke(self, login: str, zones: list[str]) -> None:
        seeder = self._seeder()
        try:
            for zone in zones:
                seeder.leave(login, group_name(zone))
        finally:
            seeder.http.close()

    def move(self, placement: Placement, to_zone: str) -> None:
        seeder = self._seeder()
        try:
            old = self._issue(seeder, placement.zone, placement)
            self._create(seeder, placement, to_zone)
            seeder._check(seeder.http.delete(f"/issue/{old}"))  # noqa: SLF001
        finally:
            seeder.http.close()
