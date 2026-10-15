"""Поддельная ELMA365 для контрактных тестов адаптера.

Формы ответов — по публичному Web API (15.10):
- введение и авторизация: https://api.elma365.com/ru/public-api/guides/IntroWebAPI/,
  https://api.elma365.com/ru/public-api/guides/WebAPIAuthorization/ —
  `https://{company}.elma365.ru/pub/v1`, `Authorization: Bearer <токен>`;
- спецификация OpenAPI: https://api.elma365.com/ru/api-v1-doc.json;
- папки: https://api.elma365.com/ru/public-api/reference/disk/directorylist/
  (`{success, result: {result: [...], total}}`, `from`/`size`, `active`),
  https://api.elma365.com/ru/public-api/reference/disk/getdirectory/
  (`{success, directory}`, 403 — «Недостаточно прав для доступа к
  директории»);
- файлы: https://api.elma365.com/ru/public-api/reference/disk/searchfiles/
  (`{success, files: [FileResponse]}` — без размера и папки),
  https://api.elma365.com/ru/public-api/reference/disk/getfile/
  (`{success, file}`, 403 — «Недостаточно прав для доступа к файлу»),
  https://api.elma365.com/ru/public-api/reference/disk/handlergetlinkget/
  (`{success, Link}` — с заглавной);
- токены: https://elma365.com/ru/help/platform/token.html (токен —
  от имени выбранного пользователя);
- лимиты облака: https://elma365.com/ru/help/saas-standard-limits.html
  (429 с Retry-After).

Список папок и поиск файлов здесь отдают всё без фильтра по правам —
худший случай: документация об этом молчит, и адаптер не должен
считать их списком видимого. Права проверяются только в запросах
одной папки, одного файла и ссылки на скачивание.
"""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from corp_ed.core.outbound import OutboundClient
from tests.fake_connector import public_resolver

HOST = "company.elma365.ru"
STORAGE_HOST = "s3.elma365-storage.example.com"
BASE = f"https://{HOST}/"
ANNA_TOKEN = "4bece94a-d372-404f-a4be-fb159019d9e0"  # noqa: S105 — поддельный сервер
BORIS_TOKEN = "5c018be7-6590-4d3a-9864-bc7f96cfe682"  # noqa: S105
ANNA = "a35cd8de-0c73-4f6a-8218-90193d02e2e0"
BORIS = "b35cd8de-0c73-4f6a-8218-90193d02e2e1"
ROOT = "00000000-0000-0000-0000-000000000000"
SIGNATURE = "X-Amz-Signature=sig"


def uid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


