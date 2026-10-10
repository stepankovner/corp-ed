"""Поддельные Яндекс ID (OAuth), Яндекс Диск и Яндекс Вики для контрактных тестов.

Формы ответов сверены с документацией 28.09 (yandex.ru/dev/id,
yandex.ru/dev/disk-api, yandex.ru/support/wiki/ru/api-ref): при
продлении приходит новый refresh-токен; `fields` отбрасывает остальные
ключи; в листинге сначала папки, потом файлы; ошибки Диска —
{error, description, message}; ссылка на скачивание требует тот же
токен и отвечает 302 на *.storage.yandex.net; общие диски — отдельный
API virtual-disks; Вики — api.wiki.yandex.net/v1 с X-Org-Id и ошибками
{error_code, debug_message}. Живым Диском и Вики не проверено (RISKS
№38, №43).

Трекер (yandex.ru/support/tracker/ru/, 09.10.2026): api.tracker.yandex.net/v3
с X-Org-ID (Яндекс 360) или X-Cloud-Org-ID (Yandex Identity Hub); очереди —
GET queues с page/perPage и X-Total-Pages; задачи очереди — POST
issues/_search {"queue": KEY} с относительной пагинацией (perPage и id,
следующая страница — в заголовке Link); комментарии — GET
issues/{id}/comments, тоже по Link; вложения — expand=attachments,
скачивание по content на том же хосте; ошибки — {errorMessages, errors}.
"""

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode

import httpx

from corp_ed.core.outbound import OutboundClient
from tests.fake_connector import public_resolver

OAUTH_HOST = "oauth.yandex.ru"
API_HOST = "cloud-api.yandex.net"
DOWNLOAD_HOST = "downloader.dst.yandex.ru"
STORAGE_HOST = "s42.storage.yandex.net"
WIKI_HOST = "api.wiki.yandex.net"
TRACKER_HOST = "api.tracker.yandex.net"
TRACKER_API = f"https://{TRACKER_HOST}/"
CLOUD_ORG_ID = "bpf3crucp1v2abcdefgh"
OAUTH_SERVER = f"https://{OAUTH_HOST}/"
DISK_API = f"https://{API_HOST}/"
WIKI_API = f"https://{WIKI_HOST}/"
CLIENT_ID = "yandex-app-id"
CLIENT_SECRET = "yandex-app-secret"  # noqa: S105 — поддельный сервер
ACCESS_TOKEN = "y0-token-7"  # noqa: S105
REFRESH_TOKEN = "y0-refresh-7"  # noqa: S105
AUTH_CODE = "ycode-7"
LOGIN = "ivan.petrov"
UID = "1130000012345678"
ORG_ID = "8123456"


def _error(status: int, name: str, message: str = "") -> httpx.Response:
    return httpx.Response(
        status, json={"error": name, "description": message or name, "message": message}
    )


