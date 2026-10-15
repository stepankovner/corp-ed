"""Право «просмотр» на страницу BookStack — так же, как его считает сам
BookStack, по ответам API.

Повторяет код BookStack v26.09 (тот же алгоритм с v23.01, когда права
содержимого переделали на «роль / все остальные»):

- `JointPermissionBuilder::createJointPermissionData` — статус права для
  пары (страница, роль):
  1. роль Admin (`system_name = admin`) — EXPLICIT_ALLOW всегда, что бы
     ни было в правах содержимого;
  2. иначе цепочка страница → глава (если есть) → книга
     (`EntityPermissionEvaluator::gatherEntityChainTypeIds`, полки не
     участвуют): на ближайшем уровне, где есть запись для этой роли или
     запись «Все остальные» (role_id 0, в API — `fallback_permissions`
     с `inheriting: false`), решает она; запись роли сильнее «Все
     остальные» того же уровня. Запись роли — EXPLICIT_ALLOW/DENY,
     «Все остальные» — IMPLICIT_ALLOW/DENY
     (`collapseAndCategorisePermissions`, `evaluatePermitsByType`);
  3. записей нет нигде — системное право роли `page-view-all`
     (IMPLICIT_ALLOW, иначе IMPLICIT_DENY); `page-view-own` даёт
     просмотр владельцу страницы (`owned_by`), и только в этой ветке:
     при записи в правах содержимого «своё» не действует
     (`createJointPermissionDataArray(..., false)`).
- `PermissionApplicator::restrictEntityQuery` — что видит пользователь:
  `max(status)` по всем его ролям; видно, если это IMPLICIT_ALLOW или
  EXPLICIT_ALLOW, либо он владелец, у одной из ролей действует «своё» и
  максимум — не EXPLICIT_DENY. Явный запрет одной роли перевешивает
  право «по умолчанию» другой; без ролей не видно ничего.

Видимость страницы не зависит от того, видна ли её книга: BookStack
отдаёт такую страницу в поиске и по API (`findVisibleByIdOrFail`) —
у нас так же.

Чего API не даёт однозначно — права уровня (ответ не разобран, книга
или глава недоступна токену): тогда доказано право только у роли Admin,
и страница достаётся только её участникам (`admins_only`).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

IMPLICIT_DENY = 0
IMPLICIT_ALLOW = 1
EXPLICIT_DENY = 2
EXPLICIT_ALLOW = 3
"""app/Permissions/PermissionStatus.php: порядок важен — max() по ролям."""

ADMIN_ROLE = "admin"
PUBLIC_ROLE = "public"


@dataclass(frozen=True)
class Rules:
    """Права содержимого одного уровня (книга, глава или страница):
    `/api/content-permissions/{type}/{id}`."""

    roles: Mapping[int, bool]
    """role_id → «просмотр» из role_permissions."""
    fallback: bool | None
    """«Все остальные»: None — наследуется (inheriting)."""


def parse_rules(data: Mapping[str, Any]) -> Rules | None:
    """Ответ content-permissions → Rules; незнакомая форма — None
    (права уровня неизвестны)."""
    entries = data.get("role_permissions")
    fallback = data.get("fallback_permissions")
    if not isinstance(entries, list) or not isinstance(fallback, dict):
        return None
    roles: dict[int, bool] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        role_id, view = entry.get("role_id"), entry.get("view")
        if not isinstance(role_id, int) or isinstance(role_id, bool):
            return None
        if not isinstance(view, bool):
            return None
        roles[role_id] = view
    inheriting = fallback.get("inheriting")
    if inheriting is True:
        return Rules(roles, None)
    view = fallback.get("view")
    if inheriting is not False or not isinstance(view, bool):
        return None
    return Rules(roles, view)


@dataclass(frozen=True)
class Role:
    id: int
    system_name: str
    view_all: bool
    """Системное право `page-view-all`."""
    view_own: bool
    """Системное право `page-view-own`."""
    users: frozenset[int]

    @property
    def admin(self) -> bool:
        return self.system_name == ADMIN_ROLE


def role_status(role: Role, chain: Iterable[Rules]) -> tuple[int, bool]:
    """(статус, действует ли «своё») для роли на странице; chain — права
    страницы, главы и книги, от ближнего к дальнему."""
    if role.admin:
        return EXPLICIT_ALLOW, False
    for rules in chain:
        if role.id in rules.roles:
            return (EXPLICIT_ALLOW if rules.roles[role.id] else EXPLICIT_DENY), False
        if rules.fallback is not None:
            return (IMPLICIT_ALLOW if rules.fallback else IMPLICIT_DENY), False
    return (IMPLICIT_ALLOW if role.view_all else IMPLICIT_DENY), role.view_own


@dataclass(frozen=True)
class Directory:
    """Пользователи и роли BookStack на один запуск."""

    emails: Mapping[int, str]
    """id пользователя → почта (без гостя)."""
    roles: Mapping[int, Role]
    user_roles: Mapping[int, frozenset[int]]
    """id пользователя → его роли (из `users` каждой роли)."""

    @property
    def people(self) -> frozenset[int]:
        return frozenset(self.emails)

    @property
    def admins(self) -> frozenset[int]:
        """Участники роли Admin: им доступна любая страница."""
        return frozenset(
            user
            for user in self.people
            if any(self.roles[r].admin for r in self.user_roles.get(user, ()))
        )

    @property
    def universal(self) -> bool:
        """Есть роль, в которой состоят все пользователи (кроме гостя)."""
        people = self.people
        return bool(people) and any(
            people <= role.users for role in self.roles.values()
        )

    def readers(self, chain: Iterable[Rules], owner: int | None) -> frozenset[int]:
        """Кто из пользователей видит страницу (restrictEntityQuery)."""
        levels = list(chain)
        by_roles: dict[frozenset[int], list[int]] = {}
        for user in self.people:
            by_roles.setdefault(self.user_roles.get(user, frozenset()), []).append(user)
        allowed: set[int] = set()
        for role_ids, users in by_roles.items():
            statuses = [role_status(self.roles[r], levels) for r in role_ids]
            if not statuses:
                continue
            status = max(s for s, _ in statuses)
            if status in (IMPLICIT_ALLOW, EXPLICIT_ALLOW):
                allowed.update(users)
            elif (
                owner is not None
                and owner in users
                and status != EXPLICIT_DENY
                and any(own for _, own in statuses)
            ):
                allowed.add(owner)
        return frozenset(allowed)

    def everyone(self, readers: frozenset[int]) -> bool:
        """«Видно всем» — только если есть роль, в которой состоят все
        пользователи, и страницу видит каждый.

        Почему так. «Вся компания» в kronto открывает документ и тем, кого
        в BookStack нет (новым сотрудникам, тем, кто ещё не заведён), —
        это честно, только если в BookStack право даёт роль, в которую
        входят все: её и получит каждый новый пользователь. Если же все
        текущие видят страницу каждый по своей роли — это совпадение, а
        не «всем»: остаётся список почт. Встроенной роли «все
        пользователи» в BookStack нет (Public — гость без входа, «Все
        остальные» в правах — не роль), поэтому она ищется по составу.

        Одной такой роли с правом недостаточно: право «по умолчанию»
        (IMPLICIT_ALLOW) у неё перевешивает явный запрет другой роли того
        же пользователя (max статусов), гарантия была бы лишь при
        EXPLICIT_ALLOW. Поэтому проверяется и итог по каждому.
        """
        return self.universal and readers >= self.people

    def emails_of(self, users: Iterable[int]) -> frozenset[str]:
        return frozenset(self.emails[u] for u in users if u in self.emails)
