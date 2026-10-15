"""Адаптер DokuWiki (режим organization): каталог, список сотрудников,
клиент JSON-RPC и его ошибки, права через core.aclCheck с группировкой
по пространствам, страницы и медиафайлы."""

import json
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    FetchedFile,
    FetchedPage,
    RemoteDocument,
)
from corp_ed.connectors.common import counting_skips
from corp_ed.connectors.dokuwiki.adapter import (
    SPEC,
    DokuWikiAdapter,
    build_adapter,
    parse_namespaces,
    parse_users,
    register,
)
from corp_ed.connectors.dokuwiki.client import (
    BasicAuth,
    DokuWikiClient,
    TokenAuth,
    wiki_root,
)
from corp_ed.connectors.registry import AdapterRegistry
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind
from tests.connectors.fake_dokuwiki import (
    ADMIN_TOKEN,
    MEMBER_TOKEN,
    PASSWORD,
    FakeDokuWiki,
    sample_dokuwiki,
)
from tests.fake_connector import public_resolver

KEY = Fernet.generate_key().decode()
MAX_BYTES = 1024 * 1024
USERS = {
    "admin": "admin@example.com",
    "ivan": "ivan@example.com",
    "maria": "maria@example.com",
    "petr": "petr@example.com",
    "olga": "olga@example.com",
}
EVERYONE = frozenset(USERS.values())
USERS_TEXT = "\n".join(f"{login} {email}" for login, email in USERS.items())


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def settings(**overrides: Any) -> ConnectorSettings:
    return ConnectorSettings(  # type: ignore[arg-type]
        secrets_keys=KEY,
        max_document_bytes=MAX_BYTES,
        max_large_document_bytes=MAX_BYTES,
        **overrides,
    )


def make_adapter(
    server: FakeDokuWiki,
    auth: TokenAuth | BasicAuth | None = None,
    *,
    users: dict[str, str] | None = None,
    sleep: Sleeps | None = None,
    limit: int = 20_000,
) -> DokuWikiAdapter:
    client = DokuWikiClient(
        server.client(),
        base_url=server.base,
        auth=auth or TokenAuth(ADMIN_TOKEN),
        sleep=sleep or Sleeps(),
    )
    return DokuWikiAdapter(
        client, users=users or USERS, max_bytes=MAX_BYTES, acl_check_limit=limit
    )


async def listed(
    adapter: DokuWikiAdapter, modules: tuple[str, ...] = ("pages", "media")
) -> dict[str, RemoteDocument]:
    return {d.external_id: d async for d in adapter.list(list(modules))}


@pytest.fixture
def server() -> FakeDokuWiki:
    return sample_dokuwiki()


# --- каталог и настройка --------------------------------------------------------------


def test_spec_is_hidden_organization_kind() -> None:
    assert SPEC.kind == "dokuwiki"
    assert SPEC.mode is ConnectorMode.ORGANIZATION and not SPEC.oauth
    assert SPEC.preview and SPEC.base
    assert [m.name for m in SPEC.modules] == ["pages", "media"]
    assert [(f.name, f.required) for f in SPEC.config_fields] == [
        ("base_url", True),
        ("users", True),
        ("namespaces", False),
    ]
    assert [(f.name, f.secret) for f in SPEC.credential_fields] == [
        ("token", True),
        ("login", False),
        ("password", True),
    ]
    assert SPEC.url_field == "base_url"


def test_kind_is_offered_only_when_enabled() -> None:
    hidden = AdapterRegistry()
    register(hidden, settings())
    assert hidden.kinds() == []
    assert hidden.spec("dokuwiki").preview
    enabled = AdapterRegistry(enabled_preview_kinds=frozenset({"dokuwiki"}))
    register(enabled, settings())
    assert [s.kind for s in enabled.kinds()] == ["dokuwiki"]


def test_users_list_is_parsed_and_validated() -> None:
    assert parse_users(
        "# логин почта\nivan Ivan@Example.com\n\nmaria=maria@example.com\n"
        "petr@example.com, petr; olga olga@example.com\n"
    ) == {
        "ivan": "ivan@example.com",
        "maria": "maria@example.com",
        "petr": "petr@example.com",
        "olga": "olga@example.com",
    }
    assert SPEC.config_check is not None
    assert SPEC.config_check({"users": USERS_TEXT}) is None
    assert SPEC.config_check({"users": " \n# пусто\n"}) == "users_empty"
    assert SPEC.config_check({"users": "ivan"}) == "users_invalid"
    assert SPEC.config_check({"users": "ivan a@b.ru c@d.ru"}) == "users_invalid"
    assert SPEC.config_check({"users": "ivan a@localhost"}) == "users_invalid"
    assert SPEC.config_check({"users": "ivan a@b.ru\nIvan c@d.ru"}) == (
        "users_duplicate"
    )


