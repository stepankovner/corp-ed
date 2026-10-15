"""Адаптер BookStack (режим organization): каталог, клиент REST, право
«просмотр» как в BookStack (роли, «Все остальные», наследование
книга → глава → страница, Admin, «своё»), страницы и вложения, лимиты."""

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
from corp_ed.connectors.bookstack.adapter import (
    SPEC,
    BookStackAdapter,
    build_adapter,
    register,
)
from corp_ed.connectors.bookstack.client import BookStackClient
from corp_ed.connectors.bookstack.permissions import (
    EXPLICIT_ALLOW,
    EXPLICIT_DENY,
    IMPLICIT_ALLOW,
    IMPLICIT_DENY,
    Role,
    Rules,
    parse_rules,
    role_status,
)
from corp_ed.connectors.common import counting_skips
from corp_ed.connectors.registry import AdapterRegistry, default_registry
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind
from tests.connectors.fake_bookstack import (
    ADMIN_TOKEN,
    EDITOR_TOKEN,
    HR_ROLE,
    NO_API_TOKEN,
    STAFF_ROLE,
    FakeBookStack,
    sample_bookstack,
)
from tests.fake_connector import public_resolver

KEY = Fernet.generate_key().decode()
MAX_BYTES = 1024 * 1024
ADMIN = "admin@example.com"
IVAN = "ivan@example.com"
MARIA = "maria@example.com"
OLGA = "olga@example.com"


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
    server: FakeBookStack,
    token: tuple[str, str] = ADMIN_TOKEN,
    *,
    sleep: Sleeps | None = None,
) -> BookStackAdapter:
    client = BookStackClient(
        server.client(),
        base_url=server.base,
        token_id=token[0],
        token_secret=token[1],
        sleep=sleep or Sleeps(),
    )
    return BookStackAdapter(client, max_bytes=MAX_BYTES)


async def listed(
    adapter: BookStackAdapter, modules: tuple[str, ...] = ("pages",)
) -> dict[str, RemoteDocument]:
    return {d.title: d async for d in adapter.list(list(modules))}


@pytest.fixture
def server() -> FakeBookStack:
    return sample_bookstack()


# --- каталог и сборка ---------------------------------------------------------------


def test_spec_is_organization_preview_with_token_pair() -> None:
    assert SPEC.kind == "bookstack"
    assert SPEC.mode is ConnectorMode.ORGANIZATION and not SPEC.oauth
    # Живьём проверен только стенд: в каталог — после проверки у клиента.
    assert SPEC.preview and SPEC.base
    assert [m.name for m in SPEC.modules] == ["pages", "attachments"]
    assert [(f.name, f.required) for f in SPEC.config_fields] == [("base_url", True)]
    assert [(f.name, f.secret) for f in SPEC.credential_fields] == [
        ("token_id", True),
        ("token_secret", True),
    ]
    assert SPEC.url_field == "base_url"


def test_kind_is_offered_only_when_enabled() -> None:
    registry = AdapterRegistry()
    register(registry, settings())
    assert not registry.offered("bookstack")
    assert registry.spec("bookstack").preview
    enabled = AdapterRegistry(enabled_preview_kinds=frozenset({"bookstack"}))
    register(enabled, settings())
    assert [s.kind for s in enabled.kinds()] == ["bookstack"]
    # В общий реестр вид подключает интегратор.
    assert "bookstack" not in {s.kind for s in default_registry(settings()).kinds()}


@pytest.mark.parametrize(
    ("address", "root"),
    [
        ("https://wiki.company.ru", "https://wiki.company.ru/"),
        ("https://wiki.company.ru/api/", "https://wiki.company.ru/"),
        ("https://company.ru/wiki/", "https://company.ru/wiki/"),
    ],
)
def test_address_is_normalised(server: FakeBookStack, address: str, root: str) -> None:
    registry = AdapterRegistry()
    register(registry, settings())
    adapter = registry.build(
        "bookstack",
        {"base_url": address},
        {"token_id": ADMIN_TOKEN[0], "token_secret": ADMIN_TOKEN[1]},
        server.client(),
    )
    assert isinstance(adapter, BookStackAdapter)
    assert adapter.root == root


def test_build_requires_both_token_parts(server: FakeBookStack) -> None:
    with pytest.raises(AdapterAuthError, match="credentials_missing"):
        build_adapter(
            SPEC,
            {"base_url": server.base},
            {"token_id": ADMIN_TOKEN[0]},
            server.client(),
            settings(),
        )


