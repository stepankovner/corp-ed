"""Семейство WebDAV-дисков: Nextcloud (пароль приложения и OAuth2),
ownCloud, Seafile, Диск VK WorkSpace, Облако Mail.ru, просто WebDAV.

Против поддельного сервера (fake_webdav): обход папок только Depth 1,
id документа общий для сотрудников лишь там, где сервер даёт сквозной
fileid, ссылки за пределы сервера и корня не принимаются, потолки
глубины и числа элементов — ошибка, а не молча обрезанный листинг.
"""

import time
from urllib.parse import quote

import pytest

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    FetchedFile,
    RemoteDocument,
)
from corp_ed.connectors.common import counting_skips
from corp_ed.connectors.registry import UserAuth, default_registry
from corp_ed.connectors.webdav import adapter as adapter_module
from corp_ed.connectors.webdav import client as client_module
from corp_ed.connectors.webdav.adapter import WebDavAdapter, build_adapter
from corp_ed.connectors.webdav.kinds import (
    MAILRU,
    NEXTCLOUD,
    NEXTCLOUD_OAUTH,
    OWNCLOUD,
    SEAFILE,
    SPECS,
    VK_WORKSPACE,
    WEBDAV,
)
from corp_ed.connectors.webdav.multistatus import parse_multistatus
from corp_ed.connectors.webdav.oauth import NextcloudOAuth
from corp_ed.core.config import ConnectorSettings
from corp_ed.domain.types import ConnectorMode, RemoteDocumentKind
from tests.connectors.fake_webdav import (
    AUTH_CODE,
    CLIENT_ID,
    CLIENT_SECRET,
    HOST,
    IVAN,
    IVAN_LOGIN,
    IVAN_PASSWORD,
    MAILRU_HOST,
    MARIA,
    MARIA_LOGIN,
    MARIA_PASSWORD,
    OTHER_HOST,
    SERVER,
    FakeDav,
    sample_nextcloud,
)

MAX_BYTES = 1024 * 1024
IVAN_CREDENTIALS = {"login": IVAN_LOGIN, "password": IVAN_PASSWORD}
MARIA_CREDENTIALS = {"login": MARIA_LOGIN, "password": MARIA_PASSWORD}


def settings(**overrides: str) -> ConnectorSettings:
    return ConnectorSettings(max_document_bytes=MAX_BYTES, **overrides)  # type: ignore[arg-type]


class Sleeps(list[float]):
    async def __call__(self, delay: float) -> None:
        self.append(delay)


def make(
    server: FakeDav,
    kind: str = NEXTCLOUD.kind,
    *,
    config: dict[str, str] | None = None,
    credentials: dict[str, str] | None = None,
    sleep: Sleeps | None = None,
) -> WebDavAdapter:
    return build_adapter(
        kind,
        {"server": SERVER, **(config or {})} if kind != MAILRU.kind else config or {},
        credentials if credentials is not None else IVAN_CREDENTIALS,
        server.client(),
        settings(),
        sleep=sleep if sleep is not None else Sleeps(),
    )


async def listed(adapter: WebDavAdapter) -> dict[str, RemoteDocument]:
    return {d.path + "/" + d.title: d async for d in adapter.list(["files"])}


@pytest.fixture
def server() -> FakeDav:
    return sample_nextcloud()


# --- каталог --------------------------------------------------------------------


def test_every_kind_is_per_user_preview_and_base() -> None:
    kinds = {spec.kind for spec in SPECS}
    assert kinds == {
        "nextcloud",
        "nextcloud_oauth",
        "owncloud",
        "seafile",
        "vk_workspace_disk",
        "mailru_cloud",
        "webdav",
    }
    for spec in SPECS:
        assert spec.mode is ConnectorMode.PER_USER
        assert spec.preview and spec.base
        assert [m.name for m in spec.modules] == ["files"]
    for spec in (NEXTCLOUD, OWNCLOUD, SEAFILE, VK_WORKSPACE, MAILRU, WEBDAV):
        assert spec.user_auth is UserAuth.FIELDS
        assert [f.name for f in spec.credential_fields] == ["login", "password"]
        assert [f.secret for f in spec.credential_fields] == [False, True]
    assert NEXTCLOUD_OAUTH.user_auth is UserAuth.OAUTH
    assert [f.name for f in NEXTCLOUD_OAUTH.app_credential_fields] == ["client_secret"]
    assert MAILRU.url_field is None
    assert {f.name for f in MAILRU.config_fields} == {"folders"}
    for spec in (NEXTCLOUD, NEXTCLOUD_OAUTH, OWNCLOUD, SEAFILE, VK_WORKSPACE, WEBDAV):
        assert spec.url_field == "server"
        assert "folders" in {f.name for f in spec.config_fields}