def test_credentials_need_token_or_login_and_password() -> None:
    check = SPEC.credentials_check
    assert check is not None
    assert check({"token": "t"}) is None
    assert check({"login": "admin", "password": "p"}) is None
    assert check({"login": "admin"}) == "credentials_incomplete"
    assert check({}) == "credentials_incomplete"


@pytest.mark.parametrize(
    ("address", "root"),
    [
        ("https://wiki.example.com", "https://wiki.example.com/"),
        ("https://wiki.example.com/dokuwiki/", "https://wiki.example.com/dokuwiki/"),
        ("https://wiki.example.com/doku.php?id=start", "https://wiki.example.com/"),
        (
            "https://wiki.example.com/w/lib/exe/jsonrpc.php",
            "https://wiki.example.com/w/",
        ),
    ],
)
def test_wiki_root(address: str, root: str) -> None:
    assert wiki_root(address) == root


def test_build_validates_config_and_credentials(server: FakeDokuWiki) -> None:
    config = {"base_url": server.base, "users": USERS_TEXT}
    adapter = build_adapter(
        SPEC, config, {"token": ADMIN_TOKEN}, server.client(), settings()
    )
    assert adapter.root == server.base
    with pytest.raises(AdapterAuthError, match="credentials_missing"):
        build_adapter(SPEC, config, {"login": "admin"}, server.client(), settings())
    with pytest.raises(AdapterConfigError, match="base_url_invalid"):
        build_adapter(
            SPEC,
            {**config, "base_url": "http://wiki.example.com/"},
            {"token": ADMIN_TOKEN},
            server.client(),
            settings(),
        )
    with pytest.raises(AdapterConfigError, match="users_empty"):
        build_adapter(
            SPEC,
            {**config, "users": ""},
            {"token": ADMIN_TOKEN},
            server.client(),
            settings(),
        )


# --- проверка и ошибки клиента ---------------------------------------------------


async def test_check_accepts_superuser_token_and_password(server: FakeDokuWiki) -> None:
    await make_adapter(server).check()
    await make_adapter(server, BasicAuth("admin", PASSWORD)).check()
    assert server.calls[0][0] == "core.whoAmI"


async def test_check_refuses_non_superuser(server: FakeDokuWiki) -> None:
    with pytest.raises(AdapterAuthError, match="admin_required"):
        await make_adapter(server, TokenAuth(MEMBER_TOKEN)).check()
    with pytest.raises(AdapterAuthError, match="admin_required"):
        await listed(make_adapter(server, BasicAuth("maria", PASSWORD)))


@pytest.mark.parametrize(
    "auth",
    [TokenAuth("wrong"), BasicAuth("admin", "wrong"), BasicAuth("nobody", PASSWORD)],
)
async def test_wrong_credentials_are_an_auth_error(
    server: FakeDokuWiki, auth: TokenAuth | BasicAuth
) -> None:
    with pytest.raises(AdapterAuthError, match="unauthorized"):
        await make_adapter(server, auth).check()


async def test_api_closed_by_remoteuser_is_a_config_error(
    server: FakeDokuWiki,
) -> None:
    server.remoteuser = ["@robots"]
    with pytest.raises(AdapterConfigError, match="forbidden"):
        await make_adapter(server).check()


async def test_disabled_api_and_old_wiki_are_config_errors(
    server: FakeDokuWiki,
) -> None:
    server.remote = False
    with pytest.raises(AdapterConfigError, match="remote_disabled"):
        await make_adapter(server).check()
    server.remote = True
    server.jsonrpc = False
    with pytest.raises(AdapterConfigError, match="jsonrpc_not_found"):
        await make_adapter(server).check()


