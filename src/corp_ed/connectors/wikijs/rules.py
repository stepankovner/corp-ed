"""Правила страниц Wiki.js 2.x: кто читает страницу (право `read:pages`).

Повторяет `checkAccess` и `_applyPageRuleSpecificity` из
`server/core/auth.js` (requarks/wiki v2.5.315, тот же файл в образе
2.5.316), как его вызывает показ страницы (`server/controllers/common.js`
— с тегами страницы):

1. Права пользователя — объединение `permissions` всех его групп.
   `manage:system` — видит всё; без `read:pages` — ничего.
2. Правила всех его групп, где в `roles` есть `read:pages`, а `locales`
   пусты или содержат локаль страницы, сравниваются с путём: START —
   `"/" + путь` начинается с `"/" + rule.path` (строкой, не по сегментам:
   правило «hr» задевает и «hr-old»), END — путь кончается на rule.path,
   EXACT — равен, TAG — у страницы есть тег rule.path, REGEX — `new
   RegExp(rule.path).test(путь)`.
3. Побеждает совпавшее правило с самым длинным `rule.path` (длина — в
   единицах UTF-16, как `String.length`); при равной длине — тип
   EXACT > TAG > REGEX > END > START; при равном типе — запрет. Порядок
   правил и групп на итог не влияет. Нет совпавших правил — не видит.

REGEX считается здесь, только если выражение из простого подмножества,
где JavaScript и Python `re` (с re.ASCII) понимают его одинаково и без
взрывного перебора (_translate). Иначе исход правила неизвестен, и
считается худший случай: неизвестное разрешение не совпало, неизвестный
запрет совпал. Итог монотонен (лишнее совпавшее разрешение может только
открыть, лишний запрет — только закрыть), поэтому «читает в худшем
случае» значит «читает при любом исходе». Только такие и получают
доступ.
"""

import re
import warnings
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

READ = "read:pages"
SYSTEM = "manage:system"

_RANK = {"START": 0, "END": 1, "REGEX": 2, "TAG": 3, "EXACT": 4}
MAX_REGEX_LENGTH = 200
MAX_QUANTIFIERS = 4
MAX_UNBOUNDED = 2
_ESCAPE = re.compile(r"\\(.)", re.DOTALL)
# Экранировать можно только \d \D \w \W \b \B и знаки препинания: \s, \u,
# \p, обратные ссылки и прочее JavaScript и Python понимают по-разному.
_ESCAPABLE = frozenset("dDwWbB.\\/-^$*+?()[]{}|")
# Группы (?…) кроме (?:, пустые и «любые» классы ([] и [^] в JavaScript),
# вложенные классы и {,n} — тоже по-разному.
_FORBIDDEN = re.compile(r"\(\?(?!:)|\[\^?\]|\[\[|\{,")
_QUANTIFIER = re.compile(r"[*+?]|\{\d+(?:,\d*)?\}")
_UNBOUNDED = re.compile(r"[*+]|\{\d+,\}")
# Квантификатор после группы — путь к взрывному перебору ((a+)+).
_QUANTIFIED_GROUP = re.compile(r"\)(?:[*+?]|\{\d)")


class Verdict(Enum):
    NO = 0
    YES = 1
    UNSURE = 2
    """Читает при одних исходах неизвестных правил и не читает при других:
    доступа не получает."""


@dataclass(frozen=True)
class PageRule:
    deny: bool
    match: str
    path: str
    roles: frozenset[str]
    locales: frozenset[str]

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "PageRule | None":
        """Правило из ответа groups.single; незнакомый тип — None."""
        match = str(raw.get("match") or "")
        if match not in _RANK or not isinstance(raw.get("path"), str):
            return None
        return cls(
            deny=raw.get("deny") is True,
            match=match,
            path=raw["path"],
            roles=frozenset(str(r) for r in raw.get("roles") or []),
            locales=frozenset(str(code) for code in raw.get("locales") or []),
        )


@dataclass(frozen=True)
class Group:
    id: int
    permissions: frozenset[str]
    rules: tuple[PageRule, ...]
    opaque: bool = False
    """Есть правило незнакомого типа (новая версия Wiki.js): его действие
    не посчитать, и участникам группы (кроме manage:system) ничего не
    открывается."""


