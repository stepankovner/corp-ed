"""Адаптер amoCRM (режим per_user, OAuth): каталог, хосты платформы,
OAuth (адрес, обмен, продление с новой парой), проверка аккаунта,
файлы сущностей только по правам сотрудника, скачивание, ошибки."""

from typing import Any

import pytest
from cryptography.fernet import Fernet

from corp_ed.connectors.amocrm import KIND, SPEC, register
from corp_ed.connectors.amocrm import files as files_module
from corp_ed.connectors.amocrm.adapter import AmoCrmAdapter, build_adapter
from corp_ed.connectors.amocrm.client import AmoAuth, AmoClient
from corp_ed.connectors.amocrm.oauth import (
    AmoOAuth,
    account_root,
    service_host_allowed,
)
from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    FetchedFile,
    RemoteDocument,
    refreshed_credentials,
)
from corp_ed.connectors.common import TokenSet, counting_skips
from corp_ed.connectors.registry import AdapterRegistry, UserAuth
from corp_ed.core.config import ConnectorSettings
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind
from tests.connectors.fake_amocrm import (
    ACCOUNT,
    ANNA_ACCESS,
    ANNA_ID,
    ANNA_REFRESH,
    BORIS_ACCESS,
    CLIENT_ID,
    CLIENT_SECRET,
    DRIVE_HOST,
    NOW,
    REDIRECT_URI,
    FakeAmo,
    sample_amo,
)

KEY = Fernet.generate_key().decode()
MAX_BYTES = 1024 * 1024
CONFIG = {"account": ACCOUNT, "client_id": CLIENT_ID}


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def settings(**overrides: Any) -> ConnectorSettings:
    overrides.setdefault("oauth_callback_url", REDIRECT_URI)
    return ConnectorSettings(  # type: ignore[arg-type]
        secrets_keys=KEY,
        max_document_bytes=MAX_BYTES,
        max_large_document_bytes=MAX_BYTES,
        **overrides,
    )


def registry(**overrides: Any) -> AdapterRegistry:
    built = AdapterRegistry()
    register(built, settings(**overrides))
    return built


def oauth(server: FakeAmo, *, secret: str = CLIENT_SECRET) -> AmoOAuth:
    return AmoOAuth(
        server.client(),
        account=ACCOUNT,
        client_id=CLIENT_ID,
        client_secret=secret,
        redirect_uri=REDIRECT_URI,
        clock=lambda: NOW,
    )


def make_adapter(
    server: FakeAmo,
    access: str = ANNA_ACCESS,
    refresh: str = ANNA_REFRESH,
    *,
    expires_at: int = NOW + 86400,
    sleep: Sleeps | None = None,
    pipelines: tuple[int, ...] = (),
    secret: str = CLIENT_SECRET,
) -> AmoCrmAdapter:
    auth = AmoAuth(
        TokenSet(access, refresh, expires_at),
        oauth(server, secret=secret),
        clock=lambda: NOW,
    )
    client = AmoClient(
        server.client(),
        account=ACCOUNT,
        auth=auth,
        min_interval=0.0,
        sleep=sleep or Sleeps(),
    )
    return AmoCrmAdapter(client, max_bytes=MAX_BYTES, pipelines=pipelines)


async def listed(adapter: AmoCrmAdapter, *modules: str) -> dict[str, RemoteDocument]:
    return {d.external_id: d async for d in adapter.list(list(modules))}


@pytest.fixture
def server() -> FakeAmo:
    return sample_amo()


# --- каталог и сборка -------------------------------------------------------------


