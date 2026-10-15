"""Кто видит файл SharePoint или OneDrive: разрешения Graph → почты.

`GET /drives/{d}/items/{i}/permissions` отдаёт действующие разрешения
файла — выданные на него и унаследованные от папок и библиотеки
(driveitem-list-permissions: «effective sharing permissions … inherited
from the DriveItem's ancestors»); `inheritedFrom` для SharePoint и
OneDrive для бизнеса не возвращается (resources/permission), поэтому
наследование не разбирается — берётся весь список. Роль: read, write,
owner дают чтение; другие строки ролей не документированы — пропуск.

Кому выдано (`grantedToV2`, `grantedToIdentitiesV2`, sharePointIdentitySet):
- user — почта из справочника по id (`/users/{id}`: mail, иначе UPN без
  `#EXT#`); отключённая учётная запись (accountEnabled = false) прав не
  получает. Без id — `user.email` из самого разрешения;
- group (группа Entra) — `/groups/{id}/transitiveMembers`: все вложенные
  участники-пользователи с почтами, кроме отключённых;
- siteUser без user/group — по loginName вида `i:0#.f|membership|<upn>`
  (пользователь), `c:0o.c|federateddirectoryclaimprovider|<id>` (участники
  группы Microsoft 365, с `_o` — её владельцы), `c:0t.c|tenant|<id>`
  (группа безопасности). Формат утверждений SharePoint в справочнике
  Graph не описан: что не разобрано — никому (сверить на живом клиенте).
  Утверждения «Все, кроме внешних пользователей» (`spo-grid-all-users`) и
  «Все» намеренно НЕ становятся «вся компания»: формат не
  задокументирован, а «видно всем по догадке» запрещено — такие файлы
  получат только явно перечисленных (RISKS: сверить и решить после живой
  проверки);
- siteGroup / sharePointGroup (группы SharePoint: «Владельцы»,
  «Участники», «Посетители» сайта) — состава групп SharePoint в Graph
  v1.0 нет. Сайт, подключённый к группе Microsoft 365, узнаётся по
  `/groups/{id}/sites/root`; его группы SharePoint по умолчанию
  называются «<имя> Owners/Members/Visitors» («Владельцы/Участники/
  Посетители сайта <имя>»), и в них лежат владельцы и участники группы
  Microsoft 365. Совпало имя с владельцами — `/groups/{id}/owners`, с
  участниками — `/groups/{id}/transitiveMembers`; посетители и любые
  другие группы SharePoint — никому (их состава Graph не отдаёт). Люди,
  добавленные в группу SharePoint напрямую, мимо группы Microsoft 365,
  прав в kronto не получат (в сторону закрытости); группа SharePoint,
  из которой админ убрал группу Microsoft 365, но оставил имя, дала бы
  лишних читателей — сверить на живом клиенте (RISKS);
- application, device — не люди, пропуск.

Ссылки общего доступа (`link`): сама ссылка — НЕ «видно всем», даже со
scope `anonymous` («все, у кого есть ссылка») или `organization` («все
в организации, у кого есть ссылка», resources/sharinglink). Ссылку надо
знать: сотрудник, которому её не прислали, файла в SharePoint не
найдёт, а ассистент выдал бы ему содержимое — это шире прав источника.
Поэтому по ссылке читают только явно перечисленные в
`grantedToIdentitiesV2` (кому ссылка выдана или кто ею уже вошёл);
`existingAccess` новых прав не даёт. Приглашение без `grantedToV2`
(ещё не принято) — никому.

Документация предупреждает: вызывающему, кто не владелец, permissions
может вернуть не все разрешения, а для полного разбора прав при
сканировании Microsoft советует Sites.FullControl.All (scan-guidance).
С разрешениями только на чтение список может оказаться неполным — это
сужает круг читателей, а не расширяет (сверить на живом клиенте).
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import structlog

from corp_ed.connectors.base import AdapterAuthError, AdapterError
from corp_ed.connectors.common import path_segment
from corp_ed.connectors.m365.client import GraphClient

logger = structlog.get_logger()

READ_ROLES = frozenset({"read", "write", "owner"})
USER_TYPE = "#microsoft.graph.user"
_SKIPPABLE = frozenset({"forbidden", "not_found", "http_400"})
_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_USER_CLAIM = re.compile(r"^i:0#\.f\|membership\|([^|\s]+@[^|\s]+)$", re.IGNORECASE)
_M365_CLAIM = re.compile(
    rf"^c:0o\.c\|federateddirectoryclaimprovider\|({_GUID})(_o)?$", re.IGNORECASE
)
_SECURITY_CLAIM = re.compile(rf"^c:0t\.c\|tenant\|({_GUID})$", re.IGNORECASE)
# Имена групп SharePoint сайта группы Microsoft 365 по умолчанию.
_OWNER_NAMES = ("{} Owners", "Владельцы сайта {}")
_MEMBER_NAMES = ("{} Members", "Участники сайта {}")

Readers = frozenset[str]
NOBODY: Readers = frozenset()


@dataclass(frozen=True)
class SiteGroups:
    """Сайт группы Microsoft 365: какой группе он принадлежит и как
    называются его группы SharePoint владельцев и участников."""

    group_id: str
    owners: frozenset[str]
    members: frozenset[str]


def collection_key(site_id: str) -> str:
    """Id семейства сайтов из id сайта `{host},{collection},{web}`: группы
    SharePoint живут на уровне семейства, подсайты делят их с корнем."""
    parts = site_id.split(",")
    return parts[1].casefold() if len(parts) == 3 else site_id.casefold()


def user_email(entry: Mapping[str, Any]) -> str | None:
    mail = str(entry.get("mail") or "").strip()
    if "@" in mail:
        return mail.casefold()
    upn = str(entry.get("userPrincipalName") or "").strip()
    if "@" in upn and "#ext#" not in upn.casefold():
        return upn.casefold()
    return None


def _grants_read(permission: Mapping[str, Any]) -> bool:
    roles = permission.get("roles")
    return isinstance(roles, list) and any(
        isinstance(role, str) and role.casefold() in READ_ROLES for role in roles
    )


def _identity_sets(permission: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Кому выдано разрешение. Ссылка existingAccess прав не добавляет;
    устаревшие grantedTo / grantedToIdentities — только если нет V2."""
    link = permission.get("link")
    if isinstance(link, Mapping) and link.get("scope") == "existingAccess":
        return []
    found: list[Any] = []
    single = permission.get("grantedToV2") or permission.get("grantedTo")
    if single is not None:
        found.append(single)
    many = permission.get("grantedToIdentitiesV2")
    if many is None:
        many = permission.get("grantedToIdentities")
    if isinstance(many, list):
        found.extend(many)
    return [item for item in found if isinstance(item, Mapping)]