@dataclass(frozen=True)
class Page:
    path: str
    locale: str
    tags: frozenset[str]


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _matches(rule: PageRule, page: Page) -> bool | None:
    """Совпало ли правило; None — не можем сказать (REGEX)."""
    if rule.match == "START":
        return f"/{page.path}".startswith(f"/{rule.path}")
    if rule.match == "END":
        return page.path.endswith(rule.path)
    if rule.match == "EXACT":
        return page.path == rule.path
    if rule.match == "TAG":
        return rule.path in page.tags
    pattern = portable_regex(rule.path)
    if pattern is None or any(c in page.path for c in "\n\r  "):
        return None
    return pattern.search(page.path) is not None


_compiled: dict[str, re.Pattern[str] | None] = {}


def portable_regex(source: str) -> re.Pattern[str] | None:
    """Выражение JavaScript → то же в Python или None, если не уверены."""
    if source not in _compiled:
        _compiled[source] = _translate(source)
    return _compiled[source]


def _translate(source: str) -> re.Pattern[str] | None:
    if not source or len(source) > MAX_REGEX_LENGTH or source.endswith("\\"):
        return None
    if any(ord(char) > 0xFFFF or ord(char) < 0x20 for char in source):
        return None
    if any(escaped not in _ESCAPABLE for escaped in _ESCAPE.findall(source)):
        return None
    # Структура — без экранированных знаков: «\)» не закрывает группу.
    bare = _ESCAPE.sub("e", source)
    if _FORBIDDEN.search(bare) or _QUANTIFIED_GROUP.search(bare):
        return None
    if len(_QUANTIFIER.findall(bare)) > MAX_QUANTIFIERS:
        return None
    if len(_UNBOUNDED.findall(bare)) > MAX_UNBOUNDED:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            return re.compile(source, re.ASCII)
    except (re.error, Warning):
        return None


def _winner_allows(groups: Sequence[Group], page: Page, *, worst: bool) -> bool:
    """Итог правил; worst — неизвестные разрешения не совпали, запреты
    совпали; иначе наоборот (лучший случай)."""
    best: tuple[int, int, bool] | None = None
    for group in groups:
        for rule in group.rules:
            if READ not in rule.roles:
                continue
            if rule.locales and page.locale not in rule.locales:
                continue
            matched = _matches(rule, page)
            if matched is None:
                matched = rule.deny if worst else not rule.deny
            if not matched:
                continue
            key = (utf16_length(rule.path), _RANK[rule.match], rule.deny)
            if best is None or key > best:
                best = key
    return best is not None and not best[2]


def can_read(groups: Sequence[Group], page: Page) -> Verdict:
    """Читает ли страницу пользователь с этими группами (checkAccess)."""
    permissions = frozenset().union(*(g.permissions for g in groups))
    if SYSTEM in permissions:
        return Verdict.YES
    if READ not in permissions:
        return Verdict.NO
    if any(group.opaque for group in groups):
        return Verdict.UNSURE
    if _winner_allows(groups, page, worst=True):
        return Verdict.YES
    if _winner_allows(groups, page, worst=False):
        return Verdict.UNSURE
    return Verdict.NO


def readers(
    memberships: Mapping[int, frozenset[int]],
    groups: Mapping[int, Group],
    page: Page,
) -> tuple[set[int], int]:
    """Кто из пользователей (id → id групп) точно читает страницу и
    скольким доступ не выдан из-за неизвестного исхода. Одинаковые наборы
    групп считаются один раз."""
    verdicts: dict[frozenset[int], Verdict] = {}
    allowed: set[int] = set()
    unsure = 0
    for user, group_ids in memberships.items():
        if group_ids not in verdicts:
            verdicts[group_ids] = can_read(_groups(groups, group_ids), page)
        verdict = verdicts[group_ids]
        if verdict is Verdict.YES:
            allowed.add(user)
        elif verdict is Verdict.UNSURE:
            unsure += 1
    return allowed, unsure


def _groups(groups: Mapping[int, Group], ids: Iterable[int]) -> list[Group]:
    return [groups[i] for i in sorted(ids) if i in groups]
