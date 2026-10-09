"""Поддельные Google OAuth (сервисный аккаунт), Drive API v3 и Directory API
для контрактных тестов коннектора gdrive.

Формы ответов — по справочнику Drive API v3 и Admin SDK Directory
(developers.google.com, 09.10): токен — обмен JWT RS256
(`grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`), ошибки
токена — {error, error_description}; ошибки API —
{error: {code, message, errors: [{reason}], status}}; списки — pageToken
/ nextPageToken; у элементов общих дисков `permissions` в files.list нет
(«Not populated for items in shared drives»), есть
`hasAugmentedPermissions`; files.list без useDomainAdminAccess — файлы
общего диска видит только его участник. Живым Workspace не проверено.
"""

import json
from dataclasses import dataclass, field
from functools import cache
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from corp_ed.core.outbound import OutboundClient
from tests.fake_connector import public_resolver

TOKEN_HOST = "oauth2.googleapis.com"
DRIVE_HOST = "www.googleapis.com"
DIRECTORY_HOST = "admin.googleapis.com"
CONTENT_HOST = "doc-0k-docs.googleusercontent.com"
TOKEN_URL = f"https://{TOKEN_HOST}/token"
DRIVE_API = f"https://{DRIVE_HOST}/drive/v3/"
DIRECTORY_API = f"https://{DIRECTORY_HOST}/admin/directory/v1/"
SA_EMAIL = "kronto@acme-project.iam.gserviceaccount.com"
SA_CLIENT_ID = "112233445566778899001"
DOMAIN = "example.com"
ADMIN = "admin@example.com"

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
USERS_SCOPE = "https://www.googleapis.com/auth/admin.directory.user.readonly"
GROUPS_SCOPE = "https://www.googleapis.com/auth/admin.directory.group.member.readonly"
ALL_SCOPES = frozenset({DRIVE_SCOPE, USERS_SCOPE, GROUPS_SCOPE})

