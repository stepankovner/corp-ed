"""Поддельный amoCRM для контрактных тестов адаптера.

Формы ответов — по документации www.amocrm.ru/developers (15.10.2026):
- OAuth: https://www.amocrm.ru/developers/content/oauth/step-by-step и
  https://www.amocrm.ru/developers/content/oauth/oauth — POST
  {аккаунт}/oauth2/access_token (JSON: client_id, client_secret,
  grant_type, code | refresh_token, redirect_uri, совпадающий с
  настройками интеграции), ответ token_type/expires_in/access_token/
  refresh_token; refresh одноразовый. Тела ошибок не описаны — здесь
  статусы 400 (код или refresh не приняты) и 401 (интеграция);
- аккаунт: https://www.amocrm.ru/developers/content/crm_platform/account-info
  — GET /api/v4/account?with=drive_url: id, subdomain, current_user_id,
  drive_url;
- сделки: https://www.amocrm.ru/developers/content/crm_platform/leads-api
  — GET /api/v4/leads (limit ≤ 250, page, `_links.next`,
  `_embedded.leads`), «в соответствии с правами пользователя»; контакты и
  компании — так же (contacts-api, companies-api);
- файлы: https://www.amocrm.ru/developers/content/files/files-api —
  GET /api/v4/{leads|contacts|companies}/{id}/files (`_embedded.files`:
  file_uuid и id связи; limit, before_id; 204 — файлов нет; 403 — нет
  прав), сервис файлов GET /v1.0/files (filter[uuid] через запятую,
  limit, page) и /v1.0/files/{uuid} — по scope интеграции, ссылки
  `_links.download` и `_links.download_version`;
- лимиты: https://www.amocrm.ru/developers/content/api/recommendations
  — 429 сверх 7 запросов в секунду, 402 — аккаунт не оплачен, 403 на
  всё — аккаунт заблокирован.

Права пользователя упрощены до amoCRM-овских «Просмотр»: A — все,
M — только свои (ответственный), D — запрещено (список раздела — 403).
"""

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx

from corp_ed.core.outbound import OutboundClient
from tests.fake_connector import public_resolver

ACCOUNT_HOST = "company.amocrm.ru"
ACCOUNT = f"https://{ACCOUNT_HOST}"
DRIVE_HOST = "drive-b.amocrm.ru"
STORAGE_HOST = "storage.amocrm-files.example.com"
CLIENT_ID = "11111111-2222-3333-4444-555555555555"
CLIENT_SECRET = "amo-client-secret"  # noqa: S105 — поддельный сервер
REDIRECT_URI = "https://kronto.example.com/api/v1/connectors/oauth/callback"
ANNA_ID = 101
BORIS_ID = 102
ANNA_ACCESS = "amo-access-anna"  # noqa: S105
ANNA_REFRESH = "amo-refresh-anna"  # noqa: S105
BORIS_ACCESS = "amo-access-boris"  # noqa: S105
BORIS_REFRESH = "amo-refresh-boris"  # noqa: S105
ACCOUNT_ID = 30_000_001
NOW = 1_800_000_000


