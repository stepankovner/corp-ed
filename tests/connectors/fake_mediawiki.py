"""Поддельная MediaWiki (Action API `api.php`) для контрактных тестов.

Формы — по документации mediawiki.org и ответам MediaWiki 1.43.11 на
стенде (`tests/live/mediawiki`, 15.10), `formatversion=2`:

- https://www.mediawiki.org/wiki/API:Login — `meta=tokens&type=login`
  ставит cookie `wiki_session`, `action=login` (POST) с бот-паролем →
  `{login: {result: Success, lguserid, lgusername}}` и cookie
  `wiki_BPsession`; неверный пароль — `{result: Failed, reason}`;
- https://www.mediawiki.org/wiki/API:Assert — `assert=user` без сессии →
  `{error: {code: assertuserfailed}}`;
- https://www.mediawiki.org/wiki/API:Errors_and_warnings — ошибки с
  HTTP 200: `{error: {code, info, docref}}`; `readapidenied` — нет права
  чтения (закрытая вики без входа или учётка без `read`);
- https://www.mediawiki.org/wiki/API:Siteinfo — general, namespaces
  (объект id → {id, name, canonical, content}), namespacealiases,
  extensions ({type, name});
- https://www.mediawiki.org/wiki/API:Users — `usprop=rights|blockinfo`:
  `missing`/`invalid` для несуществующих, `blockid`/`blockpartial` у
  заблокированных;
- https://www.mediawiki.org/wiki/API:Allpages, API:Allimages,
  API:Revisions, API:Info (`intestactions` → `actions.read`),
  API:Imageinfo, API:Continue — `generator` с `gaplimit`/`gailimit` и
  `continue: {gapcontinue|gaicontinue, continue}`;
- https://www.mediawiki.org/wiki/API:Parsing_wikitext — `action=parse` →
  `{parse: {title, pageid, revid, text}}`, `nosuchpageid`.

Файлы отдаются по `/w/images/…` (веб-сервер, без проверки прав); с
`img_auth` ссылки ведут на `/w/img_auth.php/…`, а он сессию бот-пароля не
принимает (BotPasswordSessionProvider — только api.php и rest.php):
403, как на стенде 15.10. `forbidden_files` — файлы, на которые сервер
отвечает 403 (правило веб-сервера перед `/images/`).
"""

import hashlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, quote

import httpx

from corp_ed.core.outbound import OutboundClient
from tests.fake_connector import public_resolver

HOST = "wiki.example.com"
API = f"https://{HOST}/w/api.php"
BOT = "Admin@kronto"
BOT_PASSWORD = "kronto0fake0admin0bot0pass000001"  # noqa: S105 — поддельный сервер
NO_READ_BOT = "Petr@kronto"
NO_READ_PASSWORD = "kronto0fake0petr00bot0pass000001"  # noqa: S105

NAMESPACES: dict[int, tuple[str, str, bool]] = {
    -2: ("Медиа", "Media", False),
    -1: ("Служебная", "Special", False),
    0: ("", "", True),
    1: ("Обсуждение", "Talk", False),
    2: ("Участник", "User", False),
    4: ("Компания", "Project", False),
    6: ("Файл", "File", False),
    10: ("Шаблон", "Template", False),
    12: ("Справка", "Help", False),
    3000: ("Кадры", "Кадры", False),
}
_DOCREF = "See https://wiki.example.com/w/api.php for API usage."