async def test_wiki_without_new_api_is_too_old() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"code": -32603, "message": "Method does not exist"}}
        )

    client = DokuWikiClient(
        OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            resolver=public_resolver,
        ),
        base_url="https://wiki.example.com/",
        auth=TokenAuth(ADMIN_TOKEN),
    )
    adapter = DokuWikiAdapter(client, users=USERS, max_bytes=MAX_BYTES)
    with pytest.raises(AdapterConfigError, match="dokuwiki_too_old"):
        await adapter.check()


async def test_request_shape(server: FakeDokuWiki) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return server.handle(request)

    client = DokuWikiClient(
        OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            resolver=public_resolver,
        ),
        base_url=server.base,
        auth=TokenAuth(ADMIN_TOKEN),
    )
    await client.call("core.whoAmI")
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/lib/exe/jsonrpc.php"
    assert request.headers["host"] == server.host
    assert request.headers["content-type"] == "application/json"
    assert request.headers["authorization"] == f"Bearer {ADMIN_TOKEN}"
    assert request.headers["x-dokuwiki-token"] == ADMIN_TOKEN
    body = json.loads(request.content)
    assert body == {"jsonrpc": "2.0", "id": 1, "method": "core.whoAmI", "params": {}}


async def test_rate_limit_and_server_errors_are_retried(server: FakeDokuWiki) -> None:
    server.rate_limited = 2
    server.server_errors = 1
    sleeps = Sleeps()
    await make_adapter(server, sleep=sleeps).check()
    assert sleeps.calls == [1.0, 3.0, 3.0]


async def test_rate_limit_gives_up_retryable(server: FakeDokuWiki) -> None:
    server.rate_limited = 100
    with pytest.raises(AdapterError) as caught:
        await make_adapter(server).check()
    assert caught.value.code == "rate_limited" and caught.value.retryable
    server.rate_limited = 0
    server.server_errors = 100
    with pytest.raises(AdapterError) as caught:
        await make_adapter(server).check()
    assert caught.value.code == "http_502" and caught.value.retryable


async def test_error_message_does_not_leak() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"code": 131, "message": "secret detail"}}
        )

    client = DokuWikiClient(
        OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            resolver=public_resolver,
        ),
        base_url="https://wiki.example.com/",
        auth=TokenAuth(ADMIN_TOKEN),
    )
    with pytest.raises(AdapterError) as caught:
        await client.call("core.getPageHTML", {"page": ""})
    assert caught.value.code == "dokuwiki_131"
    assert "secret" not in str(caught.value)


async def test_html_instead_of_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login</html>")

    client = DokuWikiClient(
        OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            resolver=public_resolver,
        ),
        base_url="https://wiki.example.com/",
        auth=TokenAuth(ADMIN_TOKEN),
    )
    with pytest.raises(AdapterError, match="not_json"):
        await client.call("core.whoAmI")


# --- документы и права -------------------------------------------------------------


async def test_pages_carry_dokuwiki_rights(server: FakeDokuWiki) -> None:
    documents = await listed(make_adapter(server), ("pages",))
    assert set(documents) == {
        "page:start",
        "page:public:vacation",
        "page:public:fresh",
        "page:hr:onboarding",
        "page:hr:salaries",
        "page:sales:plan",
    }
    for document in documents.values():
        # «Вся компания» не выставляется даже для @ALL: только список почт.
        assert document.visibility is MaterialVisibility.RESTRICTED
    assert documents["page:start"].allowed_emails == EVERYONE
    assert documents["page:public:vacation"].allowed_emails == EVERYONE
    assert documents["page:hr:onboarding"].allowed_emails == {
        "admin@example.com",
        "maria@example.com",
        "petr@example.com",
    }
    # Запрет на страницу для petr при доступе группы hr к пространству.
    assert documents["page:hr:salaries"].allowed_emails == {
        "admin@example.com",
        "maria@example.com",
    }
    assert documents["page:sales:plan"].allowed_emails == {
        "admin@example.com",
        "ivan@example.com",
    }


async def test_page_fields(server: FakeDokuWiki) -> None:
    documents = await listed(make_adapter(server), ("pages",))
    vacation = documents["page:public:vacation"]
    assert vacation.title == "Отпуск"
    assert vacation.kind is RemoteDocumentKind.PAGE
    assert vacation.module == "pages"
    assert vacation.url == f"{server.base}doku.php?id=public:vacation"
    assert vacation.path == "public"
    assert vacation.version == "1760000000"
    assert vacation.modified_at is not None
    assert documents["page:start"].path == ""
    # Без заголовка — id страницы.
    assert documents["page:sales:plan"].title == "sales:plan"


