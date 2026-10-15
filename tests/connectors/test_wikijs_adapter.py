"""Адаптер Wiki.js 2.x (режим organization): спецификация, клиент GraphQL
и его ошибки, зеркало правил страниц (как checkAccess самого Wiki.js),
окно публикации, локали, содержимое."""

from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    FetchedMarkdown,
    FetchedPage,
    RemoteDocument,
)
from corp_ed.connectors.registry import AdapterRegistry
from corp_ed.connectors.wikijs import WIKIJS_SPEC, register
from corp_ed.connectors.wikijs import rules as rules_module
from corp_ed.connectors.wikijs.adapter import WikiJsAdapter, build_adapter
from corp_ed.connectors.wikijs.client import WikiJsClient
from corp_ed.connectors.wikijs.rules import (
    Group,
    Page,
    PageRule,
    Verdict,
    can_read,
    portable_regex,
)
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind
from tests.connectors.fake_wikijs import (
    API_KEY,
    MEMBER_KEY,
    REVOKED_KEY,
    SAMPLE_READERS,
    FakeWikiJs,
    rule,
    sample_wikijs,
)
from tests.fake_connector import public_resolver

KEY = Fernet.generate_key().decode()
MAX_BYTES = 1024 * 1024


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
    server: FakeWikiJs,
    key: str = API_KEY,
    *,
    sleep: Sleeps | None = None,
    recorder: Any = None,
) -> WikiJsAdapter:
    client = WikiJsClient(
        server.client(),
        base_url=server.base,
        token=key,
        sleep=sleep or Sleeps(),
        recorder=recorder,
    )
    return WikiJsAdapter(client, max_bytes=MAX_BYTES)


async def listed(adapter: WikiJsAdapter) -> dict[str, RemoteDocument]:
    return {d.external_id: d async for d in adapter.list(["pages"])}


def emails(*logins: str) -> frozenset[str]:
    return frozenset(f"{login}@example.com" for login in logins)


@pytest.fixture
def server() -> FakeWikiJs:
    return sample_wikijs()


# --- спецификация и сборка -----------------------------------------------------------


def test_spec_is_organization_preview_with_admin_key() -> None:
    spec = WIKIJS_SPEC
    assert spec.kind == "wikijs"
    assert spec.mode is ConnectorMode.ORGANIZATION and not spec.oauth
    assert spec.preview and spec.base
    assert [m.name for m in spec.modules] == ["pages"]
    assert [(f.name, f.required) for f in spec.config_fields] == [("base_url", True)]
    assert [(f.name, f.secret) for f in spec.credential_fields] == [("token", True)]
    assert spec.url_field == "base_url"


def test_kind_is_offered_only_when_enabled() -> None:
    hidden = AdapterRegistry()
    register(hidden, settings())
    assert hidden.kinds() == []
    assert hidden.spec("wikijs").preview
    enabled = AdapterRegistry(enabled_preview_kinds=frozenset({"wikijs"}))
    register(enabled, settings())
    assert [s.kind for s in enabled.kinds()] == ["wikijs"]


@pytest.mark.parametrize(
    ("address", "root"),
    [
        ("https://wiki.example.com", "https://wiki.example.com/"),
        ("https://wiki.example.com/graphql", "https://wiki.example.com/"),
        ("https://wiki.example.com/kb/", "https://wiki.example.com/kb/"),
    ],
)
def test_address_is_normalised(server: FakeWikiJs, address: str, root: str) -> None:
    registry = AdapterRegistry()
    register(registry, settings())
    adapter = registry.build(
        "wikijs", {"base_url": address}, {"token": API_KEY}, server.client()
    )
    assert isinstance(adapter, WikiJsAdapter)
    assert adapter.root == root


@pytest.mark.parametrize("address", ["", "http://wiki.example.com", "ftp://x"])
def test_build_rejects_bad_address(server: FakeWikiJs, address: str) -> None:
    with pytest.raises(AdapterConfigError, match="base_url_invalid"):
        build_adapter(
            WIKIJS_SPEC,
            {"base_url": address},
            {"token": API_KEY},
            server.client(),
            settings(),
        )


def test_build_requires_a_key(server: FakeWikiJs) -> None:
    with pytest.raises(AdapterAuthError, match="credentials_missing"):
        build_adapter(
            WIKIJS_SPEC, {"base_url": server.base}, {}, server.client(), settings()
        )


# --- проверка ключа и ошибки клиента ------------------------------------------------