@dataclass
class FakeMediaWiki:
    host: str = HOST
    private: bool = False
    """Анонимное чтение запрещено ($wgGroupPermissions['*']['read'])."""
    extensions: list[str] = field(default_factory=list)
    bots: dict[str, tuple[str, str]] = field(
        default_factory=lambda: {
            BOT: (BOT_PASSWORD, "Admin"),
            NO_READ_BOT: (NO_READ_PASSWORD, "Petr"),
        }
    )
    login_result: str | None = None
    """Подменить результат входа (Aborted, Throttled …)."""
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    pages: dict[int, dict[str, Any]] = field(default_factory=dict)
    files: dict[int, dict[str, Any]] = field(default_factory=dict)
    sessions: dict[str, str] = field(default_factory=dict)
    login_tokens: dict[str, str] = field(default_factory=dict)
    drop_sessions: bool = False
    """Сессия живёт только до следующего запроса данных (сервер их теряет)."""
    upload_host: str | None = None
    img_auth: bool = False
    """Ссылки на файлы — через img_auth.php ($wgUploadPath закрытой вики)."""
    forbidden_files: set[int] = field(default_factory=set)
    """Хост ссылок на файлы ($wgUploadPath на другом домене)."""
    rate_limited: dict[str, int] = field(default_factory=dict)
    """модуль (login, siteinfo, allpages, parse …) → сколько раз ratelimited."""
    http_status: dict[str, int] = field(default_factory=dict)
    """модуль → HTTP-статус вместо ответа (один раз)."""
    retry_after: str | None = None
    calls: list[dict[str, str]] = field(default_factory=list)
    downloads: list[tuple[str, str]] = field(default_factory=list)
    _next_id: int = 100

    def client(self) -> OutboundClient:
        return OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
            resolver=public_resolver,
        )

    @property
    def api(self) -> str:
        return f"https://{self.host}/w/api.php"

    def modules(self) -> list[str]:
        return [_module(params) for params in self.calls]

    # --- наполнение ---------------------------------------------------------------

    def add_user(
        self,
        name: str,
        *,
        read: bool = True,
        blocked: bool = False,
        partial_block: bool = False,
    ) -> None:
        user: dict[str, Any] = {
            "userid": len(self.users) + 1,
            "name": name,
            "rights": (["read"] if read else []) + ["autoconfirmed"],
        }
        if blocked or partial_block:
            user.update(
                {
                    "blockid": len(self.users) + 1,
                    "blockedby": "Admin",
                    "blockexpiry": "infinite",
                    "blockpartial": partial_block,
                }
            )
        self.users[name] = user

    def add_page(
        self,
        title: str,
        html: str,
        *,
        ns: int = 0,
        redirect: bool = False,
        readable: bool = True,
        contentmodel: str = "wikitext",
        timestamp: str = "2026-10-01T10:00:00Z",
    ) -> int:
        page_id = self._new_id()
        prefix = NAMESPACES[ns][0]
        self.pages[page_id] = {
            "pageid": page_id,
            "ns": ns,
            "title": f"{prefix}:{title}" if prefix else title,
            "html": html,
            "revid": page_id * 10,
            "timestamp": timestamp,
            "redirect": redirect,
            "readable": readable,
            "contentmodel": contentmodel,
        }
        return page_id

    def edit(self, page_id: int, html: str) -> None:
        page = self.pages[page_id]
        page["html"] = html
        page["revid"] += 1
        page["timestamp"] = "2026-10-10T10:00:00Z"

    def add_file(self, name: str, data: bytes, *, readable: bool = True) -> int:
        page_id = self._new_id()
        self.files[page_id] = {
            "pageid": page_id,
            "name": name,
            "data": data,
            "readable": readable,
            "timestamp": "2026-10-01T10:00:00Z",
        }
        return page_id

    def expire_sessions(self) -> None:
        self.sessions.clear()

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    # --- обработка ------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("host", "") not in {self.host, self.upload_host}:
            return httpx.Response(404, text="unknown host")
        cookies = _cookies(request.headers.get("cookie", ""))
        path = request.url.path
        if path.startswith(("/w/images/", "/w/img_auth.php/")):
            return self._download(path, cookies)
        if path != "/w/api.php":
            return httpx.Response(404, text="<html>Not Found</html>")
        params = dict(request.url.params)
        if request.method == "POST":
            params.update(dict(parse_qsl(request.content.decode("utf-8"))))
        self.calls.append(params)
        module = _module(params)
        if module in self.http_status:
            return httpx.Response(
                self.http_status.pop(module), text="<html>error</html>"
            )
        if self.rate_limited.get(module, 0) > 0:
            self.rate_limited[module] -= 1
            headers = {"retry-after": self.retry_after} if self.retry_after else {}
            return _error("ratelimited", "You've exceeded your rate limit.", headers)
        action = params.get("action")
        if action == "login":
            return self._login(params, cookies)
        if params.get("meta") == "tokens" and params.get("type") == "login":
            session = f"s{len(self.login_tokens) + 1}"
            token = f"tok{len(self.login_tokens) + 1}+\\"
            self.login_tokens[session] = token
            return httpx.Response(
                200,
                json={
                    "batchcomplete": True,
                    "query": {"tokens": {"logintoken": token}},
                },
                headers={
                    "set-cookie": f"wiki_session={session}; path=/; secure; HttpOnly"
                },
            )
        if self.drop_sessions:
            self.sessions.clear()
        actor = self.sessions.get(cookies.get("wiki_BPsession", ""))
        if params.get("assert") == "user" and actor is None:
            return _error(
                "assertuserfailed",
                "You are no longer logged in, so the action could not be completed.",
            )
        if not self._can_read(actor):
            return _error(
                "readapidenied", "You need read permission to use this module."
            )
        if action == "query":
            return self._query(params)
        if action == "parse":
            return self._parse(params)
        return _error("badvalue", 'Unrecognized value for parameter "action".')

    def _can_read(self, actor: str | None) -> bool:
        if actor is None:
            return not self.private
        user = self.users.get(actor)
        return user is not None and "read" in user["rights"]

    def _login(self, params: dict[str, str], cookies: dict[str, str]) -> httpx.Response:
        session = cookies.get("wiki_session", "")
        if not params.get("lgtoken"):
            return httpx.Response(
                200, json={"login": {"result": "NeedToken", "token": "t+\\"}}
            )
        if self.login_tokens.get(session) != params.get("lgtoken"):
            return httpx.Response(200, json={"login": {"result": "WrongToken"}})
        if self.login_result is not None:
            return httpx.Response(
                200, json={"login": {"result": self.login_result, "reason": "x"}}
            )
        bot = self.bots.get(params.get("lgname", ""))
        if bot is None or bot[0] != params.get("lgpassword"):
            return httpx.Response(
                200,
                json={
                    "login": {
                        "result": "Failed",
                        "reason": "Incorrect username or password entered.",
                    }
                },
            )
        sid = f"bp{len(self.sessions) + 1}-{bot[1]}"
        self.sessions[sid] = bot[1]
        return httpx.Response(
            200,
            json={"login": {"result": "Success", "lguserid": 1, "lgusername": bot[1]}},
            headers={"set-cookie": f"wiki_BPsession={sid}; path=/; secure; HttpOnly"},
        )

    def _query(self, params: dict[str, str]) -> httpx.Response:
        if params.get("meta") == "siteinfo":
            return _ok({"query": self._siteinfo()})
        if params.get("list") == "users":
            return _ok({"query": {"users": self._users(params.get("ususers", ""))}})
        if params.get("generator") == "allpages":
            return self._allpages(params)
        if params.get("generator") == "allimages":
            return self._allimages(params)
        return _error("badvalue", "Unrecognized query.")

    def _siteinfo(self) -> dict[str, Any]:
        return {
            "general": {
                "sitename": "Компания",
                "generator": "MediaWiki 1.43.11",
                "server": f"https://{self.host}",
                "script": "/w/index.php",
                "case": "first-letter",
            },
            "namespaces": {
                str(ns): {
                    "id": ns,
                    "case": "first-letter",
                    "name": name,
                    "canonical": canonical or None,
                    "content": content,
                }
                for ns, (name, canonical, content) in NAMESPACES.items()
            },
            "namespacealiases": [{"id": 6, "alias": "Изображение"}],
            "extensions": [
                {"type": "skin", "name": "Vector"},
                *({"type": "other", "name": name} for name in self.extensions),
            ],
        }

    def _users(self, names: str) -> list[dict[str, Any]]:
        found = []
        for raw in names.split("|"):
            if "<" in raw or not raw.strip():
                found.append({"name": raw, "invalid": True})
                continue
            name = raw.replace("_", " ").strip()
            name = name[:1].upper() + name[1:]
            user = self.users.get(name)
            found.append(dict(user) if user else {"name": name, "missing": True})
        return found

    def _allpages(self, params: dict[str, str]) -> httpx.Response:
        ns = int(params.get("gapnamespace", "0"))
        limit = min(int(params.get("gaplimit", "10")), 50)
        start = params.get("gapcontinue", "")
        chosen = sorted(
            (
                page
                for page in self.pages.values()
                if page["ns"] == ns
                and not (
                    params.get("gapfilterredir") == "nonredirects" and page["redirect"]
                )
            ),
            key=lambda page: _key(page["title"]),
        )
        chosen = [p for p in chosen if _key(p["title"]) >= start]
        batch, rest = chosen[:limit], chosen[limit:]
        answer: dict[str, Any] = {"batchcomplete": True}
        if rest:
            answer["continue"] = {
                "gapcontinue": _key(rest[0]["title"]),
                "continue": "gapcontinue||",
            }
        if batch:
            answer["query"] = {"pages": [self._page(p, params) for p in batch]}
        return _ok(answer)

    def _page(self, page: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
        shown: dict[str, Any] = {
            "pageid": page["pageid"],
            "ns": page["ns"],
            "title": page["title"],
            "contentmodel": page["contentmodel"],
            "lastrevid": page["revid"],
            "length": len(page["html"]),
            "touched": page["timestamp"],
        }
        if "revisions" in params.get("prop", ""):
            shown["revisions"] = [
                {
                    "revid": page["revid"],
                    "parentid": 0,
                    "timestamp": page["timestamp"],
                    "size": len(page["html"]),
                }
            ]
        if page["redirect"]:
            shown["redirect"] = True
        self._info(shown, page["title"], page["readable"], params)
        return shown

    def _info(
        self, shown: dict[str, Any], title: str, readable: bool, params: dict[str, str]
    ) -> None:
        if params.get("inprop") == "url":
            link = f"https://{self.host}/w/index.php/{quote(title.replace(' ', '_'))}"
            shown["fullurl"] = link
            shown["canonicalurl"] = link
        if params.get("intestactions") == "read":
            shown["actions"] = {"read": readable}

    def _allimages(self, params: dict[str, str]) -> httpx.Response:
        limit = min(int(params.get("gailimit", "10")), 50)
        start = params.get("gaicontinue", "")
        chosen = sorted(self.files.values(), key=lambda f: _key(f["name"]))
        chosen = [f for f in chosen if _key(f["name"]) >= start]
        batch, rest = chosen[:limit], chosen[limit:]
        answer: dict[str, Any] = {"batchcomplete": True}
        if rest:
            answer["continue"] = {
                "gaicontinue": _key(rest[0]["name"]),
                "continue": "gaicontinue||",
            }
        pages = []
        files_path = "img_auth.php" if self.img_auth else "images"
        for item in batch:
            title = f"Файл:{item['name']}"
            host = self.upload_host or self.host
            shown: dict[str, Any] = {
                "pageid": item["pageid"],
                "ns": 6,
                "title": title,
                "imagerepository": "local",
                "imageinfo": [
                    {
                        "timestamp": item["timestamp"],
                        "size": len(item["data"]),
                        "width": 0,
                        "height": 0,
                        "url": f"https://{host}/w/{files_path}/{item['pageid']}/"
                        + quote(item["name"]),
                        "sha1": hashlib.sha1(item["data"]).hexdigest(),  # noqa: S324
                        "mime": "application/octet-stream",
                    }
                ],
            }
            self._info(shown, title, item["readable"], params)
            pages.append(shown)
        if pages:
            answer["query"] = {"pages": pages}
        return _ok(answer)

    def _parse(self, params: dict[str, str]) -> httpx.Response:
        page = self.pages.get(int(params.get("pageid") or 0))
        if page is None:
            return _error(
                "nosuchpageid", f"There is no page with ID {params.get('pageid')}."
            )
        if not page["readable"]:
            return _error("permissiondenied", "You don't have permission to read.")
        return _ok(
            {
                "parse": {
                    "title": page["title"],
                    "pageid": page["pageid"],
                    "revid": page["revid"],
                    "text": (
                        '<div class="mw-content-ltr mw-parser-output" lang="ru">'
                        f"{page['html']}</div>"
                    ),
                }
            }
        )

    def _download(self, path: str, cookies: dict[str, str]) -> httpx.Response:
        self.downloads.append((path, cookies.get("wiki_BPsession", "")))
        page_id = int(path.split("/")[3])
        if path.startswith("/w/img_auth.php/") or page_id in self.forbidden_files:
            return httpx.Response(403, text="<html>Access denied</html>")
        item = self.files.get(page_id)
        if item is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(
            200, content=item["data"], headers={"content-type": "text/plain"}
        )


def _module(params: dict[str, str]) -> str:
    action = params.get("action", "")
    if action != "query":
        return action
    for key in ("meta", "list", "generator"):
        if key in params:
            return params[key] if key != "meta" or params[key] != "tokens" else "tokens"
    return action


def _key(title: str) -> str:
    return title.replace(" ", "_")


def _cookies(header: str) -> dict[str, str]:
    found = {}
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep:
            found[name] = value
    return found


def _ok(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=data)


def _error(
    code: str, info: str, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(
        200,
        json={"error": {"code": code, "info": info, "docref": _DOCREF}},
        headers=headers or {},
    )


MAPPING = (
    "Anna = Anna@Example.com\n"
    "boris=boris@example.com\n"
    "Vera=vera@example.com, Gleb=gleb@example.com\n"
    "Ghost=ghost@example.com"
)
"""Сопоставление «логин = почта»: Ghost в вики нет."""


def sample_wiki(
    *, private: bool = False, extensions: tuple[str, ...] = ()
) -> FakeMediaWiki:
    """Anna, Boris — читатели; Vera заблокирована; у Gleb нет права read;
    Admin — владелец бот-пароля; Petr — бот без права чтения. Страницы в
    основном пространстве, в «Компания», «Справка», «Участник» и «Кадры»,
    перенаправление, страница стилей, закрытая для бота страница, файлы
    txt, png и md."""
    server = FakeMediaWiki(private=private, extensions=list(extensions))
    server.add_user("Admin")
    server.add_user("Anna")
    server.add_user("Boris")
    server.add_user("Vera", blocked=True)
    server.add_user("Gleb", read=False)
    server.add_user("Petr", read=not private)
    server.add_page("Отпуск", "<h2>Отпуск</h2><p>Ежегодный отпуск — 28 дней.</p>")
    server.add_page("Больничный", "<p>Справка в течение трёх дней.</p>")
    server.add_page("Старое название", "<p>→ Отпуск</p>", redirect=True)
    server.add_page("Common.css", "body{}", ns=0, contentmodel="css")
    server.add_page("Правила", "<p>Пишите коротко.</p>", ns=4)
    server.add_page("Поиск", "<p>Поиск — строка вверху.</p>", ns=12)
    server.add_page("Anna", "<p>Черновики Анны.</p>", ns=2)
    server.add_page("Зарплаты", "<p>Выплаты 5 и 20 числа.</p>", ns=3000)
    server.add_page("Секрет", "<p>Только для избранных.</p>", readable=False)
    server.add_file("Регламент.txt", "Регламент закупок.".encode())
    server.add_file("Схема.png", b"\x89PNG")
    server.add_file("Памятка.md", "# Памятка\n\nКак оформить командировку.".encode())
    return server
