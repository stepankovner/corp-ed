"""Адаптер ELMA365 (режим per_user): каталог, клиент (ошибки, лимит 429),
обход раздела «Файлы» только по тому, что прошло проверку прав по токену
сотрудника, скачивание по временной ссылке, смена версии."""

from typing import Any

import httpx
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
from corp_ed.connectors.elma365 import KIND, SPEC, register
from corp_ed.connectors.elma365 import files as files_module
from corp_ed.connectors.elma365.adapter import Elma365Adapter, build_adapter
from corp_ed.connectors.elma365.client import Elma365Client, site_root
from corp_ed.connectors.registry import AdapterRegistry, UserAuth
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import ConnectorMode, RemoteDocumentKind
from tests.connectors.fake_elma365 import (
    ANNA_TOKEN,
    BASE,
    BORIS_TOKEN,
    DIR_CLOSED,
    DIR_HR,
    FILE_BONUS,
    FILE_PIC,
    FILE_PRIVATE,
    FILE_ROOT,
    FILE_SALARY,
    FILE_VACATION,
    FakeElma365,
    sample_elma365,
    uid,
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
    server: FakeElma365,
    token: str = ANNA_TOKEN,
    *,
    sleep: Sleeps | None = None,
    folders: tuple[str, ...] = (),
    recorder: Any = None,
) -> Elma365Adapter:
    client = Elma365Client(
        server.client(),
        base_url=server.base,
        token=token,
        sleep=sleep or Sleeps(),
        recorder=recorder,
    )
    return Elma365Adapter(client, folders=folders)


async def listed(adapter: Elma365Adapter) -> dict[str, RemoteDocument]:
    return {d.external_id: d async for d in adapter.list(["files"])}


def fid(key: str) -> str:
    return f"file:{key}"


@pytest.fixture
def server() -> FakeElma365:
    return sample_elma365()


# --- каталог и сборка -------------------------------------------------------------


def test_spec_is_corporate_per_user_with_token() -> None:
    assert SPEC.kind == KIND == "elma365"
    assert SPEC.mode is ConnectorMode.PER_USER
    assert SPEC.user_auth is UserAuth.FIELDS and not SPEC.oauth
    assert SPEC.preview and not SPEC.base
    assert [m.name for m in SPEC.modules] == ["files"]
    assert [(f.name, f.required) for f in SPEC.config_fields] == [
        ("base_url", True),
        ("folders", False),
    ]
    assert [(f.name, f.secret) for f in SPEC.credential_fields] == [("token", True)]
    assert SPEC.app_credential_fields == ()
    assert SPEC.url_field == "base_url"


def test_spec_checks_folder_ids() -> None:
    assert SPEC.config_check is not None
    assert SPEC.config_check({"base_url": "https://x.elma365.ru"}) is None
    assert SPEC.config_check({"folders": f" {DIR_HR} , {DIR_CLOSED.upper()} "}) is None
    assert SPEC.config_check({"folders": f"{DIR_HR},docs"}) == "folders_invalid"


def test_kind_is_offered_only_when_enabled(server: FakeElma365) -> None:
    hidden = AdapterRegistry()
    register(hidden, settings())
    assert hidden.kinds() == []
    assert hidden.spec("elma365").preview
    enabled = AdapterRegistry(enabled_preview_kinds=frozenset({"elma365"}))
    register(enabled, settings())
    assert [spec.kind for spec in enabled.kinds()] == ["elma365"]
    adapter = enabled.build(
        "elma365", {"base_url": server.base}, {"token": ANNA_TOKEN}, server.client()
    )
    assert isinstance(adapter, Elma365Adapter)


def test_build_requires_address_token_and_valid_folders(server: FakeElma365) -> None:
    with pytest.raises(AdapterAuthError, match="credentials_missing"):
        build_adapter({"base_url": server.base}, {}, server.client())
    with pytest.raises(AdapterConfigError, match="base_url_missing"):
        build_adapter({}, {"token": ANNA_TOKEN}, server.client())
    with pytest.raises(AdapterConfigError, match="folders_invalid"):
        build_adapter(
            {"base_url": server.base, "folders": "../x"},
            {"token": ANNA_TOKEN},
            server.client(),
        )


@pytest.mark.parametrize(
    "address",
    [
        "https://company.elma365.ru",
        "https://company.elma365.ru/",
        "https://company.elma365.ru/pub/v1",
        "https://company.elma365.ru/pub/v1/",
    ],
)
async def test_api_address_is_normalized(server: FakeElma365, address: str) -> None:
    assert site_root(address) == "https://company.elma365.ru/"
    adapter = build_adapter(
        {"base_url": address}, {"token": ANNA_TOKEN}, server.client()
    )
    await adapter.check()
    assert server.calls[-1][:2] == ("POST", "disk/directory/list")