def test_kinds_are_offered_only_after_live_check() -> None:
    registry = default_registry(ConnectorSettings())
    assert "nextcloud" not in {spec.kind for spec in registry.kinds()}
    # Живой проверке (cli connector-check) вид доступен и без флага.
    assert registry.spec("nextcloud").preview
    enabled = default_registry(ConnectorSettings(preview_kinds="nextcloud,webdav"))
    offered = {spec.kind for spec in enabled.kinds()}
    assert {"nextcloud", "webdav"} <= offered
    assert "owncloud" not in offered


@pytest.mark.parametrize(
    ("folders", "code"),
    [
        ("Документы, Проекты/Архив", None),
        ("/Документы/", None),
        ("", None),
        ("Документы/../Чужое", "folders_invalid"),
        ("Документы,,", None),
        ("./Документы", "folders_invalid"),
        (",".join(f"п{i}" for i in range(60)), "folders_too_many"),
    ],
)
def test_folders_are_checked_on_save(folders: str, code: str | None) -> None:
    assert NEXTCLOUD.config_check is not None
    assert NEXTCLOUD.config_check({"server": SERVER, "folders": folders}) == code


def test_login_with_colon_is_refused() -> None:
    assert NEXTCLOUD.credentials_check is not None
    assert NEXTCLOUD.credentials_check({"login": "a:b", "password": "x"}) == (
        "login_invalid"
    )
    assert NEXTCLOUD.credentials_check(IVAN_CREDENTIALS) is None


def test_missing_credentials_are_an_auth_error(server: FakeDav) -> None:
    with pytest.raises(AdapterAuthError) as caught:
        make(server, credentials={})
    assert caught.value.code == "credentials_missing"


# --- Nextcloud: проверка и обход -------------------------------------------------


async def test_check_finds_user_root_through_principal(server: FakeDav) -> None:
    adapter = make(server)
    await adapter.check()
    assert adapter.external_user_id == IVAN
    assert ("PROPFIND", "/remote.php/dav/", "0") in server.calls
    assert ("PROPFIND", f"/remote.php/dav/files/{IVAN}/", "0") in server.calls


async def test_wrong_password_is_auth_failed(server: FakeDav) -> None:
    adapter = make(server, credentials={"login": IVAN_LOGIN, "password": "nope"})
    with pytest.raises(AdapterAuthError) as caught:
        await adapter.check()
    assert caught.value.code == "auth_failed"


async def test_login_is_used_when_server_has_no_principal() -> None:
    server = sample_nextcloud()
    server.principal = False
    server.uids[IVAN_LOGIN] = IVAN_LOGIN
    server.trees[IVAN_LOGIN] = server.trees[IVAN]
    adapter = make(server)
    await adapter.check()
    assert adapter.external_user_id == IVAN_LOGIN


async def test_walk_lists_supported_files_with_depth_one(server: FakeDav) -> None:
    with counting_skips() as skips:
        documents = await listed(make(server))

    assert set(documents) == {
        "Документы/Отпуск.txt",
        "Проекты/План 100%.md",
        "Проекты/Архив/Старый.md",
        "/Заметка.txt",
    }
    plan = documents["Проекты/План 100%.md"]
    node = server.trees[IVAN]["Проекты/План 100%.md"]
    assert plan.external_id == f"nextcloud:id:{node.fileid}"
    assert plan.kind is RemoteDocumentKind.FILE
    assert plan.module == "files"
    assert plan.filename == "План 100%.md"
    assert plan.size == len("# План\n\nСрок — май.".encode())
    assert node.etag in plan.version
    assert plan.modified_at is not None and plan.modified_at.year == 2026
    assert plan.url == f"{SERVER}index.php/f/{node.fileid}"
    # Обход — только Depth 0/1: бесконечную глубину серверы запрещают.
    assert {depth for method, _, depth in server.calls} <= {"0", "1"}
    # Пропуски посчитаны; служебный файл блокировки — молча.
    assert skips.formats() == {".png": 1}
    assert len(skips.too_large) == 1


