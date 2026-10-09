"""Поддельный WebDAV-сервер: Nextcloud, ownCloud и «просто WebDAV».

Формы ответов — как у Nextcloud (sabre/dav): 207 multistatus, у каждого
элемента href в процентной кодировке, свойства в propstat со статусом
(неизвестные — отдельным propstat с 404), getetag в кавычках,
getlastmodified в формате RFC 1123, oc:fileid — сквозной номер файла на
сервере. Корень сотрудника Nextcloud — /remote.php/dav/files/<uid>/
(uid узнаётся через current-user-principal на /remote.php/dav/), ownCloud
— /remote.php/webdav/, остальные — один адрес, дерево по учётке.

Общая папка — те же объекты Node в дереве второго сотрудника под своим
путём: fileid, etag и содержимое у них общие, как у настоящей шары.
OAuth2 — как apps/oauth2 Nextcloud: /index.php/apps/oauth2/api/v1/token,
ответ с user_id, refresh_token меняется при каждом продлении, ошибки —
{"error": ...} со статусом 400.
"""

import base64
import itertools
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import parse_qs, quote, unquote
from xml.sax.saxutils import escape

import httpx

from corp_ed.core.outbound import OutboundClient
from tests.fake_connector import public_resolver

HOST = "cloud.example.ru"
SERVER = f"https://{HOST}/"
OTHER_HOST = "cdn.example.net"
MAILRU_HOST = "webdav.cloud.mail.ru"
CLIENT_ID = "nc-client-id"
CLIENT_SECRET = "nc-client-secret"  # noqa: S105 — поддельный сервер
AUTH_CODE = "nc-code-1"
IVAN = "ivan"
MARIA = "maria"
IVAN_LOGIN = "ivan@example.ru"
MARIA_LOGIN = "maria@example.ru"
IVAN_PASSWORD = "Ivan-App-Pass"  # noqa: S105
MARIA_PASSWORD = "Maria-App-Pass"  # noqa: S105
MODIFIED = "Tue, 05 May 2026 10:00:00 GMT"

Flavor = Literal["nextcloud", "owncloud", "plain"]
_ids = itertools.count(100)


@dataclass
class Node:
    data: bytes | None
    """None — папка."""
    etag: str
    modified: str = MODIFIED
    fileid: int = field(default_factory=lambda: next(_ids))
    size: int | None = None

    @property
    def is_dir(self) -> bool:
        return self.data is None