def test_spec_is_per_user_oauth_preview() -> None:
    assert SPEC.kind == KIND == "amocrm"
    assert SPEC.mode is ConnectorMode.PER_USER
    assert SPEC.user_auth is UserAuth.OAUTH and SPEC.oauth
    assert SPEC.preview and SPEC.base
    assert [m.name for m in SPEC.modules] == [
        "lead_files",
        "contacts_files",
        "companies_files",
    ]
    assert [(f.name, f.required) for f in SPEC.config_fields] == [
        ("account", True),
        ("client_id", True),
        ("pipelines", False),
    ]
    assert [(f.name, f.secret) for f in SPEC.app_credential_fields] == [
        ("client_secret", True)
    ]
    assert SPEC.credential_fields == ()
    assert SPEC.url_field == "account"


def test_kind_is_hidden_until_enabled() -> None:
    hidden = registry()
    assert hidden.kinds() == []
    assert hidden.spec("amocrm").preview
    enabled = AdapterRegistry(enabled_preview_kinds=frozenset({"amocrm"}))
    register(enabled, settings())
    assert [spec.kind for spec in enabled.kinds()] == ["amocrm"]


@pytest.mark.parametrize(
    ("address", "root"),
    [
        ("https://company.amocrm.ru", "https://company.amocrm.ru/"),
        ("https://Company.amocrm.ru/leads/pipeline/", "https://company.amocrm.ru/"),
        ("https://firma.kommo.com", "https://firma.kommo.com/"),
        ("http://company.amocrm.ru", None),
        ("https://www.amocrm.ru", None),
        ("https://amocrm.ru", None),
        ("https://a.b.amocrm.ru", None),
        ("https://company.amocrm.ru.evil.com", None),
        ("https://companyamocrm.ru", None),
        ("https://company.amocrm.com", None),
        ("https://company.amocrm.ru:8443", None),
        ("https://user@company.amocrm.ru", None),
        ("https://company.amocrm.ru:bad", None),
    ],
)
def test_only_platform_accounts_are_accepted(address: str, root: str | None) -> None:
    assert account_root(address) == root


def test_service_hosts_must_be_on_the_same_platform() -> None:
    assert service_host_allowed("https://drive-b.amocrm.ru", platform="amocrm.ru")
    assert service_host_allowed("https://a.drive.amocrm.ru", platform="amocrm.ru")
    assert not service_host_allowed("https://www.amocrm.ru", platform="amocrm.ru")
    assert not service_host_allowed("https://amocrm.ru", platform="amocrm.ru")
    assert not service_host_allowed("https://x.amocrm.ru:bad", platform="amocrm.ru")
    assert not service_host_allowed("https://drive-b.kommo.com", platform="amocrm.ru")
    assert not service_host_allowed("http://drive-b.amocrm.ru", platform="amocrm.ru")
    assert not service_host_allowed("https://evil.example.com", platform="amocrm.ru")


def test_config_check() -> None:
    assert SPEC.config_check is not None
    assert SPEC.config_check(CONFIG) is None
    assert SPEC.config_check({**CONFIG, "pipelines": " 10, 20 "}) is None
    assert SPEC.config_check({**CONFIG, "pipelines": "10,x"}) == "pipelines_invalid"
    assert SPEC.config_check({"account": "https://evil.example.com"}) == (
        "account_invalid"
    )


def test_build_requires_tokens_app_and_callback(server: FakeAmo) -> None:
    http = server.client()
    with pytest.raises(AdapterAuthError, match="credentials_missing"):
        build_adapter(CONFIG, {}, http, settings())
    tokens = {"access_token": ANNA_ACCESS, "refresh_token": ANNA_REFRESH}
    with pytest.raises(AdapterConfigError, match="app_credentials_missing"):
        build_adapter(CONFIG, tokens, http, settings())
    with pytest.raises(AdapterConfigError, match="oauth_callback_url_missing"):
        build_adapter(
            CONFIG,
            {**tokens, "client_secret": CLIENT_SECRET},
            http,
            settings(oauth_callback_url=None),
        )
    with pytest.raises(AdapterConfigError, match="account_invalid"):
        build_adapter({"account": "https://evil.example.com"}, tokens, http, settings())
    built = registry().build(
        "amocrm", CONFIG, {**tokens, "client_secret": CLIENT_SECRET}, http
    )
    assert isinstance(built, AmoCrmAdapter)


