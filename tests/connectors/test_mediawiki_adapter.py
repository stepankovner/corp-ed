"""Адаптер MediaWiki (режим organization): спецификация, вход по
бот-паролю и сессия, ошибки API, читатели по сопоставлению «логин =
почта», пространства имён (и Lockdown), страницы, файлы, лимиты."""

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
from corp_ed.connectors.mediawiki import adapter as adapter_module
from corp_ed.connectors.mediawiki.adapter import (
    SPEC,
    MediaWikiAdapter,
    build_adapter,
    register,
)
from corp_ed.connectors.mediawiki.client import MediaWikiClient, api_endpoint
from corp_ed.connectors.mediawiki.rights import parse_user_emails
from corp_ed.connectors.registry import AdapterRegistry
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind
from tests.connectors.fake_mediawiki import (
    BOT,
    BOT_PASSWORD,
    MAPPING,
    NO_READ_BOT,
    NO_READ_PASSWORD,
    FakeMediaWiki,
    sample_wiki,
)
from tests.fake_connector import public_resolver

KEY = Fernet.generate_key().decode()
MAX_BYTES = 1024 * 1024
READERS = frozenset({"anna@example.com", "boris@example.com"})


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
    server: FakeMediaWiki,
    *,
    login: str = BOT,
    password: str = BOT_PASSWORD,
    namespaces: tuple[str, ...] = (),
    mapping: str = MAPPING,
    sleep: Sleeps | None = None,
    recorder: Any = None,
) -> MediaWikiAdapter:
    client = MediaWikiClient(
        server.client(),
        api_url=server.api,
        username=login,
        password=password,
        sleep=sleep or Sleeps(),
        recorder=recorder,
    )
    return MediaWikiAdapter(
        client,
        user_emails=parse_user_emails(mapping),
        namespaces=namespaces,
        max_bytes=MAX_BYTES,
    )


async def listed(
    adapter: MediaWikiAdapter, modules: tuple[str, ...] = ("pages", "files")
) -> dict[str, RemoteDocument]:
    return {d.title: d async for d in adapter.list(list(modules))}


@pytest.fixture
def server() -> FakeMediaWiki:
    return sample_wiki()


# --- спецификация и сборка ------------------------------------------------------


def test_spec_is_hidden_organization_kind() -> None:
    assert SPEC.kind == "mediawiki"
    assert SPEC.mode is ConnectorMode.ORGANIZATION and not SPEC.oauth
    assert SPEC.preview and SPEC.base
    assert [m.name for m in SPEC.modules] == ["pages", "files"]
    assert [(f.name, f.required) for f in SPEC.config_fields] == [
        ("api_url", True),
        ("user_emails", True),
        ("namespaces", False),
    ]
    assert [(f.name, f.secret) for f in SPEC.credential_fields] == [
        ("username", False),
        ("password", True),
    ]
    assert SPEC.url_field == "api_url"


def test_kind_is_offered_only_when_enabled() -> None:
    hidden = AdapterRegistry()
    register(hidden, settings())
    assert "mediawiki" not in {s.kind for s in hidden.kinds()}
    assert hidden.spec("mediawiki").preview
    shown = AdapterRegistry(enabled_preview_kinds=frozenset({"mediawiki"}))
    register(shown, settings())
    assert [s.kind for s in shown.kinds()] == ["mediawiki"]


@pytest.mark.parametrize(
    ("config", "code"),
    [
        ({"user_emails": MAPPING}, None),
        ({"user_emails": ""}, "user_emails_required"),
        ({"user_emails": "Anna"}, "user_emails_invalid"),
        ({"user_emails": "Anna = not-an-email"}, "user_emails_invalid"),
        ({"user_emails": MAPPING, "namespaces": "x" * 101}, "namespaces_invalid"),
    ],
)
def test_config_check(config: dict[str, str], code: str | None) -> None:
    assert SPEC.config_check is not None
    assert SPEC.config_check(config) == code


def test_credentials_check_requires_bot_password() -> None:
    assert SPEC.credentials_check is not None
    assert SPEC.credentials_check({"username": "Admin@kronto"}) is None
    assert SPEC.credentials_check({"username": "Admin"}) == "bot_password_required"


