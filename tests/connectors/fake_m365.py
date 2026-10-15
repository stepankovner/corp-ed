"""Поддельные Microsoft identity platform и Microsoft Graph v1.0 для
контрактных тестов коннектора m365.

Формы запросов и ответов — по документации (15.10.2026); живым клиентом
Microsoft 365 не проверено. Страницы:

- токен client credentials, ответ и ошибки `{error, error_codes}`, коды
  AADSTS:
  https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow
  https://learn.microsoft.com/en-us/entra/identity-platform/reference-error-codes
- ошибки Graph `{error: {code, message, innerError}}`; 429 с Retry-After
  и телом TooManyRequests; страницы `value` + абсолютная `@odata.nextLink`:
  https://learn.microsoft.com/en-us/graph/errors
  https://learn.microsoft.com/en-us/graph/throttling
  https://learn.microsoft.com/en-us/graph/paging
- сайты: `GET /sites` (с личными, `isPersonalSite`); по id, по
  `{host}:/{путь}`, сайт группы `/groups/{id}/sites/root`; подсайты:
  https://learn.microsoft.com/en-us/graph/api/site-list?view=graph-rest-1.0
  https://learn.microsoft.com/en-us/graph/api/site-get?view=graph-rest-1.0
  https://learn.microsoft.com/en-us/graph/api/site-list-subsites?view=graph-rest-1.0
- библиотеки `/sites/{id}/drives`, `/users/{id}/drive` (404 — OneDrive
  нет), owner с почтой:
  https://learn.microsoft.com/en-us/graph/api/drive-list?view=graph-rest-1.0
  https://learn.microsoft.com/en-us/graph/api/drive-get?view=graph-rest-1.0
  https://learn.microsoft.com/en-us/graph/api/resources/drive?view=graph-rest-1.0
- `/drives/{id}/root/delta`: nextLink, на последней странице deltaLink,
  удалённые с `deleted`, без `parentReference.path`, без cTag в OneDrive
  для бизнеса:
  https://learn.microsoft.com/en-us/graph/api/driveitem-delta?view=graph-rest-1.0
- права файла: действующие разрешения, `grantedToV2` /
  `grantedToIdentitiesV2` (user, siteUser, group, siteGroup), `link` со
  scope:
  https://learn.microsoft.com/en-us/graph/api/driveitem-list-permissions?view=graph-rest-1.0
  https://learn.microsoft.com/en-us/graph/api/resources/permission?view=graph-rest-1.0
  https://learn.microsoft.com/en-us/graph/api/resources/sharinglink?view=graph-rest-1.0
  https://learn.microsoft.com/en-us/graph/api/resources/sharepointidentityset?view=graph-rest-1.0
  https://learn.microsoft.com/en-us/graph/api/resources/sharepointidentity?view=graph-rest-1.0
  https://learn.microsoft.com/en-us/graph/api/resources/sharepointgroupidentity?view=graph-rest-1.0
- содержимое — 302 на подписанный адрес без Authorization:
  https://learn.microsoft.com/en-us/graph/api/driveitem-get-content?view=graph-rest-1.0
- справочник: `/users`, `/users/{id}`; `/groups?$filter=groupTypes/any(c:c
  eq 'Unified')`; `/groups/{id}/transitiveMembers` (плоский список;
  параметры запроса — только с ConsistencyLevel); `/groups/{id}/owners`:
  https://learn.microsoft.com/en-us/graph/api/user-list?view=graph-rest-1.0
  https://learn.microsoft.com/en-us/graph/api/group-list?view=graph-rest-1.0
  https://learn.microsoft.com/en-us/graph/api/group-list-transitivemembers?view=graph-rest-1.0
  https://learn.microsoft.com/en-us/graph/api/group-list-owners?view=graph-rest-1.0
- сканирование и Sites.FullControl.All для полного разбора прав;
  Sites.Selected:
  https://learn.microsoft.com/en-us/onedrive/developer/rest-api/concepts/scan-guidance
  https://learn.microsoft.com/en-us/graph/permissions-selected-overview

Чего документация не показывает и что здесь допущено: точный вид
siteGroup у файлов сайта группы (имя «<группа> Members» и т. п.) и
loginName siteUser в формате утверждений SharePoint — сверить на живом
клиенте.
"""

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx

from corp_ed.core.outbound import OutboundClient
from tests.fake_connector import public_resolver

