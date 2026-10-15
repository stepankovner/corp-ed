"""Поддельный портал Р7-Офис КС 2019 (API ONLYOFFICE Workspace) для
контрактных тестов адаптера.

Формы ответов — по документации Р7 (15.10), страницы от
`https://support.r7-office.ru/community_server/api/settings-api/`:
- вход: authorization/get-the-authentication-token/
- раздел «Общие»: files/folders/poluchit-obshhij-razdel/
- папка по id: files/folders/poluchit-papku-po-id/
- права файла: files/sharing/poluchit-informaciju-ob-obshhem-fajle/
- права папки: files/sharing/poluchit-informaciju-ob-obshhej-papke/
- ссылка на скачивание: files/file-api/poluchit-ssylku-na-skachivanie-fajla/
- все профили: peoples/profile/get-all-profiles/
- профиль текущего: peoples/profile/get-my-profile/
- группа: group/poluchit-gruppu/
- count / startIndex: `https://support.r7-office.ru/community_server/api/`
  more_information/request-filtering/

Чего в документации нет, а взято из исходного кода ONLYOFFICE Community
Server (как сервер считает права): записи доступа хранятся на файле и
папках, ответ `.../share` — по одной ближайшей записи на субъекта, для
«Общих документов» сервер дописывает «Администраторов» с полным доступом
и «Всех» с чтением, если их записей нет, и запись создателя файла с
полным доступом, если записи владельца нет; `shared` у файла — есть ли
у него свои записи. Настоящее решение «может ли прочитать» —
`readers_of` (FileSecurity: администратор в «Общих» читает всё, иначе
запись самого файла главнее записей папок, внутри — личная → группы →
администраторы → все), по нему тесты проверяют, что адаптер не
открывает лишнего.
"""

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx

from corp_ed.core.outbound import OutboundClient
from tests.fake_connector import public_resolver

HOST = "office.company.ru"
BASE = f"https://{HOST}/"
EVERYONE = "c5cc67d1-c3e8-43c0-a3ad-3928ae3e5b5e"
ADMINS = "cd84e66b-b803-40fc-99f9-b2969a54a1de"
LINK = "11111111-1111-1111-1111-111111111111"
ADMIN_LOGIN = "admin@company.ru"
ADMIN_PASSWORD = "admin-password"  # noqa: S105 — поддельный сервер
ANNA_ID = "a0000000-0000-0000-0000-00000000000a"
BORIS_ID = "b0000000-0000-0000-0000-00000000000b"
VERA_ID = "c0000000-0000-0000-0000-00000000000c"
GLEB_ID = "d0000000-0000-0000-0000-00000000000d"
ADMIN_ID = "e0000000-0000-0000-0000-00000000000e"
FINANCE = "f0000000-0000-0000-0000-0000000000f1"
COMMON_ID = 1
FULL, READ, RESTRICT, NONE, REVIEW = 1, 2, 3, 0, 5
COMMON, USER = 1, 5
READABLE = frozenset({1, 2, 5, 6, 7, 8})
_SIGNATURE = "sig-ok"
_PRIORITY_EVERYONE = 3


@dataclass
class Record:
    entry: str
    """«file» или «folder»."""
    entry_id: int
    subject: str
    access: int