def test_mapping_is_normalised_like_mediawiki_user_names() -> None:
    mapping = parse_user_emails("ivan_petrov = Ivan@Corp.ru\n  maria  =maria@corp.ru")
    assert mapping == {"Ivan petrov": "ivan@corp.ru", "Maria": "maria@corp.ru"}


@pytest.mark.parametrize(
    ("address", "api"),
    [
        ("https://wiki.company.ru/w/api.php", "https://wiki.company.ru/w/api.php"),
        ("https://wiki.company.ru/w/", "https://wiki.company.ru/w/api.php"),
        ("https://wiki.company.ru", "https://wiki.company.ru/api.php"),
        ("https://wiki.company.ru/api.php?x=1", "https://wiki.company.ru/api.php"),
    ],
)
def test_api_endpoint(address: str, api: str) -> None:
    assert api_endpoint(address) == api


def test_build_validates_credentials_and_address(server: FakeMediaWiki) -> None:
    config = {"api_url": server.api, "user_emails": MAPPING}
    good = {"username": BOT, "password": BOT_PASSWORD}
    adapter = build_adapter(config, good, server.client(), settings())
    assert adapter.api_url == server.api
    with pytest.raises(AdapterAuthError, match="credentials_missing"):
        build_adapter(config, {"username": BOT}, server.client(), settings())
    with pytest.raises(AdapterAuthError, match="bot_password_required"):
        build_adapter(
            config, {"username": "Admin", "password": "x"}, server.client(), settings()
        )
    with pytest.raises(AdapterConfigError, match="api_url_invalid"):
        build_adapter(
            {**config, "api_url": "http://wiki.example.com/w/api.php"},
            good,
            server.client(),
            settings(),
        )
    with pytest.raises(AdapterConfigError, match="user_emails_required"):
        build_adapter({**config, "user_emails": " "}, good, server.client(), settings())


# --- вход и сессия ---------------------------------------------------------------


async def test_check_logs_in_with_bot_password(server: FakeMediaWiki) -> None:
    await make_adapter(server).check()
    login = next(p for p in server.calls if p.get("action") == "login")
    assert login["lgname"] == BOT and login["lgtoken"] == "tok1+\\"
    # Всё после входа — с assert=user, вход и токен — без.
    after = server.calls[server.calls.index(login) + 1 :]
    assert after and all(p.get("assert") == "user" for p in after)
    assert server.calls[0].get("assert") is None


async def test_wrong_password_is_an_auth_error(server: FakeMediaWiki) -> None:
    with pytest.raises(AdapterAuthError, match="unauthorized"):
        await make_adapter(server, password="wrong" * 6 + "xx").check()


@pytest.mark.parametrize(
    ("result", "error", "code", "retryable"),
    [
        ("Aborted", AdapterAuthError, "login_aborted", False),
        ("Throttled", AdapterError, "login_throttled", True),
        ("WrongToken", AdapterError, "login_token_rejected", True),
    ],
)
async def test_login_results(
    server: FakeMediaWiki,
    result: str,
    error: type[AdapterError],
    code: str,
    retryable: bool,
) -> None:
    server.login_result = result
    with pytest.raises(error) as caught:
        await make_adapter(server).check()
    assert caught.value.code == code and caught.value.retryable is retryable


async def test_expired_session_logs_in_again(server: FakeMediaWiki) -> None:
    adapter = make_adapter(server)
    await adapter.check()
    server.expire_sessions()
    documents = await listed(adapter)
    assert "Отпуск" in documents
    assert [p["action"] for p in server.calls].count("login") == 2


async def test_session_lost_twice_is_an_auth_error(server: FakeMediaWiki) -> None:
    adapter = make_adapter(server)
    await adapter.check()
    server.drop_sessions = True
    with pytest.raises(AdapterAuthError, match="session_lost"):
        await adapter.check()


async def test_bot_without_read_right_is_refused() -> None:
    server = sample_wiki(private=True)
    with pytest.raises(AdapterAuthError, match="read_denied"):
        await make_adapter(server, login=NO_READ_BOT, password=NO_READ_PASSWORD).check()