class AccessResolver:
    """Разрешения → почты; справочник, группы и сайты групп — кеш на запуск."""

    def __init__(self, client: GraphClient) -> None:
        self._client = client
        self._users: dict[str, str | None] = {}
        self._groups: dict[tuple[str, str], Readers] = {}
        self._disabled: frozenset[str] | None = None
        self._sites: dict[str, SiteGroups] | None = None

    async def readers(
        self, permissions: Iterable[Any], *, collection: str | None
    ) -> Readers:
        result: set[str] = set()
        for permission in permissions:
            if not isinstance(permission, Mapping) or not _grants_read(permission):
                continue
            for identity in _identity_sets(permission):
                result |= await self._identity(identity, collection)
        return frozenset(result)

    async def user(self, user_id: str) -> str | None:
        """Почта включённой учётной записи; None — нет, отключена, не видна."""
        key = user_id.casefold()
        if key in self._users:
            return self._users[key]
        email: str | None = None
        try:
            entry = await self._client.get(
                f"users/{path_segment(user_id)}",
                {"$select": "id,mail,userPrincipalName,accountEnabled"},
            )
        except AdapterError as exc:
            _reraise(exc)
            logger.info("m365_user_unreadable", code=exc.code)
        else:
            if entry.get("accountEnabled") is not False:
                email = user_email(entry)
        self._users[key] = email
        return email

    async def members(self, group_id: str) -> Readers:
        """Все вложенные участники-пользователи группы Entra."""
        return await self._directory(group_id, "transitiveMembers")

    async def owners(self, group_id: str) -> Readers:
        return await self._directory(group_id, "owners")

    async def site_groups(self, collection: str) -> SiteGroups | None:
        """Сайт группы Microsoft 365 для семейства сайтов; None — не такой."""
        if self._sites is None:
            self._sites = await self._load_site_groups()
        return self._sites.get(collection)

    # --- внутреннее ------------------------------------------------------------------

    async def _identity(
        self, identity: Mapping[str, Any], collection: str | None
    ) -> Readers:
        user = identity.get("user")
        if isinstance(user, Mapping):
            return await self._user_identity(user)
        group = identity.get("group")
        if isinstance(group, Mapping):
            group_id = str(group.get("id") or "")
            return await self.members(group_id) if group_id else NOBODY
        site_user = identity.get("siteUser")
        if isinstance(site_user, Mapping):
            return await self._claim(str(site_user.get("loginName") or ""))
        for key in ("sharePointGroup", "siteGroup"):
            site_group = identity.get(key)
            if isinstance(site_group, Mapping):
                return await self._site_group(site_group, collection)
        return NOBODY

    async def _user_identity(self, user: Mapping[str, Any]) -> Readers:
        user_id = str(user.get("id") or "").strip()
        if user_id:
            email = await self.user(user_id)
            return frozenset({email}) if email else NOBODY
        email = str(user.get("email") or "").strip().casefold()
        return frozenset({email}) if "@" in email else NOBODY

    async def _claim(self, login: str) -> Readers:
        login = login.strip()
        if match := _USER_CLAIM.match(login):
            email = match.group(1).casefold()
            return NOBODY if "#ext#" in email else frozenset({email})
        if match := _M365_CLAIM.match(login):
            group_id = match.group(1)
            return await (
                self.owners(group_id) if match.group(2) else self.members(group_id)
            )
        if match := _SECURITY_CLAIM.match(login):
            return await self.members(match.group(1))
        if login:
            # Неразобранное утверждение (в том числе «все сотрудники»): никому.
            logger.info("m365_claim_skipped", prefix=login.split("|", 1)[0][:16])
        return NOBODY

    async def _site_group(
        self, site_group: Mapping[str, Any], collection: str | None
    ) -> Readers:
        if collection is None:
            return NOBODY
        site = await self.site_groups(collection)
        if site is None:
            return NOBODY
        names = {
            str(site_group.get(key) or "").strip().casefold()
            for key in ("displayName", "loginName", "title")
        } - {""}
        if names & site.owners:
            return await self.owners(site.group_id)
        if names & site.members:
            return await self.members(site.group_id)
        return NOBODY

    async def _directory(self, group_id: str, relation: str) -> Readers:
        cache_key = (group_id.casefold(), relation)
        if cache_key in self._groups:
            return self._groups[cache_key]
        found: set[str] = set()
        disabled = await self._disabled_ids()
        try:
            # Без $select и $top: с ними transitiveMembers и owners требуют
            # расширенных запросов (ConsistencyLevel: eventual), а их индекс
            # может отставать от свежих изменений состава.
            async for entry in self._client.pages(
                f"groups/{path_segment(group_id)}/{relation}"
            ):
                if entry.get("@odata.type") != USER_TYPE:
                    continue
                if str(entry.get("id") or "").casefold() in disabled:
                    continue
                if entry.get("accountEnabled") is False:
                    continue
                email = user_email(entry)
                if email:
                    found.add(email)
        except AdapterError as exc:
            _reraise(exc)
            # Группы нет или её не видно приложению: её участники прав не получат.
            logger.warning("m365_group_unreadable", relation=relation, code=exc.code)
            found = set()
        result = frozenset(found)
        self._groups[cache_key] = result
        return result

    async def _disabled_ids(self) -> frozenset[str]:
        """Отключённые учётные записи: состав группы без $select не несёт
        accountEnabled, поэтому они собираются одним запросом на запуск."""
        if self._disabled is None:
            ids: set[str] = set()
            try:
                async for entry in self._client.pages(
                    "users",
                    {
                        "$filter": "accountEnabled eq false",
                        "$select": "id",
                        "$top": 999,
                    },
                ):
                    if entry.get("id"):
                        ids.add(str(entry["id"]).casefold())
            except AdapterError as exc:
                if exc.retryable or isinstance(exc, AdapterAuthError):
                    raise
                # Без справочника прав нет ни у кого из групп: отказ
                # подключению, а не молчаливое «всем участникам».
                raise AdapterError(f"users_{exc.code}") from exc
            self._disabled = frozenset(ids)
        return self._disabled

    async def _load_site_groups(self) -> dict[str, SiteGroups]:
        sites: dict[str, SiteGroups] = {}
        groups: list[dict[str, Any]] = []
        try:
            async for group in self._client.pages(
                "groups",
                {
                    "$filter": "groupTypes/any(c:c eq 'Unified')",
                    "$select": "id,displayName",
                    "$top": 999,
                },
            ):
                groups.append(group)
        except AdapterError as exc:
            _reraise(exc)
            logger.warning("m365_groups_unreadable", code=exc.code)
            return sites
        for group in groups:
            group_id = str(group.get("id") or "")
            if not group_id:
                continue
            try:
                site = await self._client.get(
                    f"groups/{path_segment(group_id)}/sites/root",
                    {"$select": "id,displayName"},
                )
            except AdapterError as exc:
                # У группы нет сайта (404) или сайт не выдан приложению
                # (Sites.Selected): его группы SharePoint — никому.
                _reraise(exc)
                continue
            site_id = str(site.get("id") or "")
            if not site_id:
                continue
            titles = {
                str(value).strip()
                for value in (group.get("displayName"), site.get("displayName"))
                if value and str(value).strip()
            }
            sites[collection_key(site_id)] = SiteGroups(
                group_id=group_id,
                owners=frozenset(
                    pattern.format(title).casefold()
                    for title in titles
                    for pattern in _OWNER_NAMES
                ),
                members=frozenset(
                    pattern.format(title).casefold()
                    for title in titles
                    for pattern in _MEMBER_NAMES
                ),
            )
        return sites


def _reraise(exc: AdapterError) -> None:
    """Повторяемое, отказ токена и незнакомое — наверх; 403/404/400 — нет."""
    if exc.retryable or isinstance(exc, AdapterAuthError) or exc.code not in _SKIPPABLE:
        raise exc