LOGIN_HOST = "login.microsoftonline.com"
GRAPH_HOST = "graph.microsoft.com"
SP_HOST = "contoso.sharepoint.com"
MY_HOST = "contoso-my.sharepoint.com"
GRAPH = f"https://{GRAPH_HOST}/v1.0/"
TENANT_ID = "6f1c2d3e-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
TENANT_DOMAIN = "contoso.onmicrosoft.com"
CLIENT_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
SECRET = "Fake~Secret.Value_123"  # noqa: S105 — выдуманный секрет стенда
ALL_ROLES = frozenset(
    {"Sites.Read.All", "Files.Read.All", "User.Read.All", "GroupMember.Read.All"}
)
USER = "#microsoft.graph.user"
GROUP = "#microsoft.graph.group"

ANNA = "a0000000-0000-4000-8000-000000000001"
BORIS = "a0000000-0000-4000-8000-000000000002"
VERA = "a0000000-0000-4000-8000-000000000003"
GLEB = "a0000000-0000-4000-8000-000000000004"
DINA = "a0000000-0000-4000-8000-000000000005"
GUEST = "a0000000-0000-4000-8000-000000000006"
G_MARKETING = "b0000000-0000-4000-8000-000000000001"
G_FINANCE = "b0000000-0000-4000-8000-000000000002"
G_LEADS = "b0000000-0000-4000-8000-000000000003"


def site_id(host: str, collection: int, web: int) -> str:
    """Id сайта Graph: `{хост},{id семейства сайтов},{id сайта}`."""
    return (
        f"{host},c0000000-0000-4000-8000-{collection:012d},"
        f"d0000000-0000-4000-8000-{web:012d}"
    )


SITE_ROOT = site_id(SP_HOST, 1, 1)
SITE_MKT = site_id(SP_HOST, 2, 2)
# Подсайт — в том же семействе сайтов, что и сайт группы.
SITE_MKT_SUB = site_id(SP_HOST, 2, 3)
SITE_PERSONAL = site_id(MY_HOST, 9, 9)
D_ROOT = "b!root-documents"
D_MKT = "b!mkt-documents"
D_MKT_SUB = "b!mkt-projects"
D_ANNA = "b!onedrive-anna"
D_BORIS = "b!onedrive-boris"


def _error(status: int, code: str, message: str = "") -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "error": {
                "code": code,
                "message": message or code,
                "innerError": {
                    "date": "2026-10-15T10:00:00",
                    "request-id": "00000000-0000-0000-0000-000000000000",
                },
            }
        },
    )


def _token_error(status: int, error: str, code: int) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "error": error,
            "error_description": f"AADSTS{code}: описание с идентификаторами.",
            "error_codes": [code],
            "timestamp": "2026-10-15 10:00:00Z",
            "trace_id": "00000000-0000-0000-0000-000000000000",
            "correlation_id": "00000000-0000-0000-0000-000000000000",
        },
    )


# --- разрешения файла ----------------------------------------------------------


def perm_user(
    fake: "FakeM365", user_id: str, role: str = "read", **extra: Any
) -> dict[str, Any]:
    user = fake.users[user_id]
    item: dict[str, Any] = {
        "id": f"p-user-{user_id}-{role}",
        "roles": [role],
        "grantedToV2": {
            "user": {
                "id": user_id,
                "displayName": user["displayName"],
                "email": user["mail"],
            },
            "siteUser": {
                "id": "7",
                "displayName": user["displayName"],
                "loginName": f"i:0#.f|membership|{user['userPrincipalName']}",
            },
        },
    }
    item.update(extra)
    return item


def perm_group(fake: "FakeM365", group_id: str, role: str = "read") -> dict[str, Any]:
    group = fake.groups[group_id]
    return {
        "id": f"p-group-{group_id}-{role}",
        "roles": [role],
        "grantedToV2": {
            "group": {
                "id": group_id,
                "displayName": group["displayName"],
                "email": group.get("mail"),
            },
            "siteUser": {
                "id": "11",
                "displayName": group["displayName"],
                "loginName": f"c:0t.c|tenant|{group_id}",
            },
        },
    }


def perm_site_group(name: str, role: str, principal: str = "5") -> dict[str, Any]:
    """Группа SharePoint сайта: числовой id, уникальный только в сайте."""
    return {
        "id": f"p-sg-{principal}",
        "roles": [role],
        "grantedToV2": {
            "siteGroup": {"displayName": name, "id": principal, "loginName": name}
        },
    }


