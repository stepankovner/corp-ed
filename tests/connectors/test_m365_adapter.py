"""Адаптер Microsoft 365 (SharePoint и OneDrive через Graph): токен
приложения, обход сайтов и библиотек через delta, права из permissions
с раскрытием групп, ссылки общего доступа, скачивание по редиректу,
лимиты и ошибки."""

import json
from typing import Any

import pytest
from cryptography.fernet import Fernet

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    FetchedFile,
    RemoteDocument,
)
from corp_ed.connectors.common import counting_skips
from corp_ed.connectors.m365 import KIND, SPEC, register
from corp_ed.connectors.m365 import adapter as m365_adapter
from corp_ed.connectors.m365.adapter import M365Adapter, build_adapter
from corp_ed.connectors.m365.auth import ClientCredentials
from corp_ed.connectors.m365.client import GraphClient
from corp_ed.connectors.registry import AdapterRegistry
from corp_ed.core.config import ConnectorSettings
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind
from tests.connectors.fake_m365 import (
    CLIENT_ID,
    D_ANNA,
    D_MKT,
    D_ROOT,
    G_FINANCE,
    MY_HOST,
    SECRET,
    SP_HOST,
    TENANT_DOMAIN,
    TENANT_ID,
    FakeM365,
    perm_site_group,
    sample_m365,
)

KEY = Fernet.generate_key().decode()
MAX_BYTES = 1024 * 1024
CONFIG = {"tenant_id": TENANT_ID, "client_id": CLIENT_ID}
CREDENTIALS = {"client_secret": SECRET}
ANNA = "anna@contoso.ru"
BORIS = "boris@contoso.ru"
VERA = "vera@contoso.ru"
DINA = f"dina@{TENANT_DOMAIN}"
GLEB = "gleb@contoso.ru"


def settings(**overrides: Any) -> ConnectorSettings:
    return ConnectorSettings(
        secrets_keys=KEY,
        max_document_bytes=MAX_BYTES,
        max_large_document_bytes=MAX_BYTES,
        **overrides,
    )  # type: ignore[arg-type]


class Sleeps(list[float]):
    async def __call__(self, seconds: float) -> None:
        self.append(seconds)


def make_adapter(
    server: FakeM365,
    *,
    config: dict[str, str] | None = None,
    sleeps: Sleeps | None = None,
) -> M365Adapter:
    return build_adapter(
        {**CONFIG, **(config or {})},
        CREDENTIALS,
        server.client(),
        settings(),
        sleep=sleeps if sleeps is not None else Sleeps(),
    )


async def listed(adapter: M365Adapter, *modules: str) -> dict[str, RemoteDocument]:
    return {d.external_id: d async for d in adapter.list(list(modules))}


def doc_id(drive: str, item: str) -> str:
    return f"m365:{drive}/{item}"


@pytest.fixture
def server() -> FakeM365:
    return sample_m365()


# --- спецификация ------------------------------------------------------------------


def test_spec_is_corporate_preview_organization() -> None:
    assert SPEC.kind == KIND == "m365"
    assert SPEC.mode is ConnectorMode.ORGANIZATION
    assert SPEC.preview
    assert not SPEC.base
    assert [m.name for m in SPEC.modules] == ["sharepoint", "onedrive"]
    assert [f.name for f in SPEC.config_fields] == ["tenant_id", "client_id", "sites"]
    assert [f.required for f in SPEC.config_fields] == [True, True, False]
    assert [f.name for f in SPEC.credential_fields] == ["client_secret"]
    assert SPEC.credential_fields[0].secret
    assert SPEC.url_field is None
    assert set(SPEC.extra["permissions"].split(",")) == {
        "Sites.Read.All",
        "Files.Read.All",
        "User.Read.All",
        "GroupMember.Read.All",
    }


@pytest.mark.parametrize(
    ("config", "code"),
    [
        ({}, None),
        ({"tenant_id": TENANT_DOMAIN}, None),
        ({"tenant_id": TENANT_ID.upper()}, None),
        ({"tenant_id": "common"}, "tenant_id_invalid"),
        ({"tenant_id": "https://login.microsoftonline.com/x"}, "tenant_id_invalid"),
        ({"client_id": "kronto"}, "client_id_invalid"),
        (
            {
                "sites": "https://contoso.sharepoint.com/sites/hr, "
                "https://contoso.sharepoint.com/"
            },
            None,
        ),
        ({"sites": "https://evil.example.net/sites/hr"}, "sites_invalid"),
        ({"sites": "http://contoso.sharepoint.com/sites/hr"}, "sites_invalid"),
        ({"sites": "https://contoso.sharepoint.com/sites/../x"}, "sites_invalid"),
        ({"sites": "https://contoso.sharepoint.com/sites/hr?x=1"}, "sites_invalid"),
    ],
)
def test_config_check(config: dict[str, str], code: str | None) -> None:
    assert SPEC.config_check is not None
    assert SPEC.config_check({**CONFIG, **config}) == code