async def test_private_wiki_is_read_only_after_login() -> None:
    server = sample_wiki(private=True)
    documents = await listed(make_adapter(server))
    assert "Отпуск" in documents
    # Анонимно здесь ничего не прочитать: ни один запрос данных без сессии.
    data_calls = [p for p in server.calls if p.get("action") in {"query", "parse"}]
    assert all(
        p.get("assert") == "user" for p in data_calls if p.get("meta") != "tokens"
    )


# --- ошибки и повторы --------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error", "code"),
    [
        (401, AdapterAuthError, "unauthorized"),
        (403, AdapterConfigError, "forbidden"),
        (404, AdapterConfigError, "api_not_found"),
    ],
)
async def test_http_errors(
    server: FakeMediaWiki, status: int, error: type[AdapterError], code: str
) -> None:
    server.http_status["siteinfo"] = status
    with pytest.raises(error) as caught:
        await make_adapter(server).check()
    assert caught.value.code == code


async def test_html_instead_of_api_is_a_config_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>Главная</html>")

    client = MediaWikiClient(
        OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            resolver=public_resolver,
        ),
        api_url="https://wiki.example.com/",
        username=BOT,
        password=BOT_PASSWORD,
    )
    with pytest.raises(AdapterConfigError, match="not_mediawiki_api"):
        await client.login()


async def test_rate_limit_and_server_errors_are_retried(server: FakeMediaWiki) -> None:
    server.rate_limited["siteinfo"] = 2
    server.retry_after = "7"
    sleeps = Sleeps()
    await make_adapter(server, sleep=sleeps).check()
    assert sleeps.calls == [7.0, 7.0]

    server.http_status["users"] = 503
    sleeps = Sleeps()
    await make_adapter(server, sleep=sleeps).check()
    assert sleeps.calls == [1.0]


async def test_endless_rate_limit_is_retryable(server: FakeMediaWiki) -> None:
    server.rate_limited["siteinfo"] = 100
    sleeps = Sleeps()
    with pytest.raises(AdapterError) as caught:
        await make_adapter(server, sleep=sleeps).check()
    assert caught.value.code == "ratelimited" and caught.value.retryable
    assert sleeps.calls == [1.0, 2.0, 4.0, 8.0]


async def test_api_error_info_does_not_leak(server: FakeMediaWiki) -> None:
    adapter = make_adapter(server)
    await adapter.check()
    with pytest.raises(AdapterError) as caught:
        await adapter._client.call({"action": "nonsense"})
    assert caught.value.code == "mediawiki_badvalue"
    assert "Unrecognized" not in str(caught.value)


async def test_recorder_gets_no_password_or_tokens(server: FakeMediaWiki) -> None:
    recorded: list[tuple[str, Any, Any]] = []
    adapter = make_adapter(server, recorder=lambda m, p, r: recorded.append((m, p, r)))
    await listed(adapter)
    dumped = repr(recorded)
    assert BOT_PASSWORD not in dumped
    assert "tok1" not in dumped
    assert "bp1" not in dumped
    methods = [m for m, _, _ in recorded]
    assert "login" in methods and "query.allpages" in methods


# --- читатели и пространства имён --------------------------------------------------


async def test_documents_are_restricted_to_mapped_readers(
    server: FakeMediaWiki,
) -> None:
    documents = await listed(make_adapter(server))
    for document in documents.values():
        assert document.visibility is MaterialVisibility.RESTRICTED
        # Vera заблокирована, у Gleb нет read, Ghost в вики нет.
        assert document.allowed_emails == READERS, document.title


async def test_public_wiki_is_still_an_explicit_list(server: FakeMediaWiki) -> None:
    """Анонимное чтение включено — но «вся компания» не ставится."""
    server.private = False
    documents = await listed(make_adapter(server))
    assert {d.visibility for d in documents.values()} == {MaterialVisibility.RESTRICTED}


async def test_partial_block_keeps_reading(server: FakeMediaWiki) -> None:
    server.add_user("Vera", partial_block=True)
    documents = await listed(make_adapter(server))
    assert "vera@example.com" in documents["Отпуск"].allowed_emails


async def test_no_readers_stops_the_connector(server: FakeMediaWiki) -> None:
    with pytest.raises(AdapterConfigError, match="no_readers"):
        await make_adapter(server, mapping="Ghost = ghost@example.com").check()