async def test_check_accepts_full_access_key(server: FakeWikiJs) -> None:
    adapter = make_adapter(server)
    await adapter.check()
    assert adapter.version == "2.5.316"
    assert server.operations() == ["check"]


async def test_group_key_is_refused(server: FakeWikiJs) -> None:
    """Ключ группы «Сотрудники»: профиль есть, системной информации нет."""
    with pytest.raises(AdapterAuthError, match="admin_required"):
        await make_adapter(server, MEMBER_KEY).check()


async def test_unknown_key_is_served_as_guest_and_refused(server: FakeWikiJs) -> None:
    with pytest.raises(AdapterAuthError, match="unauthorized"):
        await make_adapter(server, "not-a-key").check()


async def test_revoked_key_is_an_auth_error(server: FakeWikiJs) -> None:
    sleeps = Sleeps()
    with pytest.raises(AdapterAuthError, match="unauthorized"):
        await make_adapter(server, REVOKED_KEY, sleep=sleeps).check()
    # 500 с «API Key is invalid» не повторяется.
    assert sleeps.calls == []


async def test_disabled_api_stops_the_connector(server: FakeWikiJs) -> None:
    server.api_enabled = False
    with pytest.raises(AdapterConfigError, match="api_disabled"):
        await make_adapter(server).check()


async def test_rate_limit_and_gateway_errors_are_retried(server: FakeWikiJs) -> None:
    server.rate_limited = 1
    server.server_errors = 2
    sleeps = Sleeps()
    await make_adapter(server, sleep=sleeps).check()
    assert sleeps.calls == [1.0, 2.0, 3.0]


async def test_persistent_server_errors_are_retryable(server: FakeWikiJs) -> None:
    server.server_errors = 100
    with pytest.raises(AdapterError) as caught:
        await make_adapter(server).check()
    assert caught.value.code == "http_502" and caught.value.retryable
    assert server.server_errors == 100 - 5