def perm_claim(login: str, name: str, role: str = "read") -> dict[str, Any]:
    """Разрешение на утверждение SharePoint (только siteUser)."""
    return {
        "id": f"p-claim-{abs(hash(login))}",
        "roles": [role],
        "grantedToV2": {
            "siteUser": {"id": "12", "displayName": name, "loginName": login}
        },
    }


def perm_link(
    fake: "FakeM365", scope: str, *user_ids: str, link_type: str = "view"
) -> dict[str, Any]:
    """Ссылка общего доступа; user_ids — кому выдана или кто ею вошёл."""
    item: dict[str, Any] = {
        "id": f"p-link-{scope}-{'-'.join(user_ids) or 'none'}",
        "roles": ["write" if link_type == "edit" else "read"],
        "link": {
            "scope": scope,
            "type": link_type,
            "webUrl": f"https://{SP_HOST}/:t:/s/marketing/Efake{scope}",
            "preventsDownload": False,
        },
        "shareId": "u!fake-share-id",
        "expirationDateTime": "0001-01-01T00:00:00Z",
    }
    if user_ids:
        item["grantedToIdentitiesV2"] = [
            {
                "user": {
                    "id": uid,
                    "displayName": fake.users[uid]["displayName"],
                    "email": fake.users[uid]["mail"],
                },
                "siteUser": {
                    "id": "13",
                    "displayName": fake.users[uid]["displayName"],
                    "loginName": "i:0#.f|membership|"
                    + fake.users[uid]["userPrincipalName"],
                },
            }
            for uid in user_ids
        ]
    return item