async def test_shared_folder_is_one_document_for_both(server: FakeDav) -> None:
    ivan = await listed(make(server))
    maria = await listed(make(server, credentials=MARIA_CREDENTIALS))

    assert set(maria) == {
        "Документы/Отпуск.txt",
        "Общее от Ивана/План 100%.md",
        "Общее от Ивана/Архив/Старый.md",
    }
    # Шара: тот же fileid — один материал, доступ обоим.
    assert (
        maria["Общее от Ивана/План 100%.md"].external_id
        == ivan["Проекты/План 100%.md"].external_id
    )
    # Свой файл с тем же путём — другой документ.
    assert (
        maria["Документы/Отпуск.txt"].external_id
        != ivan["Документы/Отпуск.txt"].external_id
    )


async def test_fetch_downloads_by_encoded_path(server: FakeDav) -> None:
    adapter = make(server)
    plan = (await listed(adapter))["Проекты/План 100%.md"]

    fetched = await adapter.fetch(plan, max_bytes=MAX_BYTES)

    assert isinstance(fetched, FetchedFile)
    assert fetched.data == "# План\n\nСрок — май.".encode()
    assert fetched.filename == "План 100%.md"
    assert ("GET", f"/remote.php/dav/files/{IVAN}/Проекты/План 100%.md", "") in (
        server.calls
    )
    assert all(h.startswith("Basic ") for h in server.auth_headers)


async def test_fetch_too_large_and_missing(server: FakeDav) -> None:
    adapter = make(server)
    note = (await listed(adapter))["/Заметка.txt"]
    with pytest.raises(AdapterError) as caught:
        await adapter.fetch(note, max_bytes=3)
    assert caught.value.code == "document_too_large"
    server.remove(IVAN, "Заметка.txt")
    with pytest.raises(AdapterError) as caught:
        await adapter.fetch(note, max_bytes=MAX_BYTES)
    assert caught.value.code == "not_found"


async def test_change_moves_version_and_removal_drops_document(
    server: FakeDav,
) -> None:
    before = await listed(make(server))
    server.change(IVAN, "Заметка.txt", "Новая заметка.".encode())
    server.remove(IVAN, "Проекты/Архив")
    after = await listed(make(server))

    assert after["/Заметка.txt"].version != before["/Заметка.txt"].version
    assert after["/Заметка.txt"].external_id == before["/Заметка.txt"].external_id
    assert (
        after["Документы/Отпуск.txt"].version == before["Документы/Отпуск.txt"].version
    )
    assert "Проекты/Архив/Старый.md" not in after


async def test_folders_setting_limits_the_walk(server: FakeDav) -> None:
    documents = await listed(
        make(server, config={"folders": "Проекты, Нет такой, Проекты/Архив"})
    )
    assert set(documents) == {"Проекты/План 100%.md", "Проекты/Архив/Старый.md"}
    # У Марии этой папки нет — пусто, но не ошибка.
    maria = make(server, config={"folders": "Проекты"}, credentials=MARIA_CREDENTIALS)
    assert await listed(maria) == {}


async def test_forbidden_subfolder_is_skipped_root_is_error(server: FakeDav) -> None:
    server.forbidden.add((IVAN, "Проекты"))
    documents = await listed(make(server))
    assert "Проекты/План 100%.md" not in documents
    assert "Документы/Отпуск.txt" in documents

    server.forbidden.add((IVAN, ""))
    with pytest.raises(AdapterError) as caught:
        await listed(make(server))
    assert caught.value.code == "forbidden"


