"""Адаптер Google Drive (Workspace): сервисный аккаунт с делегированием на
домен, общие диски и диски сотрудников, права из permissions с
раскрытием групп, экспорт Документов, лимиты квоты."""

import json

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
from corp_ed.connectors.gdrive import KIND, SPEC
from corp_ed.connectors.gdrive.adapter import GoogleDriveAdapter, build_adapter
from corp_ed.connectors.registry import default_registry
from corp_ed.core.config import ConnectorSettings
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind
from tests.connectors.fake_google import (
    ADMIN,
    ALL_SCOPES,
    CONTENT_HOST,
    DIRECTORY_API,
    DOCX,
    DOMAIN,
    DRIVE_API,
    DRIVE_SCOPE,
    GROUPS_SCOPE,
    SA_CLIENT_ID,
    SA_EMAIL,
    TOKEN_URL,
    USERS_SCOPE,
    FakeGoogle,
    perm,
    sample_google,
    service_account_key,
)

KEY = Fernet.generate_key().decode()
MAX_BYTES = 1024 * 1024
CONFIG = {"domains": DOMAIN, "admin_email": ADMIN}
ANNA = "anna@example.com"
BORIS = "boris@example.com"


def settings(**overrides: str) -> ConnectorSettings:
    return ConnectorSettings(
        secrets_keys=KEY,
        google_token_url=TOKEN_URL,
        google_drive_api=DRIVE_API,
        google_directory_api=DIRECTORY_API,
        max_document_bytes=MAX_BYTES,
        **overrides,
    )  # type: ignore[arg-type]


class Sleeps(list[float]):
    async def __call__(self, seconds: float) -> None:
        self.append(seconds)


def make_adapter(
    server: FakeGoogle,
    *,
    config: dict[str, str] | None = None,
    sleeps: Sleeps | None = None,
) -> GoogleDriveAdapter:
    return build_adapter(
        {**CONFIG, **(config or {})},
        {"service_account_key": service_account_key()},
        server.client(),
        settings(),
        sleep=sleeps if sleeps is not None else Sleeps(),
    )


async def listed(
    adapter: GoogleDriveAdapter, modules: tuple[str, ...]
) -> dict[str, RemoteDocument]:
    return {d.external_id: d async for d in adapter.list(list(modules))}


@pytest.fixture
def server() -> FakeGoogle:
    return sample_google()


def _permission_calls(server: FakeGoogle) -> list[str]:
    return [
        path.split("/")[4]
        for _, path, _ in server.calls
        if path.startswith("/drive/v3/files/") and path.endswith("/permissions")
    ]


# --- спецификация ------------------------------------------------------------------


def test_spec_is_organization_with_service_account() -> None:
    assert SPEC.kind == KIND == "gdrive"
    assert SPEC.mode is ConnectorMode.ORGANIZATION
    assert SPEC.preview and SPEC.base
    assert [m.name for m in SPEC.modules] == ["shared_drives", "user_drives"]
    assert [f.name for f in SPEC.config_fields] == ["domains", "admin_email"]
    assert [f.name for f in SPEC.credential_fields] == ["service_account_key"]
    key_field = SPEC.credential_fields[0]
    assert key_field.secret and key_field.required
    # JSON-ключ длиннее 2 КиБ: поле разрешает больше.
    assert key_field.max_length >= len(service_account_key()) > 2048
    assert SPEC.url_field is None
    assert set(SPEC.extra["scopes"].split(",")) == ALL_SCOPES