@pytest.mark.parametrize(
    ("secret", "code"),
    [(SECRET, None), ("  ", "client_secret_invalid"), ("a b", "client_secret_invalid")],
)
def test_credentials_check(secret: str, code: str | None) -> None:
    assert SPEC.credentials_check is not None
    assert SPEC.credentials_check({"client_secret": secret}) == code


def test_register_hides_kind_until_enabled(server: FakeM365) -> None:
    registry = AdapterRegistry()
    register(registry, settings())
    assert "m365" not in [spec.kind for spec in registry.kinds()]
    assert isinstance(
        registry.build("m365", CONFIG, CREDENTIALS, server.client()), M365Adapter
    )
    with pytest.raises(AdapterAuthError, match="credentials_missing"):
        registry.build("m365", CONFIG, {}, server.client())
    with pytest.raises(AdapterConfigError, match="app_settings_missing"):
        registry.build("m365", {"tenant_id": TENANT_ID}, CREDENTIALS, server.client())

    enabled = AdapterRegistry(enabled_preview_kinds=frozenset({"m365"}))
    register(enabled, settings())
    assert "m365" in [spec.kind for spec in enabled.kinds()]


# --- проверка и токен ----------------------------------------------------------


async def test_check_requests_app_token_and_probes_permissions(
    server: FakeM365,
) -> None:
    await make_adapter(server).check()

    assert server.token_requests == [
        {
            "client_id": CLIENT_ID,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        }
    ]
    assert [path for path, _ in server.calls] == ["sites", "users", "groups"]


async def test_token_is_cached_for_the_run(server: FakeM365) -> None:
    adapter = make_adapter(server)
    await adapter.check()
    await listed(adapter, "sharepoint")
    assert len(server.token_requests) == 1


@pytest.mark.parametrize(
    ("setup", "config", "error", "code"),
    [
        ({"secret": "other"}, {}, AdapterAuthError, "client_secret_invalid"),
        ({"secret_expired": True}, {}, AdapterAuthError, "client_secret_expired"),
        (
            {},
            {"client_id": "11111111-2222-4333-8444-555555555555"},
            AdapterConfigError,
            "app_not_in_tenant",
        ),
        (
            {},
            {"tenant_id": "other.onmicrosoft.com"},
            AdapterConfigError,
            "tenant_not_found",
        ),
        (
            {"roles": {"Files.Read.All", "User.Read.All", "GroupMember.Read.All"}},
            {},
            AdapterConfigError,
            "permission_missing_sites",
        ),
        (
            {"roles": {"Sites.Read.All", "Files.Read.All", "GroupMember.Read.All"}},
            {},
            AdapterConfigError,
            "permission_missing_users",
        ),
        (
            {"roles": {"Sites.Read.All", "Files.Read.All", "User.Read.All"}},
            {},
            AdapterConfigError,
            "permission_missing_groups",
        ),
        ({"reject_tokens": True}, {}, AdapterAuthError, "unauthorized"),
    ],
)
async def test_check_errors(
    server: FakeM365,
    setup: dict[str, Any],
    config: dict[str, str],
    error: type[AdapterError],
    code: str,
) -> None:
    for name, value in setup.items():
        setattr(server, name, value)
    with pytest.raises(error) as caught:
        await make_adapter(server, config=config).check()
    assert caught.value.code == code


async def test_token_endpoint_overload_is_retryable(server: FakeM365) -> None:
    sleeps = Sleeps()
    server.token_unavailable = True
    with pytest.raises(AdapterError) as caught:
        await make_adapter(server, sleeps=sleeps).check()
    assert caught.value.code == "http_503"
    assert caught.value.retryable
    assert sleeps == [1.0, 2.0, 4.0]


# --- SharePoint ---------------------------------------------------------------