# --- OAuth --------------------------------------------------------------------------


def test_authorize_url_by_platform(server: FakeAmo) -> None:
    flow = registry().build_oauth(
        "amocrm", CONFIG, {"client_secret": CLIENT_SECRET}, server.client()
    )
    assert flow.authorize_url("st") == (
        f"https://www.amocrm.ru/oauth?client_id={CLIENT_ID}&state=st&mode=post_message"
    )
    kommo = registry().build_oauth(
        "amocrm",
        {**CONFIG, "account": "https://firma.kommo.com"},
        {"client_secret": CLIENT_SECRET},
        server.client(),
    )
    assert kommo.authorize_url("st").startswith("https://www.kommo.com/oauth?")
    with pytest.raises(AdapterConfigError, match="app_credentials_missing"):
        registry().build_oauth("amocrm", CONFIG, {}, server.client())


async def test_exchange_goes_to_the_account_with_redirect_uri(
    server: FakeAmo,
) -> None:
    code = server.issue_code(ANNA_ID)
    exchanged = await oauth(server).exchange(code)
    assert set(exchanged.credentials) == {"access_token", "refresh_token", "expires_at"}
    assert exchanged.credentials["expires_at"] == str(NOW + 86400)
    assert server.token_requests[-1] == {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
        "code": code,
    }
    assert server.calls[-1][:2] == ("company.amocrm.ru", "/oauth2/access_token")
    # Токен сразу проверяется в аккаунте, id сотрудника — из check.
    adapter = build_adapter(
        CONFIG,
        {**exchanged.credentials, "client_secret": CLIENT_SECRET},
        server.client(),
        settings(),
    )
    await adapter.check()
    assert adapter.external_user_id == str(ANNA_ID)


async def test_exchange_errors(server: FakeAmo) -> None:
    with pytest.raises(AdapterAuthError, match="invalid_grant"):
        await oauth(server).exchange("used-or-expired")
    code = server.issue_code(ANNA_ID)
    with pytest.raises(AdapterConfigError, match="invalid_client"):
        await oauth(server, secret="wrong").exchange(code)
    assert not await oauth(server).revoke({"access_token": ANNA_ACCESS})


async def test_expired_access_is_refreshed_and_new_pair_reported(
    server: FakeAmo,
) -> None:
    server.expired.add(ANNA_ACCESS)
    adapter = make_adapter(server)
    assert refreshed_credentials(adapter) is None
    await adapter.check()
    saved = refreshed_credentials(adapter)
    assert saved is not None
    assert saved["access_token"] != ANNA_ACCESS
    assert saved["refresh_token"] != ANNA_REFRESH
    # Старый refresh одноразовый — больше не принимается.
    assert ANNA_REFRESH not in server.refresh
    assert saved["refresh_token"] in server.refresh


async def test_token_is_refreshed_ahead_of_expiry(server: FakeAmo) -> None:
    adapter = make_adapter(server, expires_at=NOW + 60)
    await adapter.check()
    assert refreshed_credentials(adapter) is not None
    assert [r["grant_type"] for r in server.token_requests] == ["refresh_token"]


async def test_dead_refresh_token_is_an_auth_error(server: FakeAmo) -> None:
    server.expired.add(ANNA_ACCESS)
    server.refresh.clear()
    with pytest.raises(AdapterAuthError, match="invalid_grant"):
        await make_adapter(server).check()


async def test_long_lived_token_without_refresh(server: FakeAmo) -> None:
    adapter = build_adapter(
        CONFIG, {"access_token": ANNA_ACCESS}, server.client(), settings()
    )
    await adapter.check()
    server.expired.add(ANNA_ACCESS)
    other = build_adapter(
        CONFIG, {"access_token": ANNA_ACCESS}, server.client(), settings()
    )
    with pytest.raises(AdapterAuthError, match="unauthorized"):
        await other.check()