@pytest.mark.parametrize(
    ("domains", "admin", "code"),
    [
        ("example.com", "admin@example.com", None),
        ("Example.com, corp.example.org", "a@corp.example.org", None),
        ("example.com", "admin@other.org", "admin_email_domain_mismatch"),
        ("example.com", "admin", "admin_email_invalid"),
        ("https://example.com/", "a@example.com", "domains_invalid"),
        (" , ", "a@example.com", "domains_invalid"),
    ],
)
def test_config_check(domains: str, admin: str, code: str | None) -> None:
    assert SPEC.config_check is not None
    assert SPEC.config_check({"domains": domains, "admin_email": admin}) == code


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (service_account_key(), None),
        # Поле пароля в браузере склеивает строки: JSON остаётся JSON.
        (service_account_key().replace("\n", ""), None),
        ("not json", "service_account_key_invalid"),
        ("[1, 2]", "service_account_key_invalid"),
        (service_account_key(type="authorized_user"), "service_account_key_invalid"),
        (service_account_key(client_email=""), "service_account_key_invalid"),
        (
            service_account_key(private_key="-----BEGIN PRIVATE KEY-----\nAAAA\n"),
            "service_account_key_invalid",
        ),
    ],
)
def test_credentials_check(value: str, code: str | None) -> None:
    assert SPEC.credentials_check is not None
    assert SPEC.credentials_check({"service_account_key": value}) == code


def test_registry_builds_gdrive_but_hides_it_until_checked(server: FakeGoogle) -> None:
    registry = default_registry(settings())
    assert "gdrive" not in [spec.kind for spec in registry.kinds()]
    adapter = registry.build(
        "gdrive",
        CONFIG,
        {"service_account_key": service_account_key()},
        server.client(),
    )
    assert isinstance(adapter, GoogleDriveAdapter)
    with pytest.raises(AdapterAuthError, match="credentials_missing"):
        registry.build("gdrive", CONFIG, {}, server.client())
    with pytest.raises(AdapterAuthError, match="service_account_key_invalid"):
        registry.build("gdrive", CONFIG, {"service_account_key": "{}"}, server.client())
    with pytest.raises(AdapterConfigError, match="admin_email_missing"):
        registry.build(
            "gdrive",
            {"domains": DOMAIN},
            {"service_account_key": service_account_key()},
            server.client(),
        )
    enabled = default_registry(settings(preview_kinds="gdrive"))
    assert "gdrive" in [spec.kind for spec in enabled.kinds()]


# --- проверка и токены ----------------------------------------------------------


async def test_check_signs_jwt_for_the_admin(server: FakeGoogle) -> None:
    await make_adapter(server).check()

    assert server.assertions
    claims = server.assertions[0]
    assert claims["iss"] == SA_EMAIL
    assert claims["sub"] == ADMIN
    # Адрес токенов — наш, а не token_uri из ключа.
    assert claims["aud"] == TOKEN_URL
    assert claims["exp"] - claims["iat"] <= 3600
    scopes = {c["scope"] for c in server.assertions}
    # Каждый scope — своим токеном: «Диски сотрудников» не нужны для проверки.
    assert scopes == {DRIVE_SCOPE, GROUPS_SCOPE}
    assert any(
        path == "/drive/v3/drives" and query.get("useDomainAdminAccess") == "true"
        for _, path, query in server.calls
    )


@pytest.mark.parametrize(
    ("missing", "code"),
    [
        (DRIVE_SCOPE, "delegation_missing_drive"),
        (GROUPS_SCOPE, "delegation_missing_groups"),
    ],
)
async def test_check_reports_missing_delegation(
    server: FakeGoogle, missing: str, code: str
) -> None:
    server.delegated.discard(missing)
    with pytest.raises(AdapterConfigError) as caught:
        await make_adapter(server).check()
    assert caught.value.code == code


async def test_check_requires_a_workspace_admin(server: FakeGoogle) -> None:
    server.users[ADMIN]["admin"] = False
    with pytest.raises(AdapterConfigError, match="admin_required"):
        await make_adapter(server).check()


async def test_revoked_key_is_an_auth_error(server: FakeGoogle) -> None:
    server.key_revoked = True
    with pytest.raises(AdapterAuthError) as caught:
        await make_adapter(server).check()
    assert caught.value.code == "invalid_grant"