@dataclass
class FakeM365:
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    sites: dict[str, dict[str, Any]] = field(default_factory=dict)
    drives: dict[str, dict[str, Any]] = field(default_factory=dict)
    content: dict[tuple[str, str], bytes] = field(default_factory=dict)
    roles: set[str] = field(default_factory=lambda: set(ALL_ROLES))
    """Application permissions с согласием администратора."""
    secret: str = SECRET
    secret_expired: bool = False
    selected_sites: set[str] | None = None
    """Sites.Selected: приложению выданы только эти сайты (None — все)."""
    page_size: int = 100
    """Размер страницы любого списка: тесты ставят 2 для nextLink."""
    rate_limit_hits: int = 0
    retry_after: str = "3"
    unavailable_hits: int = 0
    forbidden_drives: set[str] = field(default_factory=set)
    forbidden_permissions: set[str] = field(default_factory=set)
    """id файлов, права которых отвечают 403."""
    foreign_next_link: bool = False
    reject_tokens: bool = False
    token_unavailable: bool = False
    """Graph отвечает 401 на любой токен (приложение удалено из клиента)."""
    tokens: set[str] = field(default_factory=set)
    revoked: set[str] = field(default_factory=set)
    token_requests: list[dict[str, str]] = field(default_factory=list)
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    """(путь после /v1.0/, параметры) каждого запроса к Graph."""
    downloads: list[tuple[str, str]] = field(default_factory=list)
    """(хост, Authorization) каждого запроса за содержимым."""
    issued: int = 0

    def client(self) -> OutboundClient:
        return OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
            resolver=public_resolver,
        )

    # --- наполнение ---------------------------------------------------------

    def add_user(
        self,
        user_id: str,
        name: str,
        login: str,
        *,
        mail: str | None = None,
        enabled: bool = True,
        guest: bool = False,
    ) -> str:
        self.users[user_id] = {
            "id": user_id,
            "displayName": name,
            "mail": mail if mail is not None else f"{login}@contoso.ru",
            "userPrincipalName": f"{login}@{TENANT_DOMAIN}"
            if not guest
            else f"{login}_partner.org#EXT#@{TENANT_DOMAIN}",
            "accountEnabled": enabled,
            "userType": "Guest" if guest else "Member",
        }
        return user_id

    def add_group(
        self,
        group_id: str,
        name: str,
        *,
        members: list[str],
        owners: list[str] | None = None,
        unified: bool = False,
        site: str | None = None,
    ) -> str:
        self.groups[group_id] = {
            "id": group_id,
            "displayName": name,
            "mail": f"{group_id[:8]}@contoso.ru",
            "groupTypes": ["Unified"] if unified else [],
            "members": list(members),
            "owners": list(owners or []),
            "site": site,
        }
        return group_id

    def add_site(
        self,
        site_id: str,
        name: str,
        path: str,
        *,
        parent: str | None = None,
        personal: bool = False,
        host: str = SP_HOST,
    ) -> str:
        self.sites[site_id] = {
            "id": site_id,
            "name": path.rsplit("/", 1)[-1] or name,
            "displayName": name,
            "webUrl": f"https://{host}{path}",
            "isPersonalSite": personal,
            "parent": parent,
        }
        return site_id

    def add_drive(
        self,
        drive_id: str,
        name: str,
        *,
        site: str | None = None,
        owner: str | None = None,
        drive_type: str = "documentLibrary",
    ) -> str:
        host = MY_HOST if owner else SP_HOST
        base = (
            self.sites[site]["webUrl"]
            if site
            else f"https://{MY_HOST}/personal/{owner}"
        )
        self.drives[drive_id] = {
            "id": drive_id,
            "name": name,
            "driveType": drive_type,
            "webUrl": f"{base}/{name}",
            "site": site,
            "owner": owner,
            "host": host,
            "items": {
                "root": {"id": f"{drive_id}-root", "name": "root", "root": {}},
            },
            "tombstones": [],
        }
        return drive_id

    def add_folder(
        self, drive_id: str, item_id: str, name: str, parent: str = ""
    ) -> str:
        drive = self.drives[drive_id]
        drive["items"][item_id] = {
            "id": item_id,
            "name": name,
            "folder": {"childCount": 0},
            "_parent": parent or f"{drive_id}-root",
        }
        return item_id

    def add_file(
        self,
        drive_id: str,
        item_id: str,
        name: str,
        data: bytes = b"",
        *,
        parent: str = "",
        perms: list[dict[str, Any]] | None = None,
        size: int | None = None,
        etag: str = "1",
        package: bool = False,
    ) -> str:
        """perms — что вернёт /permissions по файлу (с унаследованными)."""
        drive = self.drives[drive_id]
        item: dict[str, Any] = {
            "id": item_id,
            "name": name,
            "size": len(data) if size is None else size,
            "eTag": f'"{{{item_id}}},{etag}"',
            "cTag": f'"c:{{{item_id}}},{etag}"',
            "lastModifiedDateTime": "2026-10-01T09:00:00Z",
            "webUrl": f"{drive['webUrl']}/{name}",
            "file": {"mimeType": "application/octet-stream"},
            "_parent": parent or f"{drive_id}-root",
            "_perms": list(perms or []),
        }
        if package:
            item.pop("file")
            item["package"] = {"type": "oneNote"}
        drive["items"][item_id] = item
        self.content[(drive_id, item_id)] = data
        return item_id

    def delete_item(self, drive_id: str, item_id: str) -> None:
        drive = self.drives[drive_id]
        drive["items"].pop(item_id)
        drive["tombstones"].append(item_id)

    def touch(self, drive_id: str, item_id: str, data: bytes, etag: str) -> None:
        item = self.drives[drive_id]["items"][item_id]
        item["eTag"] = f'"{{{item_id}}},{etag}"'
        item["cTag"] = f'"c:{{{item_id}}},{etag}"'
        item["size"] = len(data)
        self.content[(drive_id, item_id)] = data

    # --- обработка -----------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "")
        if host == LOGIN_HOST:
            return self._token(request)
        if host in (SP_HOST, MY_HOST):
            return self._download(request, host)
        if host != GRAPH_HOST:
            return httpx.Response(404, text="unknown host")
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ")
        if (
            not auth.startswith("Bearer ")
            or token not in self.tokens
            or self.reject_tokens
        ):
            return _error(401, "InvalidAuthenticationToken", "Access token is empty.")
        if token in self.revoked:
            return _error(
                401, "InvalidAuthenticationToken", "Lifetime validation failed."
            )
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        path = request.url.path.removeprefix("/v1.0/")
        self.calls.append((path, query))
        if self.rate_limit_hits > 0:
            self.rate_limit_hits -= 1
            response = _error(429, "TooManyRequests", "Please retry again later.")
            response.headers["Retry-After"] = self.retry_after
            return response
        if self.unavailable_hits > 0:
            self.unavailable_hits -= 1
            return _error(503, "serviceNotAvailable")
        return self._graph(request, path, query)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_requests.append(
            {k: v for k, v in form.items() if k != "client_secret"}
        )
        if self.token_unavailable:
            return httpx.Response(503, text="Service Unavailable")
        tenant = request.url.path.split("/")[1]
        if tenant not in (TENANT_ID, TENANT_DOMAIN):
            return _token_error(400, "invalid_request", 90002)
        if form.get("grant_type") != "client_credentials":
            return _token_error(400, "unsupported_grant_type", 70003)
        if form.get("scope") != "https://graph.microsoft.com/.default":
            return _token_error(400, "invalid_scope", 70011)
        if form.get("client_id") != CLIENT_ID:
            return _token_error(400, "unauthorized_client", 700016)
        if form.get("client_secret") != self.secret:
            return _token_error(401, "invalid_client", 7000215)
        if self.secret_expired:
            return _token_error(401, "invalid_client", 7000222)
        self.issued += 1
        token = f"eyJ0eXAi.fake-{self.issued}"
        self.tokens.add(token)
        return httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "expires_in": 3599,
                "ext_expires_in": 3599,
                "access_token": token,
            },
        )

    def _download(self, request: httpx.Request, host: str) -> httpx.Response:
        self.downloads.append((host, request.headers.get("authorization", "")))
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        if not query.get("tempauth"):
            return httpx.Response(401, text="tempauth required")
        drive_id, _, item_id = query.get("UniqueId", "").partition("/")
        data = self.content.get((drive_id, item_id))
        if data is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, content=data)

    # --- Graph ----------------------------------------------------------------

    def _need(self, *roles: str) -> httpx.Response | None:
        """403, если ни одного из разрешений нет (как без согласия)."""
        if self.roles & set(roles):
            return None
        directory = any(r.startswith(("User.", "Group")) for r in roles)
        if directory:
            return _error(
                403,
                "Authorization_RequestDenied",
                "Insufficient privileges to complete the operation.",
            )
        return _error(403, "accessDenied", "Access denied")

    def _graph(
        self, request: httpx.Request, path: str, query: dict[str, str]
    ) -> httpx.Response:
        parts = path.split("/")
        if parts[0] == "sites":
            return self._sites(parts[1:], query)
        if parts[0] == "drives":
            return self._drives(parts[1:], query)
        if parts[0] == "users":
            return self._users(parts[1:], query)
        if parts[0] == "groups":
            return self._groups(request, parts[1:], query)
        return _error(400, "BadRequest", "Resource not found for the segment.")

    def _site_allowed(self, site_id: str) -> bool:
        return self.selected_sites is None or site_id in self.selected_sites

    def _sites(self, parts: list[str], query: dict[str, str]) -> httpx.Response:
        if not parts:
            if denied := self._need("Sites.Read.All"):
                return denied
            shown = [self._site_json(s) for s in self.sites.values()]
            return self._page(shown, "sites", query)
        if self.selected_sites is None and (denied := self._need("Sites.Read.All")):
            return denied
        key = parts[0]
        rest = parts[1:]
        if ":" in key or (rest and rest[0] == ""):
            # sites/{host}:/{путь}
            joined = "/".join(parts)
            host, _, rel = joined.partition(":")
            url = f"https://{host}{rel}".rstrip("/")
            site = next(
                (s for s in self.sites.values() if s["webUrl"].rstrip("/") == url),
                None,
            )
            rest = []
        elif key in (SP_HOST, MY_HOST):
            site = next(
                (s for s in self.sites.values() if s["webUrl"] == f"https://{key}"),
                None,
            )
        else:
            site = self.sites.get(key)
        if site is None:
            return _error(404, "itemNotFound", "Requested site could not be found")
        if not self._site_allowed(site["id"]):
            return _error(403, "accessDenied", "Access denied")
        if not rest:
            return httpx.Response(200, json=self._site_json(site))
        if rest == ["sites"]:
            children = [
                self._site_json(s)
                for s in self.sites.values()
                if s["parent"] == site["id"]
            ]
            return self._page(children, f"sites/{site['id']}/sites", query)
        if rest == ["drives"]:
            drives = [
                self._drive_json(d)
                for d in self.drives.values()
                if d["site"] == site["id"]
            ]
            return self._page(drives, f"sites/{site['id']}/drives", query)
        return _error(400, "BadRequest")

    def _site_json(self, site: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": site["id"],
            "name": site["name"],
            "displayName": site["displayName"],
            "webUrl": site["webUrl"],
            "isPersonalSite": site["isPersonalSite"],
            "root": {},
            "siteCollection": {"hostname": site["webUrl"].split("/")[2]},
        }

    def _drive_json(self, drive: dict[str, Any]) -> dict[str, Any]:
        shown: dict[str, Any] = {
            "id": drive["id"],
            "name": drive["name"],
            "driveType": drive["driveType"],
            "webUrl": drive["webUrl"],
            "createdDateTime": "2026-01-10T08:00:00Z",
        }
        if drive["owner"]:
            user = self.users[drive["owner"]]
            shown["owner"] = {
                "user": {
                    "id": user["id"],
                    "displayName": user["displayName"],
                    "email": user["mail"],
                }
            }
        return shown

    def _drives(self, parts: list[str], query: dict[str, str]) -> httpx.Response:
        if denied := self._need("Files.Read.All", "Sites.Read.All", "Sites.Selected"):
            return denied
        drive = self.drives.get(parts[0]) if parts else None
        if drive is None:
            return _error(404, "itemNotFound", "The resource could not be found.")
        if drive["id"] in self.forbidden_drives or (
            drive["site"] and not self._site_allowed(drive["site"])
        ):
            return _error(403, "accessDenied", "Access denied")
        rest = parts[1:]
        if rest == ["root", "delta"]:
            return self._delta(drive, query)
        if len(rest) == 3 and rest[0] == "items":
            item = drive["items"].get(rest[1])
            if item is None or "_parent" not in item:
                return _error(404, "itemNotFound", "The resource could not be found.")
            if rest[2] == "permissions":
                if item["id"] in self.forbidden_permissions:
                    return _error(403, "accessDenied", "Access denied")
                perms = item.get("_perms", [])
                return self._page(
                    perms, f"drives/{drive['id']}/items/{item['id']}/permissions", query
                )
            if rest[2] == "content":
                if "file" not in item:
                    return _error(400, "invalidRequest", "Item is not a file.")
                location = (
                    f"https://{drive['host']}/_layouts/15/download.aspx?"
                    + urlencode(
                        {
                            "UniqueId": f"{drive['id']}/{item['id']}",
                            "Translate": "false",
                            "tempauth": "v1.fake-signed-url",
                            "ApiVersion": "2.0",
                        }
                    )
                )
                return httpx.Response(302, headers={"location": location})
        return _error(400, "BadRequest")

    def _delta(self, drive: dict[str, Any], query: dict[str, str]) -> httpx.Response:
        items: list[dict[str, Any]] = []
        for item in drive["items"].values():
            shown = {k: v for k, v in item.items() if not k.startswith("_")}
            if "root" not in item:
                shown["parentReference"] = {
                    "driveId": drive["id"],
                    "driveType": drive["driveType"],
                    "id": item["_parent"],
                }
            if drive["owner"]:
                # Delta OneDrive для бизнеса не отдаёт cTag при изменении.
                shown.pop("cTag", None)
            if "$select" not in query and "file" in item:
                shown["@microsoft.graph.downloadUrl"] = (
                    f"https://{drive['host']}/_layouts/15/download.aspx?"
                    f"UniqueId={item['id']}&tempauth=v1.secret-temp-auth"
                )
            items.append(shown)
        # Удалённые — с фасетом deleted и без имени.
        items.extend({"id": gone, "deleted": {}} for gone in drive["tombstones"])
        return self._page(items, f"drives/{drive['id']}/root/delta", query, delta=True)

    def _users(self, parts: list[str], query: dict[str, str]) -> httpx.Response:
        if parts and parts[-1] == "drive":
            if denied := self._need("Files.Read.All", "Sites.Read.All"):
                return denied
            drive = next(
                (d for d in self.drives.values() if d["owner"] == parts[0]), None
            )
            if drive is None:
                return _error(404, "itemNotFound", "The resource could not be found.")
            return httpx.Response(200, json=self._drive_json(drive))
        if denied := self._need("User.Read.All"):
            return denied
        if not parts:
            users = list(self.users.values())
            if query.get("$filter") == "accountEnabled eq false":
                users = [u for u in users if not u["accountEnabled"]]
            elif "$filter" in query:
                return _error(400, "Request_UnsupportedQuery")
            return self._page([self._select(u, query) for u in users], "users", query)
        user = self.users.get(parts[0])
        if user is None:
            return _error(
                404,
                "Request_ResourceNotFound",
                f"Resource '{parts[0]}' does not exist.",
            )
        return httpx.Response(200, json=self._select(user, query))

    def _select(self, entry: dict[str, Any], query: dict[str, str]) -> dict[str, Any]:
        public = {k: v for k, v in entry.items() if not k.startswith("_")}
        if "$select" not in query:
            # Свойства пользователя по умолчанию: accountEnabled в них нет.
            return {
                k: v
                for k, v in public.items()
                if k in ("id", "displayName", "mail", "userPrincipalName")
            }
        wanted = set(query["$select"].split(","))
        return {k: v for k, v in public.items() if k in wanted}

    def _groups(
        self, request: httpx.Request, parts: list[str], query: dict[str, str]
    ) -> httpx.Response:
        if denied := self._need("GroupMember.Read.All"):
            return denied
        if not parts:
            groups = list(self.groups.values())
            if query.get("$filter") == "groupTypes/any(c:c eq 'Unified')":
                groups = [g for g in groups if "Unified" in g["groupTypes"]]
            elif "$filter" in query:
                return _error(400, "Request_UnsupportedQuery")
            shown = [{"id": g["id"], "displayName": g["displayName"]} for g in groups]
            return self._page(shown, "groups", query)
        group = self.groups.get(parts[0])
        if group is None:
            return _error(
                404,
                "Request_ResourceNotFound",
                f"Resource '{parts[0]}' does not exist.",
            )
        rest = parts[1:]
        if rest == ["sites", "root"]:
            if denied := self._need("Sites.Read.All"):
                return denied
            site = self.sites.get(group["site"] or "")
            if site is None:
                return _error(404, "itemNotFound", "Requested site could not be found")
            return httpx.Response(200, json=self._site_json(site))
        if rest in (["transitiveMembers"], ["owners"]):
            advanced = request.headers.get("consistencylevel") == "eventual"
            # $skiptoken в nextLink — часть ссылки сервера, не запрос клиента.
            asked = [k for k in query if k.startswith("$") and k != "$skiptoken"]
            if asked and not advanced:
                return _error(
                    400,
                    "Request_UnsupportedQuery",
                    "Unsupported query without ConsistencyLevel.",
                )
            if rest == ["owners"]:
                entries = [self._directory_user(u) for u in group["owners"]]
            else:
                entries = self._transitive(group["id"], set())
            return self._page(entries, f"groups/{group['id']}/{rest[0]}", query)
        return _error(400, "BadRequest")

    def _directory_user(self, user_id: str) -> dict[str, Any]:
        user = self.users[user_id]
        return {
            "@odata.type": USER,
            "id": user_id,
            "displayName": user["displayName"],
            "mail": user["mail"],
            "userPrincipalName": user["userPrincipalName"],
        }

    def _transitive(self, group_id: str, seen: set[str]) -> list[dict[str, Any]]:
        """Плоский список: пользователи и вложенные группы со всем составом."""
        seen.add(group_id)
        found: list[dict[str, Any]] = []
        for member in self.groups[group_id]["members"]:
            if member in self.users:
                found.append(self._directory_user(member))
            elif member in self.groups and member not in seen:
                nested = self.groups[member]
                found.append(
                    {
                        "@odata.type": GROUP,
                        "id": member,
                        "displayName": nested["displayName"],
                        "mail": nested["mail"],
                    }
                )
                found.extend(self._transitive(member, seen))
        return found

    def _page(
        self,
        items: list[dict[str, Any]],
        path: str,
        query: dict[str, str],
        *,
        delta: bool = False,
    ) -> httpx.Response:
        start = int(query.get("$skiptoken", "0") or 0)
        limit = self.page_size
        if "$top" in query and not delta:
            limit = min(limit, int(query["$top"]))
        chunk = items[start : start + limit]
        body: dict[str, Any] = {
            "@odata.context": f"{GRAPH}$metadata#{path.split('/')[-1]}",
            "value": chunk,
        }
        if start + limit < len(items):
            params = {k: v for k, v in query.items() if k != "$skiptoken"}
            params["$skiptoken"] = str(start + limit)
            host = "evil.example.net" if self.foreign_next_link else GRAPH_HOST
            body["@odata.nextLink"] = f"https://{host}/v1.0/{path}?{urlencode(params)}"
        elif delta:
            body["@odata.deltaLink"] = (
                f"{GRAPH}{path}?token=MzslMjM0OyUyMzE7MzsyM2YwNDVh"
            )
        return httpx.Response(200, json=body)


