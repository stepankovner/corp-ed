"""Поддельные Яндекс ID (OAuth) и Яндекс Диск для контрактных тестов.

Формы ответов — по устоявшимся REST Диска и OAuth Яндекс ID, без
сверки с документацией (недоступна из среды). Три хоста: oauth.yandex.ru
(токены), cloud-api.yandex.net (REST Диска), downloader.disk.yandex.ru
(подписанные ссылки на файлы).
"""

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx

from corp_ed.core.outbound import OutboundClient
from tests.fake_connector import public_resolver

OAUTH_HOST = "oauth.yandex.ru"
API_HOST = "cloud-api.yandex.net"
DOWNLOAD_HOST = "downloader.disk.yandex.ru"
OAUTH_SERVER = f"https://{OAUTH_HOST}/"
DISK_API = f"https://{API_HOST}/"
CLIENT_ID = "yandex-app-id"
CLIENT_SECRET = "yandex-app-secret"  # noqa: S105 — поддельный сервер
ACCESS_TOKEN = "y0-token-7"  # noqa: S105
REFRESH_TOKEN = "y0-refresh-7"  # noqa: S105
AUTH_CODE = "ycode-7"
LOGIN = "ivan.petrov"


@dataclass
class FakeYandex:
    access_tokens: dict[str, str] = field(default_factory=dict)
    expired: set[str] = field(default_factory=set)
    refresh_tokens: dict[str, str] = field(default_factory=dict)
    codes: dict[str, str] = field(default_factory=dict)
    rotate_refresh: bool = False
    oauth_error: str | None = None
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    """path → ресурс (type dir|file, …); дети — по префиксу пути."""
    files: dict[str, bytes] = field(default_factory=dict)
    forbidden: set[str] = field(default_factory=set)
    rate_limit_hits: int = 0
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    issued: int = 0

    def __post_init__(self) -> None:
        self.access_tokens.setdefault(ACCESS_TOKEN, LOGIN)
        self.refresh_tokens.setdefault(REFRESH_TOKEN, LOGIN)
        self.codes.setdefault(AUTH_CODE, LOGIN)
        self.entries.setdefault(
            "disk:/",
            {"type": "dir", "name": "disk", "path": "disk:/", "resource_id": "root"},
        )

    def client(self) -> OutboundClient:
        return OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
            resolver=public_resolver,
        )

    # --- наполнение ---------------------------------------------------------

    def add_dir(self, path: str) -> str:
        self.entries[path] = {
            "type": "dir",
            "name": path.rstrip("/").rsplit("/", 1)[-1],
            "path": path,
            "resource_id": f"rid-{path}",
            "modified": "2026-05-10T12:00:00+00:00",
        }
        return path

    def add_file(
        self,
        path: str,
        data: bytes,
        *,
        modified: str = "2026-05-11T09:30:00+00:00",
        md5: str | None = None,
        size: int | None = None,
        public_url: str | None = None,
    ) -> str:
        name = path.rsplit("/", 1)[-1]
        entry: dict[str, Any] = {
            "type": "file",
            "name": name,
            "path": path,
            "resource_id": f"rid-{path}",
            "modified": modified,
            "size": len(data) if size is None else size,
            "md5": md5 or f"md5-{name}",
            "mime_type": "application/octet-stream",
        }
        if public_url:
            entry["public_url"] = public_url
        self.entries[path] = entry
        self.files[path] = data
        return path

    # --- обработка -----------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "")
        if host == OAUTH_HOST:
            return self._oauth(request)
        if host == DOWNLOAD_HOST:
            return self._download(request)
        if host != API_HOST:
            return httpx.Response(404, text="unknown host")
        return self._api(request)

    def _oauth(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/token" or request.method != "POST":
            return httpx.Response(404)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append(("oauth/token", form))
        if self.oauth_error:
            return httpx.Response(400, json={"error": self.oauth_error})
        if (
            form.get("client_id") != CLIENT_ID
            or form.get("client_secret") != CLIENT_SECRET
        ):
            return httpx.Response(400, json={"error": "invalid_client"})
        grant = form.get("grant_type")
        if grant == "authorization_code":
            login = self.codes.pop(form.get("code", ""), None)
        elif grant == "refresh_token":
            login = self.refresh_tokens.get(form.get("refresh_token", ""))
            if login is not None and self.rotate_refresh:
                self.refresh_tokens.pop(form["refresh_token"])
        else:
            return httpx.Response(400, json={"error": "invalid_request"})
        if login is None:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.issued += 1
        access = f"y0-token-{login}-{self.issued}"
        self.access_tokens[access] = login
        payload: dict[str, Any] = {
            "access_token": access,
            "token_type": "bearer",
            "expires_in": 31536000,
        }
        if grant == "authorization_code" or self.rotate_refresh:
            refresh = f"y0-refresh-{login}-{self.issued}"
            self.refresh_tokens[refresh] = login
            payload["refresh_token"] = refresh
        return httpx.Response(200, json=payload)

    def _api(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        self.calls.append((path, query))
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("OAuth ") if auth.startswith("OAuth ") else ""
        if not token or token in self.expired or token not in self.access_tokens:
            return httpx.Response(
                401, json={"message": "Не авторизован.", "error": "UnauthorizedError"}
            )
        if self.rate_limit_hits > 0:
            self.rate_limit_hits -= 1
            return httpx.Response(429, json={"error": "TooManyRequestsError"})
        login = self.access_tokens[token]
        if path == "/v1/disk/":
            return httpx.Response(
                200,
                json={
                    "total_space": 10**10,
                    "used_space": 10**8,
                    "user": {"login": login, "uid": "12345", "display_name": "Иван"},
                },
            )
        if path == "/v1/disk/resources":
            return self._resources(query)
        if path == "/v1/disk/resources/download":
            entry = self.entries.get(query.get("path", ""))
            if entry is None or entry["type"] != "file":
                return httpx.Response(404, json={"error": "DiskNotFoundError"})
            return httpx.Response(
                200,
                json={
                    "href": f"https://{DOWNLOAD_HOST}/disk/{entry['resource_id']}?token=signed",
                    "method": "GET",
                    "templated": False,
                },
            )
        return httpx.Response(404, json={"error": "DiskNotFoundError"})

    def _resources(self, query: dict[str, str]) -> httpx.Response:
        path = query.get("path", "")
        entry = self.entries.get(path)
        if entry is None:
            return httpx.Response(404, json={"error": "DiskNotFoundError"})
        if path in self.forbidden:
            return httpx.Response(403, json={"error": "DiskForbiddenError"})
        if entry["type"] == "file":
            return httpx.Response(200, json=entry)
        prefix = path if path.endswith("/") else path + "/"
        children = sorted(
            (
                e
                for p, e in self.entries.items()
                if p != path
                and p.startswith(prefix)
                and "/" not in p[len(prefix) :].rstrip("/")
            ),
            key=lambda e: e["name"],
        )
        limit = int(query.get("limit") or 20)
        offset = int(query.get("offset") or 0)
        return httpx.Response(
            200,
            json={
                **entry,
                "_embedded": {
                    "items": children[offset : offset + limit],
                    "total": len(children),
                    "limit": limit,
                    "offset": offset,
                    "path": path,
                },
            },
        )

    def _download(self, request: httpx.Request) -> httpx.Response:
        self.downloads.append(request.url.path)
        resource_id = request.url.path.removeprefix("/disk/")
        for path, entry in self.entries.items():
            if entry.get("resource_id") == resource_id and entry["type"] == "file":
                return httpx.Response(200, content=self.files[path])
        return httpx.Response(404, text="gone")


def sample_yandex() -> FakeYandex:
    server = FakeYandex()
    server.add_dir("disk:/Регламенты")
    server.add_file("disk:/Регламенты/Отпуск.txt", "Отпуск — 28 дней.".encode())
    server.add_file("disk:/Регламенты/Схема.png", b"\x89PNG")
    server.add_file("disk:/Регламенты/Большой.pdf", b"%PDF-", size=100 * 1024 * 1024)
    server.add_dir("disk:/Регламенты/Архив")
    server.add_file(
        "disk:/Регламенты/Архив/Старый.md",
        "# Старый\n\nТекст.".encode(),
        public_url="https://disk.yandex.ru/i/abc123",
    )
    server.add_dir("disk:/Общая папка")
    server.add_file("disk:/Общая папка/План.md", "# План\n\nСрок — май.".encode())
    server.add_dir("disk:/Закрытая")
    server.add_file("disk:/Закрытая/Тайна.txt", b"secret")
    server.forbidden.add("disk:/Закрытая")
    server.add_file("disk:/Заметка.txt", "Заметка в корне.".encode())
    return server