@dataclass
class FakeAmo:
    host: str = ACCOUNT_HOST
    subdomain: str = "company"
    drive_url: str | None = f"https://{DRIVE_HOST}"
    access: dict[str, int] = field(
        default_factory=lambda: {ANNA_ACCESS: ANNA_ID, BORIS_ACCESS: BORIS_ID}
    )
    refresh: dict[str, int] = field(
        default_factory=lambda: {ANNA_REFRESH: ANNA_ID, BORIS_REFRESH: BORIS_ID}
    )
    codes: dict[str, int] = field(default_factory=dict)
    expired: set[str] = field(default_factory=set)
    rights: dict[int, dict[str, str]] = field(default_factory=dict)
    entities: dict[str, dict[int, dict[str, Any]]] = field(
        default_factory=lambda: {"leads": {}, "contacts": {}, "companies": {}}
    )
    links: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    files_scope: bool = True
    blocked: bool = False
    unpaid: bool = False
    rate_limit_hits: int = 0
    server_errors: int = 0
    download_redirect: bool = False
    issued: int = 0
    calls: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    token_requests: list[dict[str, Any]] = field(default_factory=list)
    foreign_auth: list[str] = field(default_factory=list)
    """Заголовок Authorization, дошедший до хоста хранилища."""

    def client(self) -> OutboundClient:
        return OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
            resolver=public_resolver,
        )

    # --- наполнение -------------------------------------------------------------

    def add_entity(
        self,
        entity: str,
        entity_id: int,
        name: str,
        *,
        responsible: int = ANNA_ID,
        pipeline: int = 10,
    ) -> None:
        item: dict[str, Any] = {
            "id": entity_id,
            "name": name,
            "responsible_user_id": responsible,
            "group_id": 0,
            "updated_at": NOW,
            "is_deleted": False,
            "account_id": ACCOUNT_ID,
            "_links": {"self": {"href": f"{ACCOUNT}/api/v4/{entity}/{entity_id}"}},
        }
        if entity == "leads":
            item.update(pipeline_id=pipeline, status_id=142, price=0)
        self.entities[entity][entity_id] = item

    def add_file(
        self,
        uuid: str,
        name: str,
        data: bytes,
        *,
        size: int | None = None,
        trashed: bool = False,
        version: str | None = None,
    ) -> None:
        version = version or f"v-{uuid}-1"
        extension = name.rsplit(".", 1)[-1] if "." in name else ""
        base = f"https://{DRIVE_HOST}/download/acc-seg/{uuid}"
        self.files[uuid] = {
            "uuid": uuid,
            "name": name,
            "sanitized_name": name,
            "size": len(data) if size is None else size,
            "type": "document",
            "version_uuid": version,
            "source_id": None,
            "is_trashed": trashed,
            "has_multiple_versions": False,
            "created_at": NOW - 100,
            "updated_at": NOW,
            "created_by": {"type": "user", "id": ANNA_ID},
            "updated_by": {"type": "user", "id": ANNA_ID},
            "deleted_at": NOW if trashed else None,
            "deleted_by": {"type": "user", "id": ANNA_ID} if trashed else None,
            "metadata": {"extension": extension, "mime_type": "text/plain"},
            "previews": None,
            "_links": {
                "download": {"href": f"{base}/{name}"},
                "download_version": {"href": f"{base}/{version}/{name}"},
                "self": {"href": f"https://{DRIVE_HOST}/v1.0/files/{uuid}"},
            },
        }
        self.blobs[uuid] = data

    def link(self, entity: str, entity_id: int, uuid: str) -> None:
        links = self.links.setdefault((entity, entity_id), [])
        link_id = 5_000_000 + sum(len(v) for v in self.links.values())
        links.append({"file_uuid": uuid, "id": link_id})

    def new_version(self, uuid: str, data: bytes) -> None:
        item = self.files[uuid]
        item["version_uuid"] = f"v-{uuid}-{NOW + 1}"
        item["updated_at"] = NOW + 1
        item["size"] = len(data)
        base = f"https://{DRIVE_HOST}/download/acc-seg/{uuid}"
        item["_links"]["download_version"] = {
            "href": f"{base}/{item['version_uuid']}/{item['name']}"
        }
        self.blobs[uuid] = data

    def issue_code(self, user: int) -> str:
        code = f"code-{user}-{len(self.codes)}"
        self.codes[code] = user
        return code

    # --- обработка ----------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "")
        path = unquote(request.url.path)
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        self.calls.append((host, path, query))
        if host == STORAGE_HOST:
            self.foreign_auth.append(request.headers.get("authorization", ""))
            uuid = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                content=self.blobs.get(uuid, b""),
            )
        if host not in (self.host, DRIVE_HOST):
            return httpx.Response(404, text="unknown host")
        if request.method == "POST" and path == "/oauth2/access_token":
            if host != self.host:
                return httpx.Response(404)
            return self._token(json.loads(request.content or b"{}"))
        if self.server_errors > 0:
            self.server_errors -= 1
            return httpx.Response(503, text="unavailable")
        if self.rate_limit_hits > 0:
            self.rate_limit_hits -= 1
            return httpx.Response(429)
        auth = request.headers.get("authorization", "").removeprefix("Bearer ")
        user = self.access.get(auth)
        if user is None or auth in self.expired:
            return _problem(401, "Unauthorized")
        if self.blocked:
            return _problem(403, "Forbidden")
        if self.unpaid:
            return _problem(402, "Payment Required")
        if host == DRIVE_HOST:
            return self._drive(path, query)
        if path.startswith("/api/v4/"):
            return self._api(path.removeprefix("/api/v4/"), query, user)
        return httpx.Response(404)

    def _token(self, body: dict[str, Any]) -> httpx.Response:
        self.token_requests.append(body)
        if (
            body.get("client_id") != CLIENT_ID
            or body.get("client_secret") != CLIENT_SECRET
        ):
            return _problem(401, "Client authentication failed")
        if body.get("redirect_uri") != REDIRECT_URI:
            return _problem(400, "Invalid redirect_uri")
        grant = body.get("grant_type")
        if grant == "authorization_code":
            user = self.codes.pop(str(body.get("code")), None)
        elif grant == "refresh_token":
            user = self.refresh.pop(str(body.get("refresh_token")), None)
        else:
            user = None
        if user is None:
            return _problem(400, "Cannot decrypt the authorization code")
        self.issued += 1
        access = f"amo-access-{user}-{self.issued}"
        refresh = f"amo-refresh-{user}-{self.issued}"
        self.access[access] = user
        self.refresh[refresh] = user
        return httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "expires_in": 86400,
                "server_time": NOW,
                "access_token": access,
                "refresh_token": refresh,
            },
        )

    def _view(self, user: int, entity: str) -> str:
        return self.rights.get(user, {}).get(entity, "A")

    def _visible(self, user: int, entity: str, item: dict[str, Any]) -> bool:
        view = self._view(user, entity)
        if view == "A":
            return True
        return view == "M" and item["responsible_user_id"] == user

    def _api(self, route: str, query: dict[str, str], user: int) -> httpx.Response:
        if route == "account":
            account: dict[str, Any] = {
                "id": ACCOUNT_ID,
                "name": "Компания",
                "subdomain": self.subdomain,
                "current_user_id": user,
                "country": "RU",
                "customers_mode": "disabled",
                "is_unsorted_on": True,
            }
            if "drive_url" in query.get("with", "") and self.drive_url is not None:
                account["drive_url"] = self.drive_url
            return httpx.Response(200, json=account)
        parts = route.split("/")
        entity = parts[0]
        if entity not in self.entities:
            return httpx.Response(404)
        if len(parts) == 1:
            if self._view(user, entity) == "D":
                return _problem(403, "Forbidden")
            visible = [
                item
                for item in self.entities[entity].values()
                if self._visible(user, entity, item)
            ]
            limit = min(int(query.get("limit", 50)), 250)
            page = int(query.get("page", 1))
            chunk = visible[(page - 1) * limit : page * limit]
            if not chunk:
                return httpx.Response(204)
            links: dict[str, Any] = {
                "self": {"href": f"{ACCOUNT}/api/v4/{entity}?page={page}"}
            }
            if page * limit < len(visible):
                links["next"] = {"href": f"{ACCOUNT}/api/v4/{entity}?page={page + 1}"}
            return httpx.Response(
                200,
                json={"_page": page, "_links": links, "_embedded": {entity: chunk}},
            )
        if len(parts) == 3 and parts[2] == "files" and parts[1].isdigit():
            item = self.entities[entity].get(int(parts[1]))
            if item is None:
                return _problem(404, "Not Found")
            if not self._visible(user, entity, item):
                return _problem(403, "Forbidden")
            linked = sorted(
                self.links.get((entity, item["id"]), []),
                key=lambda link: link["id"],
                reverse=True,
            )
            if "before_id" in query:
                linked = [x for x in linked if x["id"] < int(query["before_id"])]
            linked = linked[: int(query.get("limit", 50))]
            if not linked:
                return httpx.Response(204)
            return httpx.Response(
                200,
                headers={"content-type": "application/hal+json"},
                json={
                    "_links": {"self": {"href": route}},
                    "_embedded": {"files": linked},
                },
            )
        return httpx.Response(404)

    def _drive(self, path: str, query: dict[str, str]) -> httpx.Response:
        if not self.files_scope:
            return _problem(403, "Forbidden")
        if path == "/v1.0/files":
            wanted = [u for u in query.get("filter[uuid]", "").split(",") if u]
            found = [self.files[u] for u in wanted if u in self.files]
            if not found:
                return httpx.Response(204)
            return httpx.Response(
                200,
                json={
                    "_count": len(found),
                    "_embedded": {"files": found},
                    "_links": {"self": {"href": f"https://{DRIVE_HOST}{path}"}},
                },
            )
        if path.startswith("/v1.0/files/"):
            item = self.files.get(path.removeprefix("/v1.0/files/"))
            if item is None:
                return _problem(404, "Not Found")
            return httpx.Response(200, json=item)
        if path.startswith("/download/acc-seg/"):
            uuid = path.removeprefix("/download/acc-seg/").split("/", 1)[0]
            if uuid not in self.blobs:
                return httpx.Response(404)
            if self.download_redirect:
                return httpx.Response(
                    302, headers={"location": f"https://{STORAGE_HOST}/blob/{uuid}"}
                )
            return httpx.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                content=self.blobs[uuid],
            )
        return httpx.Response(404)