# --- проверка аккаунта и ошибки клиента ---------------------------------------------


async def test_token_of_another_account_is_refused(server: FakeAmo) -> None:
    server.subdomain = "other"
    with pytest.raises(AdapterAuthError, match="account_mismatch"):
        await make_adapter(server).check()


@pytest.mark.parametrize(
    "drive", ["https://drive-b.kommo.com", "https://evil.example.com", "http://x"]
)
async def test_foreign_drive_url_is_refused(server: FakeAmo, drive: str) -> None:
    server.drive_url = drive
    with pytest.raises(AdapterConfigError, match="drive_url_invalid"):
        await make_adapter(server).check()


async def test_missing_drive_url_is_a_config_error(server: FakeAmo) -> None:
    server.drive_url = None
    with pytest.raises(AdapterConfigError, match="drive_url_missing"):
        await make_adapter(server).check()


async def test_blocked_and_unpaid_accounts_stop_the_connector(
    server: FakeAmo,
) -> None:
    server.blocked = True
    with pytest.raises(AdapterConfigError, match="account_forbidden"):
        await make_adapter(server).check()
    server.blocked = False
    server.unpaid = True
    with pytest.raises(AdapterConfigError, match="account_unpaid"):
        await make_adapter(server).check()


async def test_rate_limit_backs_off_briefly_then_gives_up(server: FakeAmo) -> None:
    server.rate_limit_hits = 2
    sleeps = Sleeps()
    await make_adapter(server, sleep=sleeps).check()
    assert sleeps.calls == [2.0, 5.0]
    server.rate_limit_hits = 10
    with pytest.raises(AdapterError) as caught:
        await make_adapter(server).check()
    assert caught.value.code == "rate_limited" and caught.value.retryable


async def test_server_errors_are_retryable(server: FakeAmo) -> None:
    server.server_errors = 10
    with pytest.raises(AdapterError) as caught:
        await make_adapter(server).check()
    assert caught.value.code == "http_503" and caught.value.retryable


async def test_requests_are_paced(server: FakeAmo) -> None:
    sleeps = Sleeps()
    ticks = iter(float(n) * 0.01 for n in range(1000))
    auth = AmoAuth(TokenSet(ANNA_ACCESS, "", 0), None)
    client = AmoClient(
        server.client(),
        account=ACCOUNT,
        auth=auth,
        min_interval=0.25,
        sleep=sleeps,
        clock=lambda: next(ticks),
    )
    await client.api("account")
    await client.api("account")
    assert len(sleeps.calls) == 1 and sleeps.calls[0] > 0.2


# --- листинг -----------------------------------------------------------------------


async def test_files_of_visible_entities(server: FakeAmo) -> None:
    with counting_skips() as tally:
        anna = await listed(
            make_adapter(server), "lead_files", "contacts_files", "companies_files"
        )
    assert set(anna) == {
        "file:f-offer",
        "file:f-boris",
        "file:f-other",
        "file:f-company",
    }
    offer = anna["file:f-offer"]
    assert offer.title == offer.filename == "КП.txt"
    assert offer.kind is RemoteDocumentKind.FILE
    # Привязан к сделке и контакту — один документ, путь сделки.
    assert offer.module == "lead_files"
    assert offer.path == "Сделки/Поставка в Казань"
    assert offer.url == f"{ACCOUNT}/leads/detail/1001"
    assert offer.version == f"v-f-offer-1:{NOW}:{offer.size}"
    assert offer.modified_at is not None
    assert offer.visibility is MaterialVisibility.RESTRICTED
    assert not offer.allowed_emails
    assert anna["file:f-company"].url == f"{ACCOUNT}/companies/detail/3001"
    assert tally.formats() == {".png": 1}
    assert tally.too_large == {"f-big"}


