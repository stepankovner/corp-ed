"""Кто может читать файл: DACL + каталог AD → множество пользователей.

Проверка доступа Windows идёт по ACE DACL сверху вниз для маркера
пользователя: запрет бита, ещё не выданного раньше, — отказ; разрешение
— выдача. Здесь то же самое, но сразу для множеств пользователей:
каждая ACE применяется ко всем пользователям, чей маркер содержит её SID
(сам пользователь, его группы транзитивно, широкие SID).

Порядок. Windows проверяет ACE в порядке хранения; канонический порядок —
явные запреты, явные разрешения, затем унаследованные (как их записал
сервер при наследовании). Для канонического DACL оба порядка дают одно и
то же; для неканонического (собран вручную через API) берём более
строгий результат — пересечение двух проходов.

Что не удалось вычислить однозначно, не даёт прав никому:
- разрешение по SID, которого нет в каталоге (чужой домен, локальная
  группа сервера без сопоставления в настройках), — пропускается;
- запрет по такому SID — считается запретом для всех, кому чтение ещё не
  выдано (агент не может доказать, что пользователь не в этой группе);
- условные/объектные разрешения не засчитываются, запреты — безусловно;
- неизвестный тип ACE — файл не виден никому.
Счётчики пропусков уходят в отчёт запуска.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from kronto_agent.security import Ace, SecurityDescriptor

# Есть в маркере любого вошедшего по сети пользователя домена: Everyone,
# Authenticated Users, NETWORK, This Organization. Domain Users (…-513)
# каталог раскрывает сам — нужен SID домена.
WIDE_SIDS = frozenset({"S-1-1-0", "S-1-5-11", "S-1-5-2", "S-1-5-15"})
OWNER_RIGHTS = "S-1-3-4"
# Не бывают в маркере пользователя при входе по SMB: заместители
# CREATOR OWNER/GROUP, учётки служб и системы, анонимный и интерактивный
# вход, метки целостности, пакеты приложений.
NEVER_IN_NETWORK_TOKEN = frozenset(
    {
        "S-1-3-0",
        "S-1-3-1",
        "S-1-3-2",
        "S-1-3-3",
        "S-1-5-4",
        "S-1-5-6",
        "S-1-5-7",
        "S-1-5-18",
        "S-1-5-19",
        "S-1-5-20",
    }
)
NEVER_IN_NETWORK_TOKEN_PREFIXES = ("S-1-5-80-", "S-1-5-82-", "S-1-15-", "S-1-16-")


class Directory(Protocol):
    """Каталог пользователей (LDAP AD или поддельный в тестах)."""

    def all_users(self) -> frozenset[str]:
        """SID всех включённых пользователей домена."""
        ...

    def members(self, sid: str) -> frozenset[str] | None:
        """SID включённых пользователей, в чьём маркере есть sid; None —
        не удалось определить."""
        ...


@dataclass
class Readers:
    users: frozenset[str]
    # Причины пропусков: unresolved_allow, unresolved_deny,
    # conditional_allow, unknown_ace, non_canonical.
    notes: Counter[str] = field(default_factory=Counter)


def _members(
    sid: str, owner: str | None, directory: Directory
) -> frozenset[str] | None:
    if sid in WIDE_SIDS:
        return directory.all_users()
    if sid in NEVER_IN_NETWORK_TOKEN or sid.startswith(NEVER_IN_NETWORK_TOKEN_PREFIXES):
        return frozenset()
    if sid == OWNER_RIGHTS:
        # OWNER RIGHTS — права владельца объекта (вместо неявных).
        return directory.members(owner) if owner else frozenset()
    return directory.members(sid)


def canonical_order(aces: Iterable[Ace]) -> list[Ace]:
    """Явные запреты, явные разрешения, затем унаследованные как есть."""
    items = list(aces)
    explicit = [a for a in items if not a.inherited]
    inherited = [a for a in items if a.inherited]
    deny = [a for a in explicit if a.is_deny]
    rest = [a for a in explicit if not a.is_deny]
    return deny + rest + inherited


def _evaluate(
    aces: list[Ace],
    owner: str | None,
    directory: Directory,
    notes: Counter[str],
) -> frozenset[str] | None:
    everyone = directory.all_users()
    granted: set[str] = set()
    denied: set[str] = set()
    for ace in aces:
        if ace.inherit_only or ace.is_ignored:
            continue
        if not (ace.is_allow or ace.is_deny):
            notes["unknown_ace"] += 1
            return None
        if not ace.reads_data:
            continue
        members = _members(ace.sid, owner, directory)
        if ace.is_deny:
            if members is None:
                notes["unresolved_deny"] += 1
                members = everyone
            denied |= members - granted
            continue
        if ace.is_conditional:
            notes["conditional_allow"] += 1
            continue
        if members is None:
            notes["unresolved_allow"] += 1
            continue
        granted |= members - denied
    return frozenset(granted)


def compute_readers(sd: SecurityDescriptor, directory: Directory) -> Readers:
    """Пользователи (SID), которым DACL разрешает читать содержимое."""
    if sd.dacl is None:
        # NULL DACL: доступ всем — в явном списке всех включённых.
        return Readers(users=directory.all_users())
    notes: Counter[str] = Counter()
    stored = list(sd.dacl)
    canonical = canonical_order(stored)
    first = _evaluate(stored, sd.owner, directory, notes)
    if first is None:
        return Readers(users=frozenset(), notes=notes)
    if canonical == stored:
        return Readers(users=first, notes=notes)
    notes["non_canonical"] += 1
    second = _evaluate(canonical, sd.owner, directory, Counter())
    users = first & second if second is not None else frozenset()
    return Readers(users=users, notes=notes)