# --- клиент -------------------------------------------------------------------------


async def test_check_accepts_token_and_rejects_wrong_one(server: FakeElma365) -> None:
    await make_adapter(server).check()
    assert server.calls[-1][2] == {"active": True, "from": 0, "size": 1}
    with pytest.raises(AdapterAuthError, match="unauthorized"):
        await make_adapter(server, "wrong").check()


async def test_disk_closed_to_token_stops_the_grant(server: FakeElma365) -> None:
    server.disk_forbidden.add(server.tokens[BORIS_TOKEN])
    with pytest.raises(AdapterAuthError, match="forbidden"):
        await make_adapter(server, BORIS_TOKEN).check()
    with pytest.raises(AdapterAuthError, match="forbidden"):
        await listed(make_adapter(server, BORIS_TOKEN))


async def test_search_closed_in_one_folder_skips_only_that_folder(
    server: FakeElma365,
) -> None:
    server.search_forbidden.add(DIR_HR)
    files = await listed(make_adapter(server))
    assert set(files) == {fid(FILE_SALARY), fid(FILE_BONUS), fid(FILE_ROOT)}


async def test_rate_limit_waits_retry_after(server: FakeElma365) -> None:
    server.rate_limit_hits = 2
    server.retry_after = "7"
    sleep = Sleeps()
    await make_adapter(server, sleep=sleep).check()
    assert sleep.calls == [7.0, 7.0]


async def test_rate_limit_without_header_backs_off_then_gives_up(
    server: FakeElma365,
) -> None:
    server.rate_limit_hits = 100
    server.retry_after = None
    sleep = Sleeps()
    with pytest.raises(AdapterError, match="rate_limited") as caught:
        await make_adapter(server, sleep=sleep).check()
    assert caught.value.retryable
    assert sleep.calls == [2.0, 5.0, 10.0, 20.0, 30.0]


async def test_long_retry_after_is_capped(server: FakeElma365) -> None:
    server.rate_limit_hits = 1
    server.retry_after = "3600"
    sleep = Sleeps()
    await make_adapter(server, sleep=sleep).check()
    assert sleep.calls == [60.0]


async def test_server_error_is_retried_then_retryable(server: FakeElma365) -> None:
    server.server_errors = 1
    await make_adapter(server).check()
    server.server_errors = 100
    with pytest.raises(AdapterError, match="http_503") as caught:
        await make_adapter(server).check()
    assert caught.value.retryable


async def test_unsuccessful_answer_is_an_error_without_server_text(
    server: FakeElma365,
) -> None:
    server.failure = True
    with pytest.raises(AdapterError) as caught:
        await make_adapter(server).check()
    assert caught.value.code == "api_error"
    assert not caught.value.retryable


async def test_html_instead_of_json_is_refused() -> None:
    page = OutboundClient(
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, text="<html>Вход</html>")
            )
        ),
        resolver=public_resolver,
    )
    client = Elma365Client(page, base_url=BASE, token=ANNA_TOKEN)
    with pytest.raises(AdapterError, match="not_json"):
        await client.get("disk/file/x")


async def test_recorder_keeps_no_token_and_no_link_signature(
    server: FakeElma365,
) -> None:
    records: list[tuple[str, Any, Any]] = []
    adapter = make_adapter(
        server, recorder=lambda path, params, data: records.append((path, params, data))
    )
    files = await listed(adapter)
    await adapter.fetch(files[fid(FILE_VACATION)], max_bytes=MAX_BYTES)
    dump = repr(records)
    assert ANNA_TOKEN not in dump
    assert "X-Amz-Signature" not in dump
    assert any(path.endswith("/get-link") for path, _, _ in records)


# --- обход --------------------------------------------------------------------------


async def test_files_follow_access_of_each_employee(server: FakeElma365) -> None:
    with counting_skips() as tally:
        anna = await listed(make_adapter(server))
    assert set(anna) == {
        fid(FILE_VACATION),
        fid(FILE_SALARY),
        fid(FILE_BONUS),
        fid(FILE_PRIVATE),
        fid(FILE_ROOT),
    }
    assert tally.formats() == {".png": 1}
    # Чужой формат не стоит запроса проверки прав.
    assert ("GET", f"disk/file/{FILE_PIC}", {}) not in server.calls

    vacation = anna[fid(FILE_VACATION)]
    assert vacation.title == vacation.filename == "Отпуск.txt"
    assert vacation.kind is RemoteDocumentKind.FILE
    assert vacation.module == "files"
    assert vacation.path == "Кадры"
    assert vacation.url == f"{server.base}files/{DIR_HR}"
    assert vacation.locator == FILE_VACATION
    assert vacation.size is None
    assert vacation.modified_at is not None
    assert anna[fid(FILE_BONUS)].path == "Кадры/Закрытое/Глубже"
    assert anna[fid(FILE_ROOT)].path == ""


