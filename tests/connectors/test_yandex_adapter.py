"""Адаптер Яндекс 360 (Диск): OAuth Яндекс ID, обход папок сотрудника,
продление токена, скачивание по подписанной ссылке."""

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
from corp_ed.connectors.yandex.oauth import YandexOAuth
from corp_ed.core.config import ConnectorSettings
from corp_ed.domain.types import ConnectorMode, RemoteDocumentKind
from tests.connectors.fake_yandex import (
    ACCESS_TOKEN,
    AUTH_CODE,
    CLIENT_ID,
    CLIENT_SECRET,
    DISK_API,
    LOGIN,
    OAUTH_SERVER,
    REFRESH_TOKEN,
    FakeYandex,
    sample_yandex,
)

KEY = Fernet.generate_key().decode()
MAX_BYTES = 1024 * 1024


def settings() -> ConnectorSettings:
    return ConnectorSettings(
        secrets_keys=KEY,
        yandex_oauth_server=OAUTH_SERVER,
        yandex_disk_api=DISK_API,
        max_document_bytes=MAX_BYTES,
    )  # type: ignore[arg-type]


def make_adapter(server: FakeYandex, **overrides: str) -> YandexAdapter:
    credentials = {
        "client_secret": CLIENT_SECRET,
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "expires_at": "0",
        **overrides,
    }
    return build_adapter(
        {"client_id": CLIENT_ID}, credentials, server.client(), settings()
    )


async def listed(adapter: YandexAdapter) -> dict[str, RemoteDocument]:
    return {d.external_id: d async for d in adapter.list(["disk"])}


@pytest.fixture
def server() -> FakeYandex:
    return sample_yandex()


def test_spec_is_per_user_oauth_without_portal() -> None:
    assert SPEC.kind == KIND == "yandex360"
    assert SPEC.mode is ConnectorMode.PER_USER and SPEC.user_auth is UserAuth.OAUTH
    assert [m.name for m in SPEC.modules] == ["disk"]
    assert [f.name for f in SPEC.config_fields] == ["client_id"]
    assert [f.name for f in SPEC.app_credential_fields] == ["client_secret"]
    assert SPEC.url_field is None
    assert "cloud_api:disk.read" in SPEC.extra["app_scopes"]


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


async def test_check_reads_login(server: FakeYandex) -> None:
    adapter = make_adapter(server)
    await adapter.check()
    assert adapter.external_user_id == LOGIN


async def test_dead_token_is_auth_error_after_one_refresh_attempt(
    server: FakeYandex,
) -> None:
    server.access_tokens.clear()
    server.refresh_tokens.clear()
    with pytest.raises(AdapterAuthError, match="invalid_grant"):
        await make_adapter(server).check()


async def test_expired_token_is_refreshed_and_refresh_token_kept(
    server: FakeYandex,
) -> None:
    server.expired.add(ACCESS_TOKEN)
    adapter = make_adapter(server)
    await adapter.check()
    refreshed = adapter.refreshed_credentials
    assert refreshed is not None
    assert refreshed["access_token"] in server.access_tokens
    # Яндекс не выдал новый refresh — сохранён прежний.
    assert refreshed["refresh_token"] == REFRESH_TOKEN
    server.rotate_refresh = True
    server.expired.add(refreshed["access_token"])
    adapter2 = make_adapter(server, access_token=refreshed["access_token"])
    await adapter2.check()
    rotated = adapter2.refreshed_credentials
    assert rotated is not None and rotated["refresh_token"] != REFRESH_TOKEN


async def test_rate_limit_backs_off(server: FakeYandex) -> None:
    server.rate_limit_hits = 2
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    adapter = make_adapter(server)
    adapter._client._sleep = sleep  # noqa: SLF001 — подмена ожидания в тесте
    await adapter.check()
    assert sleeps == [1.0, 2.0]


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


async def test_fetch_downloads_from_signed_link(server: FakeYandex) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter)
    content = await adapter.fetch(
        documents["ydisk:rid-disk:/Регламенты/Отпуск.txt"], max_bytes=MAX_BYTES
    )
    assert isinstance(content, FetchedFile)
    assert content.filename == "Отпуск.txt"
    assert content.data == "Отпуск — 28 дней.".encode()
    assert server.downloads == ["/disk/rid-disk:/Регламенты/Отпуск.txt"]


async def test_fetch_rejects_oversized_and_foreign_links(server: FakeYandex) -> None:
    adapter = make_adapter(server)
    big = RemoteDocument(
        external_id="ydisk:x",
        title="Большой.pdf",
        url="",
        version="",
        kind=RemoteDocumentKind.FILE,
        module="disk",
        locator="disk:/Регламенты/Большой.pdf",
    )
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(big, max_bytes=MAX_BYTES)
    with pytest.raises(AdapterError, match="download_url_foreign"):
        await adapter._client.download("https://evil.example.com/f", max_bytes=10)  # noqa: SLF001
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
    assert CLIENT_SECRET not in url


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
    with pytest.raises(AdapterAuthError, match="invalid_grant"):
        await flow.exchange("stale")
    server.oauth_error = "invalid_client"
    with pytest.raises(AdapterConfigError, match="invalid_client"):
        await flow.exchange(AUTH_CODE)
    server.oauth_error = "weird"
    with pytest.raises(AdapterError, match="oauth_weird"):
        await flow.exchange(AUTH_CODE)
