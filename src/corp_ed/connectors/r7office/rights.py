"""Кто читает файл «Общих документов» Р7 по ответам `.../share`.

Ответ `GET api/2.0/files/file/{id}/share` — по одной записи на субъекта
(пользователь, группа, «Администраторы», «Все»): ближайшая по дереву —
запись самого файла или ближайшей папки с записью. Сам сервер решает так
(FileSecurity в исходном коде ONLYOFFICE Community Server):

1. администратор портала или модуля «Документы» в «Общих» читает всё;
2. если у файла есть свои записи, относящиеся к сотруднику, решает
   самая приоритетная из них: личная → группы сотрудника →
   «Администраторы» → «Все»;
3. иначе — так же по записям папок (на субъекта — ближайшая);
4. чтение — всё, кроме «Доступ запрещён».

По ответу API не видно, какая запись стоит на самом файле, а какая
унаследована, — только флаг `shared` («у файла есть свои записи»).
Поэтому для файла с `shared` берётся ещё ответ его папки
(`.../folder/{id}/share`): запись, которой там нет или которая там
другая, — точно своя у файла (F); совпавшая может быть и своей, и
унаследованной (E). Чтение даётся, только если его дают все возможные
раскладки:

- есть F: в самом приоритетном классе F нет запрета, и нет запрета у E
  того же или более приоритетного класса;
- F нет: по записям папки чтение есть, и ни одна E не запрещает.

Внутри класса групп порядок у сервера произвольный — любой запрет в
классе запрещает. Чтение — 1, 2, 5, 6, 7, 8; 0, 3 и незнакомые значения —
запрет. Записи ссылок не дают доступа людям портала. Запись владельца
(`isOwner`) с правом чтения сервер дописывает сам, если у создателя нет
записи, — она не учитывается; запрет в ней — настоящий. Ответа папки нет
(403/404) — любой запрет из ответа файла запрещает.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from corp_ed.connectors.common import to_int

GROUP_EVERYONE = "c5cc67d1-c3e8-43c0-a3ad-3928ae3e5b5e"
GROUP_ADMIN = "cd84e66b-b803-40fc-99f9-b2969a54a1de"
READ_ACCESS = frozenset({1, 2, 5, 6, 7, 8})

# Классы записей по приоритету сервера.
PERSONAL, GROUPS, ADMINS, EVERYONE = range(4)


@dataclass(frozen=True)
class Person:
    email: str
    admin: bool


@dataclass(frozen=True)
class Ace:
    """Запись доступа из ответа `.../share`."""

    subject: str
    group: bool
    access: int | None
    owner: bool

    @property
    def readable(self) -> bool:
        return self.access in READ_ACCESS


def parse_aces(records: Sequence[Mapping[str, Any]]) -> list[Ace]:
    aces: list[Ace] = []
    for record in records:
        target = record.get("sharedTo")
        if not isinstance(target, dict) or not target.get("id"):
            continue
        if "shareLink" in target:
            # Доступ по ссылке — не людям портала.
            continue
        aces.append(
            Ace(
                subject=str(target["id"]).lower(),
                # Группа — {id, name, manager}, человек — профиль с displayName.
                group="name" in target and "displayName" not in target,
                access=to_int(record.get("access")),
                owner=record.get("isOwner") is True,
            )
        )
    return aces


def group_subjects(aces: Sequence[Ace]) -> set[str]:
    """Группы, состав которых нужен для решения (кроме «Все» и «Админы»)."""
    return {
        ace.subject
        for ace in aces
        if ace.group and ace.subject not in (GROUP_EVERYONE, GROUP_ADMIN)
    }


def can_read(
    person_id: str,
    person: Person,
    file_aces: Sequence[Ace],
    groups: Mapping[str, frozenset[str]],
    *,
    shared: bool,
    folder_aces: Sequence[Ace] | None,
) -> bool:
    if person.admin:
        return True
    mine = _applicable(person_id, person, file_aces, groups)
    if not shared:
        return _decide(mine)
    if folder_aces is None:
        return _decide(mine) and all(ace.readable for _, ace in mine)
    inherited = {ace.subject: ace.access for ace in folder_aces}
    own = [
        (rank, ace)
        for rank, ace in mine
        if inherited.get(ace.subject, -1) != ace.access
    ]
    maybe = [
        (rank, ace)
        for rank, ace in mine
        if inherited.get(ace.subject, -1) == ace.access
    ]
    if own:
        top = min(rank for rank, _ in own)
        return all(ace.readable for rank, ace in own if rank == top) and all(
            ace.readable for rank, ace in maybe if rank <= top
        )
    from_folder = _applicable(person_id, person, folder_aces, groups)
    return _decide(from_folder) and all(ace.readable for _, ace in maybe)


def _applicable(
    person_id: str,
    person: Person,
    aces: Sequence[Ace],
    groups: Mapping[str, frozenset[str]],
) -> list[tuple[int, Ace]]:
    found: list[tuple[int, Ace]] = []
    for ace in aces:
        if ace.owner and ace.readable:
            continue
        if not ace.group:
            if ace.subject == person_id:
                found.append((PERSONAL, ace))
        elif ace.subject == GROUP_EVERYONE:
            found.append((EVERYONE, ace))
        elif ace.subject == GROUP_ADMIN:
            if person.admin:
                found.append((ADMINS, ace))
        elif person_id in groups.get(ace.subject, frozenset()):
            found.append((GROUPS, ace))
    return found


def _decide(applicable: Sequence[tuple[int, Ace]]) -> bool:
    if not applicable:
        return False
    top = min(rank for rank, _ in applicable)
    return all(ace.readable for rank, ace in applicable if rank == top)