def _problem(status: int, title: str) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/problem+json"},
        json={"title": title, "type": "https://httpstatus.es/", "status": status},
    )


def sample_amo() -> FakeAmo:
    """Анна видит всё; Борис — только свои сделки, все контакты и ни одной
    компании. Файл КП привязан к сделке и контакту; есть файл чужой
    воронки, картинка, слишком большой PDF и файл в корзине."""
    server = FakeAmo()
    server.rights[BORIS_ID] = {"leads": "M", "contacts": "A", "companies": "D"}
    server.add_entity("leads", 1001, "Поставка в Казань")
    server.add_entity("leads", 1002, "Сделка Бориса", responsible=BORIS_ID)
    server.add_entity("leads", 1003, "Другая воронка", pipeline=20)
    server.add_entity("contacts", 2001, "Иван Петров")
    server.add_entity("companies", 3001, "ООО Ромашка")
    server.add_file("f-offer", "КП.txt", "Коммерческое предложение: 100 ед.".encode())
    server.add_file("f-boris", "Договор.md", "# Договор\n\nСрок — 30 дней.".encode())
    server.add_file("f-other", "Прайс.txt", "Прайс второй воронки.".encode())
    server.add_file("f-pic", "Фото.png", b"\x89PNG")
    server.add_file("f-big", "Каталог.pdf", b"%PDF-", size=200 * 1024 * 1024)
    server.add_file("f-trash", "Старое.txt", b"old", trashed=True)
    server.add_file("f-company", "Реквизиты.txt", "ИНН 7700000000.".encode())
    for uuid in ("f-offer", "f-pic", "f-big", "f-trash"):
        server.link("leads", 1001, uuid)
    server.link("leads", 1002, "f-boris")
    server.link("leads", 1003, "f-other")
    server.link("contacts", 2001, "f-offer")
    server.link("companies", 3001, "f-company")
    return server