async def test_closed_files_are_not_listed_even_if_search_returns_them(
    server: FakeElma365,
) -> None:
    boris = await listed(make_adapter(server, BORIS_TOKEN))
    assert set(boris) == {fid(FILE_VACATION), fid(FILE_ROOT)}
    searched = {
        body["filter"]["directory"]
        for method, route, body in server.calls
        if route == "disk/files/search"
    }
    # Закрытая папка не обыскивается вовсе; закрытый файл в открытой — был
    # в поиске, но не прошёл проверку по id.
    assert DIR_CLOSED not in searched
    assert ("GET", f"disk/file/{FILE_PRIVATE}", {}) in server.calls


async def test_link_directories_are_skipped(server: FakeElma365) -> None:
    await listed(make_adapter(server))
    probed = [
        route for _, route, _ in server.calls if route.startswith("disk/directory/")
    ]
    assert f"disk/directory/{uid(3)}" not in probed


async def test_directories_and_files_are_paged(server: FakeElma365) -> None:
    for n in range(5):
        server.add_file(uid(300 + n), f"Файл {n}.txt", b"x", directory=DIR_HR)
    client = Elma365Client(server.client(), base_url=server.base, token=ANNA_TOKEN)
    module = files_module.FilesModule(client, page_size=2)
    found = {d.external_id async for d in module.walk()}
    assert {fid(uid(300 + n)) for n in range(5)} <= found
    assert len(found) == 10
    pages = [
        body["from"]
        for _, route, body in server.calls
        if route == "disk/directory/list"
    ]
    assert pages == [0, 2, 4]


async def test_oversized_tree_stops_instead_of_truncating(
    server: FakeElma365, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files_module, "MAX_DIRECTORIES", 2)
    with pytest.raises(AdapterError, match="tree_too_large"):
        await listed(make_adapter(server))


async def test_folders_setting_limits_the_walk(server: FakeElma365) -> None:
    files = await listed(make_adapter(server, folders=(DIR_CLOSED,)))
    assert set(files) == {fid(FILE_SALARY), fid(FILE_BONUS)}


async def test_version_changes_with_new_file_version(server: FakeElma365) -> None:
    before = (await listed(make_adapter(server)))[fid(FILE_VACATION)].version
    server.update_file(FILE_VACATION, b"new", updated="2026-10-01T10:00:00Z")
    after = (await listed(make_adapter(server)))[fid(FILE_VACATION)].version
    assert before != after
    assert after.startswith("2:")


# --- скачивание ---------------------------------------------------------------------


async def test_file_is_downloaded_by_link_without_token(server: FakeElma365) -> None:
    adapter = make_adapter(server)
    files = await listed(adapter)
    content = await adapter.fetch(files[fid(FILE_VACATION)], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedFile)
    assert content.data.decode() == "Отпуск — 28 календарных дней."
    assert content.filename == "Отпуск.txt"
    assert server.storage_auth == [""]


async def test_too_large_file_is_refused(server: FakeElma365) -> None:
    adapter = make_adapter(server)
    files = await listed(adapter)
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(files[fid(FILE_VACATION)], max_bytes=5)


async def test_fetch_of_file_closed_since_listing_is_forbidden(
    server: FakeElma365,
) -> None:
    adapter = make_adapter(server, BORIS_TOKEN)
    files = await listed(adapter)
    server.file_readers[FILE_VACATION] = {"someone-else"}
    with pytest.raises(AdapterError, match="forbidden") as caught:
        await adapter.fetch(files[fid(FILE_VACATION)], max_bytes=MAX_BYTES)
    assert not isinstance(caught.value, AdapterAuthError)


async def test_unknown_document_is_refused(server: FakeElma365) -> None:
    stray = RemoteDocument(
        external_id="doc:1",
        title="x",
        url="https://x/",
        version="1",
        kind=RemoteDocumentKind.FILE,
        module="files",
    )
    with pytest.raises(AdapterError, match="unknown_document"):
        await make_adapter(server).fetch(stray, max_bytes=MAX_BYTES)