async def test_sharepoint_documents_and_readers(server: FakeM365) -> None:
    with counting_skips() as skips:
        documents = await listed(make_adapter(server), "sharepoint")

    readers = {key: set(d.allowed_emails) for key, d in documents.items()}
    assert readers == {
        # Группа безопасности с вложенной: Вера и Дина (UPN — почты нет),
        # Глеб отключён.
        doc_id(D_ROOT, "f-rules"): {VERA, DINA},
        # «Все, кроме внешних пользователей» — не «вся компания».
        doc_id(D_ROOT, "f-all"): set(),
        # Владельцы и участники сайта группы; посетители — никому.
        doc_id(D_MKT, "f-plan"): {ANNA, BORIS},
        # Ссылка на организацию без перечисленных — ничего не добавляет.
        doc_id(D_MKT, "f-budget"): {ANNA, BORIS},
        # Анонимная ссылка — только тем, кому выдана.
        doc_id(D_MKT, "f-secret"): {VERA},
        # Подсайт делит группы SharePoint с сайтом группы.
        doc_id("b!mkt-projects", "f-project"): {ANNA, BORIS},
    }
    assert all(
        d.visibility is MaterialVisibility.RESTRICTED for d in documents.values()
    )
    assert skips.formats() == {".png": 1}

    plan = documents[doc_id(D_MKT, "f-plan")]
    assert plan.title == plan.filename == "План.txt"
    assert plan.module == "sharepoint"
    assert plan.kind is RemoteDocumentKind.FILE
    assert plan.path == "SharePoint/Маркетинг/Документы/Планы/2026"
    assert plan.url == f"https://{SP_HOST}/sites/marketing/Документы/План.txt"
    assert plan.version == '"{f-plan},1"|"c:{f-plan},1"'
    assert plan.size == len("План запуска — в мае.".encode())
    assert plan.modified_at is not None
    # Личный сайт из /sites в SharePoint не попадает.
    assert not any(d.path.startswith("SharePoint/Анна") for d in documents.values())


async def test_listing_uses_select_delta_and_no_advanced_queries(
    server: FakeM365,
) -> None:
    await listed(make_adapter(server), "sharepoint")

    delta = [q for path, q in server.calls if path.endswith("/root/delta")]
    assert delta and all("deleted" in q["$select"].split(",") for q in delta)
    directory = [
        q
        for path, q in server.calls
        if path.endswith(("/transitiveMembers", "/owners"))
    ]
    assert directory and all(q == {} for q in directory)
    # Сайты групп ищутся один раз за запуск.
    lookups = [p for p, _ in server.calls if p.endswith("/sites/root")]
    assert len(lookups) == 1


async def test_pagination_follows_next_links(server: FakeM365) -> None:
    full = await listed(make_adapter(server), "sharepoint", "onedrive")
    server.page_size = 1
    server.calls.clear()

    paged = await listed(make_adapter(server), "sharepoint", "onedrive")

    assert {k: d.allowed_emails for k, d in paged.items()} == {
        k: d.allowed_emails for k, d in full.items()
    }
    assert any("$skiptoken" in q for path, q in server.calls if "delta" in path)
    assert any("$skiptoken" in q for path, q in server.calls if path == "sites")


async def test_foreign_next_link_is_not_followed(server: FakeM365) -> None:
    server.page_size = 1
    server.foreign_next_link = True
    with pytest.raises(AdapterError, match="pagination_foreign"):
        await listed(make_adapter(server), "sharepoint")


async def test_russian_site_group_names_are_recognised(server: FakeM365) -> None:
    item = server.drives[D_MKT]["items"]["f-plan"]
    item["_perms"] = [perm_site_group("Участники сайта Маркетинг", "write")]

    documents = await listed(make_adapter(server), "sharepoint")

    assert documents[doc_id(D_MKT, "f-plan")].allowed_emails == {ANNA, BORIS}


async def test_unknown_site_group_and_unknown_role_give_nobody(
    server: FakeM365,
) -> None:
    item = server.drives[D_MKT]["items"]["f-plan"]
    item["_perms"] = [
        perm_site_group("Финансы Редакторы", "write", "9"),
        {**perm_site_group("Маркетинг Members", "sp.limitedAccess")},
    ]

    documents = await listed(make_adapter(server), "sharepoint")

    assert documents[doc_id(D_MKT, "f-plan")].allowed_emails == frozenset()


async def test_deleted_items_and_unreadable_permissions_are_skipped(
    server: FakeM365,
) -> None:
    server.delete_item(D_MKT, "f-budget")
    server.forbidden_permissions.add("f-secret")

    documents = await listed(make_adapter(server), "sharepoint")

    assert doc_id(D_MKT, "f-budget") not in documents
    assert doc_id(D_MKT, "f-secret") not in documents
    assert doc_id(D_MKT, "f-plan") in documents