@pytest.mark.parametrize("address", ["", "http://wiki.company.ru", "ftp://x"])
def test_build_rejects_non_https_address(server: FakeBookStack, address: str) -> None:
    with pytest.raises(AdapterConfigError, match="base_url_invalid"):
        build_adapter(
            SPEC,
            {"base_url": address},
            {"token_id": "a", "token_secret": "b"},
            server.client(),
            settings(),
        )


# --- клиент и проверка ------------------------------------------------------------


async def test_check_accepts_admin_token(server: FakeBookStack) -> None:
    await make_adapter(server).check()
    assert server.paths() == ["books", "users", "roles"]
    assert server.calls[0][1] == {"count": "1"}


async def test_check_errors(server: FakeBookStack) -> None:
    with pytest.raises(AdapterAuthError, match="unauthorized"):
        await make_adapter(server, ("wrong", "token")).check()
    with pytest.raises(AdapterAuthError, match="api_access_denied"):
        await make_adapter(server, NO_API_TOKEN).check()
    with pytest.raises(AdapterAuthError, match="admin_required"):
        await make_adapter(server, EDITOR_TOKEN).check()
    server.no_roles_api = True
    with pytest.raises(AdapterConfigError, match="bookstack_version_unsupported"):
        await make_adapter(server).check()


async def test_token_header_format() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"data": [], "total": 0})

    client = BookStackClient(
        OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            resolver=public_resolver,
        ),
        base_url="https://wiki.example.com",
        token_id="abc",
        token_secret="xyz",
    )
    await client.get("books")
    assert seen == ["Token abc:xyz"]


async def test_rate_limit_honours_retry_after(server: FakeBookStack) -> None:
    server.rate_limited["books"] = 2
    sleeps = Sleeps()
    await make_adapter(server, sleep=sleeps).check()
    assert sleeps.calls == [7.0, 7.0]


async def test_rate_limit_wait_is_capped_and_bounded(server: FakeBookStack) -> None:
    server.rate_limited["books"] = 100
    server.retry_after = "600"
    sleeps = Sleeps()
    with pytest.raises(AdapterError) as caught:
        await make_adapter(server, sleep=sleeps).check()
    assert caught.value.code == "rate_limited" and caught.value.retryable
    assert sleeps.calls == [60.0] * 5


async def test_server_errors_are_retried_then_retryable(server: FakeBookStack) -> None:
    server.server_errors = 2
    await make_adapter(server).check()
    server.server_errors = 100
    with pytest.raises(AdapterError) as caught:
        await make_adapter(server).check()
    assert caught.value.code == "http_502" and caught.value.retryable


async def test_html_instead_of_json_and_error_details_do_not_leak() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/books"):
            return httpx.Response(200, text="<html>login</html>")
        return httpx.Response(
            500, json={"error": {"code": 500, "message": "secret details"}}
        )

    client = BookStackClient(
        OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            resolver=public_resolver,
        ),
        base_url="https://wiki.example.com",
        token_id="a",
        token_secret="b",
        sleep=Sleeps(),
    )
    with pytest.raises(AdapterError, match="not_json"):
        await client.get("books")
    with pytest.raises(AdapterError) as caught:
        await client.get("users")
    assert caught.value.code == "http_500" and caught.value.retryable
    assert "secret" not in str(caught.value)


# --- право «просмотр» ---------------------------------------------------------------


def _role(role_id: int, *, admin: bool = False, view_all: bool = False) -> Role:
    return Role(role_id, "admin" if admin else "", view_all, False, frozenset())