async def test_users_are_asked_in_batches_of_fifty(server: FakeMediaWiki) -> None:
    mapping = "\n".join(f"U{n} = u{n}@example.com" for n in range(120))
    with pytest.raises(AdapterConfigError, match="no_readers"):
        await make_adapter(server, mapping=mapping).check()
    batches = [p["ususers"].count("|") + 1 for p in server.calls if "ususers" in p]
    assert batches == [50, 50, 20]


async def test_default_is_content_namespace_only(server: FakeMediaWiki) -> None:
    documents = await listed(make_adapter(server), ("pages",))
    assert set(documents) == {"Отпуск", "Больничный"}
    assert documents["Отпуск"].path == "Компания"


async def test_namespaces_by_number_name_and_alias(server: FakeMediaWiki) -> None:
    adapter = make_adapter(
        server, namespaces=("основное", "Project", "12", "изображение")
    )
    documents = await listed(adapter)
    assert set(documents) == {
        "Отпуск",
        "Больничный",
        "Компания:Правила",
        "Справка:Поиск",
        "Регламент.txt",
        "Памятка.md",
    }
    assert documents["Справка:Поиск"].path == "Компания/Справка"


async def test_unknown_namespace_is_a_config_error(server: FakeMediaWiki) -> None:
    with pytest.raises(AdapterConfigError, match="namespace_unknown"):
        await make_adapter(server, namespaces=("Нет такого",)).check()
    with pytest.raises(AdapterConfigError, match="namespace_unknown"):
        await make_adapter(server, namespaces=("-1",)).check()


@pytest.mark.parametrize("extension", ["Lockdown", "IntraACL", "MyPermissionsHack"])
async def test_lockdown_requires_explicit_namespaces(extension: str) -> None:
    server = sample_wiki(extensions=(extension,))
    with pytest.raises(AdapterConfigError, match="namespaces_required"):
        await make_adapter(server).check()
    assert not [p for p in server.calls if p.get("generator")]


async def test_lockdown_with_listed_namespaces_syncs_only_them() -> None:
    server = sample_wiki(extensions=("Lockdown",))
    documents = await listed(make_adapter(server, namespaces=("0", "Компания")))
    assert set(documents) == {"Отпуск", "Больничный", "Компания:Правила"}
    # «Файл» не перечислен — файлы не берутся, «Кадры» — тоже.
    assert not [p for p in server.calls if p.get("generator") == "allimages"]
    with_files = await listed(make_adapter(server, namespaces=("0", "Файл")))
    assert "Регламент.txt" in with_files


# --- страницы ---------------------------------------------------------------------


async def test_page_listing(server: FakeMediaWiki) -> None:
    documents = await listed(make_adapter(server), ("pages",))
    vacation = documents["Отпуск"]
    page_id = next(p for p in server.pages.values() if p["title"] == "Отпуск")["pageid"]
    assert vacation.external_id == f"page:{page_id}"
    assert vacation.kind is RemoteDocumentKind.PAGE and vacation.module == "pages"
    assert vacation.version == str(page_id * 10)
    assert (
        vacation.url
        == "https://wiki.example.com/w/index.php/%D0%9E%D1%82%D0%BF%D1%83%D1%81%D0%BA"
    )
    assert vacation.modified_at is not None
    # Перенаправление, стили и страница, которую бот не читает, — нет.
    assert {"Старое название", "Common.css", "Секрет"}.isdisjoint(documents)


async def test_pages_are_continued(server: FakeMediaWiki) -> None:
    for n in range(120):
        server.add_page(f"Документ {n:03d}", "<p>x</p>")
    documents = await listed(make_adapter(server), ("pages",))
    assert len(documents) == 122
    asked = [
        p.get("gapcontinue") for p in server.calls if p.get("generator") == "allpages"
    ]
    assert len(asked) == 3 and asked[0] is None and all(asked[1:])
    assert all(p["gaplimit"] == "50" for p in server.calls if p.get("generator"))


async def test_foreign_page_link_falls_back_to_curid(server: FakeMediaWiki) -> None:
    original = server._info

    def foreign(shown: dict[str, Any], *args: Any) -> None:
        original(shown, *args)
        shown["fullurl"] = "https://evil.example.org/x"

    server._info = foreign  # type: ignore[method-assign]
    documents = await listed(make_adapter(server), ("pages",))
    page_id = documents["Отпуск"].external_id.removeprefix("page:")
    assert documents["Отпуск"].url == (
        f"https://wiki.example.com/w/index.php?curid={page_id}"
    )


