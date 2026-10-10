"""Адаптер Яндекс 360 (Диск): OAuth Яндекс ID, обход папок сотрудника и
общих дисков организации, продление токена, скачивание с токеном."""

import time

import pytest
from cryptography.fernet import Fernet

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    FetchedFile,
    RemoteDocument,
)
from corp_ed.connectors.registry import UserAuth, default_registry
from corp_ed.connectors.yandex import KIND, SPEC
from corp_ed.connectors.yandex.adapter import YandexAdapter, build_adapter
from corp_ed.connectors.yandex.oauth import DEVICE_ID, YandexOAuth
from corp_ed.core import outbound
from corp_ed.core.config import ConnectorSettings
from corp_ed.domain.types import ConnectorMode, RemoteDocumentKind
from tests.connectors.fake_yandex import (
    ACCESS_TOKEN,
    AUTH_CODE,
    CLIENT_ID,
    CLIENT_SECRET,
    DISK_API,
    DOWNLOAD_HOST,
    OAUTH_SERVER,
    ORG_ID,
    REFRESH_TOKEN,
    STORAGE_HOST,
    UID,
    WIKI_API,
    FakeYandex,
    sample_yandex,
)

KEY = Fernet.generate_key().decode()
MAX_BYTES = 1024 * 1024


def settings(**overrides: str) -> ConnectorSettings:
    return ConnectorSettings(
        secrets_keys=KEY,
        yandex_oauth_server=OAUTH_SERVER,
        yandex_disk_api=DISK_API,
        yandex_wiki_api=WIKI_API,
        max_document_bytes=MAX_BYTES,
        max_large_document_bytes=MAX_BYTES,
        **overrides,
    )  # type: ignore[arg-type]


def make_adapter(
    server: FakeYandex, *, config: dict[str, str] | None = None, **overrides: str
) -> YandexAdapter:
    credentials = {
        "client_secret": CLIENT_SECRET,
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "expires_at": "0",
        **overrides,
    }
    return build_adapter(
        {"client_id": CLIENT_ID, **(config or {})},
        credentials,
        server.client(),
        settings(),
    )


async def listed(
    adapter: YandexAdapter, modules: tuple[str, ...] = ("disk",)
) -> dict[str, RemoteDocument]:
    return {d.external_id: d async for d in adapter.list(list(modules))}


@pytest.fixture
def server() -> FakeYandex:
    return sample_yandex()


def test_spec_is_per_user_oauth_without_portal() -> None:
    assert SPEC.kind == KIND == "yandex360"
    assert SPEC.mode is ConnectorMode.PER_USER and SPEC.user_auth is UserAuth.OAUTH
    assert [m.name for m in SPEC.modules] == [
        "disk",
        "shared_disks",
        "wiki",
        "tracker",
    ]
    assert [f.name for f in SPEC.config_fields] == [
        "client_id",
        "org_id",
        "wiki_roots",
        "tracker_queues",
        "tracker_cloud_org_id",
    ]
    assert [f.required for f in SPEC.config_fields] == [
        True,
        False,
        False,
        False,
        False,
    ]
    assert [f.name for f in SPEC.app_credential_fields] == ["client_secret"]
    assert SPEC.url_field is None
    assert SPEC.extra["app_scopes"].split(",") == [
        "cloud_api:disk.read",
        "cloud_api:disk.info",
        "wiki:read",
    ]
    assert SPEC.config_check is not None
    assert SPEC.config_check({"client_id": "x", "org_id": "8123456"}) is None
    assert SPEC.config_check({"client_id": "x", "org_id": "acme"}) == "org_id_invalid"


def test_registry_builds_yandex(server: FakeYandex) -> None:
    registry = default_registry(settings())
    adapter = registry.build(
        "yandex360",
        {"client_id": CLIENT_ID},
        {"client_secret": CLIENT_SECRET, "access_token": "a", "refresh_token": "r"},
        server.client(),
    )
    assert isinstance(adapter, YandexAdapter)
    flow = registry.build_oauth(
        "yandex360",
        {"client_id": CLIENT_ID},
        {"client_secret": CLIENT_SECRET},
        server.client(),
    )
    assert isinstance(flow, YandexOAuth)
    with pytest.raises(AdapterAuthError, match="credentials_missing"):
        registry.build(
            "yandex360",
            {"client_id": CLIENT_ID},
            {"client_secret": "s"},
            server.client(),
        )
    with pytest.raises(AdapterConfigError, match="app_credentials_missing"):
        registry.build("yandex360", {}, {"access_token": "a"}, server.client())