async def test_pages_come_from_files_not_from_search_index(
    server: FakeDokuWiki,
) -> None:
    documents = await listed(make_adapter(server), ("pages",))
    assert "page:public:fresh" in documents
    params = next(p for m, p in server.calls if m == "core.listPages")
    assert params == {"namespace": "", "depth": 64}


async def test_acl_checks_are_grouped_by_namespace(server: FakeDokuWiki) -> None:
    for n in range(20):
        server.add_page(f"public:n{n}", f"<p>{n}</p>")
    await listed(make_adapter(server))
    checked = {p["page"] for m, p in server.calls if m == "core.aclCheck"}
    # По одному id на пространство и отдельно — id с точным правилом;
    # медиафайлы делят результат со страницами пространства.
    assert checked == {
        "start",
        "public:fresh",
        "hr:onboarding",
        "hr:salaries",
        "sales:plan",
    }
    calls = [m for m, _ in server.calls if m == "core.aclCheck"]
    assert len(calls) == len(checked) * len(USERS)
    assert ("plugin.acl.listAcls", {}) in server.calls


async def test_without_acl_plugin_every_item_is_checked(server: FakeDokuWiki) -> None:
    server.acl_plugin = False
    documents = await listed(make_adapter(server))
    checked = {p["page"] for m, p in server.calls if m == "core.aclCheck"}
    assert checked == {d.split(":", 1)[1] for d in documents}
    assert documents["page:hr:salaries"].allowed_emails == {
        "admin@example.com",
        "maria@example.com",
    }


async def test_exact_rule_on_one_page_does_not_leak_to_siblings(
    server: FakeDokuWiki,
) -> None:
    server.allow("hr:onboarding", "ivan", 1)
    documents = await listed(make_adapter(server), ("pages",))
    assert "ivan@example.com" in documents["page:hr:onboarding"].allowed_emails
    assert "ivan@example.com" not in documents["page:hr:salaries"].allowed_emails


async def test_empty_acl_table(server: FakeDokuWiki) -> None:
    server.acl = []
    documents = await listed(make_adapter(server), ("pages",))
    # Нет ни одного правила — DokuWiki не пускает никого, кроме суперпользователя.
    assert documents["page:start"].allowed_emails == {"admin@example.com"}


async def test_unknown_login_gets_only_public_rights(server: FakeDokuWiki) -> None:
    documents = await listed(
        make_adapter(server, users={"ghost": "ghost@example.com"}), ("pages",)
    )
    assert documents["page:public:vacation"].allowed_emails == {"ghost@example.com"}
    assert documents["page:start"].allowed_emails == frozenset()


def test_namespaces_setting_is_parsed() -> None:
    assert parse_namespaces(" hr, :sales:*, hr ,, wiki:docs ") == [
        "hr",
        "sales",
        "wiki:docs",
    ]
    assert parse_namespaces("") == []


async def test_namespaces_narrow_the_walk(server: FakeDokuWiki) -> None:
    server.add_page("hr:team:roles", "<p>Роли</p>")
    client = DokuWikiClient(
        server.client(), base_url=server.base, auth=TokenAuth(ADMIN_TOKEN)
    )
    adapter = DokuWikiAdapter(
        client, users=USERS, max_bytes=MAX_BYTES, namespaces=["hr", "hr:team"]
    )
    documents = await listed(adapter)
    # Вложенное пространство из настройки не даёт документ дважды.
    assert sorted(documents) == [
        "media:hr:policy.pdf",
        "page:hr:onboarding",
        "page:hr:salaries",
        "page:hr:team:roles",
    ]
    walked = [p["namespace"] for m, p in server.calls if m == "core.listPages"]
    assert walked == ["hr", "hr:team"]


def test_build_passes_namespaces(server: FakeDokuWiki) -> None:
    adapter = build_adapter(
        SPEC,
        {"base_url": server.base, "users": USERS_TEXT, "namespaces": "hr,sales"},
        {"token": ADMIN_TOKEN},
        server.client(),
        settings(),
    )
    assert adapter._namespaces == ["hr", "sales"]