async def test_unknown_admin_is_an_auth_error(server: FakeGoogle) -> None:
    adapter = make_adapter(server, config={"admin_email": "ghost@example.com"})
    with pytest.raises(AdapterAuthError, match="invalid_grant"):
        await adapter.check()


async def test_tokens_are_cached_per_subject_and_scope(server: FakeGoogle) -> None:
    adapter = make_adapter(server)
    await adapter.check()
    await adapter.check()
    assert server.issued == 2


async def test_expired_token_is_renewed_once(server: FakeGoogle) -> None:
    adapter = make_adapter(server)
    await adapter.check()
    server.revoked.update(server.tokens)
    await adapter.check()  # новый токен — и дальше
    server.revoked.update(server.tokens)
    server.key_revoked = True
    with pytest.raises(AdapterAuthError):
        await adapter.check()


# --- общие диски ---------------------------------------------------------------------


async def test_shared_drives_mirror_permissions(server: FakeGoogle) -> None:
    adapter = make_adapter(server)
    with counting_skips() as skips:
        docs = await listed(adapter, ("shared_drives",))

    assert set(docs) == {"gdrive:f-vacation", "gdrive:f-order", "gdrive:f-salary"}
    vacation = docs["gdrive:f-vacation"]
    assert vacation.module == "shared_drives"
    assert vacation.kind is RemoteDocumentKind.FILE
    assert vacation.path == "Общие диски/Кадры/Регламенты"
    assert vacation.filename == "Отпуск.txt"
    assert vacation.url == "https://docs.google.com/d/f-vacation/view"
    assert vacation.version == "2026-09-01T10:00:00.000Z:md5-f-vacation"
    # Участник диска — группа hr@ (Анна) с вложенной leads@ (Борис).
    assert vacation.visibility is MaterialVisibility.RESTRICTED
    assert vacation.allowed_emails == {ANNA, BORIS}
    order = docs["gdrive:f-order"]
    assert order.allowed_emails == {ANNA, BORIS, "partner@other.org"}
    # Папка с ограниченным доступом: только те, кого добавили в неё прямо.
    assert docs["gdrive:f-salary"].allowed_emails == {BORIS}
    assert skips.formats() == {".png": 1}

    # Файлы общего диска читаются от имени участника (организатора нет —
    # первый из группы), диски без участников из домена пропускаются.
    listings = [
        (subject, query.get("driveId"))
        for subject, path, query in server.calls
        if path == "/drive/v3/files"
    ]
    assert {subject for subject, drive in listings if drive == "drv-hr"} == {ANNA}
    # «Бухгалтерия»: никого из домена — последняя попытка от имени
    # администратора, он не участник, диск пропущен.
    assert {subject for subject, drive in listings if drive == "drv-acc"} == {ADMIN}
    # permissions.list — только где права отличаются от состава диска.
    assert sorted(_permission_calls(server)) == [
        "drv-acc",
        "drv-hr",
        "f-order",
        "f-salary",
        "fld-closed",
    ]


async def test_organizer_is_preferred_for_reading_a_drive(server: FakeGoogle) -> None:
    server.drives["drv-hr"]["perms"].append(perm("user", BORIS, "organizer"))
    await listed(make_adapter(server), ("shared_drives",))
    subjects = {
        s
        for s, path, query in server.calls
        if path == "/drive/v3/files" and query.get("driveId") == "drv-hr"
    }
    assert subjects == {BORIS}


async def test_whole_company_access(server: FakeGoogle) -> None:
    server.add_drive("drv-all", "Все", perm("domain", DOMAIN), perm("user", BORIS))
    server.add_file("f-all", "Памятка.txt", "drv-all", b"x", drive="drv-all")
    server.add_drive(
        "drv-grp", "Компания", perm("group", "all@example.com"), perm("user", ANNA)
    )
    server.add_group("all@example.com", ("CUSTOMER", "C0abc123"))
    server.add_file("f-grp", "Устав.txt", "drv-grp", b"x", drive="drv-grp")

    docs = await listed(make_adapter(server), ("shared_drives",))

    for external_id in ("gdrive:f-all", "gdrive:f-grp"):
        assert docs[external_id].visibility is MaterialVisibility.TENANT
        assert docs[external_id].allowed_emails == frozenset()


