"""Адаптер Р7-Офис КС 2019 (режим organization): каталог, вход и клиент
(повторный вход, лимиты, ошибки), обход «Общих документов», зеркало прав
по ответам `.../share` (никогда не шире, чем решает сам сервер), файлы по
подписанной ссылке."""

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
from corp_ed.connectors.r7office import KIND, SPEC, register
from corp_ed.connectors.r7office import adapter as adapter_module
from corp_ed.connectors.r7office import client as client_module
from corp_ed.connectors.r7office.adapter import R7OfficeAdapter, build_adapter
from corp_ed.connectors.r7office.client import R7Client
from corp_ed.connectors.r7office.rights import Ace, Person, can_read
from corp_ed.connectors.registry import AdapterRegistry
from corp_ed.core.config import ConnectorSettings
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind
from tests.connectors.fake_r7office import (
    ADMIN_ID,
    ADMIN_LOGIN,
    ADMIN_PASSWORD,
    ANNA_ID,
    BORIS_ID,
    EVERYONE,
    FINANCE,
    FULL,
    READ,
    RESTRICT,
    REVIEW,
    USER,
    VERA_ID,
    FakeR7,
    sample_r7,
)

KEY = Fernet.generate_key().decode()
MAX_BYTES = 1024 * 1024
ALL = {"admin@company.ru", "anna@company.ru", "boris@company.ru", "vera@company.ru"}


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
    server: FakeR7,
    login: str = ADMIN_LOGIN,
    password: str = ADMIN_PASSWORD,
    *,
    sleep: Sleeps | None = None,
    folders: tuple[str, ...] = (),
    recorder: Any = None,
) -> R7OfficeAdapter:
    client = R7Client(
        server.client(),
        base_url=server.base,
        login=login,
        password=password,
        sleep=sleep or Sleeps(),
        recorder=recorder,
    )
    return R7OfficeAdapter(client, max_bytes=MAX_BYTES, folders=folders)


async def listed(adapter: R7OfficeAdapter) -> dict[str, RemoteDocument]:
    return {d.external_id: d async for d in adapter.list(["common"])}


def emails(document: RemoteDocument) -> set[str]:
    return set(document.allowed_emails)


@pytest.fixture
def server() -> FakeR7:
    return sample_r7()


# --- каталог и сборка -------------------------------------------------------------


def test_spec_is_corporate_hidden_organization_kind() -> None:
    assert SPEC.kind == KIND == "r7office"
    assert SPEC.mode is ConnectorMode.ORGANIZATION
    assert SPEC.preview and not SPEC.base
    assert [m.name for m in SPEC.modules] == ["common"]
    assert [(f.name, f.required) for f in SPEC.config_fields] == [
        ("base_url", True),
        ("folders", False),
    ]
    assert [(f.name, f.secret) for f in SPEC.credential_fields] == [
        ("login", False),
        ("password", True),
    ]
    assert SPEC.url_field == "base_url"


def test_spec_checks_folder_ids() -> None:
    assert SPEC.config_check is not None
    assert SPEC.config_check({"base_url": "https://x.ru"}) is None
    assert SPEC.config_check({"folders": " 12, 7 "}) is None
    assert SPEC.config_check({"folders": "12,@my"}) == "folders_invalid"


