"""Поддельный портал Битрикс24 для контрактных тестов адаптера.

Отвечает на REST (вебхук и OAuth-токен), сервер авторизации
oauth.bitrix24.tech, скачивание по подписанной ссылке. Состояние —
хранилища, папки, файлы, права «чтение» по пользователям, базы знаний.
Умеет ломаться по команде: лимит запросов, редирект, истёкший токен,
чужой хост в ссылке, канонические ответы из фикстур документации.

Запросы приходят через OutboundClient: адрес закреплён IP, имя портала
— в заголовке Host, по нему и маршрутизируем.
"""

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx

from corp_ed.core.outbound import OutboundClient
from tests.fake_connector import public_resolver

DEFAULT_HOST = "portal.example.com"
OAUTH_HOST = "oauth.bitrix24.tech"
OAUTH_SERVER = f"https://{OAUTH_HOST}/"
CLIENT_ID = "local.6ab6b8f08a3069.42767144"
CLIENT_SECRET = "app-secret-value"  # noqa: S105 — поддельный портал
EMPLOYEE_ID = "7"
ADMIN_ID = "1"
WEBHOOK_CODE = "wh-code"  # noqa: S105
ACCESS_TOKEN = "tok-7"  # noqa: S105
REFRESH_TOKEN = "ref-7"  # noqa: S105
AUTH_CODE = "code-7"
UPDATED = "2026-01-14T17:05:39+03:00"