async def test_groups_outside_the_domain_are_not_expanded(server: FakeGoogle) -> None:
    server.add_drive(
        "drv-x",
        "Партнёры",
        perm("user", ANNA, "organizer"),
        perm("group", "team@other.org"),
        perm("group", "gone@example.com"),
    )
    server.add_file("f-x", "Договор.txt", "drv-x", b"x", drive="drv-x")

    docs = await listed(make_adapter(server), ("shared_drives",))

    assert docs["gdrive:f-x"].allowed_emails == {ANNA}
    groups = {p for _, p, _ in server.calls if p.startswith("/admin/directory")}
    assert "/admin/directory/v1/groups/team@other.org/members" not in groups
    assert "/admin/directory/v1/groups/gone@example.com/members" in groups


async def test_deleted_and_metadata_only_permissions_are_ignored(
    server: FakeGoogle,
) -> None:
    server.add_drive("drv-y", "Тест", perm("user", ANNA, "organizer"))
    server.add_file(
        "f-y",
        "Файл.txt",
        "drv-y",
        b"x",
        drive="drv-y",
        augmented=True,
        perms=[
            perm("user", ANNA, "organizer"),
            perm("user", "gone@example.com", deleted=True),
            perm("user", BORIS, view="metadata"),
            perm("anyone", view="published"),
        ],
    )
    docs = await listed(make_adapter(server), ("shared_drives",))
    assert docs["gdrive:f-y"].allowed_emails == {ANNA}
    assert docs["gdrive:f-y"].visibility is MaterialVisibility.RESTRICTED


async def test_listing_follows_page_tokens(server: FakeGoogle) -> None:
    server.page_size = 1
    docs = await listed(make_adapter(server), ("shared_drives", "user_drives"))
    assert len(docs) == 7
    assert docs["gdrive:f-vacation"].allowed_emails == {ANNA, BORIS}


# --- диски сотрудников ----------------------------------------------------------------


async def test_user_drives_list_owned_files(server: FakeGoogle) -> None:
    with counting_skips() as skips:
        docs = await listed(make_adapter(server), ("user_drives",))

    assert set(docs) == {
        "gdrive:f-plan",
        "gdrive:f-budget",
        "gdrive:f-report",
        "gdrive:f-partner",
    }
    plan = docs["gdrive:f-plan"]
    assert plan.module == "user_drives"
    assert plan.title == "План"
    assert plan.filename == "План.docx"
    assert plan.path == f"Диски сотрудников/{ANNA}/Проекты"
    assert plan.allowed_emails == {ANNA, BORIS}
    # Таблица «всем в домене» и PDF «всем по ссылке» — вся компания.
    assert docs["gdrive:f-budget"].filename == "Бюджет.xlsx"
    assert docs["gdrive:f-budget"].visibility is MaterialVisibility.TENANT
    report = docs["gdrive:f-report"]
    assert report.filename == "Отчёт.pdf"
    assert report.visibility is MaterialVisibility.TENANT
    # Доступ «всем в чужом домене» нашим сотрудникам ничего не даёт.
    assert docs["gdrive:f-partner"].allowed_emails == {ANNA}
    assert skips.formats() == {".form": 1}
    # Заблокированных не имперсонируем; права пришли листингом.
    assert "vera@example.com" not in {c["sub"] for c in server.assertions}
    assert _permission_calls(server) == []


async def test_user_drives_need_users_delegation(server: FakeGoogle) -> None:
    server.delegated.discard(USERS_SCOPE)
    with pytest.raises(AdapterConfigError, match="delegation_missing_users"):
        await listed(make_adapter(server), ("user_drives",))