async def test_fetch_page_as_html_with_title(server: FakeMediaWiki) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter, ("pages",))
    content = await adapter.fetch(documents["Отпуск"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedPage)
    assert content.html.startswith("<h1>Отпуск</h1>\n<div")
    assert "28 дней" in content.html
    parse = next(p for p in server.calls if p.get("action") == "parse")
    assert parse["disablelimitreport"] == "1" and parse["disableeditsection"] == "1"


async def test_fetch_page_errors(server: FakeMediaWiki) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter, ("pages",))
    vacation = documents["Отпуск"]
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(vacation, max_bytes=20)
    del server.pages[int(vacation.external_id.removeprefix("page:"))]
    with pytest.raises(AdapterError, match="not_found"):
        await adapter.fetch(vacation, max_bytes=MAX_BYTES)


async def test_huge_parse_response_is_too_large(
    server: FakeMediaWiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter_module, "PARSE_OVERHEAD", 0)
    adapter = make_adapter(server)
    documents = await listed(adapter, ("pages",))
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(documents["Отпуск"], max_bytes=100)


async def test_edit_changes_the_version(server: FakeMediaWiki) -> None:
    before = await listed(make_adapter(server), ("pages",))
    page_id = int(before["Отпуск"].external_id.removeprefix("page:"))
    server.edit(page_id, "<p>Теперь 31 день.</p>")
    after = await listed(make_adapter(server), ("pages",))
    assert after["Отпуск"].version != before["Отпуск"].version
    assert after["Больничный"].version == before["Больничный"].version


# --- файлы ------------------------------------------------------------------------


async def test_files_supported_formats_only(server: FakeMediaWiki) -> None:
    with counting_skips() as skips:
        documents = await listed(make_adapter(server), ("files",))
    assert set(documents) == {"Регламент.txt", "Памятка.md"}
    assert skips.formats() == {".png": 1}
    rules = documents["Регламент.txt"]
    assert rules.kind is RemoteDocumentKind.FILE and rules.module == "files"
    assert rules.filename == "Регламент.txt"
    assert rules.size == len("Регламент закупок.".encode())
    assert len(rules.version) == 40
    assert rules.path == "Компания/Файл"
    assert rules.allowed_emails == READERS


async def test_fetch_file_sends_the_session_cookie() -> None:
    server = sample_wiki(private=True)
    adapter = make_adapter(server)
    documents = await listed(adapter, ("files",))
    content = await adapter.fetch(documents["Регламент.txt"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedFile)
    assert content.data == "Регламент закупок.".encode()
    assert content.filename == "Регламент.txt"
    assert server.downloads[-1][1].startswith("bp")


async def test_file_errors(server: FakeMediaWiki) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter, ("files",))
    rules = documents["Регламент.txt"]
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(rules, max_bytes=5)
    server.forbidden_files.add(int(rules.external_id.removeprefix("file:")))
    with pytest.raises(AdapterError, match="download_forbidden") as caught:
        await adapter.fetch(rules, max_bytes=MAX_BYTES)
    assert not caught.value.retryable


async def test_files_behind_img_auth_are_not_taken() -> None:
    """img_auth.php не принимает сессию бот-пароля (стенд 15.10: 403):
    такие файлы не листятся, а не падают при скачивании."""
    server = sample_wiki(private=True)
    server.img_auth = True
    documents = await listed(make_adapter(server))
    assert "Отпуск" in documents
    assert {d.module for d in documents.values()} == {"pages"}


async def test_large_unreadable_and_foreign_files_are_skipped(
    server: FakeMediaWiki,
) -> None:
    server.add_file("Большой.pdf", b"x" * (MAX_BYTES + 1))
    server.add_file("Закрытый.pdf", b"%PDF", readable=False)
    with counting_skips() as skips:
        documents = await listed(make_adapter(server), ("files",))
    assert "Большой.pdf" not in documents and "Закрытый.pdf" not in documents
    assert len(skips.too_large) == 1

    server.upload_host = "cdn.example.com"
    assert await listed(make_adapter(server), ("files",)) == {}