@dataclass
class FakePortal:
    host: str = DEFAULT_HOST
    page_size: int = 50
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    inactive_users: set[str] = field(default_factory=set)
    access_tokens: dict[str, str] = field(default_factory=dict)
    expired: set[str] = field(default_factory=set)
    expire_all: bool = False
    refresh_tokens: dict[str, str] = field(default_factory=dict)
    codes: dict[str, str] = field(default_factory=dict)
    oauth_error: str | None = None
    storages: list[dict[str, Any]] = field(default_factory=list)
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    children: dict[str, list[str]] = field(default_factory=dict)
    files: dict[str, bytes] = field(default_factory=dict)
    hidden: dict[str, set[str]] = field(default_factory=dict)
    denied: set[str] = field(default_factory=set)
    """Папки, которые видны в листинге, но getchildren по ним — ACCESS_DENIED
    (права сняли посреди обхода)."""
    sites: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    pages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    blocks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    group_kb_supported: bool = False
    rate_limit_hits: int = 0
    redirect_to: str | None = None
    download_host: str | None = None
    download_content_type: str = "application/octet-stream"
    canned: dict[str, Any] = field(default_factory=dict)
    """method → JSON (или (status, JSON)): ответ как есть, без состояния."""
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    auth_log: list[tuple[str, str]] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    issued: int = 0

    def __post_init__(self) -> None:
        self.users.setdefault(
            ADMIN_ID,
            {"ID": ADMIN_ID, "ADMIN": True, "NAME": "Админ", "LAST_NAME": "А."},
        )
        self.users.setdefault(
            EMPLOYEE_ID,
            {"ID": EMPLOYEE_ID, "ADMIN": False, "NAME": "Иван", "LAST_NAME": "Петров"},
        )
        self.access_tokens.setdefault(ACCESS_TOKEN, EMPLOYEE_ID)
        self.refresh_tokens.setdefault(REFRESH_TOKEN, EMPLOYEE_ID)
        self.codes.setdefault(AUTH_CODE, EMPLOYEE_ID)
        self.sites.setdefault("KNOWLEDGE", [])
        self.sites.setdefault("GROUP", [])

    # --- адреса -----------------------------------------------------------------

    @property
    def portal(self) -> str:
        return f"https://{self.host}/"

    @property
    def webhook(self) -> str:
        return f"{self.portal}rest/{ADMIN_ID}/{WEBHOOK_CODE}/"

    def client(self) -> OutboundClient:
        return OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
            resolver=public_resolver,
        )

    # --- наполнение -------------------------------------------------------------

    def add_storage(
        self, storage_id: str, entity_type: str, entity_id: str, name: str
    ) -> str:
        root = f"root-{storage_id}"
        self.storages.append(
            {
                "ID": storage_id,
                "NAME": name,
                "CODE": None,
                "MODULE_ID": "disk",
                "ENTITY_TYPE": entity_type,
                "ENTITY_ID": entity_id,
                "ROOT_OBJECT_ID": root,
            }
        )
        self.children.setdefault(root, [])
        return root

    def add_folder(self, folder_id: str, parent: str, name: str) -> str:
        self.objects[folder_id] = {
            "ID": folder_id,
            "NAME": name,
            "TYPE": "folder",
            "PARENT_ID": parent,
            "DELETED_TYPE": "0",
            "UPDATE_TIME": UPDATED,
            "DETAIL_URL": f"{self.portal}disk/path/{name}",
        }
        self.children.setdefault(parent, []).append(folder_id)
        self.children.setdefault(folder_id, [])
        return folder_id

    def add_file(
        self,
        file_id: str,
        parent: str,
        name: str,
        data: bytes,
        *,
        updated: str = UPDATED,
        version: str = "1",
        deleted: str = "0",
        size: int | None = None,
    ) -> str:
        self.objects[file_id] = {
            "ID": file_id,
            "NAME": name,
            "TYPE": "file",
            "PARENT_ID": parent,
            "DELETED_TYPE": deleted,
            "GLOBAL_CONTENT_VERSION": version,
            "SIZE": str(len(data) if size is None else size),
            "UPDATE_TIME": updated,
            "DETAIL_URL": f"{self.portal}disk/file/{name}",
        }
        self.files[file_id] = data
        self.children.setdefault(parent, []).append(file_id)
        return file_id

    def add_site(self, scope: str, site_id: str, title: str, code: str) -> str:
        self.sites.setdefault(scope, []).append(
            {"ID": site_id, "TITLE": title, "CODE": code, "TYPE": scope, "ACTIVE": "Y"}
        )
        self.pages.setdefault(site_id, [])
        return site_id

    def add_page(
        self,
        site_id: str,
        page_id: str,
        title: str,
        code: str,
        *,
        url: str | None = None,
        modified: str = "10/10/2022 03:25:30 pm",
        published: str | None = "10/11/2022 09:00:00 am",
    ) -> str:
        self.pages.setdefault(site_id, []).append(
            {
                "ID": page_id,
                "TITLE": title,
                "CODE": code,
                "SITE_ID": site_id,
                "FOLDER": "N",
                "DATE_MODIFY": modified,
                "DATE_PUBLIC": published,
                "DOMAIN_ID": "5",
                "PUBLIC_URL": url if url is not None else "",
            }
        )
        self.blocks.setdefault(page_id, [])
        return page_id

    def add_block(
        self, page_id: str, block_id: int, content: str, *, active: bool = True
    ) -> None:
        self.blocks.setdefault(page_id, []).append(
            {
                "id": block_id,
                "lid": int(page_id),
                "code": "01.text",
                "name": "Текст",
                "active": active,
                "meta": {},
                "content": content,
                "css": [],
                "js": [],
            }
        )

    def hide(self, user_id: str, *object_ids: str) -> None:
        self.hidden.setdefault(user_id, set()).update(object_ids)

    # --- обработка ------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "")
        path = request.url.path
        if host == OAUTH_HOST:
            return self._oauth(request)
        if host not in {self.host, self.download_host}:
            return httpx.Response(404, text="unknown host")
        if path == "/rest/download.json":
            return self._download(request)
        if self.redirect_to and path.startswith("/rest/"):
            return httpx.Response(301, headers={"location": self.redirect_to})
        if path.startswith("/rest/"):
            return self._rest(request)
        return httpx.Response(404, text="not found")

    def _rest(self, request: httpx.Request) -> httpx.Response:
        segments = request.url.path.removeprefix("/rest/").split("/")
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            body = {}
        if len(segments) == 3:
            user_id, code, method = segments
            method = method.removesuffix(".json").lower()
            self.calls.append((method, body))
            self.auth_log.append(("webhook", user_id))
            if user_id != ADMIN_ID or code != WEBHOOK_CODE:
                return _error(401, "INVALID_CREDENTIALS")
        elif len(segments) == 1:
            method = segments[0].removesuffix(".json").lower()
            token = body.pop("auth", None)
            self.calls.append((method, body))
            if token is None:
                return _error(401, "NO_AUTH_FOUND")
            self.auth_log.append(("oauth", str(token)))
            if self.expire_all or token in self.expired:
                return _error(401, "expired_token")
            if token not in self.access_tokens:
                return _error(401, "invalid_token")
            user_id = self.access_tokens[token]
        else:
            return _error(404, "ERROR_METHOD_NOT_FOUND")
        if self.rate_limit_hits > 0:
            self.rate_limit_hits -= 1
            return _error(503, "QUERY_LIMIT_EXCEEDED")
        if method in self.canned:
            canned = self.canned[method]
            if isinstance(canned, tuple):
                status, payload = canned
                return (
                    httpx.Response(status, json=payload)
                    if not isinstance(payload, str)
                    else httpx.Response(status, text=payload)
                )
            return httpx.Response(200, json=canned)
        handler = getattr(self, "_m_" + method.replace(".", "_"), None)
        if handler is None:
            return _error(404, "ERROR_METHOD_NOT_FOUND")
        result = handler(user_id, body)
        return result if isinstance(result, httpx.Response) else _ok(result)

    # --- методы --------------------------------------------------------------------

    def _m_profile(self, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if user_id in self.inactive_users:
            return {}
        return self.users[user_id]

    def _m_disk_storage_getlist(
        self, user_id: str, body: dict[str, Any]
    ) -> httpx.Response:
        return self._page(self.storages, body)

    def _m_disk_storage_getchildren(
        self, user_id: str, body: dict[str, Any]
    ) -> httpx.Response:
        storage = next(
            (s for s in self.storages if s["ID"] == str(body.get("id"))), None
        )
        if storage is None:
            return _error(400, "ERROR_NOT_FOUND")
        return self._page(self._children(user_id, storage["ROOT_OBJECT_ID"]), body)

    def _m_disk_folder_getchildren(
        self, user_id: str, body: dict[str, Any]
    ) -> httpx.Response:
        folder_id = str(body.get("id"))
        if folder_id not in self.objects:
            return _error(400, "ERROR_NOT_FOUND")
        if folder_id in self.hidden.get(user_id, set()) or folder_id in self.denied:
            return _error(400, "ACCESS_DENIED")
        return self._page(self._children(user_id, folder_id), body)

    def _m_disk_file_get(self, user_id: str, body: dict[str, Any]) -> Any:
        file_id = str(body.get("id"))
        entry = self.objects.get(file_id)
        if entry is None or entry["TYPE"] != "file":
            return _error(400, "ERROR_NOT_FOUND")
        if file_id in self.hidden.get(user_id, set()):
            return _error(400, "ACCESS_DENIED")
        host = self.download_host or self.host
        return {
            **entry,
            "DOWNLOAD_URL": (
                f"https://{host}/rest/download.json?auth=signed-{user_id}"
                f"&token=disk%7C{file_id}"
            ),
        }

    def _m_landing_site_getlist(self, user_id: str, body: dict[str, Any]) -> Any:
        scope = str(body.get("scope") or "PAGE")
        if scope == "GROUP" and not self.group_kb_supported:
            return _error(400, "ACCESS_DENIED")
        return list(self.sites.get(scope, []))

    def _m_landing_landing_getlist(self, user_id: str, body: dict[str, Any]) -> Any:
        params = body.get("params") or {}
        site_id = str((params.get("filter") or {}).get("SITE_ID"))
        pages = [p for p in self.pages.get(site_id, []) if p["FOLDER"] == "N"]
        offset = int(params.get("offset") or 0)
        limit = int(params.get("limit") or len(pages))
        return pages[offset : offset + limit]

    def _m_landing_block_getlist(self, user_id: str, body: dict[str, Any]) -> Any:
        page_id = str(body.get("lid"))
        if page_id not in self.blocks:
            return _error(400, "ERROR_NOT_FOUND")
        return list(self.blocks[page_id])

    # --- скачивание и OAuth -----------------------------------------------------------

    def _download(self, request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        token = (query.get("token") or [""])[0]
        self.downloads.append(request.url.path + "?" + request.url.query.decode())
        if request.headers.get("user-agent", "") == "" or not request.headers.get(
            "referer"
        ):
            return httpx.Response(404, text="nginx")
        file_id = token.removeprefix("disk|")
        if file_id not in self.files:
            return httpx.Response(404, text="nginx")
        return httpx.Response(
            200,
            content=self.files[file_id],
            headers={"content-type": self.download_content_type},
        )

    def _oauth(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/oauth/token/":
            return httpx.Response(404)
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        self.calls.append(("oauth/token", {k: v for k, v in query.items()}))
        if self.oauth_error:
            return httpx.Response(400, json={"error": self.oauth_error})
        if (
            query.get("client_id") != CLIENT_ID
            or query.get("client_secret") != CLIENT_SECRET
        ):
            return httpx.Response(400, json={"error": "invalid_client"})
        grant = query.get("grant_type")
        if grant == "authorization_code":
            user_id = self.codes.pop(query.get("code", ""), None)
        elif grant == "refresh_token":
            user_id = self.refresh_tokens.pop(query.get("refresh_token", ""), None)
        else:
            return httpx.Response(400, json={"error": "invalid_request"})
        if user_id is None:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.issued += 1
        access, refresh = f"tok-{user_id}-{self.issued}", f"ref-{user_id}-{self.issued}"
        self.access_tokens[access] = user_id
        self.refresh_tokens[refresh] = user_id
        payload: dict[str, Any] = {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": 3600,
            "expires": 1_800_000_000 + self.issued,
            "client_endpoint": f"{self.portal}rest/",
            "server_endpoint": f"{OAUTH_SERVER}rest/",
            "domain": OAUTH_HOST,
            "member_id": "member-1",
            "scope": "disk,landing",
            "status": "F",
        }
        if grant == "refresh_token":
            payload["user_id"] = int(user_id)
        return httpx.Response(200, json=payload)

    # --- вспомогательное ------------------------------------------------------

    def _children(self, user_id: str, parent: str) -> list[dict[str, Any]]:
        hidden = self.hidden.get(user_id, set())
        return [
            self.objects[child]
            for child in self.children.get(parent, [])
            if child not in hidden
        ]

    def _page(
        self, items: list[dict[str, Any]], body: dict[str, Any]
    ) -> httpx.Response:
        start = int(body.get("start") or 0)
        chunk = items[start : start + self.page_size]
        payload: dict[str, Any] = {"result": chunk, "total": len(items), "time": {}}
        if start + self.page_size < len(items):
            payload["next"] = start + self.page_size
        return httpx.Response(200, json=payload)


def _ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"result": result, "time": {}})


def _error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": code, "error_description": code})


def sample_portal(host: str = DEFAULT_HOST) -> FakePortal:
    """Портал с общим диском, диском группы, личными дисками и базой знаний."""
    portal = FakePortal(host=host)
    common = portal.add_storage("10", "common", "shared_files_s1", "Общий диск")
    rules = portal.add_folder("101", common, "Регламенты")
    portal.add_file("102", rules, "Отпуск.txt", "Отпуск — 28 дней.".encode())
    portal.add_file("103", rules, "Схема.png", b"\x89PNG")
    portal.add_file("104", rules, "Большой.pdf", b"%PDF-1.4", size=100 * 1024 * 1024)
    portal.add_file("105", common, "Корзина.txt", b"old", deleted="3")
    secret = portal.add_folder("106", common, "Секретная")
    portal.add_file("107", secret, "Тайна.txt", b"secret")
    portal.hide(EMPLOYEE_ID, secret)
    group = portal.add_storage("20", "group", "3", "Проект X")
    portal.add_file("201", group, "План.md", "# План\n\nСрок — май.".encode())
    mine = portal.add_storage("30", "user", EMPLOYEE_ID, "Мой диск")
    portal.add_file("301", mine, "Личное.txt", "Личные заметки.".encode())
    other = portal.add_storage("31", "user", "8", "Диск коллеги")
    portal.add_file("311", other, "Чужое.txt", b"not yours")
    site = portal.add_site("KNOWLEDGE", "157", "База знаний", "/company/")
    portal.add_page(
        site,
        "985",
        "Отпуск",
        "vacation",
        url=f"{portal.portal}knowledge/company/vacation/",
    )
    portal.add_block(
        "985",
        1,
        '<div class="block-wrapper"><nav><a href="/knowledge/">Меню</a></nav></div>',
    )
    portal.add_block(
        "985",
        2,
        "<section><h2>Отпуск</h2><p>Отпуск — 28 календарных дней.</p>"
        "<script>track()</script></section>",
    )
    portal.add_block("985", 3, "<p>Черновик про больничный</p>", active=False)
    portal.add_page(site, "573", "Командировки", "trips", published=None)
    portal.add_block("573", 4, "<p>Суточные — 700 рублей.</p>")
    portal.add_page(site, "600", "Пустая", "empty")
    return portal