async def test_user_drives_need_a_directory_admin(server: FakeGoogle) -> None:
    server.users[ADMIN]["admin"] = False
    with pytest.raises(AdapterConfigError, match="admin_required"):
        await listed(make_adapter(server), ("user_drives",))


async def test_user_who_cannot_be_impersonated_is_skipped(server: FakeGoogle) -> None:
    server.token_rejects.add(ANNA)
    docs = await listed(make_adapter(server), ("user_drives",))
    assert docs == {}


async def test_revoked_key_mid_run_stops_instead_of_skipping_everyone(
    server: FakeGoogle,
) -> None:
    adapter = make_adapter(server)
    await adapter.check()
    await adapter._client.auth.token(ADMIN, USERS_SCOPE)  # noqa: SLF001
    server.key_revoked = True
    with pytest.raises(AdapterAuthError, match="invalid_grant"):
        await listed(adapter, ("user_drives",))


async def test_drive_reader_rejected_mid_listing_is_retried(
    server: FakeGoogle,
) -> None:
    server.page_size = 1
    adapter = make_adapter(server)
    documents = adapter.list(["shared_drives"])
    await anext(documents)
    # Токен читающего истёк, а сам он за это время заблокирован.
    server.revoked.update(t for t, (s, _) in server.tokens.items() if s == ANNA)
    server.token_rejects.add(ANNA)
    with pytest.raises(AdapterError) as caught:
        async for _ in documents:
            pass
    assert caught.value.code == "drive_interrupted"
    assert caught.value.retryable


async def test_unreadable_drive_or_user_is_skipped(server: FakeGoogle) -> None:
    """Диск выключен для отдела сотрудника — его диски пропускаются,
    остальное обходится, запуск не падает."""
    server.add_drive("drv-b", "Склад", perm("user", BORIS, "organizer"))
    server.add_file("f-b", "Остатки.txt", "drv-b", b"x", drive="drv-b")
    server.listing_forbidden.add(ANNA)

    docs = await listed(make_adapter(server), ("shared_drives", "user_drives"))

    assert set(docs) == {"gdrive:f-b"}


async def test_listing_broken_midway_is_retried_not_trimmed(
    server: FakeGoogle,
) -> None:
    server.page_size = 1
    server.forbid_next_file_pages = True
    documents = make_adapter(server).list(["shared_drives"])
    assert (await anext(documents)).external_id == "gdrive:f-vacation"
    with pytest.raises(AdapterError) as caught:
        async for _ in documents:
            pass
    assert caught.value.code == "drive_interrupted"
    assert caught.value.retryable


async def test_disabled_api_is_a_setup_error(server: FakeGoogle) -> None:
    server.api_disabled = True
    with pytest.raises(AdapterConfigError, match="api_not_enabled"):
        await make_adapter(server).check()


async def test_drive_open_to_the_whole_account_is_read_as_admin(
    server: FakeGoogle,
) -> None:
    server.add_group("everyone@example.com", ("CUSTOMER", "C0abc123"))
    server.add_group("all@example.com", ("CUSTOMER", "C0abc123"), ("USER", ADMIN))
    server.add_drive("drv-all", "Всем", perm("group", "everyone@example.com"))
    # Фейк считает участником по составу группы: админ — в группе all@.
    server.drives["drv-all"]["perms"].append(perm("group", "all@example.com"))
    server.add_file("f-all", "Памятка.txt", "drv-all", b"x", drive="drv-all")

    docs = await listed(make_adapter(server), ("shared_drives",))

    assert docs["gdrive:f-all"].visibility is MaterialVisibility.TENANT


# --- скачивание ----------------------------------------------------------------------