@dataclass
class FakeDav:
    flavor: Flavor = "nextcloud"
    host: str = HOST
    plain_root: str = "/dav/"
    absolute_hrefs: bool = False
    """href полным адресом (https://host/...), как отвечают некоторые NAS."""
    users: dict[str, str] = field(default_factory=dict)
    """логин → пароль приложения."""
    uids: dict[str, str] = field(default_factory=dict)
    """логин → uid Nextcloud (путь /files/<uid>/ — не логин)."""
    trees: dict[str, dict[str, Node]] = field(default_factory=dict)
    """uid → путь в дереве сотрудника ("" — корень) → узел."""
    bearer: dict[str, str] = field(default_factory=dict)
    refresh: dict[str, str] = field(default_factory=dict)
    codes: dict[str, str] = field(default_factory=dict)
    forbidden: set[tuple[str, str]] = field(default_factory=set)
    """(uid, путь папки) — 403 на PROPFIND."""
    extra_hrefs: dict[str, list[str]] = field(default_factory=dict)
    """путь папки → лишние href в её ответе (чужой хост, `..`, внук)."""
    rate_limit_hits: int = 0
    retry_after: str = "2"
    html_instead: bool = False
    principal: bool = True
    get_redirect: str | None = None
    """GET файла отвечает 302 на этот адрес."""
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    """(метод, путь, Depth) каждого запроса к WebDAV."""
    auth_headers: list[str] = field(default_factory=list)
    issued: int = 0

    def client(self) -> OutboundClient:
        return OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
            resolver=public_resolver,
        )

    # --- наполнение ---------------------------------------------------------

    def add_user(self, login: str, password: str, uid: str) -> None:
        self.users[login] = password
        self.uids[login] = uid
        self.trees.setdefault(uid, {"": Node(None, f"root-{uid}")})

    def add_dir(self, uid: str, path: str) -> Node:
        node = Node(None, f"d-{uid}-{path}")
        self.trees[uid][path] = node
        return node

    def add_file(
        self, uid: str, path: str, data: bytes, *, size: int | None = None
    ) -> Node:
        node = Node(data, f"e-{next(_ids)}", size=size)
        self.trees[uid][path] = node
        return node

    def change(self, uid: str, path: str, data: bytes) -> None:
        node = self.trees[uid][path]
        node.data = data
        node.etag = f"e-{next(_ids)}"
        node.modified = "Wed, 06 May 2026 11:00:00 GMT"

    def remove(self, uid: str, path: str) -> None:
        for key in [
            k for k in self.trees[uid] if k == path or k.startswith(path + "/")
        ]:
            del self.trees[uid][key]

    def share(self, owner: str, path: str, to: str, as_path: str) -> None:
        """Папку или файл owner видит to под своим путём — те же узлы."""
        tree = self.trees[owner]
        for key, node in list(tree.items()):
            if key == path or key.startswith(path + "/"):
                self.trees[to][as_path + key[len(path) :]] = node

    # --- запросы ------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "")
        if host == OTHER_HOST:
            return httpx.Response(200, content=b"foreign bytes")
        if host != self.host:
            return httpx.Response(404, text="unknown host")
        path = unquote(request.url.raw_path.decode("ascii").split("?", 1)[0])
        if path == "/index.php/apps/oauth2/api/v1/token":
            return self._token(request)
        self.calls.append((request.method, path, request.headers.get("depth", "")))
        self.auth_headers.append(request.headers.get("authorization", ""))
        if self.rate_limit_hits > 0:
            self.rate_limit_hits -= 1
            return httpx.Response(429, headers={"Retry-After": self.retry_after})
        uid = self._authenticate(request)
        if uid is None:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Basic realm="Nextcloud"'},
                text="<?xml version='1.0'?><d:error xmlns:d='DAV:'/>",
            )
        if self.html_instead:
            return httpx.Response(
                200, text="<html>login</html>", headers={"content-type": "text/html"}
            )
        if self.flavor == "nextcloud" and path.rstrip("/") == "/remote.php/dav":
            return self._principal(uid)
        prefix = self._prefix(uid)
        if not path.startswith(unquote(prefix)):
            return httpx.Response(404)
        relative = path[len(unquote(prefix)) :].strip("/")
        node = self.trees[uid].get(relative)
        if node is None:
            return httpx.Response(404)
        if request.method == "GET":
            if node.is_dir:
                return httpx.Response(405)
            if self.get_redirect:
                return httpx.Response(302, headers={"location": self.get_redirect})
            return httpx.Response(200, content=node.data or b"")
        if request.method != "PROPFIND":
            return httpx.Response(405)
        if (uid, relative) in self.forbidden:
            return httpx.Response(403)
        depth = request.headers.get("depth", "infinity")
        if depth not in ("0", "1"):
            return httpx.Response(403, text="infinite depth disabled")
        return self._multistatus(uid, prefix, relative, node, depth == "1")

    def _authenticate(self, request: httpx.Request) -> str | None:
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer "):
            return self.bearer.get(header.removeprefix("Bearer "))
        if not header.startswith("Basic "):
            return None
        login, _, password = (
            base64.b64decode(header.removeprefix("Basic ")).decode().partition(":")
        )
        if self.users.get(login) != password:
            return None
        return self.uids[login]

    def _prefix(self, uid: str) -> str:
        if self.flavor == "nextcloud":
            return f"/remote.php/dav/files/{quote(uid)}/"
        if self.flavor == "owncloud":
            return "/remote.php/webdav/"
        return self.plain_root

    def _href(self, prefix: str, relative: str, is_dir: bool) -> str:
        encoded = "/".join(quote(part, safe="") for part in relative.split("/") if part)
        href = prefix + encoded
        if is_dir and encoded:
            href += "/"
        if self.absolute_hrefs:
            return f"https://{self.host}{href}"
        return href

    def _principal(self, uid: str) -> httpx.Response:
        principal = (
            f"<d:current-user-principal><d:href>/remote.php/dav/principals/users/"
            f"{quote(uid)}/</d:href></d:current-user-principal>"
            if self.principal
            else ""
        )
        body = (
            '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
            "<d:response><d:href>/remote.php/dav/</d:href><d:propstat><d:prop>"
            f"{principal}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
            "</d:response></d:multistatus>"
        )
        return _xml(body)

    def _multistatus(
        self, uid: str, prefix: str, relative: str, node: Node, children: bool
    ) -> httpx.Response:
        parts = [self._response(prefix, relative, node)]
        if children:
            for key, child in self.trees[uid].items():
                if not key or key == relative:
                    continue
                parent = key.rsplit("/", 1)[0] if "/" in key else ""
                if parent == relative:
                    parts.append(self._response(prefix, key, child))
            for href in self.extra_hrefs.get(relative, []):
                parts.append(_response_xml(href, "", "", "x", None, None))
        body = (
            '<?xml version="1.0"?>\n<d:multistatus xmlns:d="DAV:" '
            'xmlns:s="http://sabredav.org/ns" xmlns:oc="http://owncloud.org/ns" '
            'xmlns:nc="http://nextcloud.org/ns">' + "".join(parts) + "</d:multistatus>"
        )
        return _xml(body)

    def _response(self, prefix: str, relative: str, node: Node) -> str:
        href = self._href(prefix, relative, node.is_dir)
        size = node.size if node.size is not None else len(node.data or b"")
        fileid = node.fileid if self.flavor != "plain" else None
        return _response_xml(
            href,
            node.modified,
            f'"{node.etag}"',
            "dir" if node.is_dir else "file",
            None if node.is_dir else size,
            fileid,
        )

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append(("POST", "oauth/token", form.get("grant_type", "")))
        if (
            form.get("client_id") != CLIENT_ID
            or form.get("client_secret") != CLIENT_SECRET
        ):
            return httpx.Response(400, json={"error": "invalid_client"})
        grant = form.get("grant_type")
        if grant == "authorization_code":
            uid = self.codes.pop(form.get("code", ""), None)
        elif grant == "refresh_token":
            uid = self.refresh.pop(form.get("refresh_token", ""), None)
        else:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if uid is None:
            # apps/oauth2: неизвестный или уже обменянный код — invalid_request.
            return httpx.Response(400, json={"error": "invalid_request"})
        self.issued += 1
        access = f"nc-access-{uid}-{self.issued}"
        refresh = f"nc-refresh-{uid}-{self.issued}"
        self.bearer[access] = uid
        self.refresh[refresh] = uid
        return httpx.Response(
            200,
            json={
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": refresh,
                "user_id": uid,
            },
        )


