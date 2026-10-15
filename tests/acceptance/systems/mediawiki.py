"""MediaWiki (режим organization): засев Docker-стенда и шаги изменений.

Берётся закрытая вики стенда (`wiki_private`: читает только группа `staff`).
Зона манифеста — пространство имён `Zone_<зона>` (номера с 3100) со своей
группой `acc<зона>`; закрытые зоны закрывает расширение Lockdown:
`$wgNamespacePermissionLockdown[<номер>]['*'] = ['acc<зона>', 'sysop']`.
Открытая зона `public` — «читает любой из staff», группа этой зоны — сама
`staff`. Сотрудник манифеста — учётка вики с логином манифеста (первая буква
заглавная) в группах своих зон. Страница лежит под названием
`Zone_<зона>:<шифр>`, Markdown исходника переведён в викитекст.

Что умеет вики и что нет — и почему здесь `synced_zones = ["public"]`:
право `read` в MediaWiki одно на всю вики, Lockdown закрывает пространства,
но Action API показывает лишь «читает ли пользователь вообще». Адаптер
поэтому отдаёт ВСЕ страницы подключения одним списком читателей, а при
Lockdown требует от админа перечислить пространства, открытые всем
читателям. Подключение здесь перечисляет только `Zone_public`; закрытые
зоны лежат в вики и защищены Lockdown, но в kronto попадать не должны —
сценарий считает их попадание утечкой.

Отзыв доступа — снять с учётки группу зоны (maintenance `eval`);
у зоны `public` это `staff`, без неё нет и права `read`. Правка, удаление и
перенос — Action API бот-паролем `Admin@acc` (edit, delete, move).
"""

import os
import re
import secrets
from collections.abc import Iterable
from typing import Any

import httpx

from corp_ed.domain.types import ConnectorMode
from tests.acceptance.model import Expected
from tests.acceptance.stand import STANDS, exec_in
from tests.acceptance.systems.base import Creds, Driver, Placement, placements
from tests.live.dav_stand import PASSWORD, trust

SERVICE = "wiki_private"
ADMIN_PASSWORD = "Kronto-Test-1-wiki"
"""Пароль администратора вики из compose.yaml стенда (выдуманный)."""
BOT_KRONTO = "Admin@kronto"
BOT_SEED = "Admin@acc"
FIRST_NAMESPACE = 3100
WIKI_DIR = "/var/www/html"
DATA_DIR = "/var/www/data"
STAFF = "staff"
"""Группа, которой стенд даёт `read` (settings.php)."""


def namespace_name(zone: str) -> str:
    return f"Zone_{zone}"


def group_name(zone: str) -> str:
    """Группа зоны. У открытой зоны — `staff`: её право `read` и есть
    доступ к открытой зоне."""
    return STAFF if zone == "public" else f"acc{zone}"


def page_title(zone: str, doc_id: str) -> str:
    return f"{namespace_name(zone)}:{doc_id}"


def wiki_user(login: str) -> str:
    """Имя учётки в вики: первая буква заглавная ($wgCapitalLinks)."""
    return login[:1].upper() + login[1:]


def to_wikitext(markdown: str) -> str:
    """Markdown исходников → викитекст: заголовки, списки, таблицы, жирный."""
    out: list[str] = []
    in_table = False
    header_done = False

    def close_table() -> None:
        nonlocal in_table, header_done
        if in_table:
            out.append("|}")
            out.append("")
        in_table = header_done = False

    def inline(text: str) -> str:
        return re.sub(r"\*\*(.+?)\*\*", r"'''\1'''", text)

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if not in_table:
                out.append('{| class="wikitable"')
                in_table = True
            if not header_done:
                out.append("! " + " !! ".join(inline(c) for c in cells))
                header_done = True
            else:
                out.append("|-")
                out.append("| " + " || ".join(inline(c) for c in cells))
            continue
        close_table()
        if line.startswith("### "):
            out.append(f"=== {line[4:]} ===")
        elif line.startswith("## "):
            out.append(f"== {line[3:]} ==")
        elif line.startswith("# "):
            out.append(f"= {line[2:]} =")
        elif line.startswith("- "):
            out.append(f"* {inline(line[2:])}")
        elif re.match(r"\d+\. ", line):
            out.append(f"# {inline(line.split('. ', 1)[1])}")
        else:
            out.append(inline(line))
            # Строки подряд склеивались бы в один абзац.
            if line:
                out.append("")
    close_table()
    return "\n".join(out) + "\n"


def settings_php(zones: Iterable[str]) -> str:
    """Пространства имён зон и Lockdown закрытых зон."""
    lines = [
        "<?php",
        "// Зоны приёмки kronto: пространство на зону, Lockdown на закрытые.",
    ]
    for index, zone in enumerate(zones):
        ns = FIRST_NAMESPACE + 2 * index
        name = namespace_name(zone)
        lines.append(f"$wgExtraNamespaces[{ns}] = '{name}';")
        lines.append(f"$wgExtraNamespaces[{ns + 1}] = '{name}_talk';")
        if zone != "public":
            lines.append(
                f"$wgNamespacePermissionLockdown[{ns}]['*'] = "
                f"['{group_name(zone)}', 'sysop'];"
            )
    return "\n".join(lines) + "\n"