def test_role_status_follows_bookstack_evaluator() -> None:
    staff = _role(10)
    editor = _role(2, view_all=True)
    page = Rules({}, None)
    chapter = Rules({10: False}, None)
    book = Rules({10: True}, False)
    # Ближайшая запись роли решает: глава запрещает, хотя книга разрешает.
    assert role_status(staff, [page, chapter, book]) == (EXPLICIT_DENY, False)
    # «Все остальные» книги — роль без записи.
    assert role_status(editor, [page, chapter, book]) == (IMPLICIT_DENY, False)
    # «Все остальные» ближе записи роли — она уже не действует.
    assert role_status(staff, [Rules({}, True), book]) == (IMPLICIT_ALLOW, False)
    # Запись роли сильнее «Все остальные» того же уровня.
    assert role_status(staff, [Rules({10: True}, False)]) == (EXPLICIT_ALLOW, False)
    # Записей нет — системные права роли.
    assert role_status(editor, [page]) == (IMPLICIT_ALLOW, False)
    assert role_status(staff, [page]) == (IMPLICIT_DENY, False)
    # Admin видит всё, даже при явном запрете.
    assert role_status(_role(1, admin=True), [Rules({1: False}, False)]) == (
        EXPLICIT_ALLOW,
        False,
    )


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"role_permissions": {}, "fallback_permissions": {"inheriting": True}},
        {"role_permissions": [], "fallback_permissions": None},
        {
            "role_permissions": [{"role_id": 2, "view": 1}],
            "fallback_permissions": {"inheriting": True},
        },
        {
            "role_permissions": [{"role_id": "2", "view": True}],
            "fallback_permissions": {"inheriting": True},
        },
        {"role_permissions": [], "fallback_permissions": {"inheriting": False}},
        {
            "role_permissions": [],
            "fallback_permissions": {"inheriting": "no", "view": True},
        },
    ],
)
def test_unknown_rules_shape_is_not_guessed(data: dict[str, Any]) -> None:
    assert parse_rules(data) is None


async def test_permissions_mirror_bookstack(server: FakeBookStack) -> None:
    documents = await listed(make_adapter(server))
    restricted = {
        title: d.allowed_emails
        for title, d in documents.items()
        if d.visibility is MaterialVisibility.RESTRICTED
    }
    assert restricted == {
        # Переопределение на странице общей книги.
        "Премии": {ADMIN, MARIA},
        # Закрытая книга роли «Кадры».
        "Зарплаты": {ADMIN, MARIA},
        # Явный запрет «Сотрудникам» снят явным правом «Кадров» по книге.
        "Увольнения": {ADMIN, MARIA},
        # Без прав содержимого: Editor (view-all); гость не в счёт.
        "План": {ADMIN, IVAN},
        # «Своё»: автор видит свою страницу.
        "Заметки": {ADMIN, IVAN, OLGA},
        # Явный запрет одной роли перевешивает просмотр по умолчанию другой.
        "Смета": {ADMIN},
    }
    # Роль «Сотрудники» — у всех, и страницу видит каждый: вся компания.
    for title in ("Отпуск", "Суточные", "Оргструктура"):
        assert documents[title].visibility is MaterialVisibility.TENANT, title
        assert documents[title].allowed_emails == frozenset(), title


async def test_documents_carry_path_url_and_version(server: FakeBookStack) -> None:
    documents = await listed(make_adapter(server))
    trips = documents["Суточные"]
    assert trips.kind is RemoteDocumentKind.PAGE
    assert trips.module == "pages"
    assert trips.path == "Общая/Командировки"
    assert trips.url == f"{server.base}books/%D0%BE%D0%B1%D1%89%D0%B0%D1%8F/page/" + (
        "%D1%81%D1%83%D1%82%D0%BE%D1%87%D0%BD%D1%8B%D0%B5"
    )
    assert trips.version == "1:2026-09-01T10:00:00.000000Z"
    assert trips.modified_at is not None
    assert trips.external_id.startswith("page:")
    assert documents["Отпуск"].path == "Общая"


async def test_drafts_and_templates_are_skipped(server: FakeBookStack) -> None:
    documents = await listed(make_adapter(server))
    assert "Черновик" not in documents and "Шаблон" not in documents
    assert len(documents) == 9


async def test_no_universal_role_means_restricted_lists(server: FakeBookStack) -> None:
    """Пользователь вне «Сотрудников»: общей роли нет — «вся компания» не
    ставится, даже если страницу видят все."""
    server.add_user(7, "new@example.com", HR_ROLE)
    documents = await listed(make_adapter(server))
    holiday = documents["Отпуск"]
    assert holiday.visibility is MaterialVisibility.RESTRICTED
    assert holiday.allowed_emails == {
        ADMIN,
        IVAN,
        MARIA,
        "petr@example.com",
        OLGA,
    }
    # Оргструктуру видит и он: явное право «Кадров» по книге.
    assert "new@example.com" in documents["Оргструктура"].allowed_emails