async def test_acl_check_limit_stops_the_run(server: FakeDokuWiki) -> None:
    with pytest.raises(AdapterConfigError, match="acl_checks_limit"):
        await listed(make_adapter(server, limit=7))


async def test_media_rights_and_formats(server: FakeDokuWiki) -> None:
    with counting_skips() as skips:
        documents = await listed(make_adapter(server), ("media",))
    assert set(documents) == {"media:hr:policy.pdf", "media:public:guide.pdf"}
    assert skips.formats() == {".png": 1}
    policy = documents["media:hr:policy.pdf"]
    assert policy.kind is RemoteDocumentKind.FILE
    assert policy.module == "media"
    assert policy.filename == "policy.pdf" and policy.title == "policy.pdf"
    assert policy.path == "hr"
    assert policy.url == f"{server.base}lib/exe/fetch.php?media=hr:policy.pdf"
    assert policy.allowed_emails == {
        "admin@example.com",
        "maria@example.com",
        "petr@example.com",
    }
    assert documents["media:public:guide.pdf"].allowed_emails == EVERYONE


async def test_large_media_is_skipped_in_listing(server: FakeDokuWiki) -> None:
    server.add_media("public:big.pdf", b"%PDF" + b"x" * MAX_BYTES)
    with counting_skips() as skips:
        documents = await listed(make_adapter(server), ("media",))
    assert "media:public:big.pdf" not in documents
    assert skips.too_large == {"public:big.pdf"}


async def test_unknown_module_lists_nothing(server: FakeDokuWiki) -> None:
    assert await listed(make_adapter(server), ("other",)) == {}
    assert server.calls == []


async def test_fetch_page_html(server: FakeDokuWiki) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter, ("pages",))
    content = await adapter.fetch(documents["page:hr:salaries"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedPage)
    assert content.html == "<h1>Зарплаты</h1><p>Оклады в марте.</p>"


async def test_fetch_media_file(server: FakeDokuWiki) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter, ("media",))
    content = await adapter.fetch(documents["media:hr:policy.pdf"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedFile)
    assert content.data == b"%PDF-1.4 policy"
    assert content.filename == "policy.pdf"


async def test_fetch_limits_and_errors(server: FakeDokuWiki) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter)
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(documents["page:hr:salaries"], max_bytes=10)
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(documents["media:public:guide.pdf"], max_bytes=5)
    server.pages["start"]["html"] = "  \n"
    with pytest.raises(AdapterError, match="empty_page"):
        await adapter.fetch(documents["page:start"], max_bytes=MAX_BYTES)
    del server.pages["sales:plan"]
    with pytest.raises(AdapterError) as caught:
        await adapter.fetch(documents["page:sales:plan"], max_bytes=MAX_BYTES)
    assert caught.value.code == "not_found" and not caught.value.retryable
    del server.media["hr:policy.pdf"]
    with pytest.raises(AdapterError, match="not_found"):
        await adapter.fetch(documents["media:hr:policy.pdf"], max_bytes=MAX_BYTES)


async def test_media_response_over_the_limit_is_too_large(
    server: FakeDokuWiki,
) -> None:
    """Размер в листинге мог устареть: ответ больше лимита обрывается на
    чтении и считается слишком большим документом."""
    adapter = make_adapter(server)
    documents = await listed(adapter, ("media",))
    server.media["public:guide.pdf"]["data"] = b"%PDF" + b"y" * (300 * 1024)
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(documents["media:public:guide.pdf"], max_bytes=100)


async def test_new_revision_changes_the_version(server: FakeDokuWiki) -> None:
    before = await listed(make_adapter(server))
    server.pages["public:vacation"]["revision"] = 1_770_000_000
    server.media["public:guide.pdf"]["revision"] = 1_770_000_000
    after = await listed(make_adapter(server))
    assert (
        after["page:public:vacation"].version != before["page:public:vacation"].version
    )
    assert after["media:public:guide.pdf"].version == "1770000000"
    assert after["page:start"].version == before["page:start"].version


async def test_unknown_document_is_refused(server: FakeDokuWiki) -> None:
    document = RemoteDocument(
        external_id="other:x",
        title="x",
        url="https://wiki.example.com/",
        version="1",
        kind=RemoteDocumentKind.PAGE,
        module="pages",
    )
    with pytest.raises(AdapterError, match="unknown_document"):
        await make_adapter(server).fetch(document, max_bytes=MAX_BYTES)