FOLDER = "application/vnd.google-apps.folder"
GDOC = "application/vnd.google-apps.document"
GSHEET = "application/vnd.google-apps.spreadsheet"
GSLIDES = "application/vnd.google-apps.presentation"
GFORM = "application/vnd.google-apps.form"
SHORTCUT = "application/vnd.google-apps.shortcut"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_EXPORTS = {
    GDOC: {DOCX},
    GSHEET: {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    GSLIDES: {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    },
}


@cache
def _private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def private_key_pem() -> str:
    return (
        _private_key()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )


def service_account_key(**overrides: Any) -> str:
    """JSON-ключ, как его скачивает консоль Google Cloud (с отступами)."""
    key: dict[str, Any] = {
        "type": "service_account",
        "project_id": "acme-project",
        "private_key_id": "0f" * 20,
        "private_key": private_key_pem(),
        "client_email": SA_EMAIL,
        "client_id": SA_CLIENT_ID,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        # Адрес токенов из ключа адаптер брать не должен: в ключе может
        # оказаться что угодно.
        "token_uri": "https://evil.example.net/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": (
            "https://www.googleapis.com/robot/v1/metadata/x509/"
            "kronto%40acme-project.iam.gserviceaccount.com"
        ),
        "universe_domain": "googleapis.com",
    }
    key.update(overrides)
    return json.dumps(key, indent=2)


def perm(
    kind: str,
    value: str = "",
    role: str = "reader",
    *,
    inherited: bool | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Разрешение Drive: user/group — почта, domain — домен, anyone — ничего."""
    item: dict[str, Any] = {
        "id": f"p-{kind}-{value}-{role}",
        "type": kind,
        "role": role,
    }
    if kind in ("user", "group"):
        item["emailAddress"] = value
    elif kind == "domain":
        item["domain"] = value
    if inherited is not None:
        item["permissionDetails"] = [
            {"permissionType": "file", "role": role, "inherited": inherited}
        ]
    item.update(extra)
    return item


def _error(status: int, reason: str, message: str = "") -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "error": {
                "code": status,
                "message": message or reason,
                "errors": [{"domain": "global", "reason": reason, "message": message}],
                "status": "ERROR",
            }
        },
    )


@dataclass
class FakeGoogle:
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    groups: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    drives: dict[str, dict[str, Any]] = field(default_factory=dict)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    content: dict[str, bytes] = field(default_factory=dict)
    delegated: set[str] = field(default_factory=lambda: set(ALL_SCOPES))
    page_size: int = 1000
    """Потолок pageSize: тесты ставят 2, чтобы проверить nextPageToken."""
    tokens: dict[str, tuple[str, str]] = field(default_factory=dict)
    """Выданный токен → (почта, scope)."""
    revoked: set[str] = field(default_factory=set)
    key_revoked: bool = False
    token_rejects: set[str] = field(default_factory=set)
    """Почты, на которые токен не выдаётся (удалён между листингом и обменом)."""
    rate_limit_hits: int = 0
    rate_limit_403_hits: int = 0
    export_too_large: set[str] = field(default_factory=set)
    listing_forbidden: set[str] = field(default_factory=set)
    """Почты, кому files.list отвечает 403 (Диск выключен для их отдела)."""
    forbid_next_file_pages: bool = False
    """files.list по файлам (не папкам) со второй страницы — 403."""
    api_disabled: bool = False
    """API не включён в проекте сервисного аккаунта: 403 на всё."""
    forbidden_files: set[str] = field(default_factory=set)
    calls: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    """(почта, путь, параметры) каждого запроса к API."""
    assertions: list[dict[str, Any]] = field(default_factory=list)
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
        self, email: str, *, suspended: bool = False, admin: bool = False
    ) -> str:
        self.users[email] = {"suspended": suspended, "admin": admin}
        return email

    def add_group(self, email: str, *members: tuple[str, str]) -> str:
        """members — (type, email): USER, GROUP, CUSTOMER, EXTERNAL."""
        self.groups[email] = [
            {"email": value, "type": kind, "role": "MEMBER", "status": "ACTIVE"}
            for kind, value in members
        ]
        return email

    def add_drive(self, drive_id: str, name: str, *permissions: dict[str, Any]) -> str:
        self.drives[drive_id] = {
            "id": drive_id,
            "name": name,
            "perms": list(permissions),
        }
        return drive_id

    def add_folder(
        self,
        file_id: str,
        name: str,
        parent: str,
        *,
        drive: str | None = None,
        owner: str | None = None,
        perms: list[dict[str, Any]] | None = None,
        augmented: bool = False,
        limited: bool = False,
    ) -> str:
        return self.add_file(
            file_id,
            name,
            parent,
            mime=FOLDER,
            drive=drive,
            owner=owner,
            perms=perms,
            augmented=augmented,
            limited=limited,
        )

    def add_file(
        self,
        file_id: str,
        name: str,
        parent: str,
        data: bytes = b"",
        *,
        mime: str = "text/plain",
        drive: str | None = None,
        owner: str | None = None,
        perms: list[dict[str, Any]] | None = None,
        augmented: bool = False,
        limited: bool = False,
        size: int | None = None,
        modified: str = "2026-09-01T10:00:00.000Z",
        trashed: bool = False,
    ) -> str:
        """perms — что вернёт permissions.list по файлу (с унаследованными)."""
        item: dict[str, Any] = {
            "kind": "drive#file",
            "id": file_id,
            "name": name,
            "mimeType": mime,
            "parents": [parent],
            "modifiedTime": modified,
            "version": "7",
            "trashed": trashed,
            "webViewLink": f"https://docs.google.com/d/{file_id}/view",
            "_perms": list(perms or []),
            "_owner": owner,
        }
        if drive:
            item["driveId"] = drive
            item["hasAugmentedPermissions"] = augmented
        if limited:
            item["inheritedPermissionsDisabled"] = True
        if mime != FOLDER and not mime.startswith("application/vnd.google-apps."):
            item["md5Checksum"] = f"md5-{file_id}"
            item["size"] = str(len(data) if size is None else size)
        elif size is not None:
            item["size"] = str(size)
        self.files[file_id] = item
        self.content[file_id] = data
        return file_id

    # --- обработка -----------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "")
        if host == TOKEN_HOST:
            return self._token(request)
        if host == CONTENT_HOST:
            self.downloads.append((host, request.headers.get("authorization", "")))
            file_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, content=self.content.get(file_id, b""))
        if host not in (DRIVE_HOST, DIRECTORY_HOST):
            return httpx.Response(404, text="unknown host")
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ")
        if not auth.startswith("Bearer ") or token not in self.tokens:
            return _error(401, "authError", "Invalid Credentials")
        if token in self.revoked:
            return _error(401, "authError", "Invalid Credentials")
        subject, scope = self.tokens[token]
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        path = unquote(request.url.path)
        self.calls.append((subject, path, query))
        if self.api_disabled:
            response = _error(403, "accessNotConfigured", "API has not been used")
            body = response.json()
            body["error"]["details"] = [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "SERVICE_DISABLED",
                }
            ]
            return httpx.Response(403, json=body)
        if self.rate_limit_hits > 0:
            self.rate_limit_hits -= 1
            return _error(429, "rateLimitExceeded", "Rate Limit Exceeded")
        if self.rate_limit_403_hits > 0:
            self.rate_limit_403_hits -= 1
            return _error(403, "userRateLimitExceeded", "User Rate Limit Exceeded")
        if host == DIRECTORY_HOST:
            return self._directory(subject, scope, path, query)
        if scope != DRIVE_SCOPE:
            return _error(403, "insufficientPermissions")
        return self._drive(request, subject, path, query)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("grant_type") != "urn:ietf:params:oauth:grant-type:jwt-bearer":
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        try:
            claims = jwt.decode(
                form.get("assertion", ""),
                _private_key().public_key(),
                algorithms=["RS256"],
                audience=TOKEN_URL,
            )
        except jwt.PyJWTError:
            return httpx.Response(
                400,
                json={"error": "invalid_grant", "error_description": "Invalid JWT"},
            )
        self.assertions.append(claims)
        if self.key_revoked or claims.get("iss") != SA_EMAIL:
            return httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": "Invalid JWT Signature.",
                },
            )
        scope = str(claims.get("scope", ""))
        if not set(scope.split()) <= self.delegated:
            return httpx.Response(
                401,
                json={
                    "error": "unauthorized_client",
                    "error_description": "Client is unauthorized to retrieve "
                    "access tokens using this method, or client not authorized "
                    "for any of the scopes requested.",
                },
            )
        subject = str(claims.get("sub", ""))
        user = self.users.get(subject)
        if user is None or user["suspended"] or subject in self.token_rejects:
            return httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": "Invalid email or User ID",
                },
            )
        self.issued += 1
        token = f"ya29.fake-{self.issued}"
        self.tokens[token] = (subject, scope)
        return httpx.Response(
            200,
            json={"access_token": token, "expires_in": 3599, "token_type": "Bearer"},
        )

    # --- Directory ------------------------------------------------------------

    def _directory(
        self, subject: str, scope: str, path: str, query: dict[str, str]
    ) -> httpx.Response:
        if not self.users.get(subject, {}).get("admin"):
            return _error(
                403, "forbidden", "Not Authorized to access this resource/api"
            )
        rest = path.removeprefix("/admin/directory/v1/")
        if rest == "users":
            if scope != USERS_SCOPE:
                return _error(403, "insufficientPermissions")
            if query.get("customer") != "my_customer":
                return _error(400, "badRequest")
            users = [
                {"primaryEmail": email, "suspended": info["suspended"]}
                for email, info in sorted(self.users.items())
            ]
            return self._page(users, "users", query, "maxResults", 500)
        if rest.startswith("groups/") and rest.endswith("/members"):
            if scope != GROUPS_SCOPE:
                return _error(403, "insufficientPermissions")
            key = rest.removeprefix("groups/").removesuffix("/members")
            if key not in self.groups:
                return _error(404, "notFound", "Resource Not Found: groupKey")
            return self._page(self.groups[key], "members", query, "maxResults", 200)
        return _error(404, "notFound")

    # --- Drive ----------------------------------------------------------------

    def _drive(
        self, request: httpx.Request, subject: str, path: str, query: dict[str, str]
    ) -> httpx.Response:
        rest = path.removeprefix("/drive/v3/")
        if rest == "about":
            return httpx.Response(200, json={"user": {"emailAddress": subject}})
        if rest == "drives":
            if query.get("useDomainAdminAccess") == "true":
                if not self.users.get(subject, {}).get("admin"):
                    return _error(403, "insufficientPermissions")
                drives = list(self.drives.values())
            else:
                drives = [d for d in self.drives.values() if self._member(subject, d)]
            shown = [{"id": d["id"], "name": d["name"]} for d in drives]
            return self._page(shown, "drives", query, "pageSize", 100)
        if rest == "files":
            return self._list(subject, query)
        parts = rest.split("/")
        if len(parts) >= 2 and parts[0] == "files":
            file_id = parts[1]
            if len(parts) == 4 and parts[2] == "permissions":
                return _error(404, "notFound")
            if len(parts) == 3 and parts[2] == "permissions":
                return self._permissions(subject, file_id, query)
            item = self.files.get(file_id)
            if item is None or not self._can_read(subject, item):
                return _error(404, "notFound", f"File not found: {file_id}.")
            if file_id in self.forbidden_files:
                return _error(403, "cannotDownloadFile")
            if len(parts) == 3 and parts[2] == "export":
                allowed = _EXPORTS.get(item["mimeType"], set())
                if query.get("mimeType") not in allowed:
                    return _error(403, "fileNotExportable")
                if file_id in self.export_too_large:
                    return _error(
                        403,
                        "exportSizeLimitExceeded",
                        "This file is too large to be exported.",
                    )
                return httpx.Response(200, content=self.content[file_id])
            if query.get("alt") == "media":
                if item["mimeType"].startswith("application/vnd.google-apps."):
                    return _error(403, "fileNotDownloadable")
                # Как у Google: содержимое отдаёт другой хост по редиректу.
                return httpx.Response(
                    302,
                    headers={"location": f"https://{CONTENT_HOST}/files/{file_id}"},
                )
            return httpx.Response(200, json=self._shown(item, subject))
        return _error(404, "notFound")

    def _list(self, subject: str, query: dict[str, str]) -> httpx.Response:
        q = query.get("q", "")
        if subject in self.listing_forbidden:
            return _error(403, "forbidden", "Drive is disabled for this user")
        if self.forbid_next_file_pages and "pageToken" in query and "!=" in q:
            return _error(403, "forbidden")
        corpora = query.get("corpora", "user")
        if corpora == "drive":
            drive = self.drives.get(query.get("driveId", ""))
            if (
                drive is None
                or query.get("includeItemsFromAllDrives") != "true"
                or query.get("supportsAllDrives") != "true"
            ):
                return _error(400, "badRequest")
            if not self._member(subject, drive):
                return _error(404, "notFound", "Shared drive not found")
            items = [f for f in self.files.values() if f.get("driveId") == drive["id"]]
        elif corpora == "user":
            items = [f for f in self.files.values() if not f.get("driveId")]
            if "'me' in owners" not in q:
                return _error(400, "badRequest", "test: owner filter expected")
            items = [f for f in items if f.get("_owner") == subject]
        else:
            return _error(400, "badRequest")
        if "trashed = false" in q:
            items = [f for f in items if not f["trashed"]]
        if f"mimeType = '{FOLDER}'" in q:
            items = [f for f in items if f["mimeType"] == FOLDER]
        elif f"mimeType != '{FOLDER}'" in q:
            items = [f for f in items if f["mimeType"] != FOLDER]
        shown = [self._shown(f, subject) for f in items]
        return self._page(shown, "files", query, "pageSize", 100)

    def _permissions(
        self, subject: str, file_id: str, query: dict[str, str]
    ) -> httpx.Response:
        if file_id in self.drives:
            drive = self.drives[file_id]
            admin = query.get("useDomainAdminAccess") == "true" and self.users.get(
                subject, {}
            ).get("admin")
            if not admin and not self._member(subject, drive):
                return _error(404, "notFound")
            return self._page(drive["perms"], "permissions", query, "pageSize", 100)
        item = self.files.get(file_id)
        if item is None or not self._can_read(subject, item):
            return _error(404, "notFound", f"File not found: {file_id}.")
        if item.get("driveId") and query.get("supportsAllDrives") != "true":
            return _error(404, "notFound")
        return self._page(item["_perms"], "permissions", query, "pageSize", 100)

    def _shown(self, item: dict[str, Any], subject: str) -> dict[str, Any]:
        shown = {k: v for k, v in item.items() if not k.startswith("_")}
        # permissions в листинге — только тем, кто может делиться, и не для
        # элементов общих дисков.
        if not item.get("driveId") and item.get("_owner") == subject:
            shown["permissions"] = item["_perms"]
        return shown

    def _member(self, subject: str, drive: dict[str, Any]) -> bool:
        for p in drive["perms"]:
            if p.get("type") == "user" and p.get("emailAddress") == subject:
                return True
            if p.get("type") == "group" and any(
                m["email"] == subject for m in self.groups.get(p["emailAddress"], [])
            ):
                return True
        return False

    def _can_read(self, subject: str, item: dict[str, Any]) -> bool:
        drive_id = item.get("driveId")
        if drive_id:
            return self._member(subject, self.drives[drive_id])
        return item.get("_owner") == subject or any(
            p.get("emailAddress") == subject for p in item["_perms"]
        )

    def _page(
        self,
        items: list[dict[str, Any]],
        key: str,
        query: dict[str, str],
        size_param: str,
        default: int,
    ) -> httpx.Response:
        size = min(int(query.get(size_param) or default), self.page_size)
        start = int(query.get("pageToken") or 0)
        chunk = items[start : start + size]
        body: dict[str, Any] = {key: chunk}
        if start + size < len(items):
            body["nextPageToken"] = str(start + size)
        return httpx.Response(200, json=body)


def sample_google() -> FakeGoogle:
    """Домен example.com: админ, Анна, Борис, Вера (заблокирована).

    Общий диск «Кадры» (участник — группа hr@, в ней Анна и вложенная
    группа leads@ с Борисом): регламент без своих прав, приказ с личным
    доступом Веры… и папка «Закрытая» с ограниченным доступом. Общий
    диск «Бухгалтерия» — без участников из домена (только внешний).
    Мой диск Анны: документ Google, таблица, PDF без расширения, форма,
    ярлык, файл в корзине, файл «всем в домене» и «всем по ссылке».
    """
    server = FakeGoogle()
    server.add_user(ADMIN, admin=True)
    anna = server.add_user("anna@example.com")
    boris = server.add_user("boris@example.com")
    server.add_user("vera@example.com", suspended=True)
    server.add_group("leads@example.com", ("USER", boris))
    server.add_group("hr@example.com", ("USER", anna), ("GROUP", "leads@example.com"))
    hr_members = [perm("group", "hr@example.com", "writer", inherited=True)]

    # Общий диск «Кадры».
    server.add_drive("drv-hr", "Кадры", perm("group", "hr@example.com", "writer"))
    server.add_folder("fld-rules", "Регламенты", "drv-hr", drive="drv-hr")
    server.add_file(
        "f-vacation",
        "Отпуск.txt",
        "fld-rules",
        "Отпуск — 28 дней.".encode(),
        drive="drv-hr",
        perms=hr_members,
    )
    server.add_file(
        "f-order",
        "Приказ.txt",
        "drv-hr",
        "Приказ о пропусках.".encode(),
        drive="drv-hr",
        augmented=True,
        perms=[*hr_members, perm("user", "Partner@Other.org", inherited=False)],
    )
    server.add_folder(
        "fld-closed",
        "Закрытая",
        "drv-hr",
        drive="drv-hr",
        augmented=True,
        limited=True,
        perms=[
            *hr_members,
            perm("user", "boris@example.com", "reader", inherited=False),
        ],
    )
    server.add_file(
        "f-salary",
        "Зарплаты.txt",
        "fld-closed",
        "Зарплаты — секрет.".encode(),
        drive="drv-hr",
        # Что вернёт permissions.list у файла в папке с ограниченным
        # доступом, документация не говорит: худший случай — участники
        # диска тоже в списке. Адаптер должен их отсечь.
        perms=[
            *hr_members,
            perm("user", "boris@example.com", "reader", inherited=True),
        ],
    )
    server.add_file(
        "f-scan", "Скан.png", "drv-hr", b"\x89PNG", drive="drv-hr", mime="image/png"
    )

    # Общий диск без участников из домена: от чьего имени читать — некому.
    server.add_drive(
        "drv-acc", "Бухгалтерия", perm("user", "auditor@other.org", "organizer")
    )
    server.add_file("f-acc", "Баланс.txt", "drv-acc", b"secret", drive="drv-acc")

    # Мой диск Анны.
    owner = perm("user", anna, "owner")
    server.add_folder("fld-anna", "Проекты", "root-anna", owner=anna, perms=[owner])
    server.add_file(
        "f-plan",
        "План",
        "fld-anna",
        b"PK-docx-plan",
        mime=GDOC,
        owner=anna,
        perms=[owner, perm("user", "boris@example.com", "commenter")],
    )
    server.add_file(
        "f-budget",
        "Бюджет",
        "root-anna",
        b"PK-xlsx",
        mime=GSHEET,
        owner=anna,
        perms=[owner, perm("domain", DOMAIN)],
    )
    server.add_file(
        "f-report",
        "Отчёт",
        "root-anna",
        b"%PDF-1.7 report",
        mime="application/pdf",
        owner=anna,
        perms=[owner, perm("anyone", allowFileDiscovery=False)],
    )
    server.add_file(
        "f-survey", "Опрос", "root-anna", mime=GFORM, owner=anna, perms=[owner]
    )
    server.add_file(
        "f-link", "Ярлык", "root-anna", mime=SHORTCUT, owner=anna, perms=[owner]
    )
    server.add_file(
        "f-old",
        "Старое.txt",
        "root-anna",
        b"old",
        owner=anna,
        perms=[owner],
        trashed=True,
    )
    server.add_file(
        "f-partner",
        "Для партнёра.txt",
        "root-anna",
        "Условия партнёрства.".encode(),
        owner=anna,
        perms=[owner, perm("domain", "other.org")],
    )
    return server