class WikiApi:
    """Action API от имени бот-пароля `Admin@acc` (edit, delete, move)."""

    def __init__(self, url: str, password: str, ca: str) -> None:
        self.url = url.rstrip("/") + "/api.php"
        self._http = httpx.Client(verify=trust(ca), timeout=60)
        token = self._get(meta="tokens", type="login")["query"]["tokens"]["logintoken"]
        answer = self._post(
            action="login", lgname=BOT_SEED, lgpassword=password, lgtoken=token
        )
        if answer["login"]["result"] != "Success":
            raise RuntimeError(f"вход бот-пароля: {answer['login']}")

    def close(self) -> None:
        self._http.close()

    def _get(self, **params: str) -> dict[str, Any]:
        response = self._http.get(
            self.url, params={"action": "query", "format": "json", **params}
        )
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]

    def _post(self, **data: str) -> dict[str, Any]:
        response = self._http.post(self.url, data={"format": "json", **data})
        response.raise_for_status()
        answer = response.json()
        if "error" in answer:
            raise RuntimeError(f"{data.get('action')}: {answer['error']}")
        return answer  # type: ignore[no-any-return]

    def csrf(self) -> str:
        return self._get(meta="tokens")["query"]["tokens"]["csrftoken"]  # type: ignore[no-any-return]

    def edit(self, title: str, text: str) -> None:
        self._post(
            action="edit", title=title, text=text, token=self.csrf(), summary="acc"
        )

    def delete(self, title: str) -> None:
        self._post(action="delete", title=title, token=self.csrf(), reason="acc")

    def move(self, source: str, target: str) -> None:
        self._post(
            action="move",
            **{"from": source, "to": target},
            noredirect="1",
            token=self.csrf(),
            reason="acc",
        )


class MediaWikiDriver(Driver):
    mode = ConnectorMode.ORGANIZATION
    modules = ("pages",)
    seedable = True
    default_port = 8541

    def _ca(self) -> str:
        return os.environ.get("DAV_STAND_CA", "")

    def _url(self) -> str:
        assert self.creds is not None
        return self.creds.extra["url"]

    def _stand(self):  # type: ignore[no-untyped-def]
        return STANDS["mediawiki"]

    def _php(self, script: str) -> str:
        return exec_in(
            self._stand(), SERVICE, "bash", "-s", user="www-data", input_text=script
        )

    def _api(self) -> WikiApi:
        assert self.creds is not None
        return WikiApi(self._url(), self.creds.extra["seed_password"], self._ca())

    def seed(self, expected: Expected) -> Creds:
        url = self._url()
        zones = list(self.manifest.zones)
        kronto_password = secrets.token_hex(16)
        seed_password = secrets.token_hex(16)
        lines = ["set -euo pipefail", f"cd {WIKI_DIR}"]
        run = "php maintenance/run.php"
        for employee in self.manifest.employees.values():
            groups = ",".join(
                [
                    group_name(zone)
                    for zone, members in expected.members.items()
                    if employee.login in members
                ]
            )
            lines.append(
                f"{run} createAndPromote --force {wiki_user(employee.login)} "
                f"{PASSWORD}-{employee.login} --custom-groups {groups} > /dev/null"
            )
        lines.append(
            f"cat > {DATA_DIR}/acc-settings.php <<'PHP'\n{settings_php(zones)}PHP"
        )
        lines.append(
            f"grep -q acc-settings {DATA_DIR}/LocalSettings.php || "
            f"echo \"require_once '{DATA_DIR}/acc-settings.php';\" "
            f">> {DATA_DIR}/LocalSettings.php"
        )
        # Названия пространств вики держит в кэше локализации и сама его не
        # обновляет: страницы «Zone_…:…» до пересборки легли бы в основное.
        lines.append(f"{run} rebuildLocalisationCache --force > /dev/null")
        lines.append(
            f"{run} createBotPassword --appid kronto --grants basic Admin "
            f"{kronto_password} > /dev/null"
        )
        lines.append(
            f"{run} createBotPassword --appid acc "
            f"--grants basic,editpage,createeditmovepage,delete Admin "
            f"{seed_password} > /dev/null"
        )
        self._php("\n".join(lines) + "\n")
        api = WikiApi(url, seed_password, self._ca())
        try:
            for placement in placements(expected, self.system):
                api.edit(
                    page_title(placement.zone, placement.document.id),
                    to_wikitext(placement.text()),
                )
        finally:
            api.close()
        mapping = "\n".join(
            f"{wiki_user(e.login)} = {e.email}"
            for e in self.manifest.employees.values()
            if e.login in self.system.accounts
        )
        synced = self.system.synced_zones or tuple(zones)
        return Creds(
            config={
                "api_url": url + "api.php",
                "user_emails": mapping,
                "namespaces": ",".join(namespace_name(z) for z in synced),
            },
            organization={
                "username": BOT_KRONTO,
                "password": kronto_password,
            },
            extra={"url": url, "seed_password": seed_password},
        )

    # --- шаги ---------------------------------------------------------------

    def edit(self, placement: Placement) -> None:
        api = self._api()
        try:
            api.edit(
                page_title(placement.zone, placement.document.id),
                to_wikitext(placement.text()),
            )
        finally:
            api.close()

    def delete(self, placement: Placement) -> None:
        api = self._api()
        try:
            api.delete(page_title(placement.zone, placement.document.id))
        finally:
            api.close()

    def revoke(self, login: str, zones: list[str]) -> None:
        user = wiki_user(login)
        code = "\n".join(
            f"$m->removeUserFromGroup("
            f"User::newFromName('{user}'), '{group_name(zone)}');"
            for zone in zones
        )
        script = (
            f"cd {WIKI_DIR}\n"
            "php maintenance/run.php eval <<'PHP' > /dev/null\n"
            "$m = MediaWiki\\MediaWikiServices::getInstance()->getUserGroupManager();\n"
            f"{code}\n"
            "PHP\n"
        )
        self._php(script)

    def move(self, placement: Placement, to_zone: str) -> None:
        api = self._api()
        try:
            api.move(
                page_title(placement.zone, placement.document.id),
                page_title(to_zone, placement.document.id),
            )
        finally:
            api.close()