async def test_links_outside_the_server_or_folder_are_ignored(
    server: FakeDav,
) -> None:
    server.extra_hrefs["Документы"] = [
        f"https://{OTHER_HOST}/remote.php/dav/files/{IVAN}/Документы/Чужой.txt",
        f"/remote.php/dav/files/{IVAN}/Документы/../../{MARIA}/Документы/Отпуск.txt",
        f"/remote.php/dav/files/{IVAN}/Документы/%2E%2E/Тайна.txt",
        f"/remote.php/dav/files/{MARIA}/Документы/Отпуск.txt",
        f"/remote.php/dav/files/{IVAN}/Документы/Глубже/Внук.txt",
        "http://cloud.example.ru/remote.php/dav/files/ivan/Документы/Http.txt",
        "",
    ]
    documents = await listed(make(server))
    assert set(documents) == {
        "Документы/Отпуск.txt",
        "Проекты/План 100%.md",
        "Проекты/Архив/Старый.md",
        "/Заметка.txt",
    }
    assert not any(
        path.startswith(f"/remote.php/dav/files/{MARIA}") for _, path, _ in server.calls
    )


async def test_absolute_hrefs_are_accepted() -> None:
    server = sample_nextcloud(absolute_hrefs=True)
    documents = await listed(make(server))
    assert "Проекты/Архив/Старый.md" in documents


async def test_download_does_not_follow_redirect_off_server(server: FakeDav) -> None:
    adapter = make(server)
    note = (await listed(adapter))["/Заметка.txt"]
    server.get_redirect = f"https://{OTHER_HOST}/file"
    with pytest.raises(AdapterError) as caught:
        await adapter.fetch(note, max_bytes=MAX_BYTES)
    assert caught.value.code == "download_redirect_foreign"


async def test_rate_limit_waits_for_retry_after(server: FakeDav) -> None:
    server.rate_limit_hits = 2
    sleeps = Sleeps()
    documents = await listed(make(server, sleep=sleeps))
    assert "/Заметка.txt" in documents
    assert sleeps == [2.0, 2.0]

    server.rate_limit_hits = 10
    server.retry_after = "3600"
    sleeps = Sleeps()
    with pytest.raises(AdapterError) as caught:
        await listed(make(server, sleep=sleeps))
    assert caught.value.code == "rate_limited"
    assert caught.value.retryable
    assert max(sleeps) <= client_module.MAX_RETRY_AFTER


async def test_html_instead_of_multistatus(server: FakeDav) -> None:
    server.html_instead = True
    with pytest.raises(AdapterError) as caught:
        await make(server).check()
    assert caught.value.code == "not_webdav"


async def test_oversized_listing_is_refused(
    server: FakeDav, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "MAX_LISTING_BYTES", 300)
    with pytest.raises(AdapterError) as caught:
        await listed(make(server))
    assert caught.value.code == "response_too_large"


async def test_tree_limits_are_errors_not_silent_truncation(
    server: FakeDav, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter_module, "MAX_ENTRIES", 5)
    with pytest.raises(AdapterError) as caught:
        await listed(make(server))
    assert caught.value.code == "tree_too_large"
    assert not caught.value.retryable

    monkeypatch.setattr(adapter_module, "MAX_ENTRIES", 10_000)
    monkeypatch.setattr(adapter_module, "MAX_DEPTH", 1)
    with pytest.raises(AdapterError) as caught:
        await listed(make(server))
    assert caught.value.code == "tree_too_deep"


# --- id без сквозного fileid ------------------------------------------------------


async def test_plain_webdav_ids_are_per_employee() -> None:
    """Без fileid путь не говорит, что файл тот же: у двух сотрудников
    «Документы/Отпуск.txt» — разные файлы (домашние папки NAS)."""
    server = sample_nextcloud("plain")
    config = {"server": f"https://{HOST}/dav/"}
    ivan = await listed(make(server, WEBDAV.kind, config=config))
    again = await listed(make(server, WEBDAV.kind, config=config))
    maria = await listed(
        make(server, WEBDAV.kind, config=config, credentials=MARIA_CREDENTIALS)
    )

    assert ivan["Документы/Отпуск.txt"].external_id.startswith("webdav:u:")
    assert (
        ivan["Документы/Отпуск.txt"].external_id
        != maria["Документы/Отпуск.txt"].external_id
    )
    assert (
        ivan["Проекты/План 100%.md"].external_id
        != maria["Общее от Ивана/План 100%.md"].external_id
    )
    assert {d.external_id for d in ivan.values()} == {
        d.external_id for d in again.values()
    }
    assert all(len(d.external_id) <= 128 for d in ivan.values())
    plan = ivan["Проекты/План 100%.md"]
    # Веб-интерфейса у WebDAV нет: ссылка — сам файл (браузер спросит пароль).
    assert plan.url == (
        f"https://{HOST}/dav/{quote('Проекты', safe='')}/"
        f"{quote('План 100%.md', safe='')}"
    )
    fetched = await make(server, WEBDAV.kind, config=config).fetch(
        plan, max_bytes=MAX_BYTES
    )
    assert isinstance(fetched, FetchedFile)
    assert fetched.data.startswith("# План".encode())