async def test_universal_role_with_a_veto_is_not_company_wide(
    server: FakeBookStack,
) -> None:
    """У всех есть «Сотрудники» с правом по умолчанию, но явный запрет
    роли «Кадры» на странице перевешивает его у maria."""
    holiday = next(p for p in server.pages.values() if p["name"] == "Отпуск")
    server.grant("page", holiday["id"], {HR_ROLE: False}, fallback=True)
    documents = await listed(make_adapter(server))
    found = documents["Отпуск"]
    assert found.visibility is MaterialVisibility.RESTRICTED
    assert MARIA not in found.allowed_emails
    assert {ADMIN, IVAN, OLGA} <= found.allowed_emails


async def test_rules_that_cannot_be_read_leave_only_admins(
    server: FakeBookStack,
) -> None:
    """Книга не видна токену (404 на права) или ответ незнаком — право
    доказано только у роли Admin."""
    hr = next(b["id"] for b in server.books.values() if b["name"] == "Кадры")
    server.hidden_from_token.add(f"book:{hr}")
    plan = next(p["id"] for p in server.pages.values() if p["name"] == "План")
    server.permissions[f"page:{plan}"] = {"role_permissions": "?"}
    documents = await listed(make_adapter(server))
    for title in ("Зарплаты", "Оргструктура", "Увольнения", "План"):
        assert documents[title].visibility is MaterialVisibility.RESTRICTED, title
        assert documents[title].allowed_emails == {ADMIN}, title
    assert documents["Отпуск"].visibility is MaterialVisibility.TENANT


async def test_locked_rules_stop_the_listing(server: FakeBookStack) -> None:
    page = next(iter(server.pages))
    server.locked.add(f"page:{page}")
    with pytest.raises(AdapterAuthError, match="admin_required"):
        await listed(make_adapter(server))


async def test_page_gone_between_listing_and_rules_is_skipped(
    server: FakeBookStack,
) -> None:
    page = next(p for p in server.pages.values() if p["name"] == "План")
    server.hidden_from_token.add(f"page:{page['id']}")
    documents = await listed(make_adapter(server))
    assert "План" not in documents and "Смета" in documents


async def test_book_and_chapter_rules_are_read_once(server: FakeBookStack) -> None:
    await listed(make_adapter(server))
    asked = [p for p in server.paths() if p.startswith("content-permissions/")]
    assert len(asked) == len(set(asked))
    # Страницы (9 + 0 пропущенных), главы (1) и книги (3).
    assert len(asked) == 9 + 1 + 3


async def test_lists_are_paged_and_sorted_by_id(server: FakeBookStack) -> None:
    common = next(iter(server.books))
    for n in range(230):
        server.add_page(common, f"Страница {n}", "<p>x</p>")
    for n in range(120):
        server.add_user(100 + n, f"user{n}@example.com", STAFF_ROLE)
    documents = await listed(make_adapter(server))
    assert len(documents) == 239
    pages = [p for path, p in server.calls if path == "pages"]
    assert [p["offset"] for p in pages] == ["0", "100", "200"]
    assert {p["sort"] for p in pages} == {"id"} and {p["count"] for p in pages} == {
        "100"
    }
    users = [p.get("offset") for path, p in server.calls if path == "users"]
    # Первый — из check (count=1).
    assert users == [None, "0", "100"]
    assert documents["Страница 0"].visibility is MaterialVisibility.TENANT


async def test_listing_without_modules_asks_nothing(server: FakeBookStack) -> None:
    assert await listed(make_adapter(server), ()) == {}
    assert server.calls == []


# --- содержимое ------------------------------------------------------------------


async def test_fetch_page_html_with_title(server: FakeBookStack) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter)
    content = await adapter.fetch(documents["Отпуск"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedPage)
    assert content.html == "<h1>Отпуск</h1>\n<p>Отпуск — 28 дней.</p>"


async def test_fetch_page_limits(server: FakeBookStack) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter)
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(documents["Отпуск"], max_bytes=10)
    page_id = int(documents["План"].external_id.removeprefix("page:"))
    server.html[page_id] = "   "
    with pytest.raises(AdapterError, match="empty_page"):
        await adapter.fetch(documents["План"], max_bytes=MAX_BYTES)
    with pytest.raises(AdapterError, match="unknown_document"):
        await adapter.fetch(
            RemoteDocument(
                "x:1", "x", "u", "v", RemoteDocumentKind.PAGE, module="pages"
            ),
            max_bytes=MAX_BYTES,
        )