async def test_check_reads_uid(server: FakeYandex) -> None:
    adapter = make_adapter(server)
    await adapter.check()
    # uid — идентификатор сотрудника в API Яндекс 360; логин может смениться.
    assert adapter.external_user_id == UID


async def test_oversized_api_response_is_an_adapter_error(
    server: FakeYandex, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ответ Диска или OAuth больше потолка — ошибка источника, а не сбой
    воркера."""
    monkeypatch.setattr(outbound, "MAX_RESPONSE_BYTES", 16)
    with pytest.raises(AdapterError) as excinfo:
        await make_adapter(server).check()
    assert excinfo.value.code == "response_too_large"
    assert not excinfo.value.retryable
    flow = YandexOAuth(
        server.client(),
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        server=OAUTH_SERVER,
    )
    with pytest.raises(AdapterError) as excinfo:
        await flow.exchange(AUTH_CODE)
    assert excinfo.value.code == "oauth_response_too_large"


async def test_unknown_error_codes_from_yandex_are_scrubbed(
    server: FakeYandex,
) -> None:
    """Код ошибки из ответа Диска и OAuth уходит в API, аудит и
    интерфейс — только латиница, цифры и _, не длиннее 64."""
    server.api_error = (400, {"error": "Odd Error\n<b>"})
    with pytest.raises(AdapterError) as excinfo:
        await make_adapter(server).check()
    assert excinfo.value.code == "odd_error__b_"
    server.api_error = (500, {"error": "Ошибка" + "!" * 100})
    with pytest.raises(AdapterError) as excinfo:
        await make_adapter(server).check()
    assert excinfo.value.code == "_" * 64 and excinfo.value.retryable
    server.api_error = None
    server.oauth_error = "Odd Error\n<b>"
    flow = YandexOAuth(
        server.client(),
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        server=OAUTH_SERVER,
    )
    with pytest.raises(AdapterError) as excinfo:
        await flow.exchange(AUTH_CODE)
    assert excinfo.value.code == "oauth_odd_error__b_"


async def test_dead_token_is_auth_error_after_one_refresh_attempt(
    server: FakeYandex,
) -> None:
    server.access_tokens.clear()
    server.refresh_tokens.clear()
    with pytest.raises(AdapterAuthError, match="invalid_grant"):
        await make_adapter(server).check()


async def test_expired_token_is_refreshed_with_a_new_refresh_token(
    server: FakeYandex,
) -> None:
    """«Яндекс OAuth возвращает токен и новый refresh-токен»."""
    server.expired.add(ACCESS_TOKEN)
    adapter = make_adapter(server)
    await adapter.check()
    refreshed = adapter.refreshed_credentials
    assert refreshed is not None
    assert refreshed["access_token"] in server.access_tokens
    assert refreshed["refresh_token"] != REFRESH_TOKEN
    assert refreshed["refresh_token"] in server.refresh_tokens


async def test_refresh_without_new_refresh_token_keeps_the_old_one(
    server: FakeYandex,
) -> None:
    server.rotate_refresh = False
    server.expired.add(ACCESS_TOKEN)
    adapter = make_adapter(server)
    await adapter.check()
    refreshed = adapter.refreshed_credentials
    assert refreshed is not None and refreshed["refresh_token"] == REFRESH_TOKEN


async def test_token_close_to_expiry_is_refreshed_before_the_first_call(
    server: FakeYandex,
) -> None:
    """Refresh-токен живёт столько же, сколько access: после истечения
    продлевать нечем, поэтому продление — заранее, без ожидания 401."""
    soon = str(int(time.time()) + 3 * 24 * 3600)
    adapter = make_adapter(server, expires_at=soon)
    await adapter.check()
    grants = [f["grant_type"] for m, f in server.calls if m == "oauth/token"]
    assert grants == ["refresh_token"]
    assert adapter.refreshed_credentials is not None

    server.calls.clear()
    later = str(int(time.time()) + 200 * 24 * 3600)
    fresh = make_adapter(server, expires_at=later)
    await fresh.check()
    assert "oauth/token" not in [m for m, _ in server.calls]
    assert fresh.refreshed_credentials is None


async def test_missing_refresh_token_is_grant_problem_not_app_problem(
    server: FakeYandex,
) -> None:
    server.expired.add(ACCESS_TOKEN)
    adapter = make_adapter(server, refresh_token="")
    with pytest.raises(AdapterAuthError, match="refresh_token_missing"):
        await adapter.check()
    assert "oauth/token" not in [m for m, _ in server.calls]


async def test_rate_limit_backs_off(server: FakeYandex) -> None:
    server.rate_limit_hits = 2
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    adapter = make_adapter(server)
    adapter._disk._sleep = sleep  # noqa: SLF001 — подмена ожидания в тесте
    await adapter.check()
    assert sleeps == [1.0, 2.0]


async def test_maintenance_is_retryable(server: FakeYandex) -> None:
    """423 — «Технические работы»: повторить позже, а не бросить."""
    server.maintenance = True
    with pytest.raises(AdapterError, match="maintenance") as info:
        await make_adapter(server).check()
    assert info.value.retryable


async def test_walk_recurses_and_filters(server: FakeYandex) -> None:
    documents = await listed(make_adapter(server))
    assert set(documents) == {
        "ydisk:rid-disk:/Регламенты/Отпуск.txt",
        "ydisk:rid-disk:/Регламенты/Архив/Старый.md",
        "ydisk:rid-disk:/Общая папка/План.md",
        "ydisk:rid-disk:/Заметка.txt",
    }
    vacation = documents["ydisk:rid-disk:/Регламенты/Отпуск.txt"]
    assert vacation.kind is RemoteDocumentKind.FILE
    assert vacation.module == "disk"
    assert vacation.path == "Диск/Регламенты"
    assert vacation.locator == "disk:/Регламенты/Отпуск.txt"
    assert vacation.version == "md5-Отпуск.txt:2026-05-11T09:30:00+00:00"
    assert (
        vacation.url
        == "https://disk.yandex.ru/client/disk/%D0%A0%D0%B5%D0%B3%D0%BB%D0%B0%D0%BC%D0%B5%D0%BD%D1%82%D1%8B"
    )
    assert vacation.modified_at is not None and vacation.modified_at.month == 5
    old = documents["ydisk:rid-disk:/Регламенты/Архив/Старый.md"]
    assert old.url == "https://disk.yandex.ru/i/abc123"
    assert old.path == "Диск/Регламенты/Архив"
    root = documents["ydisk:rid-disk:/Заметка.txt"]
    assert root.url == "https://disk.yandex.ru/client/disk/"
    # Закрытая папка пропущена, а не уронила обход.
    assert "ydisk:rid-disk:/Закрытая/Тайна.txt" not in documents
    # Общие диски — не часть личного Диска.
    assert not any("vd:" in key for key in documents)


async def test_forbidden_root_is_an_error_not_an_empty_disk(
    server: FakeYandex,
) -> None:
    """403 на корне — права приложения или заблокированный Диск: пустой
    листинг удалил бы все документы сотрудника."""
    server.forbidden.add("disk:/")
    with pytest.raises(AdapterError, match="forbidden"):
        await listed(make_adapter(server))


async def test_walk_pages_large_folders(server: FakeYandex) -> None:
    for index in range(450):
        server.add_file(f"disk:/Много/f{index:03d}.txt", b"x")
    server.add_dir("disk:/Много")
    documents = await listed(make_adapter(server))
    assert sum(1 for d in documents if d.startswith("ydisk:rid-disk:/Много/")) == 450
    offsets = [
        q.get("offset") for p, q in server.calls if q.get("path") == "disk:/Много"
    ]
    assert offsets == ["0", "200", "400"]


async def test_fetch_downloads_with_the_token_but_not_on_the_storage_host(
    server: FakeYandex,
) -> None:
    """«Скачать файл по полученному адресу, указав тот же OAuth-токен»;
    после 302 на хранилище токен не уходит."""
    adapter = make_adapter(server)
    documents = await listed(adapter)
    content = await adapter.fetch(
        documents["ydisk:rid-disk:/Регламенты/Отпуск.txt"], max_bytes=MAX_BYTES
    )
    assert isinstance(content, FetchedFile)
    assert content.filename == "Отпуск.txt"
    assert content.data == "Отпуск — 28 дней.".encode()
    assert server.downloads == [
        (DOWNLOAD_HOST, f"OAuth {ACCESS_TOKEN}"),
        (STORAGE_HOST, ""),
    ]
    # Метаданные не перечитываются: один вызов за ссылкой и скачивание.
    api_calls = [
        p for p, q in server.calls if q.get("path") == "disk:/Регламенты/Отпуск.txt"
    ]
    assert api_calls == ["/v1/disk/resources/download"]


async def test_fetch_rejects_oversized_foreign_and_templated_links(
    server: FakeYandex,
) -> None:
    adapter = make_adapter(server)
    big = RemoteDocument(
        external_id="ydisk:x",
        title="Большой.pdf",
        url="",
        version="",
        kind=RemoteDocumentKind.FILE,
        module="disk",
        locator="disk:/Регламенты/Большой.pdf",
        size=200 * 1024 * 1024,
    )
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(big, max_bytes=MAX_BYTES)
    for foreign in (
        "https://evil.example.com/f",
        "https://bucket.yandexcloud.net/f",
        "http://downloader.dst.yandex.ru/f",
    ):
        with pytest.raises(AdapterError, match="download_url_foreign"):
            await adapter._disk.download(foreign, max_bytes=10)  # noqa: SLF001
    no_locator = RemoteDocument(
        external_id="ydisk:y",
        title="",
        url="",
        version="",
        kind=RemoteDocumentKind.FILE,
        module="disk",
    )
    with pytest.raises(AdapterError, match="locator_missing"):
        await adapter.fetch(no_locator, max_bytes=MAX_BYTES)
    server.templated_links = True
    note = RemoteDocument(
        external_id="ydisk:rid-disk:/Заметка.txt",
        title="Заметка.txt",
        url="",
        version="",
        kind=RemoteDocumentKind.FILE,
        module="disk",
        locator="disk:/Заметка.txt",
    )
    with pytest.raises(AdapterError, match="download_url_templated"):
        await adapter.fetch(note, max_bytes=MAX_BYTES)


async def test_fetch_missing_file_is_error(server: FakeYandex) -> None:
    adapter = make_adapter(server)
    gone = RemoteDocument(
        external_id="ydisk:z",
        title="",
        url="",
        version="",
        kind=RemoteDocumentKind.FILE,
        module="disk",
        locator="disk:/Нет.txt",
    )
    with pytest.raises(AdapterError, match="not_found"):
        await adapter.fetch(gone, max_bytes=MAX_BYTES)


# --- общие диски организации ---------------------------------------------------


async def test_shared_disks_need_the_organization_id(server: FakeYandex) -> None:
    with pytest.raises(AdapterConfigError, match="org_id_missing"):
        await listed(make_adapter(server), ("disk", "shared_disks"))


@pytest.mark.parametrize("relative", [False, True])
async def test_shared_disks_are_walked_through_their_own_api(
    server: FakeYandex, relative: bool
) -> None:
    server.relative_virtual_paths = relative
    adapter = make_adapter(server, config={"org_id": ORG_ID})
    documents = await listed(adapter, ("shared_disks",))

    assert set(documents) == {
        "ydisk:rid-vd:h4sh:disk:/Политики/Отпуска.md",
        "ydisk:rid-vd:h4sh:disk:/Приказ.txt",
    }
    policy = documents["ydisk:rid-vd:h4sh:disk:/Политики/Отпуска.md"]
    assert policy.module == "shared_disks"
    assert policy.path == "Общие диски/Кадры/Политики"
    # Путь для скачивания — составной, в какой бы форме его ни отдал листинг.
    assert policy.locator == "vd:h4sh:disk:/Политики/Отпуска.md"
    assert policy.url.startswith("https://disk.yandex.ru/client/vd/h4sh/")
    # Диск без права чтения не обходится.
    assert not any("n0read" in key for key in documents)
    discovery = [q for p, q in server.calls if p == "/v1/disk/virtual-disks/discovery"]
    assert discovery == [{"org_id": ORG_ID, "limit": "100", "offset": "0"}]

    content = await adapter.fetch(policy, max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedFile)
    assert content.data.startswith("# Отпуска".encode())
    downloads = [p for p, q in server.calls if q.get("path") == policy.locator]
    assert downloads[-1] == "/v1/disk/virtual-disks/resources/download"


async def test_shared_disk_without_access_is_skipped(server: FakeYandex) -> None:
    server.forbidden.add("vd:h4sh:disk:/")
    documents = await listed(
        make_adapter(server, config={"org_id": ORG_ID}), ("shared_disks",)
    )
    assert documents == {}


# --- OAuth -------------------------------------------------------------------


def test_authorize_url(server: FakeYandex) -> None:
    flow = YandexOAuth(
        server.client(),
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        server=OAUTH_SERVER,
    )
    url = flow.authorize_url("st")
    assert url.startswith(f"{OAUTH_SERVER}authorize?")
    assert (
        "response_type=code" in url
        and f"client_id={CLIENT_ID}" in url
        and "state=st" in url
    )
    # Выбор аккаунта: сотрудник мог быть вошёл в личный Яндекс.
    assert "force_confirm=yes" in url
    assert "redirect_uri" not in url
    assert CLIENT_SECRET not in url


def test_authorize_url_names_our_callback_when_configured(server: FakeYandex) -> None:
    """Без redirect_uri Яндекс берёт первый адрес из настроек приложения."""
    registry = default_registry(
        settings(
            oauth_callback_url="https://api.example.ru/api/v1/connectors/oauth/callback"
        )
    )
    flow = registry.build_oauth(
        "yandex360",
        {"client_id": CLIENT_ID},
        {"client_secret": CLIENT_SECRET},
        server.client(),
    )
    url = flow.authorize_url("st")
    assert "redirect_uri=https%3A%2F%2Fapi.example.ru%2Fapi%2Fv1%2Fconnectors" in url


async def test_exchange_and_errors(server: FakeYandex) -> None:
    flow = YandexOAuth(
        server.client(),
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        server=OAUTH_SERVER,
    )
    exchanged = await flow.exchange(AUTH_CODE)
    assert set(exchanged.credentials) == {"access_token", "refresh_token", "expires_at"}
    assert exchanged.credentials["access_token"] in server.access_tokens
    method, form = server.calls[-1]
    assert method == "oauth/token" and form["grant_type"] == "authorization_code"
    # Код просрочен или уже использован — вопрос к сотруднику.
    with pytest.raises(AdapterAuthError, match="invalid_grant"):
        await flow.exchange("stale")
    for code in (
        "invalid_client",
        "invalid_scope",
        "unauthorized_client",
        "unsupported_grant_type",
    ):
        server.oauth_error = code
        with pytest.raises(AdapterConfigError, match=code):
            await flow.exchange(AUTH_CODE)
    server.oauth_error = "weird"
    with pytest.raises(AdapterError, match="oauth_weird"):
        await flow.exchange(AUTH_CODE)


# --- отзыв токена ---------------------------------------------------------------


def _flow(server: FakeYandex) -> YandexOAuth:
    return YandexOAuth(
        server.client(),
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        server=OAUTH_SERVER,
    )


async def test_tokens_are_issued_for_a_device_so_they_can_be_revoked(
    server: FakeYandex,
) -> None:
    """Яндекс ID отзывает только токен, выданный с device_id: без него
    revoke_token отвечает unsupported_token_type."""
    flow = _flow(server)
    url = flow.authorize_url("st")
    assert f"device_id={DEVICE_ID}" in url and "device_name=kronto" in url
    exchanged = await flow.exchange(AUTH_CODE)
    _, form = server.calls[-1]
    assert form["device_id"] == DEVICE_ID
    assert exchanged.credentials["access_token"] in server.device_tokens


async def test_revoke_sends_the_token_with_the_app_secret(server: FakeYandex) -> None:
    flow = _flow(server)
    token = (await flow.exchange(AUTH_CODE)).credentials["access_token"]

    assert await flow.revoke({"access_token": token, "refresh_token": "r"}) is True

    method, form = server.calls[-1]
    assert method == "oauth/revoke_token"
    assert form == {
        "access_token": token,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    assert token not in server.access_tokens


async def test_token_without_device_is_not_revocable(server: FakeYandex) -> None:
    # Токены, выданные до привязки к устройству, отозвать нельзя.
    assert await _flow(server).revoke({"access_token": ACCESS_TOKEN}) is False
    assert ACCESS_TOKEN in server.access_tokens


async def test_revoke_without_token_does_nothing(server: FakeYandex) -> None:
    assert await _flow(server).revoke({"refresh_token": REFRESH_TOKEN}) is False
    assert server.calls == []


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (400, {"error": "invalid_client"}, "oauth_revoke_invalid_client"),
        (401, {"error": "invalid_client"}, "oauth_revoke_invalid_client"),
        (400, {"error": "invalid_grant"}, "oauth_revoke_invalid_grant"),
        (400, {"error": "Странная <ошибка>"}, "oauth_revoke_"),
        (500, {"message": "oops"}, "oauth_http_500"),
    ],
)
async def test_revoke_errors_are_codes(
    server: FakeYandex, status: int, body: dict[str, str], code: str
) -> None:
    server.revoke_error = (status, body)
    with pytest.raises(AdapterError) as caught:
        await _flow(server).revoke({"access_token": ACCESS_TOKEN})
    assert caught.value.code.startswith(code)
    assert ACCESS_TOKEN not in caught.value.code