async def test_seafile_and_vk_workspace_use_the_given_address() -> None:
    server = sample_nextcloud("plain", plain_root="/seafdav/")
    documents = await listed(
        make(server, SEAFILE.kind, config={"server": f"https://{HOST}/seafdav"})
    )
    assert "/Заметка.txt" in documents
    assert next(iter(documents.values())).external_id.startswith("seafile:u:")

    server = sample_nextcloud("plain", plain_root="/")
    documents = await listed(make(server, VK_WORKSPACE.kind))
    assert "/Заметка.txt" in documents


async def test_mailru_cloud_uses_fixed_address() -> None:
    server = sample_nextcloud("plain", plain_root="/", host=MAILRU_HOST)
    adapter = make(server, MAILRU.kind)
    await adapter.check()
    documents = await listed(adapter)
    plan = documents["Проекты/План 100%.md"]
    assert plan.external_id.startswith("mailru_cloud:u:")
    assert plan.url == (
        "https://cloud.mail.ru/home/%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B/"
    )


async def test_owncloud_uses_legacy_webdav_root_and_fileid() -> None:
    server = sample_nextcloud("owncloud")
    adapter = make(server, OWNCLOUD.kind)
    await adapter.check()
    documents = await listed(adapter)
    node = server.trees[IVAN]["Заметка.txt"]
    assert documents["/Заметка.txt"].external_id == f"owncloud:id:{node.fileid}"
    assert ("PROPFIND", "/remote.php/webdav/", "0") in server.calls


# --- Nextcloud OAuth2 -------------------------------------------------------------


def oauth(server: FakeDav, secret: str = CLIENT_SECRET) -> NextcloudOAuth:
    return NextcloudOAuth(
        server.client(), server=SERVER, client_id=CLIENT_ID, client_secret=secret
    )


async def test_oauth_authorize_url_points_to_the_server(server: FakeDav) -> None:
    url = oauth(server).authorize_url("st-1")
    assert url.startswith(f"{SERVER}index.php/apps/oauth2/authorize?")
    assert f"client_id={CLIENT_ID}" in url
    assert "state=st-1" in url and "response_type=code" in url
    assert CLIENT_SECRET not in url


async def test_oauth_exchange_returns_tokens_and_user(server: FakeDav) -> None:
    server.codes[AUTH_CODE] = IVAN
    exchanged = await oauth(server).exchange(AUTH_CODE)
    assert exchanged.external_user_id == IVAN
    assert set(exchanged.credentials) == {
        "access_token",
        "refresh_token",
        "expires_at",
        "user_id",
    }
    assert int(exchanged.credentials["expires_at"]) > time.time() + 3000

    adapter = make(
        server,
        NEXTCLOUD_OAUTH.kind,
        config={"client_id": CLIENT_ID},
        credentials={"client_secret": CLIENT_SECRET, **exchanged.credentials},
    )
    await adapter.check()
    assert adapter.external_user_id == IVAN
    assert adapter.refreshed_credentials is None
    assert server.auth_headers[-1].startswith("Bearer ")
    # Код одноразовый.
    with pytest.raises(AdapterAuthError) as caught:
        await oauth(server).exchange(AUTH_CODE)
    assert caught.value.code == "invalid_grant"