def test_kind_is_offered_only_when_enabled(server: FakeR7) -> None:
    hidden = AdapterRegistry()
    register(hidden, settings())
    assert hidden.kinds() == []
    assert hidden.spec("r7office").preview
    enabled = AdapterRegistry(enabled_preview_kinds=frozenset({"r7office"}))
    register(enabled, settings())
    assert [spec.kind for spec in enabled.kinds()] == ["r7office"]
    adapter = enabled.build(
        "r7office",
        {"base_url": server.base},
        {"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD},
        server.client(),
    )
    assert isinstance(adapter, R7OfficeAdapter)


def test_build_requires_https_address_and_credentials(server: FakeR7) -> None:
    creds = {"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD}
    with pytest.raises(AdapterConfigError, match="base_url_invalid"):
        build_adapter({}, creds, server.client(), settings())
    with pytest.raises(AdapterConfigError, match="base_url_invalid"):
        build_adapter({"base_url": "http://x.ru"}, creds, server.client(), settings())
    with pytest.raises(AdapterConfigError, match="folders_invalid"):
        build_adapter(
            {"base_url": server.base, "folders": "a"},
            creds,
            server.client(),
            settings(),
        )
    with pytest.raises(AdapterAuthError, match="credentials_missing"):
        build_adapter(
            {"base_url": server.base}, {"login": "x"}, server.client(), settings()
        )


@pytest.mark.parametrize(
    "address",
    [
        "https://office.company.ru",
        "https://office.company.ru/",
        "https://office.company.ru/api/2.0",
        "https://office.company.ru/api/",
    ],
)
async def test_api_address_is_normalized(server: FakeR7, address: str) -> None:
    adapter = build_adapter(
        {"base_url": address},
        {"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD},
        server.client(),
        settings(),
    )
    await adapter.check()
    assert [route for _, route, _ in server.calls] == ["authentication", "people/@self"]


# --- вход и клиент ----------------------------------------------------------------


async def test_token_goes_in_authorization_header_as_is(server: FakeR7) -> None:
    await make_adapter(server).check()
    assert server.auth_headers == ["token-1"]


async def test_check_accepts_portal_and_documents_admins_only(server: FakeR7) -> None:
    with pytest.raises(AdapterAuthError, match="admin_required"):
        await make_adapter(server, "anna@company.ru", "pw").check()
    server.users[ANNA_ID]["listAdminModules"] = ["documents"]
    await make_adapter(server, "anna@company.ru", "pw").check()


async def test_wrong_password_and_two_factor_are_auth_errors(server: FakeR7) -> None:
    with pytest.raises(AdapterAuthError, match="unauthorized"):
        await make_adapter(server, ADMIN_LOGIN, "wrong").check()
    server.tfa_logins.add(ADMIN_LOGIN)
    with pytest.raises(AdapterAuthError, match="tfa_not_supported"):
        await make_adapter(server).check()


async def test_expired_token_is_renewed_once(server: FakeR7) -> None:
    adapter = make_adapter(server)
    await adapter.check()
    server.expire_after = 0
    documents = await listed(adapter)
    assert "file:100" in documents
    assert [r for _, r, _ in server.calls].count("authentication") == 2
    server.always_expired = True
    with pytest.raises(AdapterAuthError, match="unauthorized"):
        await listed(make_adapter(server))


async def test_rate_limit_honours_retry_after_then_gives_up(server: FakeR7) -> None:
    server.rate_limit_hits = 1
    server.retry_after = "3"
    sleeps = Sleeps()
    await make_adapter(server, sleep=sleeps).check()
    assert sleeps.calls == [3.0]
    server.rate_limit_hits = 100
    server.retry_after = None
    sleeps = Sleeps()
    with pytest.raises(AdapterError) as caught:
        await make_adapter(server, sleep=sleeps).check()
    assert caught.value.code == "rate_limited" and caught.value.retryable
    assert sleeps.calls == list(client_module.RATE_LIMIT_BACKOFF)


async def test_server_errors_are_retried_then_retryable(server: FakeR7) -> None:
    server.server_errors = 1
    await make_adapter(server).check()
    server.server_errors = 100
    with pytest.raises(AdapterError) as caught:
        await make_adapter(server).check()
    assert caught.value.code == "http_503" and caught.value.retryable


async def test_recorder_keeps_no_token_or_signed_link(server: FakeR7) -> None:
    recorded: list[tuple[str, Any, Any]] = []
    adapter = make_adapter(
        server,
        recorder=lambda path, params, data: recorded.append((path, params, data)),
    )
    documents = await listed(adapter)
    await adapter.fetch(documents["file:100"], max_bytes=MAX_BYTES)
    dump = repr(recorded)
    assert "token-1" not in dump and ADMIN_PASSWORD not in dump
    assert "stream_auth" not in dump
    assert any(path == "people" for path, _, _ in recorded)


# --- обход --------------------------------------------------------------------------


async def test_common_tree_is_walked_with_paths(server: FakeR7) -> None:
    with counting_skips() as tally:
        documents = await listed(make_adapter(server))
    assert set(documents) == {
        "file:100",
        "file:103",
        "file:200",
        "file:201",
        "file:202",
    }
    budget = documents["file:200"]
    assert budget.title == budget.filename == "Бюджет.txt"
    assert budget.path == "Общие документы/Финансы/Бюджет"
    assert budget.kind is RemoteDocumentKind.FILE
    assert budget.module == "common"
    assert budget.url == f"{server.base}Products/Files/DocEditor.aspx?fileid=200"
    assert budget.version == "1:2026-09-01T10:00:00.0000000Z"
    assert budget.size == len("Смета на год.".encode())
    assert budget.modified_at is not None
    assert budget.visibility is MaterialVisibility.RESTRICTED
    # Лишний формат и слишком большой файл считаются, но не отдаются;
    # стороннее хранилище не обходится вовсе.
    assert tally.formats() == {".png": 1}
    assert tally.too_large == {"file:102"}
    assert not [r for _, r, _ in server.calls if "sbox" in r or r == "files/30"]


async def test_unknown_module_lists_nothing(server: FakeR7) -> None:
    assert [d async for d in make_adapter(server).list(["other"])] == []
    assert server.calls == []


async def test_folders_and_people_are_paged(
    server: FakeR7, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "PAGE_SIZE", 2)
    for n in range(5):
        server.add_file(400 + n, f"Файл {n}.txt", 10, b"x")
    documents = await listed(make_adapter(server))
    assert {f"file:{400 + n}" for n in range(5)} <= set(documents)
    pages = [q for _, r, q in server.calls if r == "files/10"]
    assert [q["startIndex"] for q in pages] == ["0", "2", "4", "6"]
    people = [q["startIndex"] for _, r, q in server.calls if r == "people"]
    assert people == ["0", "2", "4", "6"]


async def test_configured_folders_narrow_the_walk(server: FakeR7) -> None:
    documents = await listed(make_adapter(server, folders=("21",)))
    assert set(documents) == {"file:200"}
    assert documents["file:200"].path == "Бюджет"
    for order in (("20", "21"), ("21", "20")):
        server.calls.clear()
        nested = [d async for d in make_adapter(server, folders=order).list(["common"])]
        assert sorted(d.external_id for d in nested) == [
            "file:200",
            "file:201",
            "file:202",
        ]
        assert [r for _, r, _ in server.calls].count("files/21") == 1


async def test_configured_folder_outside_common_or_missing_is_a_config_error(
    server: FakeR7,
) -> None:
    server.add_folder(50, "Мои документы", None, root_type=USER)
    with pytest.raises(AdapterConfigError, match="folder_not_common"):
        await listed(make_adapter(server, folders=("50",)))
    with pytest.raises(AdapterConfigError, match="folder_not_found"):
        await listed(make_adapter(server, folders=("999",)))


async def test_oversized_or_deep_tree_stops_instead_of_truncating(
    server: FakeR7, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter_module, "MAX_ENTRIES", 4)
    with pytest.raises(AdapterError, match="tree_too_large"):
        await listed(make_adapter(server))
    monkeypatch.setattr(adapter_module, "MAX_ENTRIES", 20_000)
    monkeypatch.setattr(adapter_module, "MAX_DEPTH", 1)
    with pytest.raises(AdapterError, match="tree_too_deep"):
        await listed(make_adapter(server))


async def test_new_version_changes_the_version(server: FakeR7) -> None:
    before = (await listed(make_adapter(server)))["file:100"].version
    server.files[100].update(version=2, updated="2026-10-01T10:00:00.0000000Z")
    after = (await listed(make_adapter(server)))["file:100"].version
    assert before != after == "2:2026-10-01T10:00:00.0000000Z"


# --- права --------------------------------------------------------------------------


async def test_everyone_means_every_active_portal_user(server: FakeR7) -> None:
    documents = await listed(make_adapter(server))
    # Глеб заблокирован, у гостя нет почты; регистр почты не важен.
    assert emails(documents["file:100"]) == ALL
    assert emails(documents["file:103"]) == ALL


async def test_closed_folder_is_read_by_its_group_minus_personal_ban(
    server: FakeR7,
) -> None:
    documents = await listed(make_adapter(server))
    assert emails(documents["file:200"]) == {"admin@company.ru", "anna@company.ru"}


async def test_own_file_record_opens_the_file_to_that_person(server: FakeR7) -> None:
    documents = await listed(make_adapter(server))
    # У файла своя запись для Бориса. Анну (по группе папки) правило не
    # пускает: по ответу API не отличить, не стоит ли запрет «Всем» на
    # самом файле, — в сторону закрытости.
    assert emails(documents["file:201"]) == {"admin@company.ru", "boris@company.ru"}
    assert server.readers_of(201) == {
        "admin@company.ru",
        "anna@company.ru",
        "boris@company.ru",
    }


async def test_link_access_opens_nothing(server: FakeR7) -> None:
    documents = await listed(make_adapter(server))
    assert emails(documents["file:202"]) == {"admin@company.ru"}


async def test_listing_never_exceeds_what_the_server_allows(server: FakeR7) -> None:
    server.share("folder", 10, BORIS_ID, RESTRICT)
    server.share("file", 100, EVERYONE, RESTRICT)
    server.share("file", 100, VERA_ID, REVIEW)
    server.share("folder", 21, ANNA_ID, RESTRICT)
    server.share("folder", 21, BORIS_ID, FULL)
    server.share("file", 103, FINANCE, RESTRICT)
    documents = await listed(make_adapter(server))
    for external_id, document in documents.items():
        real = server.readers_of(int(external_id.removeprefix("file:")))
        assert emails(document) <= real, external_id
    assert emails(documents["file:100"]) == {"admin@company.ru", "vera@company.ru"}
    assert emails(documents["file:200"]) == {"admin@company.ru", "boris@company.ru"}


async def test_unreadable_rights_skip_the_file(server: FakeR7) -> None:
    server.share_forbidden.add(100)
    documents = await listed(make_adapter(server))
    assert "file:100" not in documents
    assert "file:103" in documents


async def test_unreadable_folder_rights_make_the_rule_strict(server: FakeR7) -> None:
    server.folder_share_forbidden.add(20)
    documents = await listed(make_adapter(server))
    # Без ответа папки любой относящийся запрет решает: Борису «Все» запрещено.
    assert emails(documents["file:201"]) == {"admin@company.ru"}


async def test_unknown_group_has_no_members(server: FakeR7) -> None:
    server.share("folder", 10, EVERYONE, RESTRICT)
    server.share("folder", 10, "99999999-0000-0000-0000-000000000000", READ)
    server.groups["99999999-0000-0000-0000-000000000000"] = {
        "id": "99999999-0000-0000-0000-000000000000",
        "name": "Удалённая",
        "members": {BORIS_ID},
    }
    documents = await listed(make_adapter(server))
    assert emails(documents["file:100"]) == {"admin@company.ru", "boris@company.ru"}
    del server.groups["99999999-0000-0000-0000-000000000000"]
    documents = await listed(make_adapter(server))
    assert emails(documents["file:100"]) == {"admin@company.ru"}


async def test_file_nobody_can_read_is_not_listed(server: FakeR7) -> None:
    # Администратор без почты; «Финансам» на «Бюджет» запрещено.
    server.users[ADMIN_ID]["email"] = None
    server.share("folder", 21, FINANCE, RESTRICT)
    documents = await listed(make_adapter(server))
    assert "file:200" not in documents
    assert server.readers_of(200) == set()


def test_owner_record_with_access_is_not_a_grant() -> None:
    person = Person("boris@company.ru", admin=False)
    owner = Ace(BORIS_ID, group=False, access=FULL, owner=True)
    closed = Ace(EVERYONE, group=True, access=RESTRICT, owner=False)
    assert not can_read(
        BORIS_ID, person, [owner, closed], {}, shared=False, folder_aces=None
    )
    banned = Ace(BORIS_ID, group=False, access=RESTRICT, owner=True)
    opened = Ace(EVERYONE, group=True, access=READ, owner=False)
    assert not can_read(
        BORIS_ID, person, [banned, opened], {}, shared=False, folder_aces=None
    )


def test_unknown_access_values_deny() -> None:
    person = Person("boris@company.ru", admin=False)
    for access in (0, 4, 9, None):
        ace = Ace(EVERYONE, group=True, access=access, owner=False)
        assert not can_read(BORIS_ID, person, [ace], {}, shared=False, folder_aces=None)


# --- содержимое ---------------------------------------------------------------------


async def test_fetch_downloads_by_signed_link_without_token(server: FakeR7) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter)
    content = await adapter.fetch(documents["file:100"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedFile)
    assert content.data == "Отпуск — 28 дней.".encode()
    assert content.filename == "Отпуск.txt"
    assert server.download_auth == [""]


async def test_signed_link_over_http_on_portal_host_is_upgraded(server: FakeR7) -> None:
    server.presigned_scheme = "http"
    adapter = make_adapter(server)
    documents = await listed(adapter)
    content = await adapter.fetch(documents["file:100"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedFile)


async def test_fetch_limits_and_login_page(server: FakeR7) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter)
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(documents["file:100"], max_bytes=5)
    server.download_html = True
    with pytest.raises(AdapterError, match="download_failed"):
        await adapter.fetch(documents["file:100"], max_bytes=MAX_BYTES)


async def test_fetch_of_deleted_file_is_not_found(server: FakeR7) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter)
    del server.files[100]
    with pytest.raises(AdapterError) as caught:
        await adapter.fetch(documents["file:100"], max_bytes=MAX_BYTES)
    assert caught.value.code == "not_found" and not caught.value.retryable


async def test_unknown_document_is_refused(server: FakeR7) -> None:
    stray = RemoteDocument(
        external_id="other:1",
        title="x",
        url="",
        version="",
        kind=RemoteDocumentKind.FILE,
        module="common",
    )
    with pytest.raises(AdapterError, match="unknown_document"):
        await make_adapter(server).fetch(stray, max_bytes=MAX_BYTES)