@dataclass
class FakeR7:
    host: str = HOST
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    passwords: dict[str, tuple[str, str]] = field(default_factory=dict)
    """логин → (пароль, id пользователя)."""
    tfa_logins: set[str] = field(default_factory=set)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    folders: dict[int, dict[str, Any]] = field(default_factory=dict)
    files: dict[int, dict[str, Any]] = field(default_factory=dict)
    blobs: dict[int, bytes] = field(default_factory=dict)
    records: list[Record] = field(default_factory=list)
    tokens: dict[str, str] = field(default_factory=dict)
    expire_after: int | None = None
    """Через сколько запросов с токеном считать его истёкшим (раз)."""
    always_expired: bool = False
    rate_limit_hits: int = 0
    retry_after: str | None = None
    server_errors: int = 0
    share_forbidden: set[int] = field(default_factory=set)
    folder_share_forbidden: set[int] = field(default_factory=set)
    presigned_scheme: str = "https"
    download_html: bool = False
    calls: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    auth_headers: list[str] = field(default_factory=list)
    download_auth: list[str] = field(default_factory=list)
    _issued: int = 0

    @property
    def base(self) -> str:
        return f"https://{self.host}/"

    def client(self) -> OutboundClient:
        return OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
            resolver=public_resolver,
        )

    # --- наполнение -------------------------------------------------------------

    def add_user(
        self,
        user_id: str,
        email: str | None,
        *,
        login: str | None = None,
        password: str = "pw",
        admin: bool = False,
        status: int = 1,
        admin_modules: tuple[str, ...] = (),
        visitor: bool = False,
    ) -> None:
        self.users[user_id] = {
            "id": user_id,
            "userName": (email or user_id).split("@")[0],
            "isVisitor": visitor,
            "firstName": "Имя",
            "lastName": "Фамилия",
            "email": email,
            "status": status,
            "activationStatus": 1,
            "displayName": email,
            "title": "",
            "groups": [],
            "isAdmin": admin,
            "isLDAP": False,
            "listAdminModules": list(admin_modules),
            "isOwner": False,
            "isSSO": False,
            "avatarSmall": "",
            "profileUrl": "",
        }
        if login:
            self.passwords[login] = (password, user_id)

    def add_group(self, group_id: str, name: str, members: set[str]) -> None:
        self.groups[group_id] = {"id": group_id, "name": name, "members": members}

    def add_folder(
        self,
        folder_id: int,
        title: str,
        parent: int | None,
        *,
        root_type: int = COMMON,
        provider: bool = False,
    ) -> None:
        self.folders[folder_id] = {
            "id": folder_id,
            "title": title,
            "parentId": parent,
            "rootFolderType": root_type,
            "providerItem": provider,
        }

    def add_file(
        self,
        file_id: int,
        title: str,
        folder: int,
        data: bytes,
        *,
        creator: str = ADMIN_ID,
        size: int | None = None,
        version: int = 1,
        updated: str = "2026-09-01T10:00:00.0000000Z",
        provider: bool = False,
    ) -> None:
        self.files[file_id] = {
            "id": file_id,
            "title": title,
            "folderId": folder,
            "version": version,
            "versionGroup": 1,
            "updated": updated,
            "created": "2026-01-01T10:00:00.0000000Z",
            "pureContentLength": len(data) if size is None else size,
            "creator": creator,
            "providerItem": provider,
        }
        self.blobs[file_id] = data

    def share(self, entry: str, entry_id: int, subject: str, access: int) -> None:
        self.records = [
            r
            for r in self.records
            if not (
                r.entry == entry and r.entry_id == entry_id and r.subject == subject
            )
        ]
        self.records.append(Record(entry, entry_id, subject, access))

    # --- как сервер считает права --------------------------------------------------

    def _ancestors(self, folder: int) -> list[int]:
        chain: list[int] = []
        current: int | None = folder
        while current is not None and current in self.folders and current not in chain:
            chain.append(current)
            current = self.folders[current]["parentId"]
        return chain

    def _root_type(self, folder: int) -> int:
        return int(self.folders[self._ancestors(folder)[-1]]["rootFolderType"])

    def _graded(self, file_id: int) -> list[tuple[int, Record]]:
        """Записи файла (уровень -1) и его папок (уровень — расстояние)."""
        f = self.files[file_id]
        graded = [
            (-1, r) for r in self.records if r.entry == "file" and r.entry_id == file_id
        ]
        for level, folder in enumerate(self._ancestors(f["folderId"])):
            graded += [
                (level, r)
                for r in self.records
                if r.entry == "folder" and r.entry_id == folder
            ]
        return graded

    def shared_info(self, file_id: int) -> list[dict[str, Any]]:
        """GetSharedInfo: ближайшая запись на субъекта и дописанные сервером."""
        f = self.files[file_id]
        nearest: dict[str, tuple[int, Record]] = {}
        for level, record in self._graded(file_id):
            known = nearest.get(record.subject)
            if known is None or level < known[0]:
                nearest[record.subject] = (level, record)
        result: list[dict[str, Any]] = []
        for subject, (_, record) in nearest.items():
            if subject == LINK:
                result.append(
                    {
                        "access": record.access,
                        "sharedTo": {
                            "id": LINK,
                            "shareLink": f"{self.base}s/{file_id}",
                        },
                        "isLocked": False,
                        "isOwner": False,
                    }
                )
                continue
            result.append(self._ace(subject, record.access, subject == f["creator"]))
        if not any(r["isOwner"] for r in result):
            result.append(self._ace(f["creator"], FULL, True))
        if self._root_type(f["folderId"]) == COMMON:
            subjects = {r["sharedTo"]["id"] for r in result}
            if ADMINS not in subjects:
                result.append(self._ace(ADMINS, FULL, False))
            if EVERYONE not in subjects:
                result.append(self._ace(EVERYONE, READ, False))
        return result

    def folder_shared_info(self, folder_id: int) -> list[dict[str, Any]]:
        """GetSharedInfo для папки: записи её и её предков, ближайшая."""
        nearest: dict[str, tuple[int, Record]] = {}
        for level, folder in enumerate(self._ancestors(folder_id)):
            for record in self.records:
                if record.entry != "folder" or record.entry_id != folder:
                    continue
                known = nearest.get(record.subject)
                if known is None or level < known[0]:
                    nearest[record.subject] = (level, record)
        creator = ADMIN_ID
        result = [
            self._ace(subject, record.access, subject == creator)
            for subject, (_, record) in nearest.items()
            if subject != LINK
        ]
        if not any(r["isOwner"] for r in result):
            result.append(self._ace(creator, FULL, True))
        if self._root_type(folder_id) == COMMON:
            subjects = {r["sharedTo"]["id"] for r in result}
            if ADMINS not in subjects:
                result.append(self._ace(ADMINS, FULL, False))
            if EVERYONE not in subjects:
                result.append(self._ace(EVERYONE, READ, False))
        return result

    def _ace(self, subject: str, access: int, owner: bool) -> dict[str, Any]:
        if subject in self.users:
            target: dict[str, Any] = dict(self.users[subject])
        else:
            name = {EVERYONE: "Все", ADMINS: "Администраторы"}.get(
                subject, self.groups.get(subject, {}).get("name", "?")
            )
            target = {"id": subject, "name": name, "manager": None}
        return {
            "access": access,
            "sharedTo": target,
            "isLocked": False,
            "isOwner": owner,
        }

    def user_subjects(self, user_id: str) -> list[str]:
        """GetUserSubjects: пользователь, его группы, администраторы, все."""
        subjects = [user_id]
        subjects += [g for g, info in self.groups.items() if user_id in info["members"]]
        if self._is_admin(user_id):
            subjects.append(ADMINS)
        subjects.append(EVERYONE)
        return subjects

    def _is_admin(self, user_id: str) -> bool:
        user = self.users[user_id]
        return bool(user["isAdmin"] or "documents" in user["listAdminModules"])

    def can_read(self, user_id: str, file_id: int) -> bool:
        """FileSecurity.Filter для чтения файла."""
        f = self.files[file_id]
        common = self._root_type(f["folderId"]) == COMMON
        if common and self._is_admin(user_id):
            return True
        subjects = self.user_subjects(user_id)

        def rank(record: Record) -> int:
            return subjects.index(record.subject)

        mine = [
            (level, r) for level, r in self._graded(file_id) if r.subject in subjects
        ]
        own = sorted((r for level, r in mine if level == -1), key=rank)
        if own:
            access = own[0].access
        else:
            inherited = sorted(
                ((level, r) for level, r in mine if level >= 0),
                key=lambda item: (rank(item[1]), item[0]),
            )
            if inherited:
                access = inherited[0][1].access
            else:
                access = READ if common else RESTRICT
        return access != RESTRICT

    def readers_of(self, file_id: int) -> set[str]:
        """Почты тех, кто на самом деле может прочитать файл."""
        return {
            str(u["email"]).lower()
            for uid, u in self.users.items()
            if u["status"] == 1 and u["email"] and self.can_read(uid, file_id)
        }

    # --- обработка ----------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("host", "") != self.host:
            return httpx.Response(404, text="unknown host")
        path = request.url.path
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        if path == "/Products/Files/HttpHandlers/filehandler.ashx":
            return self._stream(request, query)
        if not path.startswith("/api/2.0/"):
            return httpx.Response(404, text="not found")
        route = path.removeprefix("/api/2.0/")
        self.calls.append((request.method, route, query))
        if self.server_errors > 0:
            self.server_errors -= 1
            return httpx.Response(503, text="unavailable")
        if self.rate_limit_hits > 0:
            self.rate_limit_hits -= 1
            headers = {"retry-after": self.retry_after} if self.retry_after else {}
            return httpx.Response(429, headers=headers, text="Too Many Requests")
        if route == "authentication" and request.method == "POST":
            return self._login(request)
        token = request.headers.get("authorization", "")
        self.auth_headers.append(token)
        user = self.tokens.get(token)
        if user is None or self.always_expired:
            return _error(401, "Access denied")
        if self.expire_after is not None:
            if self.expire_after <= 0:
                self.expire_after = None
                del self.tokens[token]
                return _error(401, "Access denied")
            self.expire_after -= 1
        return self._route(route, query, user)

    def _login(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        login = payload.get("userName")
        known = self.passwords.get(login)
        if known is None or known[0] != payload.get("password"):
            return _error(401, "User authentication failed")
        if login in self.tfa_logins:
            return _ok({"sms": False, "tfa": True, "tfaKey": "x", "expires": None})
        self._issued += 1
        token = f"token-{self._issued}"
        self.tokens[token] = known[1]
        return _ok({"token": token, "expires": "2027-01-01T00:00:00.0000000Z"})

    def _route(self, route: str, query: dict[str, str], user: str) -> httpx.Response:
        if route == "people/@self":
            return _ok(self.users[user])
        if route == "people":
            start = int(query.get("startIndex", 0))
            count = int(query.get("count", 25))
            people = list(self.users.values())
            return _ok(people[start : start + count])
        if route.startswith("group/"):
            group = self.groups.get(route.removeprefix("group/"))
            if group is None:
                return _error(404, "Group not found")
            members = [
                {"id": m, "displayName": self.users[m]["displayName"]}
                for m in sorted(group["members"])
                if m in self.users
            ]
            return _ok({"id": group["id"], "name": group["name"], "members": members})
        parts = route.split("/")
        if len(parts) == 4 and parts[:2] == ["files", "file"] and parts[3] == "share":
            return self._share(int(parts[2]), user)
        if len(parts) == 4 and parts[:2] == ["files", "folder"] and parts[3] == "share":
            folder_id = _int(parts[2])
            if folder_id is None or folder_id not in self.folders:
                return _error(404, "Folder not found")
            if folder_id in self.folder_share_forbidden or not self._is_admin(user):
                return _error(403, "Access denied")
            return _ok(self.folder_shared_info(folder_id))
        if (
            len(parts) == 4
            and parts[:2] == ["files", "file"]
            and parts[3] == "presigneduri"
        ):
            file_id = int(parts[2])
            if file_id not in self.files:
                return _error(404, "File not found")
            return _ok(
                f"{self.presigned_scheme}://{self.host}/Products/Files/HttpHandlers/"
                f"filehandler.ashx?action=stream&fileid={file_id}"
                f"&stream_auth={_SIGNATURE}"
            )
        if len(parts) == 2 and parts[0] == "files":
            folder = COMMON_ID if parts[1] == "@common" else _int(parts[1])
            if folder is None or folder not in self.folders:
                return _error(404, "Folder not found")
            return _ok(self._content(folder, query))
        return _error(404, "Not found")

    def _share(self, file_id: int, user: str) -> httpx.Response:
        if file_id not in self.files:
            return _error(404, "File not found")
        if file_id in self.share_forbidden or not self._is_admin(user):
            # CanSetAccess: в «Общих» — только администраторам.
            return _error(403, "Access denied")
        return _ok(self.shared_info(file_id))

    def _content(self, folder: int, query: dict[str, str]) -> dict[str, Any]:
        sub = [f for f in self.folders.values() if f["parentId"] == folder]
        files = [f for f in self.files.values() if f["folderId"] == folder]
        items: list[tuple[str, dict[str, Any]]] = [("folder", f) for f in sub]
        items += [("file", f) for f in files]
        start = int(query.get("startIndex", 0))
        count = int(query.get("count", 25))
        page = items[start : start + count]
        root_type = self._root_type(folder)
        return {
            "files": [self._file_wrapper(f, root_type) for k, f in page if k == "file"],
            "folders": [
                self._folder_wrapper(f, root_type) for k, f in page if k == "folder"
            ],
            "current": self._folder_wrapper(self.folders[folder], root_type),
            "pathParts": [folder],
            "startIndex": start,
            "count": len(page),
            "total": len(items),
        }

    def _owner(self, user_id: str) -> dict[str, Any]:
        return {
            "id": user_id,
            "displayName": "Сотрудник",
            "title": "",
            "avatarSmall": "",
        }

    def _folder_wrapper(self, f: dict[str, Any], root_type: int) -> dict[str, Any]:
        wrapper: dict[str, Any] = {
            "parentId": f["parentId"] or 0,
            "filesCount": 0,
            "foldersCount": 0,
            "id": f["id"] if not f["providerItem"] else f"sbox-{f['id']}",
            "title": f["title"],
            "access": 0,
            "shared": False,
            "rootFolderType": root_type,
            "updatedBy": self._owner(ADMIN_ID),
            "created": "2026-01-01T10:00:00.0000000Z",
            "createdBy": self._owner(ADMIN_ID),
            "updated": "2026-01-01T10:00:00.0000000Z",
        }
        if f["providerItem"]:
            wrapper["providerItem"] = True
            wrapper["providerKey"] = "DropboxV2"
        return wrapper

    def _file_wrapper(self, f: dict[str, Any], root_type: int) -> dict[str, Any]:
        own = any(r.entry == "file" and r.entry_id == f["id"] for r in self.records)
        wrapper: dict[str, Any] = {
            "folderId": f["folderId"],
            "version": f["version"],
            "versionGroup": f["versionGroup"],
            "contentLength": f"{f['pureContentLength']} B",
            "pureContentLength": f["pureContentLength"],
            "fileStatus": 0,
            "viewUrl": f"{self.base}Products/Files/HttpHandlers/filehandler.ashx"
            f"?action=download&fileid={f['id']}",
            "webUrl": f"{self.base}Products/Files/DocEditor.aspx?fileid={f['id']}",
            "fileType": 7,
            "fileExst": "." + f["title"].rsplit(".", 1)[-1],
            "comment": None,
            "id": f["id"] if not f["providerItem"] else f"sbox-{f['id']}",
            "title": f["title"],
            "access": 0,
            "shared": own,
            "rootFolderType": root_type,
            "updatedBy": self._owner(f["creator"]),
            "created": f["created"],
            "createdBy": self._owner(f["creator"]),
            "updated": f["updated"],
        }
        if f["providerItem"]:
            wrapper["providerItem"] = True
        return wrapper

    def _stream(self, request: httpx.Request, query: dict[str, str]) -> httpx.Response:
        self.download_auth.append(request.headers.get("authorization", ""))
        if query.get("stream_auth") != _SIGNATURE:
            return httpx.Response(403)
        if self.download_html:
            return httpx.Response(
                200, headers={"content-type": "text/html"}, text="<html>login</html>"
            )
        data = self.blobs.get(_int(query.get("fileid", "")) or -1)
        if data is None:
            return httpx.Response(404)
        return httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, content=data
        )