async def test_google_document_is_exported_to_docx(server: FakeGoogle) -> None:
    adapter = make_adapter(server)
    docs = await listed(adapter, ("user_drives",))

    fetched = await adapter.fetch(docs["gdrive:f-plan"], max_bytes=MAX_BYTES)

    assert isinstance(fetched, FetchedFile)
    assert fetched.data == b"PK-docx-plan"
    assert fetched.filename == "План.docx"
    export = [
        (subject, query)
        for subject, path, query in server.calls
        if path == "/drive/v3/files/f-plan/export"
    ]
    assert export == [(ANNA, {"mimeType": DOCX})]


async def test_file_is_downloaded_without_token_on_content_host(
    server: FakeGoogle,
) -> None:
    adapter = make_adapter(server)
    docs = await listed(adapter, ("shared_drives",))

    fetched = await adapter.fetch(docs["gdrive:f-vacation"], max_bytes=MAX_BYTES)

    assert isinstance(fetched, FetchedFile)
    assert fetched.data == "Отпуск — 28 дней.".encode()
    assert fetched.filename == "Отпуск.txt"
    assert server.downloads == [(CONTENT_HOST, "")]


async def test_oversized_export_is_document_too_large(server: FakeGoogle) -> None:
    server.export_too_large.add("f-plan")
    adapter = make_adapter(server)
    docs = await listed(adapter, ("user_drives",))
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(docs["gdrive:f-plan"], max_bytes=MAX_BYTES)


async def test_large_files_are_skipped_before_download(server: FakeGoogle) -> None:
    server.add_file(
        "f-big",
        "Архив.pdf",
        "root-anna",
        b"%PDF",
        mime="application/pdf",
        owner=ANNA,
        perms=[perm("user", ANNA, "owner")],
        size=MAX_BYTES + 1,
    )
    adapter = make_adapter(server)
    with counting_skips() as skips:
        docs = await listed(adapter, ("user_drives",))
    assert "gdrive:f-big" not in docs
    assert skips.too_large == {"f-big"}


async def test_download_refused_by_owner_is_forbidden(server: FakeGoogle) -> None:
    adapter = make_adapter(server)
    docs = await listed(adapter, ("user_drives",))
    server.forbidden_files.add("f-report")
    with pytest.raises(AdapterError, match="forbidden"):
        await adapter.fetch(docs["gdrive:f-report"], max_bytes=MAX_BYTES)


async def test_fetch_rejects_a_document_without_locator(server: FakeGoogle) -> None:
    document = RemoteDocument(
        external_id="gdrive:f-plan",
        title="План",
        url="",
        version="v",
        kind=RemoteDocumentKind.FILE,
        module="user_drives",
    )
    with pytest.raises(AdapterError, match="locator_missing"):
        await make_adapter(server).fetch(document, max_bytes=MAX_BYTES)


# --- лимиты и сбои --------------------------------------------------------------


async def test_rate_limits_back_off_then_give_up(server: FakeGoogle) -> None:
    sleeps = Sleeps()
    adapter = make_adapter(server, sleeps=sleeps)
    server.rate_limit_hits = 2
    server.rate_limit_403_hits = 1
    await adapter.check()
    assert sleeps == [1.0, 2.0, 4.0]

    server.rate_limit_hits = 100
    with pytest.raises(AdapterError) as caught:
        await adapter.check()
    assert caught.value.code == "rate_limited" and caught.value.retryable


async def test_errors_do_not_leak_provider_text(server: FakeGoogle) -> None:
    adapter = make_adapter(server)
    docs = await listed(adapter, ("shared_drives",))
    del server.files["f-vacation"]
    with pytest.raises(AdapterError) as caught:
        await adapter.fetch(docs["gdrive:f-vacation"], max_bytes=MAX_BYTES)
    assert caught.value.code == "not_found"
    assert "f-vacation" not in str(caught.value)


def test_key_with_unknown_fields_still_parses() -> None:
    raw = json.loads(service_account_key())
    raw["new_field"] = {"nested": True}
    assert SPEC.credentials_check is not None
    assert SPEC.credentials_check({"service_account_key": json.dumps(raw)}) is None
    assert raw["client_id"] == SA_CLIENT_ID