def _xml(body: str) -> httpx.Response:
    return httpx.Response(
        207,
        content=body.encode(),
        headers={"content-type": "application/xml; charset=utf-8"},
    )


def _response_xml(
    href: str,
    modified: str,
    etag: str,
    kind: str,
    size: int | None,
    fileid: int | None,
) -> str:
    found = [f"<d:getlastmodified>{modified}</d:getlastmodified>"] if modified else []
    missing = []
    if etag:
        found.append(f"<d:getetag>{escape(etag)}</d:getetag>")
    found.append(
        "<d:resourcetype><d:collection/></d:resourcetype>"
        if kind == "dir"
        else "<d:resourcetype/>"
    )
    if size is not None:
        found.append(f"<d:getcontentlength>{size}</d:getcontentlength>")
    else:
        missing.append("<d:getcontentlength/>")
    if fileid is not None:
        found.append(f"<oc:fileid>{fileid}</oc:fileid>")
    else:
        missing.append("<oc:fileid/>")
    missing_block = (
        "<d:propstat><d:prop>"
        + "".join(missing)
        + "</d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>"
        if missing
        else ""
    )
    return (
        f"<d:response><d:href>{escape(href)}</d:href><d:propstat><d:prop>"
        + "".join(found)
        + "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        + missing_block
        + "</d:response>"
    )


def sample_nextcloud(flavor: Flavor = "nextcloud", **kwargs: object) -> FakeDav:
    """Иван и Мария; у Ивана папка «Проекты», расшаренная Марии как
    «Общее от Ивана»; у каждого — свои файлы с одинаковым путём."""
    server = FakeDav(flavor=flavor, **kwargs)  # type: ignore[arg-type]
    server.add_user(IVAN_LOGIN, IVAN_PASSWORD, IVAN)
    server.add_user(MARIA_LOGIN, MARIA_PASSWORD, MARIA)
    server.add_dir(IVAN, "Документы")
    server.add_file(IVAN, "Документы/Отпуск.txt", "Отпуск — 28 дней.".encode())
    server.add_file(IVAN, "Документы/Схема.png", b"\x89PNG")
    server.add_file(IVAN, "Документы/Большой.pdf", b"%PDF-", size=100 * 1024 * 1024)
    server.add_file(IVAN, "Документы/.~lock.Отпуск.txt#", b"lock")
    server.add_dir(IVAN, "Проекты")
    server.add_file(IVAN, "Проекты/План 100%.md", "# План\n\nСрок — май.".encode())
    server.add_dir(IVAN, "Проекты/Архив")
    server.add_file(IVAN, "Проекты/Архив/Старый.md", "# Старый".encode())
    server.add_file(IVAN, "Заметка.txt", "Заметка Ивана.".encode())
    server.add_dir(MARIA, "Документы")
    server.add_file(MARIA, "Документы/Отпуск.txt", "Мария: отпуск в июле.".encode())
    server.share(IVAN, "Проекты", MARIA, "Общее от Ивана")
    return server