async def test_files_follow_the_employee_rights(server: FakeAmo) -> None:
    boris = make_adapter(server, BORIS_ACCESS, "")
    leads = await listed(boris, "lead_files")
    # Только своя сделка: файлы чужих сделок не запрашиваются вовсе.
    assert set(leads) == {"file:f-boris"}
    requested = {p for _, p, _ in server.calls if p.startswith("/api/v4/leads/")}
    assert requested == {"/api/v4/leads/1002/files"}
    contacts = await listed(boris, "contacts_files")
    assert set(contacts) == {"file:f-offer"}
    assert contacts["file:f-offer"].path == "Контакты/Иван Петров"
    # Раздел «Компании» ему закрыт: пусто, а не ошибка.
    assert await listed(boris, "companies_files") == {}


async def test_drive_is_asked_only_about_uuids_from_entities(
    server: FakeAmo,
) -> None:
    await listed(make_adapter(server, BORIS_ACCESS, ""), "lead_files")
    drive = [q for h, p, q in server.calls if h == DRIVE_HOST and p == "/v1.0/files"]
    assert [q["filter[uuid]"] for q in drive] == ["f-boris"]


async def test_entity_closed_mid_walk_is_skipped(
    server: FakeAmo, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = server._api

    def flaky(route: str, query: dict[str, str], user: int) -> Any:
        if route == "leads/1001/files":
            return original("leads/999/files", query, user)  # 404
        return original(route, query, user)

    monkeypatch.setattr(server, "_api", flaky)
    anna = await listed(make_adapter(server), "lead_files")
    assert set(anna) == {"file:f-boris", "file:f-other"}


async def test_account_blocked_mid_walk_is_not_an_empty_listing(
    server: FakeAmo, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = server._api

    def block_after_list(route: str, query: dict[str, str], user: int) -> Any:
        response = original(route, query, user)
        if route == "leads":
            server.blocked = True
        return response

    monkeypatch.setattr(server, "_api", block_after_list)
    with pytest.raises(AdapterConfigError, match="account_forbidden"):
        await listed(make_adapter(server), "lead_files")


async def test_pipelines_narrow_the_leads(server: FakeAmo) -> None:
    anna = await listed(make_adapter(server, pipelines=(10,)), "lead_files")
    assert set(anna) == {"file:f-offer", "file:f-boris"}
    assert "/api/v4/leads/1003/files" not in {p for _, p, _ in server.calls}


async def test_entities_and_entity_files_are_paged(
    server: FakeAmo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files_module, "ENTITY_PAGE", 2)
    monkeypatch.setattr(files_module, "FILES_PAGE", 2)
    monkeypatch.setattr(files_module, "METADATA_BATCH", 2)
    for n in range(4):
        server.add_file(f"f-many-{n}", f"Файл {n}.txt", b"x")
        server.link("leads", 1003, f"f-many-{n}")
    anna = await listed(make_adapter(server), "lead_files")
    assert {f"file:f-many-{n}" for n in range(4)} <= set(anna)
    assert len(anna) == 7
    pages = [q["page"] for _, p, q in server.calls if p == "/api/v4/leads"]
    assert pages == ["1", "2"]
    cursors = [
        q.get("before_id")
        for _, p, q in server.calls
        if p == "/api/v4/leads/1003/files"
    ]
    assert len(cursors) == 3 and cursors[0] is None
    assert int(cursors[1] or 0) > int(cursors[2] or 0)


async def test_too_many_entities_stop_instead_of_truncating(
    server: FakeAmo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files_module, "MAX_ENTITIES", 2)
    with pytest.raises(AdapterError) as caught:
        await listed(make_adapter(server), "lead_files")
    assert caught.value.code == "too_many_entities" and not caught.value.retryable
    # Сужение воронками укладывает обход в предел.
    narrowed = await listed(make_adapter(server, pipelines=(10,)), "lead_files")
    assert set(narrowed) == {"file:f-offer", "file:f-boris"}


async def test_drive_without_files_scope_is_a_config_error(server: FakeAmo) -> None:
    server.files_scope = False
    with pytest.raises(AdapterConfigError, match="files_scope_missing"):
        await listed(make_adapter(server), "lead_files")


async def test_new_version_changes_document_version(server: FakeAmo) -> None:
    before = (await listed(make_adapter(server), "lead_files"))["file:f-offer"]
    server.new_version("f-offer", "Новое КП.".encode())
    after = (await listed(make_adapter(server), "lead_files"))["file:f-offer"]
    assert after.version != before.version


# --- скачивание --------------------------------------------------------------------


async def test_fetch_downloads_by_the_version_link(server: FakeAmo) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter, "lead_files")
    content = await adapter.fetch(documents["file:f-offer"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedFile)
    assert content.data == "Коммерческое предложение: 100 ед.".encode()
    assert content.filename == "КП.txt"
    downloads = [p for h, p, _ in server.calls if p.startswith("/download/")]
    assert downloads == ["/download/acc-seg/f-offer/v-f-offer-1/КП.txt"]


async def test_redirect_to_storage_drops_the_token(server: FakeAmo) -> None:
    server.download_redirect = True
    adapter = make_adapter(server)
    documents = await listed(adapter, "lead_files")
    content = await adapter.fetch(documents["file:f-offer"], max_bytes=MAX_BYTES)
    assert content.data.startswith("Коммерческое".encode())  # type: ignore[union-attr]
    assert server.foreign_auth == [""]


async def test_fetch_limits_and_trash(server: FakeAmo) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter, "lead_files")
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(documents["file:f-offer"], max_bytes=5)
    server.files["f-offer"]["is_trashed"] = True
    with pytest.raises(AdapterError, match="file_not_found"):
        await adapter.fetch(documents["file:f-offer"], max_bytes=MAX_BYTES)
    del server.files["f-offer"]
    with pytest.raises(AdapterError, match="file_not_found"):
        await adapter.fetch(documents["file:f-offer"], max_bytes=MAX_BYTES)


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example.com/steal",
        "https://drive-b.amocrm.ru:bad/x",
        "https://company.amocrm.ru/download/x",
        "http://drive-b.amocrm.ru/download/x",
    ],
)
async def test_foreign_download_link_is_refused(server: FakeAmo, href: str) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter, "lead_files")
    server.files["f-offer"]["_links"] = {"download": {"href": href}}
    with pytest.raises(AdapterError, match="download_url_foreign"):
        await adapter.fetch(documents["file:f-offer"], max_bytes=MAX_BYTES)
    assert "evil.example.com" not in {h for h, _, _ in server.calls}


async def test_recorded_responses_have_no_tokens_or_download_links(
    server: FakeAmo,
) -> None:
    recorded: list[tuple[str, Any, Any]] = []
    auth = AmoAuth(TokenSet(ANNA_ACCESS, "", 0), None)
    client = AmoClient(
        server.client(),
        account=ACCOUNT,
        auth=auth,
        min_interval=0.0,
        recorder=lambda *entry: recorded.append(entry),
    )
    adapter = AmoCrmAdapter(client, max_bytes=MAX_BYTES)
    await listed(adapter, "lead_files")
    text = repr(recorded)
    assert ANNA_ACCESS not in text
    assert "/download/" not in text
    assert any(label == "drive:files" for label, _, _ in recorded)


async def test_unknown_document_is_refused(server: FakeAmo) -> None:
    stray = RemoteDocument(
        external_id="other:1",
        title="x",
        url="",
        version="",
        kind=RemoteDocumentKind.FILE,
        module="lead_files",
    )
    with pytest.raises(AdapterError, match="unknown_document"):
        await make_adapter(server).fetch(stray, max_bytes=MAX_BYTES)
