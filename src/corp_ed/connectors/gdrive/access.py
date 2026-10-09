"""Кто видит файл Google Drive: разрешения (permissions) → почты или «вся
компания».

Тип разрешения (справочник Drive API v3, ресурс permissions):
- user — почта сотрудника (любая роль даёт чтение);
- group — состав группы из Directory API (`groups/{key}/members`),
  вложенные группы раскрываются, участник типа CUSTOMER («все в
  аккаунте») — вся компания. Группы чужих доменов Directory не отдаёт —
  не раскрываются (их участники прав не получают: в сторону закрытости);
- domain — вся компания, если домен — один из доменов подключения;
  чужой домен нашим сотрудникам ничего не даёт;
- anyone (по ссылке или в поиске) — вся компания: файл и так открыт
  любому, у кого есть ссылка.

Пропускаются удалённые (`deleted`) и разрешения представлений (`view`:
metadata — видна только папка, не содержимое; published — публикация в
интернете, не доступ к файлу).

Папка с ограниченным доступом (`inheritedPermissionsDisabled`): её
содержимое видят только добавленные в неё напрямую и организаторы
(владелец). Что permissions.list отдаёт по файлам внутри такой папки,
документация не говорит, поэтому читатели файла сужаются до читателей
папки, кроме выданных на сам файл напрямую (RISKS: сверить на живом
Workspace).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from corp_ed.connectors.base import AdapterAuthError, AdapterError
from corp_ed.connectors.common import path_segment
from corp_ed.connectors.gdrive.auth import GROUPS_SCOPE
from corp_ed.connectors.gdrive.client import GoogleClient

logger = structlog.get_logger()

MEMBERS_PAGE = 200
MAX_GROUP_DEPTH = 10
_ALWAYS_OPEN_ROLES = frozenset({"owner", "organizer"})
_SKIPPABLE = frozenset({"forbidden", "not_found", "http_400"})


@dataclass(frozen=True)
class Audience:
    """Читатели: вся компания или перечень почт (в нижнем регистре)."""

    everyone: bool = False
    emails: frozenset[str] = frozenset()

    def __or__(self, other: "Audience") -> "Audience":
        if self.everyone or other.everyone:
            return EVERYONE
        return Audience(emails=self.emails | other.emails)

    def __and__(self, other: "Audience") -> "Audience":
        if self.everyone:
            return other
        if other.everyone:
            return self
        return Audience(emails=self.emails & other.emails)


NOBODY = Audience()
EVERYONE = Audience(everyone=True)


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].casefold() if "@" in email else ""


def readable(permissions: Iterable[Any]) -> list[Mapping[str, Any]]:
    """Разрешения, дающие доступ к содержимому."""
    return [
        p
        for p in permissions
        if isinstance(p, Mapping) and not p.get("deleted") and not p.get("view")
    ]


def direct(permissions: Iterable[Any]) -> list[Mapping[str, Any]]:
    """Выданные на сам элемент, а не унаследованные (permissionDetails)."""
    found = []
    for p in readable(permissions):
        details = p.get("permissionDetails")
        if isinstance(details, list) and any(
            isinstance(d, Mapping) and d.get("inherited") is False for d in details
        ):
            found.append(p)
    return found


def opening(permissions: Iterable[Any]) -> list[Mapping[str, Any]]:
    """Кто видит содержимое папки с ограниченным доступом: добавленные
    напрямую и организаторы (владелец)."""
    direct_ids = {id(p) for p in direct(permissions)}
    return [
        p
        for p in readable(permissions)
        if id(p) in direct_ids or p.get("role") in _ALWAYS_OPEN_ROLES
    ]


class AccessResolver:
    """Разрешения → Audience с раскрытием групп; кеш групп — на запуск."""

    def __init__(
        self, client: GoogleClient, *, admin: str, domains: Sequence[str]
    ) -> None:
        self._client = client
        self._admin = admin
        self._domains = frozenset(d.casefold() for d in domains)
        self._groups: dict[str, Audience] = {}

    @property
    def domains(self) -> frozenset[str]:
        return self._domains

    def in_domain(self, email: str) -> bool:
        return email_domain(email) in self._domains

    async def audience(self, permissions: Iterable[Any]) -> Audience:
        result = NOBODY
        for p in readable(permissions):
            kind = p.get("type")
            if kind == "anyone":
                return EVERYONE
            if kind == "domain":
                if str(p.get("domain") or "").casefold() in self._domains:
                    return EVERYONE
                continue
            email = str(p.get("emailAddress") or "").strip().casefold()
            if not email:
                continue
            if kind == "user":
                result = result | Audience(emails=frozenset({email}))
            elif kind == "group":
                result = result | await self.group(email)
            if result.everyone:
                return result
        return result

    async def group(self, email: str, depth: int = 0) -> Audience:
        """Состав группы с вложенными; цикл и глубина — ограничены."""
        email = email.casefold()
        if email in self._groups:
            return self._groups[email]
        if not self.in_domain(email):
            # Directory отдаёт только группы своего аккаунта.
            logger.info("gdrive_group_foreign", domain=email_domain(email))
            self._groups[email] = NOBODY
            return NOBODY
        if depth > MAX_GROUP_DEPTH:
            logger.warning("gdrive_group_too_deep")
            return NOBODY
        # Защита от цикла: пока группа раскрывается, она пуста.
        self._groups[email] = NOBODY
        result = NOBODY
        nested: list[str] = []
        try:
            async for member in self._client.directory_pages(
                self._admin,
                GROUPS_SCOPE,
                f"groups/{path_segment(email)}/members",
                {
                    "maxResults": MEMBERS_PAGE,
                    "fields": "nextPageToken,members(email,type)",
                },
                "members",
            ):
                kind = str(member.get("type") or "").upper()
                value = str(member.get("email") or "").strip().casefold()
                if kind == "CUSTOMER":
                    result = EVERYONE
                elif kind == "GROUP" and value:
                    nested.append(value)
                elif value:
                    result = result | Audience(emails=frozenset({value}))
        except AdapterError as exc:
            if (
                exc.retryable
                or isinstance(exc, AdapterAuthError)
                or exc.code not in _SKIPPABLE
            ):
                raise
            # Группы нет или её не видно администратору: её участники прав
            # не получат.
            logger.warning("gdrive_group_unreadable", code=exc.code)
            return NOBODY
        for inner in nested:
            if result.everyone:
                break
            result = result | await self.group(inner, depth + 1)
        self._groups[email] = result
        return result