@pytest.mark.parametrize(
    ("status", "code"), [(401, "unauthorized"), (403, "forbidden")]
)
async def test_proxy_refusals_are_auth_errors(status: int, code: str) -> None:
    """401/403 Wiki.js сам не шлёт, но их отдаёт прокси с входом перед ним;
    текст ответа в код не попадает."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="<html>secret!</html>")

    client = WikiJsClient(
        OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            resolver=public_resolver,
        ),
        base_url="https://wiki.example.com",
        token=API_KEY,
    )
    with pytest.raises(AdapterAuthError) as caught:
        await client.query("check", "{ x }")
    assert caught.value.code == code
    assert "secret" not in str(caught.value)


async def test_schema_rejection_and_wrong_address() -> None:
    server = FakeWikiJs()
    adapter = make_adapter(server)
    with pytest.raises(AdapterError) as caught:
        await adapter._client.query("x", "{ nonsense }")
    assert caught.value.code == "query_rejected" and not caught.value.retryable
    other = WikiJsClient(server.client(), base_url=f"{server.base}wiki/", token=API_KEY)
    with pytest.raises(AdapterConfigError, match="graphql_not_found"):
        await other.query("check", "{ x }")


async def test_recorder_keeps_no_key_and_no_stack(server: FakeWikiJs) -> None:
    recorded: list[tuple[str, Any, Any]] = []
    adapter = make_adapter(
        server, "not-a-key", recorder=lambda *entry: recorded.append(entry)
    )
    with pytest.raises(AdapterAuthError):
        await adapter.check()
    dumped = repr(recorded)
    assert "not-a-key" not in dumped
    assert "stacktrace" not in dumped
    assert recorded[0][2]["errors"][0] == {
        "path": ["users", "profile"],
        "code": 1019,
        "forbidden": False,
    }


# --- права ----------------------------------------------------------------------------


async def test_listing_mirrors_wikijs_page_rules(server: FakeWikiJs) -> None:
    documents = await listed(make_adapter(server))
    assert set(documents) == {f"page:{n}" for n in SAMPLE_READERS}
    for page_id, allowed in SAMPLE_READERS.items():
        document = documents[f"page:{page_id}"]
        if allowed is None:
            assert document.visibility is MaterialVisibility.TENANT, page_id
            assert document.allowed_emails == frozenset(), page_id
        else:
            assert document.visibility is MaterialVisibility.RESTRICTED, page_id
            # Почта «anna@Example.com» — в нижнем регистре; olga отключена.
            assert document.allowed_emails == emails(*allowed), page_id


async def test_document_fields(server: FakeWikiJs) -> None:
    documents = await listed(make_adapter(server))
    vacation = documents["page:2"]
    assert vacation.title == "Отпуск"
    assert vacation.kind is RemoteDocumentKind.PAGE
    assert vacation.module == "pages"
    assert vacation.url == f"{server.base}en/general/vacation"
    assert vacation.path == "en/general"
    assert vacation.version == "2026-10-01T10:00:00.000Z"
    assert vacation.modified_at is not None
    russian = documents["page:9"]
    assert russian.url == f"{server.base}ru/general/otpusk"
    assert russian.path == "ru/general"


async def test_new_installation_guests_see_everything() -> None:
    """Правило гостей по умолчанию (START "") — вся вики без входа."""
    server = sample_wikijs()
    server.groups[2]["pageRules"] = [rule("START", "")]
    documents = await listed(make_adapter(server))
    assert {d.visibility for d in documents.values()} == {MaterialVisibility.TENANT}


async def test_guest_through_unsure_regex_is_not_company_wide(
    server: FakeWikiJs,
) -> None:
    server.groups[2]["pageRules"] = [rule("REGEX", r"^(?<p>public)/")]
    documents = await listed(make_adapter(server))
    welcome = documents["page:1"]
    assert welcome.visibility is MaterialVisibility.RESTRICTED
    assert welcome.allowed_emails == emails("admin", "ivan", "anna", "petr")


async def test_rule_change_changes_readers_without_new_version(
    server: FakeWikiJs,
) -> None:
    before = await listed(make_adapter(server))
    server.members[4].discard(4)
    after = await listed(make_adapter(server))
    assert after["page:3"].allowed_emails == emails("admin")
    assert after["page:3"].version == before["page:3"].version


async def test_user_without_groups_sees_nothing(server: FakeWikiJs) -> None:
    server.add_user(7, "nobody")
    documents = await listed(make_adapter(server))
    assert all("nobody@example.com" not in d.allowed_emails for d in documents.values())


async def test_group_key_cannot_list(server: FakeWikiJs) -> None:
    with pytest.raises(AdapterAuthError, match="admin_required"):
        await listed(make_adapter(server, MEMBER_KEY))


# --- страницы: публикация, окно, пакеты ---------------------------------------------


async def test_unpublished_private_and_outside_window_are_skipped(
    server: FakeWikiJs,
) -> None:
    server.add_page(20, "general/private", "Личная", "x", private=True)
    server.add_page(21, "general/soon", "Скоро", "x", start="2999-01-01T00:00:00Z")
    server.add_page(22, "general/old", "Старое", "x", end="2001-01-01T00:00:00Z")
    server.add_page(23, "general/bad", "Битая дата", "x", start="когда-нибудь")
    server.add_page(
        24,
        "general/now",
        "Сейчас",
        "x",
        start="2001-01-01T00:00:00.000Z",
        end="2999-01-01T00:00:00.000Z",
    )
    documents = await listed(make_adapter(server))
    assert "page:10" not in documents
    assert {"page:20", "page:21", "page:22", "page:23"}.isdisjoint(documents)
    assert "page:24" in documents


async def test_publish_windows_are_read_in_batches(server: FakeWikiJs) -> None:
    for n in range(100, 220):
        server.add_page(n, f"general/p{n}", f"Страница {n}", "x")
    for n in range(10, 40):
        server.add_group(n, f"Группа {n}", [], [])
    documents = await listed(make_adapter(server))
    assert len(documents) == len(SAMPLE_READERS) + 120
    assert server.operations().count("pages.list") == 1
    assert server.operations().count("pages.batch") == 3
    assert server.operations().count("groups.batch") == 2


async def test_page_deleted_during_listing_is_skipped(server: FakeWikiJs) -> None:
    server.vanish_after_list = {4}
    documents = await listed(make_adapter(server))
    assert "page:4" not in documents
    assert "page:2" in documents


async def test_other_modules_are_ignored(server: FakeWikiJs) -> None:
    adapter = make_adapter(server)
    assert [d async for d in adapter.list(["attachments"])] == []
    assert server.calls == []


# --- содержимое ---------------------------------------------------------------------


async def test_fetch_returns_rendered_html_without_anchors(server: FakeWikiJs) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter)
    content = await adapter.fetch(documents["page:2"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedPage)
    assert "¶" not in content.html
    assert content.html.count("Отпуск</h1>") == 1
    assert "<p>Отпуск — 28 дней.</p>" in content.html


async def test_fetch_adds_title_when_render_has_none(server: FakeWikiJs) -> None:
    server.pages[2]["render"] = "<p>Только текст.</p>"
    adapter = make_adapter(server)
    documents = await listed(adapter)
    content = await adapter.fetch(documents["page:2"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedPage)
    assert content.html.startswith("<h1>Отпуск</h1>")


async def test_fetch_falls_back_to_source(server: FakeWikiJs) -> None:
    server.pages[2]["render"] = ""
    server.add_page(30, "general/html", "Визуальная", "x", content_type="html")
    server.pages[30].update(render="", content="<p>Из визуального редактора.</p>")
    adapter = make_adapter(server)
    documents = await listed(adapter)
    markdown = await adapter.fetch(documents["page:2"], max_bytes=MAX_BYTES)
    assert isinstance(markdown, FetchedMarkdown)
    assert markdown.markdown == "# Отпуск\n\nОтпуск — 28 дней."
    html = await adapter.fetch(documents["page:30"], max_bytes=MAX_BYTES)
    assert isinstance(html, FetchedPage)
    assert html.html.startswith("<h1>Визуальная</h1>")


async def test_fetch_errors(server: FakeWikiJs) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter)
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(documents["page:2"], max_bytes=10)
    server.pages[2].update(render=" ", content=" ")
    with pytest.raises(AdapterError, match="empty_page"):
        await adapter.fetch(documents["page:2"], max_bytes=MAX_BYTES)
    del server.pages[3]
    with pytest.raises(AdapterError, match="not_found"):
        await adapter.fetch(documents["page:3"], max_bytes=MAX_BYTES)
    with pytest.raises(AdapterError, match="unknown_document"):
        await adapter.fetch(
            RemoteDocument("doc:1", "x", "u", "v", RemoteDocumentKind.PAGE, "pages"),
            max_bytes=MAX_BYTES,
        )


async def test_edit_changes_version(server: FakeWikiJs) -> None:
    before = await listed(make_adapter(server))
    server.pages[2]["updatedAt"] = "2026-10-15T09:00:00.000Z"
    after = await listed(make_adapter(server))
    assert after["page:2"].version != before["page:2"].version
    assert after["page:3"].version == before["page:3"].version


# --- правила страниц (rules.py) ------------------------------------------------------


def group(
    group_id: int,
    *items: dict[str, Any],
    permissions: tuple[str, ...] = ("read:pages",),
) -> Group:
    parsed = [PageRule.parse(item) for item in items]
    return Group(
        group_id,
        frozenset(permissions),
        tuple(r for r in parsed if r is not None),
        opaque=any(r is None for r in parsed),
    )


def page(path: str, locale: str = "en", *tags: str) -> Page:
    return Page(path, locale, frozenset(tags))


def test_equal_length_deny_beats_allow_across_groups() -> None:
    allow = group(1, rule("START", "hr"))
    deny = group(2, rule("START", "hr", deny=True), permissions=())
    assert can_read([allow, deny], page("hr/x")) is Verdict.NO
    assert can_read([deny, allow], page("hr/x")) is Verdict.NO


def test_longer_path_wins_whatever_it_says() -> None:
    staff = group(1, rule("START", ""), rule("START", "hr", deny=True))
    assert can_read([staff], page("hr/x")) is Verdict.NO
    assert (
        can_read([staff, group(2, rule("START", "hr/"))], page("hr/x")) is Verdict.YES
    )
    assert can_read([staff], page("general/x")) is Verdict.YES


def test_type_rank_at_equal_length() -> None:
    # EXACT > TAG > REGEX > END > START при одинаковой длине пути правила.
    exact = group(1, rule("EXACT", "a/b"), rule("START", "a/b", deny=True))
    assert can_read([exact], page("a/b")) is Verdict.YES
    tag = group(1, rule("TAG", "abc"), rule("START", "a/b", deny=True))
    assert can_read([tag], page("a/b/c", "en", "abc")) is Verdict.YES
    end = group(1, rule("END", "b/c"), rule("START", "a/b", deny=True))
    assert can_read([end], page("a/b/c")) is Verdict.YES
    start = group(1, rule("START", "a/b"), rule("END", "b/c", deny=True))
    assert can_read([start], page("a/b/c")) is Verdict.NO


def test_start_is_a_string_prefix_and_end_a_suffix() -> None:
    hr = group(1, rule("START", "hr"))
    assert can_read([hr], page("hr-old/x")) is Verdict.YES
    assert can_read([hr], page("old/hr")) is Verdict.NO
    assert can_read([group(1, rule("END", "/faq"))], page("it/faq")) is Verdict.YES


def test_locales_limit_rules() -> None:
    staff = group(1, rule("START", ""), rule("START", "d", deny=True, locales=("ru",)))
    assert can_read([staff], page("d/x", "en")) is Verdict.YES
    assert can_read([staff], page("d/x", "ru")) is Verdict.NO


def test_permissions_come_from_any_group() -> None:
    rules_only = group(1, rule("START", ""), permissions=())
    assert can_read([rules_only], page("x")) is Verdict.NO
    perms_only = group(2, permissions=("read:pages",))
    assert can_read([perms_only], page("x")) is Verdict.NO
    assert can_read([rules_only, perms_only], page("x")) is Verdict.YES
    admin = group(3, rule("START", "", deny=True), permissions=("manage:system",))
    assert can_read([admin], page("x")) is Verdict.YES


def test_rules_for_other_roles_are_ignored() -> None:
    comments = {**rule("START", "x", deny=True), "roles": ["read:comments"]}
    staff = group(1, rule("START", ""), comments)
    assert can_read([staff], page("x/y")) is Verdict.YES


def test_specificity_counts_utf16_units() -> None:
    # «😀» — две единицы UTF-16 (String.length в JavaScript), в Python — 1.
    allow = group(1, rule("START", "😀"))
    shorter = group(2, rule("END", "b", deny=True), permissions=())
    # 2 > 1 — разрешение точнее; по len() было бы 1 = 1 и END > START — запрет.
    assert can_read([allow, shorter], page("😀b")) is Verdict.YES
    same = group(3, rule("END", "ab", deny=True), permissions=())
    assert can_read([allow, same], page("😀ab")) is Verdict.NO


@pytest.mark.parametrize(
    "pattern",
    [r"^reports/\d{4}$", r"^(hr|finance)/", r"budget", r"^a.*b$", r"\.pdf$", "^отдел/"],
)
def test_simple_regex_is_portable(pattern: str) -> None:
    assert portable_regex(pattern) is not None


@pytest.mark.parametrize(
    "pattern",
    [
        r"^(?<year>\d{4})",
        r"(?=a)",
        r"(?i)hr",
        r"(a+)+$",
        r"\s+",
        r"\p{L}",
        r"(a)\1",
        r"[^]",
        r"[]a]",
        r"a{,3}",
        r".*.*.*",
        r"[",
        "a" * 300,
    ],
)
def test_unportable_regex_is_unknown(pattern: str) -> None:
    assert portable_regex(pattern) is None


def test_portable_regex_matches_like_javascript() -> None:
    reports = group(1, rule("REGEX", r"^reports/\d{4}$"))
    assert can_read([reports], page("reports/2026")) is Verdict.YES
    assert can_read([reports], page("reports/2026/q1")) is Verdict.NO
    # \d без re.ASCII в Python совпал бы с арабскими цифрами.
    assert can_read([reports], page("reports/٢٠٢٦")) is Verdict.NO


def test_unknown_regex_allow_grants_nothing() -> None:
    unsure = group(1, rule("REGEX", r"(?<x>hr)/"))
    assert can_read([unsure], page("hr/x")) is Verdict.UNSURE
    # Разрешение и без него — точно читает.
    covered = group(1, rule("START", "hr/x"), rule("REGEX", r"(?<x>hr)/"))
    assert can_read([covered], page("hr/x")) is Verdict.YES


def test_unknown_regex_deny_counts_as_matched() -> None:
    staff = group(1, rule("START", ""), rule("REGEX", r"(?<x>hr)", deny=True))
    assert can_read([staff], page("hr/x")) is Verdict.UNSURE
    # Запрет короче более точного разрешения — исход не важен.
    precise = group(2, rule("START", "hr/salaries/2026"))
    assert can_read([staff, precise], page("hr/salaries/2026")) is Verdict.YES


def test_unknown_rule_type_closes_the_group() -> None:
    odd = group(1, rule("START", ""), {**rule("START", "x"), "match": "GLOB"})
    assert odd.opaque
    assert can_read([odd], page("y")) is Verdict.UNSURE


def test_regex_cache_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    original = rules_module._translate

    def counting(source: str) -> Any:
        calls.append(source)
        return original(source)

    monkeypatch.setattr(rules_module, "_compiled", {})
    monkeypatch.setattr(rules_module, "_translate", counting)
    for _ in range(3):
        portable_regex("^x/")
    assert calls == ["^x/"]