def sample_m365() -> FakeM365:
    """Компания: сайт-корень, сайт группы «Маркетинг» с подсайтом, личный
    сайт (в /sites он есть, в модуль SharePoint не попадает), OneDrive
    Анны и Бориса; у Дины OneDrive нет, Глеб отключён, гость — внешний."""
    fake = FakeM365()
    fake.add_user(ANNA, "Анна", "anna", mail="anna@contoso.ru")
    fake.add_user(BORIS, "Борис", "boris", mail="Boris@Contoso.ru")
    fake.add_user(VERA, "Вера", "vera")
    fake.add_user(GLEB, "Глеб", "gleb", enabled=False)
    # Почты нет — берётся UPN.
    fake.add_user(DINA, "Дина", "dina", mail="")
    fake.add_user(GUEST, "Гость", "ext", mail="ext@partner.org", guest=True)

    fake.add_site(SITE_ROOT, "Contoso", "")
    fake.add_site(SITE_MKT, "Маркетинг", "/sites/marketing")
    fake.add_site(SITE_MKT_SUB, "Проекты", "/sites/marketing/projects", parent=SITE_MKT)
    fake.add_site(
        SITE_PERSONAL,
        "Анна",
        "/personal/anna_contoso_onmicrosoft_com",
        personal=True,
        host=MY_HOST,
    )
    fake.add_group(
        G_MARKETING,
        "Маркетинг",
        members=[ANNA, BORIS],
        owners=[ANNA],
        unified=True,
        site=SITE_MKT,
    )
    fake.add_group(G_LEADS, "Руководители", members=[GLEB, DINA])
    fake.add_group(G_FINANCE, "Финансы", members=[VERA, G_LEADS])

    owners = perm_site_group("Маркетинг Owners", "owner", "3")
    members = perm_site_group("Маркетинг Members", "write", "5")
    visitors = perm_site_group("Маркетинг Visitors", "read", "4")

    fake.add_drive(D_ROOT, "Документы", site=SITE_ROOT)
    fake.add_file(
        D_ROOT,
        "f-rules",
        "Регламент.txt",
        "Регламент закупок.".encode(),
        perms=[perm_group(fake, G_FINANCE)],
    )
    fake.add_file(
        D_ROOT,
        "f-all",
        "Новости.txt",
        b"Everyone",
        perms=[
            perm_claim(
                f"c:0-.f|rolemanager|spo-grid-all-users/{TENANT_ID}",
                "Все, кроме внешних пользователей",
            )
        ],
    )

    fake.add_drive(D_MKT, "Документы", site=SITE_MKT)
    fake.add_folder(D_MKT, "d-plans", "Планы")
    fake.add_folder(D_MKT, "d-2026", "2026", parent="d-plans")
    fake.add_file(
        D_MKT,
        "f-plan",
        "План.txt",
        "План запуска — в мае.".encode(),
        parent="d-2026",
        perms=[owners, members, visitors],
    )
    fake.add_file(
        D_MKT,
        "f-budget",
        "Бюджет.txt",
        b"Budget",
        parent="d-plans",
        perms=[
            owners,
            perm_user(fake, BORIS, "write"),
            perm_link(fake, "organization"),
        ],
    )
    fake.add_file(
        D_MKT,
        "f-secret",
        "Секрет.txt",
        b"Secret",
        perms=[perm_link(fake, "anonymous", VERA)],
    )
    fake.add_file(D_MKT, "f-logo", "Логотип.png", b"\x89PNG", perms=[members])
    fake.add_file(D_MKT, "f-notes", "Заметки", package=True, perms=[members])

    fake.add_drive(D_MKT_SUB, "Документы", site=SITE_MKT_SUB)
    fake.add_file(
        D_MKT_SUB, "f-project", "Проект.md", "# Проект".encode(), perms=[members]
    )

    fake.add_drive(D_ANNA, "OneDrive", owner=ANNA)
    fake.add_file(
        D_ANNA,
        "f-draft",
        "Черновик.txt",
        b"Draft",
        perms=[perm_user(fake, ANNA, "owner")],
    )
    fake.add_file(
        D_ANNA,
        "f-for-boris",
        "Для Бориса.txt",
        b"For Boris",
        perms=[perm_user(fake, ANNA, "owner"), perm_link(fake, "users", BORIS)],
    )
    fake.add_file(
        D_ANNA,
        "f-org-link",
        "Всем по ссылке.txt",
        b"Org link",
        perms=[perm_user(fake, ANNA, "owner"), perm_link(fake, "organization")],
    )
    fake.add_drive(D_BORIS, "OneDrive", owner=BORIS)
    return fake
