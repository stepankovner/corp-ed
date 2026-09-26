"""Адаптер Битрикс24: модули диска и базы знаний, OAuth-обмен, каталог,
контракт по образцам из документации."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

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
from corp_ed.connectors.bitrix24 import KIND, SPEC
from corp_ed.connectors.bitrix24.adapter import Bitrix24Adapter, build_client
from corp_ed.connectors.bitrix24.oauth import Bitrix24OAuth, _tokens
from corp_ed.connectors.html import html_to_markdown
from corp_ed.connectors.registry import UserAuth, default_registry
from corp_ed.core.config import ConnectorSettings
from corp_ed.domain.types import ConnectorMode, RemoteDocumentKind
from tests.connectors.fake_portal import (
    ACCESS_TOKEN,
    AUTH_CODE,
    CLIENT_ID,
    CLIENT_SECRET,
    EMPLOYEE_ID,
    OAUTH_SERVER,
    REFRESH_TOKEN,
    FakePortal,
    sample_portal,
)

FIXTURES = Path(__file__).parent / "fixtures" / "bitrix24"
KEY = Fernet.generate_key().decode()
MAX_BYTES = 1024 * 1024


def settings(**overrides: Any) -> ConnectorSettings:
    return ConnectorSettings(
        secrets_keys=KEY,
        bitrix24_oauth_server=OAUTH_SERVER,
        max_document_bytes=MAX_BYTES,
        **overrides,
    )  # type: ignore[arg-type]


def make_adapter(
    portal: FakePortal, *, auth: str = "oauth", **credential_overrides: str
) -> Bitrix24Adapter:
    config = {"portal": portal.portal, "client_id": CLIENT_ID}
    if auth == "webhook":
        credentials = {"webhook": portal.webhook}
    else:
        credentials = {
            "client_secret": CLIENT_SECRET,
            "access_token": ACCESS_TOKEN,
            "refresh_token": REFRESH_TOKEN,
            "expires_at": "0",
        }
    credentials.update(credential_overrides)
    client = build_client(
        config, credentials, portal.client(), settings(), min_interval=0.0
    )
    return Bitrix24Adapter(client, max_bytes=MAX_BYTES)


async def listed(adapter: Bitrix24Adapter, *modules: str) -> dict[str, RemoteDocument]:
    return {d.external_id: d async for d in adapter.list(list(modules))}


@pytest.fixture
def portal() -> FakePortal:
    return sample_portal()


# --- каталог и сборка ------------------------------------------------------------


def test_spec_is_per_user_with_oauth() -> None:
    assert SPEC.kind == KIND == "bitrix24"
    assert SPEC.mode is ConnectorMode.PER_USER
    assert SPEC.user_auth is UserAuth.OAUTH and SPEC.oauth
    assert [m.name for m in SPEC.modules] == [
        "disk",
        "disk_personal",
        "knowledge_base",
        "knowledge_base_v2",
    ]
    assert [f.name for f in SPEC.config_fields] == ["portal", "client_id"]
    assert [f.name for f in SPEC.app_credential_fields] == ["client_secret"]
    assert SPEC.app_credential_fields[0].secret
    assert SPEC.credential_fields == ()
    assert SPEC.url_field == "portal"
    assert SPEC.extra["app_scopes"] == "disk,landing"


def test_default_registry_has_bitrix24(portal: FakePortal) -> None:
    registry = default_registry(settings())
    assert [spec.kind for spec in registry.kinds()] == [
        "bitrix24",
        "confluence",
        "yandex360",
    ]
    config = {"portal": portal.portal, "client_id": CLIENT_ID}
    adapter = registry.build(
        "bitrix24",
        config,
        {"client_secret": CLIENT_SECRET, "access_token": "a", "refresh_token": "r"},
        portal.client(),
    )
    assert isinstance(adapter, Bitrix24Adapter)
    flow = registry.build_oauth(
        "bitrix24", config, {"client_secret": CLIENT_SECRET}, portal.client()
    )
    assert isinstance(flow, Bitrix24OAuth)


def test_build_without_tokens_or_secret_fails_early(portal: FakePortal) -> None:
    registry = default_registry(settings())
    config = {"portal": portal.portal, "client_id": CLIENT_ID}
    with pytest.raises(AdapterAuthError, match="credentials_missing"):
        registry.build(
            "bitrix24", config, {"client_secret": CLIENT_SECRET}, portal.client()
        )
    with pytest.raises(AdapterConfigError, match="app_credentials_missing"):
        registry.build(
            "bitrix24",
            config,
            {"access_token": "a", "refresh_token": "r"},
            portal.client(),
        )
    with pytest.raises(AdapterConfigError, match="app_credentials_missing"):
        registry.build_oauth("bitrix24", config, {}, portal.client())
    with pytest.raises(AdapterConfigError, match="portal_missing"):
        registry.build("bitrix24", {}, {"webhook": portal.webhook}, portal.client())


# --- проверка ------------------------------------------------------------


async def test_check_uses_profile_and_remembers_user(portal: FakePortal) -> None:
    adapter = make_adapter(portal)
    await adapter.check()
    assert adapter.external_user_id == EMPLOYEE_ID
    assert portal.calls == [("profile", {})]


async def test_check_rejects_inactive_user(portal: FakePortal) -> None:
    portal.inactive_users.add(EMPLOYEE_ID)
    with pytest.raises(AdapterAuthError, match="user_inactive"):
        await make_adapter(portal).check()


# --- диск ------------------------------------------------------------


async def test_disk_walks_common_and_group_drives(portal: FakePortal) -> None:
    documents = await listed(make_adapter(portal), "disk")
    assert set(documents) == {"disk:102", "disk:201"}
    vacation = documents["disk:102"]
    assert vacation.title == "Отпуск.txt"
    assert vacation.filename == "Отпуск.txt"
    assert vacation.kind is RemoteDocumentKind.FILE
    assert vacation.module == "disk"
    assert vacation.path == "Общий диск/Регламенты"
    assert vacation.url == f"{portal.portal}disk/file/Отпуск.txt"
    size = len("Отпуск — 28 дней.".encode())
    assert vacation.version == f"1:2026-01-14T17:05:39+03:00:{size}"
    assert vacation.size == size
    assert vacation.modified_at == datetime.fromisoformat("2026-01-14T17:05:39+03:00")
    assert documents["disk:201"].path == "Проект X"


async def test_disk_skips_trash_unsupported_oversized_and_hidden(
    portal: FakePortal,
) -> None:
    documents = await listed(make_adapter(portal), "disk")
    assert "disk:103" not in documents  # png
    assert "disk:104" not in documents  # больше лимита по метаданным
    assert "disk:105" not in documents  # корзина
    assert "disk:107" not in documents  # папка без права чтения не листится
    folders = [body["id"] for m, body in portal.calls if m == "disk.folder.getchildren"]
    assert folders == ["101"]


async def test_disk_denied_folder_is_skipped_not_fatal(portal: FakePortal) -> None:
    """Папка видна, но права сняли до обхода: пропуск, а не сбой запуска."""
    portal.hidden.clear()
    portal.denied.add("106")
    documents = await listed(make_adapter(portal), "disk")
    assert "disk:107" not in documents
    assert {"disk:102", "disk:201"} <= set(documents)
    folders = [body["id"] for m, body in portal.calls if m == "disk.folder.getchildren"]
    assert folders == ["101", "106"]


async def test_personal_module_reads_only_own_drive(portal: FakePortal) -> None:
    documents = await listed(make_adapter(portal), "disk_personal")
    assert set(documents) == {"disk:301"}
    assert documents["disk:301"].module == "disk_personal"
    assert documents["disk:301"].path == "Мой диск"
    # check вызван сам: без ID сотрудника «Мой диск» не отличить от чужого.
    assert portal.calls[0] == ("profile", {})


async def test_modules_are_additive(portal: FakePortal) -> None:
    documents = await listed(
        make_adapter(portal), "disk", "disk_personal", "knowledge_base"
    )
    assert set(documents) == {
        "disk:102",
        "disk:201",
        "disk:301",
        "kb:KNOWLEDGE:985",
        "kb:KNOWLEDGE:573",
        "kb:KNOWLEDGE:600",
    }


async def test_disk_fetch_downloads_through_file_get(portal: FakePortal) -> None:
    adapter = make_adapter(portal)
    documents = await listed(adapter, "disk")
    content = await adapter.fetch(documents["disk:102"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedFile)
    assert content.filename == "Отпуск.txt"
    assert content.data == "Отпуск — 28 дней.".encode()
    assert portal.downloads == ["/rest/download.json?auth=signed-7&token=disk%7C102"]


async def test_disk_fetch_rejects_oversized_by_metadata_without_download(
    portal: FakePortal,
) -> None:
    adapter = make_adapter(portal)
    document = RemoteDocument(
        external_id="disk:104",
        title="Большой.pdf",
        url="",
        version="1",
        kind=RemoteDocumentKind.FILE,
        module="disk",
    )
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(document, max_bytes=MAX_BYTES)
    assert portal.downloads == []


async def test_disk_fetch_rejects_foreign_download_host(portal: FakePortal) -> None:
    portal.download_host = "cdn.example.net"
    adapter = make_adapter(portal)
    documents = await listed(adapter, "disk")
    with pytest.raises(AdapterError, match="download_url_foreign"):
        await adapter.fetch(documents["disk:102"], max_bytes=MAX_BYTES)


async def test_disk_auth_error_mid_walk_propagates(portal: FakePortal) -> None:
    portal.canned["disk.folder.getchildren"] = (401, {"error": "invalid_token"})
    with pytest.raises(AdapterAuthError, match="invalid_token"):
        await listed(make_adapter(portal), "disk")


async def test_disk_retryable_error_mid_walk_propagates(portal: FakePortal) -> None:
    portal.canned["disk.folder.getchildren"] = (500, {"error": "INTERNAL_SERVER_ERROR"})
    with pytest.raises(AdapterError) as excinfo:
        await listed(make_adapter(portal), "disk")
    assert excinfo.value.retryable


# --- база знаний ------------------------------------------------------------


async def test_kb_lists_published_pages_with_links(portal: FakePortal) -> None:
    documents = await listed(make_adapter(portal), "knowledge_base")
    vacation = documents["kb:KNOWLEDGE:985"]
    assert vacation.kind is RemoteDocumentKind.PAGE
    assert vacation.module == "knowledge_base"
    assert vacation.title == "Отпуск"
    assert vacation.url == f"{portal.portal}knowledge/company/vacation/"
    assert vacation.version == "10/10/2022 03:25:30 pm|10/11/2022 09:00:00 am"
    assert vacation.path == "База знаний"
    trips = documents["kb:KNOWLEDGE:573"]
    # PUBLIC_URL пуст — адрес собран из кодов сайта и страницы.
    assert trips.url == f"{portal.portal}knowledge/company/trips/"
    assert trips.version == "10/10/2022 03:25:30 pm|"
    scopes = [body["scope"] for m, body in portal.calls if m == "landing.site.getlist"]
    assert scopes == ["KNOWLEDGE", "GROUP"]


async def test_kb_group_scope_is_optional(portal: FakePortal) -> None:
    portal.group_kb_supported = True
    portal.add_site("GROUP", "300", "База проекта", "/project/")
    portal.add_page("300", "3001", "Регламент проекта", "rules")
    documents = await listed(make_adapter(portal), "knowledge_base")
    assert "kb:GROUP:3001" in documents
    assert documents["kb:GROUP:3001"].path == "База проекта"


async def test_kb_pages_are_paged_by_offset(portal: FakePortal) -> None:
    for index in range(60):
        portal.add_page("157", str(1000 + index), f"Страница {index}", f"p{index}")
    documents = await listed(make_adapter(portal), "knowledge_base")
    assert len(documents) == 63
    offsets = [
        body["params"]["offset"]
        for m, body in portal.calls
        if m == "landing.landing.getlist"
    ]
    assert offsets == [0, 50]


async def test_kb_fetch_concatenates_active_blocks(portal: FakePortal) -> None:
    adapter = make_adapter(portal)
    documents = await listed(adapter, "knowledge_base")
    content = await adapter.fetch(documents["kb:KNOWLEDGE:985"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedPage)
    assert content.html.startswith("<h1>Отпуск</h1>")
    assert "28 календарных" in content.html
    assert "Черновик" not in content.html
    markdown = html_to_markdown(content.html)
    assert "# Отпуск" in markdown
    assert "28 календарных дней" in markdown
    assert "track()" not in markdown
    call = next(body for m, body in portal.calls if m == "landing.block.getlist")
    assert call == {"scope": "KNOWLEDGE", "lid": 985, "params": {"get_content": True}}


async def test_kb_fetch_empty_page_is_error(portal: FakePortal) -> None:
    adapter = make_adapter(portal)
    documents = await listed(adapter, "knowledge_base")
    with pytest.raises(AdapterError, match="empty_page"):
        await adapter.fetch(documents["kb:KNOWLEDGE:600"], max_bytes=MAX_BYTES)


async def test_kb_missing_scope_stops_as_config_error(portal: FakePortal) -> None:
    portal.canned["landing.site.getlist"] = (401, {"error": "insufficient_scope"})
    with pytest.raises(AdapterConfigError, match="insufficient_scope"):
        await listed(make_adapter(portal), "knowledge_base")


async def test_fetch_unknown_document(portal: FakePortal) -> None:
    document = RemoteDocument(
        external_id="x:1",
        title="",
        url="",
        version="",
        kind=RemoteDocumentKind.FILE,
        module="disk",
    )
    with pytest.raises(AdapterError, match="unknown_document"):
        await make_adapter(portal).fetch(document, max_bytes=MAX_BYTES)


# --- вебхук (cli connector-check) -----------------------------------------------------


async def test_webhook_credentials_work_for_diagnostics(portal: FakePortal) -> None:
    adapter = make_adapter(portal, auth="webhook")
    await adapter.check()
    assert adapter.external_user_id == "1"
    documents = await listed(adapter, "disk")
    # Админ видит и «Секретную» папку: права — пользователя токена.
    assert "disk:107" in documents
    assert adapter.refreshed_credentials is None


# --- OAuth ------------------------------------------------------------


def test_authorize_url_points_to_portal(portal: FakePortal) -> None:
    flow = Bitrix24OAuth(
        portal.client(),
        portal=portal.portal,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        server=OAUTH_SERVER,
    )
    url = flow.authorize_url("st-ate")
    assert url.startswith(f"{portal.portal}oauth/authorize/?")
    assert f"client_id={CLIENT_ID}" in url
    assert "state=st-ate" in url
    assert "response_type=code" in url
    assert "secret" not in url


async def test_exchange_returns_credentials_for_secret_box(portal: FakePortal) -> None:
    flow = Bitrix24OAuth(
        portal.client(),
        portal=portal.portal,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        server=OAUTH_SERVER,
    )
    exchanged = await flow.exchange(AUTH_CODE)
    assert set(exchanged.credentials) == {
        "access_token",
        "refresh_token",
        "expires_at",
        "member_id",
    }
    assert exchanged.credentials["access_token"] in portal.access_tokens
    assert exchanged.external_user_id is None  # первый обмен user_id не несёт
    method, params = portal.calls[-1]
    assert method == "oauth/token"
    assert params["grant_type"] == "authorization_code"
    assert params["client_secret"] == CLIENT_SECRET


@pytest.mark.parametrize(
    ("setup", "kind", "code"),
    [
        (lambda p: p.codes.clear(), AdapterAuthError, "invalid_grant"),
        (
            lambda p: setattr(p, "oauth_error", "PAYMENT_REQUIRED"),
            AdapterConfigError,
            "payment_required",
        ),
        (
            lambda p: setattr(p, "oauth_error", "invalid_client"),
            AdapterConfigError,
            "invalid_client",
        ),
        (
            lambda p: setattr(p, "oauth_error", "SOMETHING_ELSE"),
            AdapterError,
            "oauth_something_else",
        ),
    ],
)
async def test_exchange_errors(
    portal: FakePortal, setup: Any, kind: type[AdapterError], code: str
) -> None:
    setup(portal)
    flow = Bitrix24OAuth(
        portal.client(),
        portal=portal.portal,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        server=OAUTH_SERVER,
    )
    with pytest.raises(kind) as excinfo:
        await flow.exchange(AUTH_CODE)
    assert type(excinfo.value) is kind
    assert excinfo.value.code == code


# --- контракт по образцам документации ------------------------------------------------


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))[
        "response"
    ]


async def test_documented_disk_responses_parse(portal: FakePortal) -> None:
    portal.canned["profile"] = fixture("profile")
    portal.canned["disk.storage.getlist"] = fixture("disk.storage.getlist")
    portal.canned["disk.storage.getchildren"] = fixture("disk.folder.getchildren")
    portal.canned["disk.folder.getchildren"] = {"result": [], "total": 0}
    portal.canned["disk.file.get"] = fixture("disk.file.get")
    adapter = make_adapter(portal, auth="webhook")
    await adapter.check()
    assert adapter.external_user_id == "1"
    documents = await listed(adapter, "disk")
    # Из образца: png — не наш формат, Старый.txt — в корзине, папка пуста.
    assert set(documents) == {"disk:9043"}
    document = documents["disk:9043"]
    assert document.title == "Тест.docx"
    assert document.path == "Общий диск"
    assert document.version == "2:2026-02-16T12:56:20+03:00:21668"
    assert document.url.endswith("/disk/file/Папка/Папка/Тест.docx")
    assert document.size == 21668
    # Личные диски из образца не читаются даже с вебхуком админа.
    storage_calls = [
        body["id"] for m, body in portal.calls if m == "disk.storage.getchildren"
    ]
    assert storage_calls == ["1357"]
    with pytest.raises(AdapterError, match="download_url_foreign"):
        # DOWNLOAD_URL образца ведёт на test.bitrix24.ru — не наш портал.
        await adapter.fetch(document, max_bytes=MAX_BYTES)


async def test_documented_knowledge_base_responses_parse(portal: FakePortal) -> None:
    portal.canned["landing.site.getlist"] = fixture("landing.site.getlist")
    portal.canned["landing.landing.getlist"] = fixture("landing.landing.getlist")
    portal.canned["landing.block.getlist"] = fixture("landing.block.getlist")
    adapter = make_adapter(portal, auth="webhook")
    documents = await listed(adapter, "knowledge_base")
    # Один и тот же канонический ответ на оба scope — страницы дедуплицированы ядром
    # по external_id; здесь важно, что оба scope разобраны.
    assert {d.split(":")[1] for d in documents} == {"KNOWLEDGE", "GROUP"}
    vacation = documents["kb:KNOWLEDGE:985"]
    assert vacation.url == "https://test.bitrix24.ru/knowledge/company/vacation/"
    assert (
        documents["kb:KNOWLEDGE:573"].url == f"{portal.portal}knowledge/company/trips/"
    )
    content = await adapter.fetch(vacation, max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedPage)
    markdown = html_to_markdown(content.html)
    assert "Отпуск — 28 календарных дней" in markdown
    assert "Черновик" not in markdown
    assert "track()" not in markdown


def test_documented_token_responses_parse() -> None:
    first = _tokens(fixture("oauth.token"), now=1_000_000)
    assert first.expires_at == 1_000_000 + 3600
    assert first.member_id == "a223c6b3710f85df22e9377d6c4f7553"
    assert first.user_id is None
    renewed = _tokens(fixture("oauth.refresh"))
    assert renewed.expires_at == 1780319382
    assert renewed.user_id == "67"
    assert renewed.as_credentials()["expires_at"] == "1780319382"
    with pytest.raises(AdapterError, match="oauth_bad_response"):
        _tokens({"access_token": "only"})