def _int(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def _ok(payload: Any) -> httpx.Response:
    count = len(payload) if isinstance(payload, list) else 1
    return httpx.Response(
        200,
        json={"count": count, "response": payload, "status": 0, "statusCode": 200},
    )


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"error": {"message": message}, "status": 1, "statusCode": status},
    )


def sample_r7() -> FakeR7:
    """Портал: администратор, Анна и Вера в «Финансах», Борис, Глеб
    заблокирован, гость без почты. «Общие документы»: открытая папка,
    закрытая «Финансам» (с личным запретом Вере), файл со своими записями,
    лишний формат, слишком большой, стороннее хранилище, ссылка."""
    server = FakeR7()
    server.add_user(
        ADMIN_ID,
        "admin@company.ru",
        login=ADMIN_LOGIN,
        password=ADMIN_PASSWORD,
        admin=True,
    )
    server.add_user(ANNA_ID, "Anna@Company.ru", login="anna@company.ru")
    server.add_user(BORIS_ID, "boris@company.ru", login="boris@company.ru")
    server.add_user(VERA_ID, "vera@company.ru")
    server.add_user(GLEB_ID, "gleb@company.ru", status=2)
    server.add_user("e1000000-0000-0000-0000-0000000000e1", None, visitor=True)
    server.add_group(FINANCE, "Финансы", {ANNA_ID, VERA_ID})

    server.add_folder(COMMON_ID, "Общие документы", None)
    server.add_folder(10, "Кадры", COMMON_ID)
    server.add_file(100, "Отпуск.txt", 10, "Отпуск — 28 дней.".encode())
    server.add_file(101, "Схема.png", 10, b"\x89PNG")
    server.add_file(102, "Большой.pdf", 10, b"%PDF-", size=200 * 1024 * 1024)

    server.add_folder(20, "Финансы", COMMON_ID)
    server.share("folder", 20, EVERYONE, RESTRICT)
    server.share("folder", 20, FINANCE, READ)
    server.share("folder", 20, VERA_ID, RESTRICT)
    server.add_folder(21, "Бюджет", 20)
    server.add_file(200, "Бюджет.txt", 21, "Смета на год.".encode())
    # Своя запись файла: Борису можно, «Все» — по наследству запрещено.
    server.add_file(201, "Премии.txt", 20, "Премии по итогам года.".encode())
    server.share("file", 201, BORIS_ID, READ)
    # Доступ по ссылке ничего не открывает людям портала.
    server.add_file(202, "Отчёт.md", 20, "# Отчёт".encode())
    server.share("file", 202, LINK, READ)

    server.add_folder(30, "Dropbox", COMMON_ID, provider=True)
    server.add_file(300, "Внешний.txt", 30, b"external", provider=True)
    server.add_file(
        103, "Памятка.md", COMMON_ID, "# Памятка\n\nКорневой файл.".encode()
    )
    return server