async def test_forbidden_library_is_skipped(server: FakeM365) -> None:
    server.forbidden_drives.add(D_ROOT)

    documents = await listed(make_adapter(server), "sharepoint")

    assert not any(key.startswith(f"m365:{D_ROOT}/") for key in documents)
    assert doc_id(D_MKT, "f-plan") in documents


async def test_huge_library_stops_the_run(
    server: FakeM365, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(m365_adapter, "MAX_DRIVE_ITEMS", 2)
    with pytest.raises(AdapterError, match="tree_too_large"):
        await listed(make_adapter(server), "sharepoint")


async def test_version_changes_with_etag(server: FakeM365) -> None:
    before = await listed(make_adapter(server), "sharepoint")
    server.touch(D_MKT, "f-plan", "План запуска — в июне.".encode(), etag="2")

    after = await listed(make_adapter(server), "sharepoint")

    key = doc_id(D_MKT, "f-plan")
    assert before[key].version != after[key].version
    assert (
        before[doc_id(D_MKT, "f-budget")].version
        == after[doc_id(D_MKT, "f-budget")].version
    )


# --- Sites.Selected -------------------------------------------------------------


async def test_selected_sites_without_tenant_wide_read(server: FakeM365) -> None:
    server.roles = {"Sites.Selected", "User.Read.All", "GroupMember.Read.All"}
    server.selected_sites = {
        "contoso.sharepoint.com,c0000000-0000-4000-8000-000000000002,"
        "d0000000-0000-4000-8000-000000000002",
        "contoso.sharepoint.com,c0000000-0000-4000-8000-000000000002,"
        "d0000000-0000-4000-8000-000000000003",
    }
    adapter = make_adapter(
        server, config={"sites": f"https://{SP_HOST}/sites/marketing"}
    )

    await adapter.check()
    documents = await listed(adapter, "sharepoint")

    assert ("sites", {"$select": "id"}) not in server.calls
    assert set(documents) == {
        doc_id(D_MKT, "f-plan"),
        doc_id(D_MKT, "f-budget"),
        doc_id(D_MKT, "f-secret"),
        doc_id("b!mkt-projects", "f-project"),
    }
    # Сайт группы не находится (/groups/{id}/sites/root требует
    # Sites.Read.All): группы SharePoint — никому, явные права остаются.
    assert documents[doc_id(D_MKT, "f-plan")].allowed_emails == frozenset()
    assert documents[doc_id(D_MKT, "f-budget")].allowed_emails == {BORIS}


async def test_selected_site_not_granted_fails_check(server: FakeM365) -> None:
    server.roles = {"Sites.Selected", "User.Read.All", "GroupMember.Read.All"}
    server.selected_sites = set()
    adapter = make_adapter(
        server, config={"sites": f"https://{SP_HOST}/sites/marketing"}
    )
    with pytest.raises(AdapterConfigError, match="site_unavailable"):
        await adapter.check()


# --- OneDrive -------------------------------------------------------------------


async def test_onedrive_documents_and_readers(server: FakeM365) -> None:
    documents = await listed(make_adapter(server), "onedrive")

    assert {k: set(d.allowed_emails) for k, d in documents.items()} == {
        doc_id(D_ANNA, "f-draft"): {ANNA},
        doc_id(D_ANNA, "f-for-boris"): {ANNA, BORIS},
        doc_id(D_ANNA, "f-org-link"): {ANNA},
    }
    draft = documents[doc_id(D_ANNA, "f-draft")]
    assert draft.module == "onedrive"
    assert draft.path == f"OneDrive/{ANNA}"
    # cTag delta OneDrive для бизнеса не отдаёт: версия держится на eTag.
    assert draft.version == '"{f-draft},1"|'
    drive_calls = [p for p, _ in server.calls if p.endswith("/drive")]
    # Отключённый Глеб и гость не обходятся; у Дины OneDrive нет (404).
    assert len(drive_calls) == 4


async def test_onedrive_without_files_permission_is_config_error(
    server: FakeM365,
) -> None:
    server.roles = {"User.Read.All", "GroupMember.Read.All"}
    with pytest.raises(AdapterConfigError, match="permission_missing_files"):
        await listed(make_adapter(server), "onedrive")


# --- скачивание ---------------------------------------------------------------


async def test_fetch_follows_redirect_without_bearer(server: FakeM365) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter, "sharepoint", "onedrive")

    plan = await adapter.fetch(documents[doc_id(D_MKT, "f-plan")], max_bytes=MAX_BYTES)
    draft = await adapter.fetch(
        documents[doc_id(D_ANNA, "f-draft")], max_bytes=MAX_BYTES
    )

    assert plan == FetchedFile(
        data="План запуска — в мае.".encode(), filename="План.txt"
    )
    assert isinstance(draft, FetchedFile) and draft.data == b"Draft"
    # Подписанный адрес — без токена приложения.
    assert server.downloads == [(SP_HOST, ""), (MY_HOST, "")]


