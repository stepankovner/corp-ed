"""Права MediaWiki: кто из сотрудников читает вики и какие пространства
имён можно брать.

Прав на отдельную страницу в MediaWiki нет: право `read` выдаётся группам
на всю вики (`$wgGroupPermissions`). Почт пользователей Action API не
отдаёт никому, кроме самого пользователя (`list=allusers` и `list=users`
их не содержат, `meta=userinfo&uiprop=email` — только своя), поэтому
сопоставление «логин в вики = почта» вводит админ компании. Каждый логин
проверяется в вики (`list=users&usprop=rights|blockinfo`): читатель —
учётка существует, не заблокирована на всей вики и у неё есть право
`read` с учётом всех её групп, отзыва прав и хуков `UserGetRights` (так
считает сам MediaWiki). Логин, которого в вики нет, — не читатель.

Расширения вроде Lockdown закрывают отдельные пространства имён группам,
и API этого не показывает. Если в `meta=siteinfo&siprop=extensions` есть
такое расширение (список и признаки — ниже), без явного списка
пространств в настройке подключения не синхронизируется ничего: админ
перечисляет пространства, открытые всем, у кого есть `read`. Хуки прав в
самом LocalSettings.php API не видны вовсе — это ограничение вида.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from corp_ed.connectors.base import AdapterConfigError
from corp_ed.connectors.common import to_int

USERS_BATCH = 50
"""ususers — до 50 имён за запрос (без apihighlimits)."""
MAX_MAPPING = 5000
NS_MAIN = 0
NS_FILE = 6

# Расширения, которые закрывают чтение по пространствам имён, категориям
# или страницам (mediawiki.org, Category:Page specific user rights
# extensions), и признаки в имени для незнакомых: лучше лишний раз
# попросить у админа список пространств, чем открыть закрытое.
READ_RESTRICTING = frozenset(
    {
        "lockdown",
        "nsfilerepo",
        "accesscontrol",
        "haloacl",
        "intraacl",
        "simplesecurity",
        "categorylockdown",
        "restrictaccessbycategoryandgroup",
        "permissionacl",
        "bluespicepermissionmanager",
        "bluespicenamespacemanager",
        "privatepageprotection",
        "pagepermissions",
    }
)
_RESTRICTING_MARKERS = (
    "lockdown",
    "acl",
    "accesscontrol",
    "permission",
    "restrict",
    "security",
    "private",
)
_MAIN_ALIASES = frozenset({"основное", "(основное)", "main", "(main)", "статьи"})
_EMAIL = re.compile(r"^[^@\s,;=]+@[^@\s,;=]+\.[^@\s,;=]+$")
_SEPARATORS = re.compile(r"[\n,;]+")


# --- сопоставление «логин = почта» ------------------------------------------------


def user_key(name: str) -> str:
    """Имя пользователя, как его хранит MediaWiki: `_` → пробел, пробелы
    схлопнуты, первая буква — заглавная ($wgCapitalLinks по умолчанию)."""
    clean = " ".join(name.replace("_", " ").split())
    return clean[:1].upper() + clean[1:]


def parse_user_emails(value: str) -> dict[str, str]:
    """`Логин = почта` по одному на строку (или через запятую) → словарь
    нормализованный логин → почта. Ошибка формы — AdapterConfigError."""
    mapping: dict[str, str] = {}
    for entry in _SEPARATORS.split(value or ""):
        if not entry.strip():
            continue
        login, sep, email = entry.partition("=")
        login, email = user_key(login), email.strip().lower()
        if not sep or not login or not _EMAIL.match(email):
            raise AdapterConfigError("user_emails_invalid")
        mapping[login] = email
    if not mapping:
        raise AdapterConfigError("user_emails_required")
    if len(mapping) > MAX_MAPPING:
        raise AdapterConfigError("user_emails_too_many")
    return mapping


def user_emails_problem(value: str) -> str | None:
    try:
        parse_user_emails(value)
    except AdapterConfigError as exc:
        return exc.code
    return None


@dataclass(frozen=True)
class Readers:
    emails: frozenset[str]
    missing: int = 0
    """Логинов из сопоставления нет в вики (или имя недопустимо)."""
    blocked: int = 0
    no_read: int = 0


def readers_from(
    users: Iterable[Mapping[str, Any]], mapping: Mapping[str, str]
) -> Readers:
    """Ответы `list=users` (rights, blockinfo) → почты читателей вики."""
    found = {
        user_key(str(user.get("name") or "")): user
        for user in users
        if isinstance(user, Mapping)
    }
    emails: set[str] = set()
    missing = blocked = no_read = 0
    for login, email in mapping.items():
        user = found.get(login)
        if user is None or user.get("missing") or user.get("invalid"):
            missing += 1
            continue
        if user.get("userid") is None:
            missing += 1
            continue
        if user.get("blockid") is not None and user.get("blockpartial") is not True:
            # Блокировка на всей вики: в MediaWiki она не всегда закрывает
            # чтение, но доступ в kronto не даём — в сторону закрытости.
            blocked += 1
            continue
        rights = user.get("rights")
        if not isinstance(rights, list) or "read" not in rights:
            no_read += 1
            continue
        emails.add(email)
    return Readers(frozenset(emails), missing, blocked, no_read)


# --- пространства имён ------------------------------------------------------------


@dataclass(frozen=True)
class Namespace:
    id: int
    name: str
    canonical: str
    content: bool


def namespaces_from(info: Mapping[str, Any]) -> dict[int, Namespace]:
    """`siprop=namespaces` (formatversion=2: объект id → описание)."""
    raw = info.get("namespaces")
    items = raw.values() if isinstance(raw, Mapping) else raw or []
    found: dict[int, Namespace] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        ns = to_int(item.get("id"))
        if ns is None or ns < 0:
            continue
        found[ns] = Namespace(
            id=ns,
            name=str(item.get("name") or ""),
            canonical=str(item.get("canonical") or ""),
            content=item.get("content") is True,
        )
    return found


def split_namespaces(value: str) -> list[str]:
    return [part.strip() for part in _SEPARATORS.split(value or "") if part.strip()]


def namespaces_problem(value: str) -> str | None:
    """Проверка формы до записи: только непустые элементы разумной длины."""
    parts = split_namespaces(value)
    if len(parts) > 200 or any(len(part) > 100 for part in parts):
        return "namespaces_invalid"
    return None


def resolve_namespaces(
    tokens: Sequence[str],
    namespaces: Mapping[int, Namespace],
    aliases: Iterable[Mapping[str, Any]] = (),
) -> list[int]:
    """Номера или названия (местные, канонические, синонимы) → номера.
    Незнакомое или служебное (Служебная, Медиа) — AdapterConfigError."""
    by_name: dict[str, int] = {}
    for ns in namespaces.values():
        for name in (ns.name, ns.canonical):
            if name:
                by_name[_name_key(name)] = ns.id
    for alias in aliases:
        ns_id = to_int(alias.get("id"))
        alias_name = alias.get("alias")
        if ns_id is not None and ns_id in namespaces and isinstance(alias_name, str):
            by_name.setdefault(_name_key(alias_name), ns_id)
    chosen: list[int] = []
    for token in tokens:
        number = to_int(token)
        if number is None:
            key = _name_key(token)
            number = NS_MAIN if key in _MAIN_ALIASES else by_name.get(key)
        if number is None or number not in namespaces:
            raise AdapterConfigError("namespace_unknown")
        if number not in chosen:
            chosen.append(number)
    return chosen


def restricting_extensions(info: Mapping[str, Any]) -> list[str]:
    """Расширения из `siprop=extensions`, которые могут закрывать чтение
    по пространствам имён или страницам."""
    found: list[str] = []
    raw = info.get("extensions")
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping) or item.get("type") == "skin":
            continue
        name = str(item.get("name") or "")
        key = re.sub(r"[\s_\-]", "", name.lower())
        if key in READ_RESTRICTING or any(m in key for m in _RESTRICTING_MARKERS):
            found.append(name)
    return found


def _name_key(name: str) -> str:
    return " ".join(name.replace("_", " ").split()).casefold()