@dataclass
class FakeElma365:
    host: str = HOST
    tokens: dict[str, str] = field(
        default_factory=lambda: {ANNA_TOKEN: ANNA, BORIS_TOKEN: BORIS}
    )
    directories: dict[str, dict[str, Any]] = field(default_factory=dict)
    dir_readers: dict[str, set[str] | None] = field(default_factory=dict)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    file_readers: dict[str, set[str] | None] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    max_size: int = 10_000
    rate_limit_hits: int = 0
    retry_after: str | None = "1"
    server_errors: int = 0
    failure: bool = False
    """Ответ 200 с success: false на всё."""
    disk_forbidden: set[str] = field(default_factory=set)
    """Пользователи, которым закрыт весь раздел (403 на списки)."""
    search_forbidden: set[str] = field(default_factory=set)
    """Папки, поиск файлов в которых отвечает 403 (а запрос папки — 200)."""
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    storage_auth: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.directories:
            self.directories[ROOT] = {
                "__id": ROOT,
                "__name": "root",
                "system": True,
                "parentsList": [],
            }
            self.dir_readers[ROOT] = None

    @property
    def base(self) -> str:
        return f"https://{self.host}/"

    def client(self) -> OutboundClient:
        return OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
            resolver=public_resolver,
        )

    # --- наполнение -------------------------------------------------------------

    def add_directory(
        self,
        key: str,
        name: str,
        *,
        parent: str = ROOT,
        readers: set[str] | None = None,
        target: str | None = None,
    ) -> str:
        parents = [parent, *self.directories[parent].get("parentsList", [])]
        item: dict[str, Any] = {
            "__id": key,
            "__name": name,
            "__createdAt": "2026-01-01T10:00:00Z",
            "__updatedAt": "2026-01-01T10:00:00Z",
            "directory": parent,
            "parentsList": parents,
            "system": False,
        }
        if target is not None:
            item.update(target=target, type="directory")
        self.directories[key] = item
        self.dir_readers[key] = readers
        return key

    def add_file(
        self,
        key: str,
        name: str,
        data: bytes,
        *,
        directory: str = ROOT,
        readers: set[str] | None = None,
        version: int = 1,
        updated: str = "2026-09-01T10:00:00Z",
    ) -> str:
        body = f"hash-{key}-{version}"
        self.files[key] = {
            "__id": key,
            "__name": name,
            "__createdAt": "2026-01-01T10:00:00Z",
            "__updatedAt": updated,
            "__createdBy": ANNA,
            "hash": body,
            "version": version,
            "directory": directory,
        }
        self.file_readers[key] = readers
        self.blobs[body] = data
        return key

    def update_file(self, key: str, data: bytes, *, updated: str) -> None:
        item = self.files[key]
        item["version"] += 1
        item["hash"] = f"hash-{key}-{item['version']}"
        item["__updatedAt"] = updated
        self.blobs[item["hash"]] = data

    # --- обработка ----------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "")
        if host == STORAGE_HOST:
            return self._storage(request)
        if host != self.host:
            return httpx.Response(404, text="unknown host")
        path = request.url.path
        if not path.startswith("/pub/v1/"):
            return httpx.Response(404, text="<html>not found</html>")
        route = path.removeprefix("/pub/v1/")
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, route, body))
        if self.server_errors > 0:
            self.server_errors -= 1
            return httpx.Response(503, text="unavailable")
        if self.rate_limit_hits > 0:
            self.rate_limit_hits -= 1
            headers = (
                {} if self.retry_after is None else {"retry-after": self.retry_after}
            )
            return httpx.Response(429, headers=headers, text="Too Many Requests")
        auth = request.headers.get("authorization", "")
        user = self.tokens.get(auth.removeprefix("Bearer ")) if auth else None
        if user is None:
            return httpx.Response(401, text="Unauthorized")
        if self.failure:
            return _json({"success": False, "error": "внутренняя ошибка"})
        return self._route(request.method, route, body, user)

    def _route(
        self, method: str, route: str, body: dict[str, Any], user: str
    ) -> httpx.Response:
        if route in ("disk/directory/list", "disk/files/search") and (
            user in self.disk_forbidden
        ):
            return httpx.Response(403, text="forbidden")
        if method == "POST" and route == "disk/directory/list":
            active = [
                d
                for d in self.directories.values()
                if not (body.get("active") and d.get("__deletedAt"))
            ]
            page = self._page(active, body)
            return _json(
                {"success": True, "result": {"result": page, "total": len(active)}}
            )
        if method == "POST" and route == "disk/files/search":
            directory = (body.get("filter") or {}).get("directory")
            if directory in self.search_forbidden:
                return httpx.Response(403, text="forbidden")
            found = [
                {k: v for k, v in f.items() if k != "directory"}
                for f in self.files.values()
                if directory is None or f["directory"] == directory
            ]
            return _json({"success": True, "files": self._page(found, body)})
        parts = route.split("/")
        if method == "GET" and len(parts) == 3 and parts[:2] == ["disk", "directory"]:
            item = self.directories.get(parts[2])
            if item is None:
                return httpx.Response(404, text="not found")
            if not self._dir_readable(parts[2], user):
                return httpx.Response(403, text="forbidden")
            return _json({"success": True, "directory": item})
        if len(parts) >= 3 and parts[:2] == ["disk", "file"]:
            item = self.files.get(parts[2])
            if item is None:
                return httpx.Response(404, text="not found")
            if not self._file_readable(parts[2], user):
                return httpx.Response(403, text="forbidden")
            if method == "GET" and len(parts) == 3:
                shown = {k: v for k, v in item.items() if k != "directory"}
                return _json({"success": True, "file": shown})
            if len(parts) == 4 and parts[3] == "get-link":
                link = f"https://{STORAGE_HOST}/bucket/{item['hash']}?{SIGNATURE}"
                return _json({"success": True, "Link": link})
        return httpx.Response(404, text="not found")

    def _page(self, items: list[dict[str, Any]], body: dict[str, Any]) -> list[Any]:
        offset = int(body.get("from", 0))
        size = min(int(body.get("size", 100)), self.max_size)
        return items[offset : offset + size]

    def _dir_readable(self, key: str, user: str) -> bool:
        readers = self.dir_readers.get(key)
        return readers is None or user in readers

    def _file_readable(self, key: str, user: str) -> bool:
        readers = self.file_readers.get(key)
        directory = self.files[key]["directory"]
        return (readers is None or user in readers) and self._dir_readable(
            directory, user
        )

    def _storage(self, request: httpx.Request) -> httpx.Response:
        self.storage_auth.append(request.headers.get("authorization", ""))
        if SIGNATURE not in request.url.query.decode():
            return httpx.Response(403)
        parts = request.url.path.split("/")
        data = self.blobs.get(parts[-1])
        if data is None:
            return httpx.Response(404)
        return httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, content=data
        )


def _json(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


DIR_HR = uid(1)
DIR_CLOSED = uid(2)
DIR_LINK = uid(3)
DIR_DEEP = uid(4)
FILE_VACATION = uid(101)
FILE_SALARY = uid(102)
FILE_PIC = uid(103)
FILE_ROOT = uid(104)
FILE_PRIVATE = uid(105)
FILE_BONUS = uid(106)


def sample_elma365() -> FakeElma365:
    """Папка «Кадры» для всех; внутри «Закрытое» только Анне (и глубже);
    в открытой папке файл, закрытый Борису отдельно; картинка (не наш
    формат); ссылка-папка на «Кадры»; файл в корне."""
    server = FakeElma365()
    server.add_directory(DIR_HR, "Кадры")
    server.add_directory(DIR_CLOSED, "Закрытое", parent=DIR_HR, readers={ANNA})
    server.add_directory(DIR_DEEP, "Глубже", parent=DIR_CLOSED, readers={ANNA})
    server.add_directory(DIR_LINK, "Ссылка на кадры", target=DIR_HR)
    server.add_file(
        FILE_VACATION,
        "Отпуск.txt",
        "Отпуск — 28 календарных дней.".encode(),
        directory=DIR_HR,
    )
    server.add_file(
        FILE_SALARY, "Зарплаты.md", "# Ведомость".encode(), directory=DIR_CLOSED
    )
    server.add_file(
        FILE_BONUS, "Премии.txt", "Премии по итогам года.".encode(), directory=DIR_DEEP
    )
    server.add_file(
        FILE_PRIVATE,
        "Личное.txt",
        "Только для Анны.".encode(),
        directory=DIR_HR,
        readers={ANNA},
    )
    server.add_file(FILE_PIC, "Схема.png", b"\x89PNG", directory=DIR_HR)
    server.add_file(FILE_ROOT, "Памятка.txt", "Корневой файл.".encode())
    return server