async def test_fetch_size_limits(server: FakeM365) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter, "sharepoint")
    plan = documents[doc_id(D_MKT, "f-plan")]

    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(plan, max_bytes=5)
    assert server.downloads == []

    # Размер из листинга мог устареть: обрыв по факту чтения.
    unsized = RemoteDocument(**{**plan.__dict__, "size": None})
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(unsized, max_bytes=5)


async def test_too_large_file_is_not_listed(server: FakeM365) -> None:
    server.drives[D_MKT]["items"]["f-plan"]["size"] = MAX_BYTES + 1
    with counting_skips() as skips:
        documents = await listed(make_adapter(server), "sharepoint")
    assert doc_id(D_MKT, "f-plan") not in documents
    assert skips.too_large == {f"{D_MKT}/f-plan"}


# --- лимиты и ошибки Graph ---------------------------------------------------------


async def test_rate_limit_waits_retry_after(server: FakeM365) -> None:
    sleeps = Sleeps()
    server.rate_limit_hits = 2
    server.retry_after = "7"

    documents = await listed(make_adapter(server, sleeps=sleeps), "sharepoint")

    assert sleeps == [7.0, 7.0]
    assert doc_id(D_MKT, "f-plan") in documents


async def test_rate_limit_exhausted_is_retryable(server: FakeM365) -> None:
    sleeps = Sleeps()
    server.rate_limit_hits = 100
    server.retry_after = "600"
    with pytest.raises(AdapterError) as caught:
        await listed(make_adapter(server, sleeps=sleeps), "sharepoint")
    assert caught.value.code == "rate_limited"
    assert caught.value.retryable
    # Retry-After больше минуты не держит воркер дольше потолка.
    assert sleeps and max(sleeps) <= 60.0


async def test_service_unavailable_is_retryable(server: FakeM365) -> None:
    server.unavailable_hits = 100
    with pytest.raises(AdapterError) as caught:
        await make_adapter(server).check()
    assert caught.value.code == "http_503"
    assert caught.value.retryable


async def test_expired_token_is_renewed_once(server: FakeM365) -> None:
    adapter = make_adapter(server)
    await adapter.check()
    server.revoked |= server.tokens

    await adapter.check()

    assert len(server.token_requests) == 2


async def test_fetch_of_deleted_file_is_not_found(server: FakeM365) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter, "sharepoint")
    server.delete_item(D_MKT, "f-plan")
    with pytest.raises(AdapterError) as caught:
        await adapter.fetch(documents[doc_id(D_MKT, "f-plan")], max_bytes=MAX_BYTES)
    assert caught.value.code == "not_found"
    assert not caught.value.retryable


async def test_group_unreadable_gives_nobody(server: FakeM365) -> None:
    server.groups.pop(G_FINANCE)
    documents = await listed(make_adapter(server), "sharepoint")
    assert documents[doc_id(D_ROOT, "f-rules")].allowed_emails == frozenset()


# --- запись фикстур -------------------------------------------------------------


async def test_recorder_keeps_no_tokens_or_signed_urls(server: FakeM365) -> None:
    records: list[tuple[str, Any, Any]] = []

    def recorder(path: str, params: Any, data: Any) -> None:
        records.append((path, params, data))

    adapter = build_adapter(
        CONFIG, CREDENTIALS, server.client(), settings(), recorder=recorder
    )
    await listed(adapter, "sharepoint")
    # Delta без $select несёт подписанные ссылки — они вырезаются.
    client = GraphClient(
        server.client(),
        ClientCredentials(
            server.client(),
            tenant=TENANT_ID,
            client_id=CLIENT_ID,
            client_secret=SECRET,
        ),
        recorder=recorder,
    )
    raw = await client.get(f"drives/{D_MKT}/root/delta")
    assert any("@microsoft.graph.downloadUrl" in item for item in raw["value"])

    dumped = json.dumps(records, ensure_ascii=False)
    assert records
    assert "tempauth" not in dumped
    assert "downloadUrl" not in dumped
    assert SECRET not in dumped
    assert "eyJ0eXAi" not in dumped