@dataclass
class FakeYandex:
    access_tokens: dict[str, str] = field(default_factory=dict)
    expired: set[str] = field(default_factory=set)
    refresh_tokens: dict[str, str] = field(default_factory=dict)
    codes: dict[str, str] = field(default_factory=dict)
    rotate_refresh: bool = True
    oauth_error: str | None = None
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    """path → ресурс (type dir|file, …); дети — по префиксу пути."""
    files: dict[str, bytes] = field(default_factory=dict)
    forbidden: set[str] = field(default_factory=set)
    maintenance: bool = False
    rate_limit_hits: int = 0
    templated_links: bool = False
    virtual_disks: list[dict[str, Any]] = field(default_factory=list)
    relative_virtual_paths: bool = False
    wiki_pages: dict[int, dict[str, Any]] = field(default_factory=dict)
    wiki_hidden: set[int] = field(default_factory=set)
    wiki_needs_sync: bool = False
    api_error: tuple[int, dict[str, Any]] | None = None
    """Ответ Диска и Вики на любой авторизованный запрос (статус, тело)."""
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    downloads: list[tuple[str, str]] = field(default_factory=list)
    """(хост, Authorization) каждого запроса к ссылке на файл."""
    wiki_headers: list[dict[str, str]] = field(default_factory=list)
    issued: int = 0
    device_tokens: set[str] = field(default_factory=set)
    """Токены, выданные с device_id: только их Яндекс ID умеет отозвать."""
    revoke_error: tuple[int, dict[str, Any]] | None = None
    """Ответ /revoke_token вместо обычного (статус, тело)."""
    queues: list[dict[str, Any]] = field(default_factory=list)
    issues: dict[str, dict[str, Any]] = field(default_factory=dict)
    """id задачи → задача (как в ответе API; вложения — в _attachments)."""
    comments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    attachment_files: dict[str, bytes] = field(default_factory=dict)
    """id вложения → байты."""
    tracker_hidden: set[str] = field(default_factory=set)
    """Задачи, которых сотрудник не видит: поиск их не отдаёт."""
    tracker_forbidden_queues: set[str] = field(default_factory=set)
    tracker_per_page: int = 2
    """Сколько отдавать на страницу, сколько бы ни попросили: пагинация."""
    tracker_rate_limit_hits: int = 0
    tracker_cloud: bool = False
    """Трекер привязан к организации Identity Hub: нужен X-Cloud-Org-ID."""
    tracker_headers: list[dict[str, str]] = field(default_factory=list)

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

    def add_virtual_disk(
        self, vd_hash: str, name: str, *, permissions: tuple[str, ...] = ("read",)
    ) -> str:
        root = f"vd:{vd_hash}:disk:/"
        self.virtual_disks.append(
            {
                "name": name,
                "resource_id": f"vd-{vd_hash}",
                "vd_hash": vd_hash,
                "permissions": list(permissions),
            }
        )
        self.entries[root] = {
            "type": "dir",
            "name": name,
            "path": root,
            "resource_id": f"rid-{root}",
        }
        return root

    def add_wiki_page(
        self,
        page_id: int,
        slug: str,
        title: str,
        content: str = "",
        *,
        page_type: str = "wysiwyg",
        modified_at: str = "2026-06-01T10:00:00+03:00",
        is_draft: bool = False,
        redirect: dict[str, Any] | None = None,
    ) -> int:
        self.wiki_pages[page_id] = {
            "id": page_id,
            "slug": slug,
            "title": title,
            "page_type": page_type,
            "content": content,
            "attributes": {"modified_at": modified_at, "is_draft": is_draft},
            "redirect": redirect,
        }
        return page_id

    # --- обработка -----------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "")
        if host == OAUTH_HOST:
            return self._oauth(request)
        if host in (DOWNLOAD_HOST, STORAGE_HOST):
            return self._download(request, host)
        if host == WIKI_HOST:
            return self._wiki(request)
        if host == TRACKER_HOST:
            return self._tracker(request)
        if host != API_HOST:
            return httpx.Response(404, text="unknown host")
        return self._api(request)

    def _token_of(self, request: httpx.Request) -> str | None:
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("OAuth ") if auth.startswith("OAuth ") else ""
        if not token or token in self.expired or token not in self.access_tokens:
            return None
        return token

    def _oauth(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/revoke_token" and request.method == "POST":
            return self._revoke(request)
        if request.url.path != "/token" or request.method != "POST":
            return httpx.Response(404)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append(("oauth/token", form))
        if self.oauth_error:
            return httpx.Response(
                400, json={"error": self.oauth_error, "error_description": "…"}
            )
        if (
            form.get("client_id") != CLIENT_ID
            or form.get("client_secret") != CLIENT_SECRET
        ):
            return httpx.Response(
                400,
                json={
                    "error": "invalid_client",
                    "error_description": "Client not found",
                },
            )
        grant = form.get("grant_type")
        if grant == "authorization_code":
            login = self.codes.pop(form.get("code", ""), None)
            if login is None:
                return httpx.Response(400, json={"error": "bad_verification_code"})
        elif grant == "refresh_token":
            login = self.refresh_tokens.get(form.get("refresh_token", ""))
            if login is not None and self.rotate_refresh:
                self.refresh_tokens.pop(form["refresh_token"])
        else:
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        if login is None:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.issued += 1
        access = f"y0-token-{login}-{self.issued}"
        self.access_tokens[access] = login
        if form.get("device_id"):
            self.device_tokens.add(access)
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

    def _revoke(self, request: httpx.Request) -> httpx.Response:
        """POST /revoke_token (yandex.ru/dev/id/doc/ru/tokens/token-invalidate):
        отозвать можно только токен, выданный с device_id; уже
        недействительный — тоже {"status": "ok"}."""
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append(("oauth/revoke_token", form))
        if self.revoke_error is not None:
            status, body = self.revoke_error
            return httpx.Response(status, json=body)
        if (
            form.get("client_id") != CLIENT_ID
            or form.get("client_secret") != CLIENT_SECRET
        ):
            return httpx.Response(
                400,
                json={"error": "invalid_client", "error_description": "…"},
            )
        token = form.get("access_token")
        if not token:
            return httpx.Response(400, json={"error": "invalid_request"})
        if token in self.access_tokens and token not in self.device_tokens:
            return httpx.Response(
                400,
                json={
                    "error": "unsupported_token_type",
                    "error_description": "Token without device_id",
                },
            )
        self.access_tokens.pop(token, None)
        self.device_tokens.discard(token)
        return httpx.Response(200, json={"status": "ok"})

    def _api(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        self.calls.append((path, query))
        token = self._token_of(request)
        if token is None:
            return _error(401, "UnauthorizedError", "Не авторизован.")
        if self.api_error is not None:
            return httpx.Response(self.api_error[0], json=self.api_error[1])
        if self.rate_limit_hits > 0:
            self.rate_limit_hits -= 1
            return _error(429, "TooManyRequestsError")
        if self.maintenance:
            return _error(423, "LockedError", "Технические работы.")
        login = self.access_tokens[token]
        if path == "/v1/disk/":
            return httpx.Response(
                200,
                json={
                    "total_space": 10**10,
                    "used_space": 10**8,
                    "user": {"login": login, "uid": UID, "display_name": "Иван"},
                },
            )
        if path == "/v1/disk/resources":
            return self._resources(query, virtual=False)
        if path == "/v1/disk/virtual-disks/resources":
            if "fields" in query:
                return _error(400, "FieldValidationError", "fields")
            return self._resources(query, virtual=True)
        if path == "/v1/disk/virtual-disks/discovery":
            return self._discovery(query)
        if path in (
            "/v1/disk/resources/download",
            "/v1/disk/virtual-disks/resources/download",
        ):
            entry = self.entries.get(query.get("path", ""))
            if entry is None or entry["type"] != "file":
                return _error(404, "DiskNotFoundError", "Не удалось найти ресурс.")
            return httpx.Response(
                200,
                json={
                    "href": f"https://{DOWNLOAD_HOST}/disk/{entry['resource_id']}?disposition=attachment",
                    "method": "GET",
                    "templated": self.templated_links,
                },
            )
        return _error(404, "DiskNotFoundError")

    def _discovery(self, query: dict[str, str]) -> httpx.Response:
        if query.get("org_id") != ORG_ID:
            return _error(403, "ForbiddenError", "org")
        limit = int(query.get("limit") or 10)
        offset = int(query.get("offset") or 0)
        return httpx.Response(
            200,
            json={
                "items": self.virtual_disks[offset : offset + limit],
                "total": len(self.virtual_disks),
                "limit": limit,
                "offset": offset,
            },
        )

    def _resources(self, query: dict[str, str], *, virtual: bool) -> httpx.Response:
        path = query.get("path", "")
        if path.startswith("vd:") != virtual:
            return _error(400, "FieldValidationError", "path")
        entry = self.entries.get(path)
        if entry is None:
            return _error(404, "DiskNotFoundError", "Не удалось найти ресурс.")
        if path in self.forbidden:
            return _error(403, "DiskForbiddenError", "Доступ запрещён.")
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
            # «сначала перечисляются все вложенные папки, затем — файлы»
            key=lambda e: (e["type"] != "dir", e["name"]),
        )
        limit = int(query.get("limit") or 20)
        offset = int(query.get("offset") or 0)
        items = [
            self._shown(dict(e), virtual) for e in children[offset : offset + limit]
        ]
        fields = query.get("fields")
        if fields:
            wanted = {
                f.removeprefix("_embedded.items.")
                for f in fields.split(",")
                if f.startswith("_embedded.items.")
            }
            items = [{k: v for k, v in item.items() if k in wanted} for item in items]
        return httpx.Response(
            200,
            json={
                **entry,
                "_embedded": {
                    "items": items,
                    "total": len(children),
                    "limit": limit,
                    "offset": offset,
                    "path": path,
                },
            },
        )

    def _shown(self, item: dict[str, Any], virtual: bool) -> dict[str, Any]:
        if virtual and self.relative_virtual_paths:
            item["path"] = "/" + item["path"].split(":disk:/", 1)[-1]
        return item

    def _download(self, request: httpx.Request, host: str) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        self.downloads.append((host, auth))
        resource_id = unquote(request.url.path.removeprefix("/disk/"))
        if host == DOWNLOAD_HOST:
            # «Скачать файл по полученному адресу, указав тот же OAuth-токен»
            if self._token_of(request) is None:
                return _error(401, "UnauthorizedError")
            location = f"https://{STORAGE_HOST}/disk/{quote(resource_id, safe=':/')}"
            return httpx.Response(302, headers={"location": location})
        for path, entry in self.entries.items():
            if entry.get("resource_id") == resource_id and entry["type"] == "file":
                return httpx.Response(200, content=self.files[path])
        return httpx.Response(404, text="gone")

    # --- Вики ------------------------------------------------------------------

    def _wiki(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        self.calls.append((f"wiki{path}", query))
        self.wiki_headers.append(dict(request.headers))
        if self._token_of(request) is None:
            return httpx.Response(
                401, json={"error_code": "UNAUTHORIZED", "debug_message": "…"}
            )
        if request.headers.get("x-org-id") != ORG_ID:
            return httpx.Response(
                403, json={"error_code": "ORG_NOT_FOUND", "debug_message": "org"}
            )
        if self.api_error is not None:
            return httpx.Response(self.api_error[0], json=self.api_error[1])
        if self.wiki_needs_sync:
            return httpx.Response(
                403,
                json={
                    "error_code": "FORCED_SYNC_REQUIRED",
                    "debug_message": (
                        "OAuth was unsuccessful; "
                        "Please authenticate user via frontend first"
                    ),
                },
            )
        visible = {
            pid: page
            for pid, page in self.wiki_pages.items()
            if pid not in self.wiki_hidden
        }
        if path == "/v1/pages/descendants":
            root = query.get("slug", "")
            matching = [
                page
                for page in sorted(visible.values(), key=lambda p: p["id"])
                if page["slug"] == root or page["slug"].startswith(root + "/")
            ]
            if not any(page["slug"] == root for page in visible.values()):
                return httpx.Response(
                    404, json={"error_code": "NOT_FOUND", "debug_message": "slug"}
                )
            if query.get("include_self") != "true":
                matching = [page for page in matching if page["slug"] != root]
            size = int(query.get("page_size") or 50)
            start = int(query.get("cursor") or 0)
            chunk = matching[start : start + size]
            following = start + size
            return httpx.Response(
                200,
                json={
                    "results": [{"id": p["id"], "slug": p["slug"]} for p in chunk],
                    "next_cursor": str(following)
                    if following < len(matching)
                    else None,
                    "prev_cursor": None,
                },
            )
        if path.startswith("/v1/pages/"):
            raw = path.removeprefix("/v1/pages/")
            if not raw.isdigit() or int(raw) not in visible:
                return httpx.Response(
                    404, json={"error_code": "NOT_FOUND", "debug_message": "page"}
                )
            page = visible[int(raw)]
            fields = set((query.get("fields") or "").replace(" ", "").split(","))
            body: dict[str, Any] = {
                "id": page["id"],
                "slug": page["slug"],
                "title": page["title"],
                "page_type": page["page_type"],
            }
            if "attributes" in fields:
                body["attributes"] = page["attributes"]
            if "content" in fields:
                body["content"] = page["content"]
            if "redirect" in fields:
                body["redirect"] = page["redirect"]
            if "breadcrumbs" in fields:
                parts = page["slug"].split("/")
                body["breadcrumbs"] = [
                    {
                        "title": self._title("/".join(parts[: i + 1])),
                        "slug": "/".join(parts[: i + 1]),
                    }
                    for i in range(len(parts))
                ]
            return httpx.Response(200, json=body)
        return httpx.Response(
            404, json={"error_code": "NOT_FOUND", "debug_message": path}
        )

    def _title(self, slug: str) -> str:
        for page in self.wiki_pages.values():
            if page["slug"] == slug:
                return str(page["title"])
        return slug

    # --- Трекер ---------------------------------------------------------------

    def add_queue(self, key: str, name: str) -> str:
        self.queues.append({"id": str(len(self.queues) + 1), "key": key, "name": name})
        return key

    def add_issue(
        self,
        issue_id: str,
        key: str,
        summary: str,
        description: str = "",
        *,
        status: str = "Открыт",
        version: int = 1,
        updated: str = "2026-09-01T10:00:00.000+0000",
        attachments: tuple[tuple[str, str, bytes], ...] = (),
        comments: tuple[tuple[str, str], ...] = (),
    ) -> str:
        queue = key.split("-", 1)[0]
        listed = []
        for att_id, name, data in attachments:
            self.attachment_files[att_id] = data
            listed.append(
                {
                    "self": f"https://{TRACKER_HOST}/v3/attachments/{att_id}",
                    "id": att_id,
                    "name": name,
                    "content": f"https://{TRACKER_HOST}/v3/issues/{key}"
                    f"/attachments/{att_id}/{quote(name)}",
                    "mimetype": "application/octet-stream",
                    "size": len(data),
                    "createdAt": updated,
                }
            )
        self.issues[issue_id] = {
            "self": f"https://{TRACKER_HOST}/v3/issues/{key}",
            "id": issue_id,
            "key": key,
            "version": version,
            "summary": summary,
            "description": description,
            "updatedAt": updated,
            "createdAt": "2026-08-01T09:00:00.000+0000",
            "status": {"key": "open", "display": status},
            "queue": {"key": queue, "display": self._queue_name(queue)},
            "createdBy": {"id": "1", "display": "Иван Петров"},
            "assignee": {"id": "2", "display": "Мария Иванова"},
            "commentWithoutExternalMessageCount": len(comments),
            "commentWithExternalMessageCount": 0,
            "_attachments": listed,
        }
        self.comments[issue_id] = []
        for author, text in comments:
            self.add_comment(issue_id, author, text)
        return issue_id

    def add_comment(self, issue_id: str, author: str, text: str) -> None:
        comments = self.comments.setdefault(issue_id, [])
        number = len(comments) + 1
        comments.append(
            {
                "id": number,
                "longId": f"c{issue_id}-{number}",
                "text": text,
                "createdBy": {"display": author},
                "createdAt": "2026-09-02T12:00:00.000+0000",
                "updatedAt": "2026-09-02T12:00:00.000+0000",
                "type": "standard",
                "transport": "internal",
            }
        )
        issue = self.issues.get(issue_id)
        if issue is not None:
            issue["commentWithoutExternalMessageCount"] = len(comments)

    def _queue_name(self, key: str) -> str:
        for queue in self.queues:
            if queue["key"] == key:
                return str(queue["name"])
        return key

    def _tracker_error(self, status: int, message: str) -> httpx.Response:
        return httpx.Response(
            status,
            json={"errorMessages": [message], "errors": {}, "statusCode": status},
        )

    def _tracker(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        self.calls.append((f"tracker{path}", query))
        self.tracker_headers.append(dict(request.headers))
        if self._token_of(request) is None:
            return self._tracker_error(401, "Не авторизован")
        org = request.headers.get("x-org-id")
        cloud = request.headers.get("x-cloud-org-id")
        if (self.tracker_cloud and cloud != CLOUD_ORG_ID) or (
            not self.tracker_cloud and org != ORG_ID
        ):
            return self._tracker_error(403, "Организация не найдена")
        if self.tracker_rate_limit_hits > 0:
            self.tracker_rate_limit_hits -= 1
            return httpx.Response(429, headers={"retry-after": "2"})
        per_page = min(int(query.get("perPage") or 50), self.tracker_per_page)
        if path in ("/v3/queues", "/v3/queues/"):
            page = int(query.get("page") or 1)
            chunk = self.queues[(page - 1) * per_page : page * per_page]
            total = max(1, -(-len(self.queues) // per_page))
            headers = {"x-total-pages": str(total)}
            return httpx.Response(200, json=chunk, headers=headers)
        if path == "/v3/issues/_search" and request.method == "POST":
            return self._search(request, query, per_page)
        parts = path.removeprefix("/v3/issues/").split("/")
        if path.startswith("/v3/issues/") and parts:
            issue = self._find_issue(unquote(parts[0]))
            if issue is None or issue["id"] in self.tracker_hidden:
                return self._tracker_error(404, "Задача не найдена")
            if len(parts) == 1:
                return httpx.Response(200, json=self._issue_view(issue, False))
            if parts[1:] == ["comments"]:
                return self._comments(issue, path, query, per_page)
            if len(parts) >= 3 and parts[1] == "attachments":
                data = self.attachment_files.get(parts[2])
                if data is None:
                    return self._tracker_error(404, "Файл не найден")
                return httpx.Response(
                    200, content=data, headers={"content-type": "application/pdf"}
                )
        return self._tracker_error(404, "Нет такого ресурса")

    def _search(
        self, request: httpx.Request, query: dict[str, str], per_page: int
    ) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        queue = body.get("queue")
        if not isinstance(queue, str):
            return self._tracker_error(400, "Нужна очередь")
        if queue in self.tracker_forbidden_queues:
            return self._tracker_error(403, "Нет доступа к очереди")
        if queue not in {q["key"] for q in self.queues}:
            return self._tracker_error(404, "Очередь не найдена")
        matching = sorted(
            (
                issue
                for issue in self.issues.values()
                if issue["queue"]["key"] == queue
                and issue["id"] not in self.tracker_hidden
            ),
            key=lambda issue: int(issue["key"].split("-")[1]),
        )
        after = query.get("id")
        if after:
            ids = [issue["id"] for issue in matching]
            matching = matching[ids.index(after) + 1 :] if after in ids else []
        chunk = matching[:per_page]
        expand = query.get("expand", "")
        items = [self._issue_view(issue, "attachments" in expand) for issue in chunk]
        headers = {}
        if len(matching) > per_page:
            link = f"https://{TRACKER_HOST}/v3/issues/_search?" + urlencode(
                {"perPage": per_page, "id": chunk[-1]["id"]}
            )
            headers["link"] = f'<{link}>; rel="next"'
        return httpx.Response(200, json=items, headers=headers)

    def _comments(
        self,
        issue: dict[str, Any],
        path: str,
        query: dict[str, str],
        per_page: int,
    ) -> httpx.Response:
        comments = self.comments.get(issue["id"], [])
        after = int(query.get("id") or 0)
        rest = [c for c in comments if c["id"] > after]
        chunk = rest[:per_page]
        headers = {}
        if len(rest) > per_page:
            first = f"https://{TRACKER_HOST}{path}?perPage={per_page}"
            link = f"https://{TRACKER_HOST}{path}?" + urlencode(
                {"perPage": per_page, "id": chunk[-1]["id"]}
            )
            headers["link"] = f'<{first}>; rel="first", <{link}>; rel="next"'
        return httpx.Response(200, json=chunk, headers=headers)

    def _find_issue(self, ref: str) -> dict[str, Any] | None:
        if ref in self.issues:
            return self.issues[ref]
        for issue in self.issues.values():
            if issue["key"] == ref:
                return issue
        return None

    @staticmethod
    def _issue_view(issue: dict[str, Any], attachments: bool) -> dict[str, Any]:
        view = {k: v for k, v in issue.items() if k != "_attachments"}
        if attachments and issue["_attachments"]:
            view["attachments"] = issue["_attachments"]
        return view


def sample_tracker(server: FakeYandex | None = None) -> FakeYandex:
    """Три очереди, задачи с комментариями и вложениями; одна задача
    скрыта от сотрудника, одна очередь закрыта."""
    server = server or FakeYandex()
    server.add_queue("SUP", "Поддержка")
    server.add_queue("HR", "Кадры")
    server.add_queue("SEC", "Безопасность")
    server.add_issue(
        "id-1",
        "SUP-1",
        "Клиент Альфа: сроки доставки",
        "Клиент спрашивает про **сроки**.\n\n"
        "{% note info %}\n\nВажно.\n\n{% endnote %}",
        status="В работе",
        attachments=(
            ("a1", "Договор.pdf", b"%PDF-1.4 contract"),
            ("a2", "Скриншот.png", b"\x89PNG"),
            ("a3", "Огромный.docx", b"x" * 2048),
        ),
        comments=(
            ("Иван Петров", "Решили: доставка за 3 дня."),
            ("Мария Иванова", "Клиент согласен."),
            ("Иван Петров", "Закрываю."),
        ),
    )
    server.add_issue("id-2", "SUP-2", "Вопрос по счёту", "Счёт № 15.")
    server.add_issue("id-3", "SUP-3", "Скрытая задача", "secret")
    server.tracker_hidden.add("id-3")
    server.add_issue("id-4", "HR-1", "Отпуск в мае", "Заявление подано.")
    server.add_issue("id-5", "SEC-1", "Закрытая очередь", "secret")
    server.tracker_forbidden_queues.add("SEC")
    return server


def sample_yandex() -> FakeYandex:
    server = FakeYandex()
    server.add_dir("disk:/Регламенты")
    server.add_file("disk:/Регламенты/Отпуск.txt", "Отпуск — 28 дней.".encode())
    server.add_file("disk:/Регламенты/Схема.png", b"\x89PNG")
    server.add_file("disk:/Регламенты/Большой.pdf", b"%PDF-", size=200 * 1024 * 1024)
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
    # Общие диски организации: отдельное хранилище, не в disk:/.
    root = server.add_virtual_disk("h4sh", "Кадры")
    server.add_dir(f"{root}Политики")
    server.add_file(
        f"{root}Политики/Отпуска.md", "# Отпуска\n\nГрафик — в январе.".encode()
    )
    server.add_file(f"{root}Приказ.txt", "Приказ о пропусках.".encode())
    server.add_virtual_disk("n0read", "Бухгалтерия", permissions=())
    server.add_file("vd:n0read:disk:/Зарплаты.txt", b"secret")
    # Вики.
    server.add_wiki_page(1, "homepage", "Главная", "Добро пожаловать.")
    server.add_wiki_page(
        2,
        "homepage/hr",
        "Кадры",
        "# Кадры\n\n#|\n|| Документ | Срок ||\n|| Отпуск | 14 дней ||\n|#\n",
    )
    server.add_wiki_page(3, "homepage/hr/draft", "Черновик", "…", is_draft=True)
    server.add_wiki_page(4, "homepage/tables", "Таблица", "", page_type="grid")
    server.add_wiki_page(
        5, "homepage/old", "Старое", "", redirect={"page_id": 2, "slug": "homepage/hr"}
    )
    server.add_wiki_page(6, "homepage/secret", "Тайное", "…")
    server.wiki_hidden.add(6)
    server.add_wiki_page(
        7,
        "sales",
        "Продажи",
        "{% note info %}\n\nСкидка — 5 %.\n\n{% endnote %}\n\n"
        '{% file src="x" name="Прайс.pdf" %}\n{{toc}}\n',
    )
    return server