async def test_huge_page_response_is_too_large(
    server: FakeBookStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter)
    page_id = int(documents["Отпуск"].external_id.removeprefix("page:"))
    server.html[page_id] = "<p>" + "x" * 4096 + "</p>"
    from corp_ed.core import outbound

    monkeypatch.setattr(outbound, "MAX_RESPONSE_BYTES", 1024)
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(documents["Отпуск"], max_bytes=MAX_BYTES)


async def test_attachments_get_their_page_rights(server: FakeBookStack) -> None:
    pages = {p["name"]: p["id"] for p in server.pages.values()}
    server.add_attachment(pages["Отпуск"], "памятка.txt", "Памятка.".encode())
    server.add_attachment(pages["Зарплаты"], "Ведомость", b"%PDF-1.4", extension="pdf")
    server.add_attachment(pages["Отпуск"], "Сайт", None, link="https://example.com/")
    server.add_attachment(pages["Отпуск"], "setup.exe", b"MZ")
    server.add_attachment(pages["Черновик"], "черновик.txt", b"draft")
    adapter = make_adapter(server)
    with counting_skips() as skips:
        documents = await listed(adapter, ("attachments",))
    assert set(documents) == {"памятка.txt", "Ведомость.pdf"}
    memo = documents["памятка.txt"]
    assert memo.kind is RemoteDocumentKind.FILE
    assert memo.module == "attachments"
    assert memo.filename == "памятка.txt"
    assert memo.path == "Общая/Отпуск"
    assert memo.url == f"{server.base}attachments/1"
    assert memo.visibility is MaterialVisibility.TENANT
    sheet = documents["Ведомость.pdf"]
    assert sheet.visibility is MaterialVisibility.RESTRICTED
    assert sheet.allowed_emails == {ADMIN, MARIA}
    assert skips.formats() == {".exe": 1}
    # Только вложения — страницы модуля pages не отдаются.
    assert not any(d.module == "pages" for d in documents.values())

    content = await adapter.fetch(memo, max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedFile)
    assert content.data == "Памятка.".encode()
    assert content.filename == "памятка.txt"


async def test_attachment_limits(server: FakeBookStack) -> None:
    page = next(p["id"] for p in server.pages.values() if p["name"] == "Отпуск")
    attachment = server.add_attachment(page, "big.txt", b"x" * 300)
    adapter = make_adapter(server)
    documents = await listed(adapter, ("attachments",))
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(documents["big.txt"], max_bytes=100)
    # Ответ сильно больше лимита обрывается, не дочитываясь.
    server.files[attachment] = b"x" * 400_000
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(documents["big.txt"], max_bytes=1000)
    # Вложение стало ссылкой после листинга.
    server.attachments[attachment]["external"] = True
    server.files[attachment] = b"https://example.com/"
    with pytest.raises(AdapterError, match="attachment_external"):
        await adapter.fetch(documents["big.txt"], max_bytes=MAX_BYTES)


async def test_broken_attachment_content(server: FakeBookStack) -> None:
    page = next(p["id"] for p in server.pages.values() if p["name"] == "Отпуск")
    server.add_attachment(page, "memo.txt", b"ok")
    adapter = make_adapter(server)
    documents = await listed(adapter, ("attachments",))

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/attachments/"):
            return httpx.Response(200, json={"external": False, "content": "@@@"})
        return server.handle(request)

    client = BookStackClient(
        OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(broken)),
            resolver=public_resolver,
        ),
        base_url=server.base,
        token_id=ADMIN_TOKEN[0],
        token_secret=ADMIN_TOKEN[1],
    )
    with pytest.raises(AdapterError, match="content_invalid"):
        await BookStackAdapter(client, max_bytes=MAX_BYTES).fetch(
            documents["memo.txt"], max_bytes=MAX_BYTES
        )


async def test_version_changes_with_revision(server: FakeBookStack) -> None:
    before = await listed(make_adapter(server))
    page_id = int(before["План"].external_id.removeprefix("page:"))
    server.pages[page_id]["revision_count"] = 2
    server.pages[page_id]["updated_at"] = "2026-10-01T10:00:00.000000Z"
    after = await listed(make_adapter(server))
    assert after["План"].version != before["План"].version
    assert after["План"].version == "2:2026-10-01T10:00:00.000000Z"
    assert after["Отпуск"].version == before["Отпуск"].version