async def test_oauth_wrong_secret_is_a_config_error(server: FakeDav) -> None:
    server.codes[AUTH_CODE] = IVAN
    with pytest.raises(AdapterConfigError) as caught:
        await oauth(server, secret="wrong").exchange(AUTH_CODE)
    assert caught.value.code == "invalid_client"


async def test_oauth_expired_token_is_refreshed_and_rotated(server: FakeDav) -> None:
    server.refresh["old-refresh"] = IVAN
    adapter = make(
        server,
        NEXTCLOUD_OAUTH.kind,
        config={"client_id": CLIENT_ID},
        credentials={
            "client_secret": CLIENT_SECRET,
            "access_token": "expired",
            "refresh_token": "old-refresh",
            "expires_at": str(int(time.time()) - 10),
            "user_id": IVAN,
        },
    )
    documents = await listed(adapter)
    assert "/Заметка.txt" in documents
    refreshed = adapter.refreshed_credentials
    assert refreshed is not None
    assert refreshed["access_token"] in server.bearer
    assert refreshed["refresh_token"] in server.refresh
    assert "old-refresh" not in server.refresh
    assert refreshed["user_id"] == IVAN


async def test_oauth_token_rejected_after_refresh_is_invalid_grant(
    server: FakeDav,
) -> None:
    adapter = make(
        server,
        NEXTCLOUD_OAUTH.kind,
        config={"client_id": CLIENT_ID},
        credentials={
            "client_secret": CLIENT_SECRET,
            "access_token": "revoked",
            "refresh_token": "used-elsewhere",
            "expires_at": str(int(time.time()) + 3000),
        },
    )
    with pytest.raises(AdapterAuthError) as caught:
        await adapter.check()
    assert caught.value.code == "invalid_grant"


async def test_oauth_without_app_secret_is_config_error(server: FakeDav) -> None:
    with pytest.raises(AdapterConfigError):
        make(
            server,
            NEXTCLOUD_OAUTH.kind,
            config={"client_id": CLIENT_ID},
            credentials={"access_token": "a", "refresh_token": "r"},
        )


async def test_oauth_revoke_is_not_offered(server: FakeDav) -> None:
    assert await oauth(server).revoke({"access_token": "a"}) is False


def test_registry_builds_every_kind(server: FakeDav) -> None:
    registry = default_registry(settings())
    for spec in SPECS:
        assert registry.spec(spec.kind).title == spec.title
    flow = registry.build_oauth(
        NEXTCLOUD_OAUTH.kind,
        {"server": SERVER, "client_id": CLIENT_ID},
        {"client_secret": CLIENT_SECRET},
        server.client(),
    )
    assert isinstance(flow, NextcloudOAuth)


# --- разбор multistatus -----------------------------------------------------------


def test_multistatus_refuses_dtd_and_non_xml() -> None:
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">'
        b'<!ENTITY b "&a;&a;&a;&a;">]><d:multistatus xmlns:d="DAV:">'
        b"<d:response><d:href>&b;</d:href></d:response></d:multistatus>"
    )
    for body in (bomb, b"<html>login</html>", b"not xml", b'{"a": 1}'):
        with pytest.raises(AdapterError) as caught:
            parse_multistatus(body)
        assert caught.value.code == "not_webdav"


def test_multistatus_takes_only_found_props() -> None:
    body = (
        b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
        b'xmlns:oc="http://owncloud.org/ns"><d:response><d:href>/dav/a.txt</d:href>'
        b"<d:propstat><d:prop><d:getetag>W/&quot;e1&quot;</d:getetag>"
        b"<d:getlastmodified>Tue, 05 May 2026 10:00:00 GMT</d:getlastmodified>"
        b"<d:resourcetype/></d:prop><d:status>HTTP/1.1 200 OK</d:status>"
        b"</d:propstat><d:propstat><d:prop><oc:fileid>99</oc:fileid>"
        b"<d:getcontentlength>12</d:getcontentlength></d:prop>"
        b"<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response>"
        b"<d:response><d:href></d:href></d:response></d:multistatus>"
    )
    [entry] = parse_multistatus(body)
    assert entry.href == "/dav/a.txt"
    assert entry.etag == "e1"
    assert entry.modified is not None and entry.modified.tzinfo is not None
    assert not entry.is_collection
    assert entry.file_id == "" and entry.size is None
