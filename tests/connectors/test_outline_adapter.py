"""Адаптер Outline и Yonote (режим organization): каталог, клиент RPC,
зеркало прав (коллекции, группы, участники документа), документы и их
Markdown, лимиты частоты."""

from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    FetchedMarkdown,
    RemoteDocument,
)
from corp_ed.connectors.outline import adapter as adapter_module
from corp_ed.connectors.outline.adapter import (
    OUTLINE_SPEC,
    YONOTE_SPEC,
    OutlineAdapter,
    build_adapter,
)
from corp_ed.connectors.outline.client import OutlineClient
from corp_ed.connectors.registry import default_registry
from corp_ed.core.config import ConnectorSettings
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind
from tests.connectors.fake_outline import (
    API_KEY,
    MEMBER_KEY,
    FakeOutline,
    sample_outline,
)

KEY = Fernet.generate_key().decode()
MAX_BYTES = 1024 * 1024


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def settings(**overrides: Any) -> ConnectorSettings:
    return ConnectorSettings(  # type: ignore[arg-type]
        secrets_keys=KEY, max_document_bytes=MAX_BYTES, **overrides
    )


def make_adapter(
    server: FakeOutline, key: str = API_KEY, *, sleep: Sleeps | None = None
) -> OutlineAdapter:
    client = OutlineClient(
        server.client(), base_url=server.base, token=key, sleep=sleep or Sleeps()
    )
    return OutlineAdapter(
        client, document_memberships=server.dialect == "outline", max_bytes=MAX_BYTES
    )


async def listed(adapter: OutlineAdapter) -> dict[str, RemoteDocument]:
    return {d.external_id: d async for d in adapter.list(["documents"])}


@pytest.fixture
def server() -> FakeOutline:
    return sample_outline()


# --- каталог и сборка ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "kind"), [(OUTLINE_SPEC, "outline"), (YONOTE_SPEC, "yonote")]
)
def test_specs_mirror_rights_with_admin_key(spec: Any, kind: str) -> None:
    assert spec.kind == kind
    assert spec.mode is ConnectorMode.ORGANIZATION and not spec.oauth
    assert spec.preview and spec.base
    assert [m.name for m in spec.modules] == ["documents"]
    assert [(f.name, f.required) for f in spec.config_fields] == [("base_url", False)]
    assert [(f.name, f.secret) for f in spec.credential_fields] == [("token", True)]
    assert spec.url_field == "base_url"


def test_kinds_are_offered_only_when_enabled() -> None:
    assert not {"outline", "yonote"} & {
        s.kind for s in default_registry(settings()).kinds()
    }
    enabled = default_registry(settings(preview_kinds="outline,yonote"))
    assert {"outline", "yonote"} <= {s.kind for s in enabled.kinds()}


@pytest.mark.parametrize(
    ("kind", "config", "root"),
    [
        ("outline", {}, "https://app.getoutline.com/"),
        ("yonote", {}, "https://app.yonote.ru/"),
        (
            "outline",
            {"base_url": "https://wiki.company.ru/api/"},
            "https://wiki.company.ru/",
        ),
        (
            "yonote",
            {"base_url": "https://yonote.company.ru"},
            "https://yonote.company.ru/",
        ),
    ],
)
def test_default_and_custom_address(
    server: FakeOutline, kind: str, config: dict[str, str], root: str
) -> None:
    adapter = default_registry(settings()).build(
        kind, config, {"token": API_KEY}, server.client()
    )
    assert isinstance(adapter, OutlineAdapter)
    assert adapter.root == root


def test_build_requires_a_key(server: FakeOutline) -> None:
    with pytest.raises(AdapterAuthError, match="credentials_missing"):
        build_adapter(OUTLINE_SPEC, {}, {}, server.client(), settings())


# --- клиент и проверка ------------------------------------------------------------


async def test_check_accepts_admin_key_only(server: FakeOutline) -> None:
    await make_adapter(server).check()
    with pytest.raises(AdapterAuthError, match="admin_required"):
        await make_adapter(server, MEMBER_KEY).check()
    with pytest.raises(AdapterAuthError, match="unauthorized"):
        await make_adapter(server, "wrong").check()
    assert server.calls[0] == ("auth.info", {})


async def test_yonote_admin_flag_is_accepted() -> None:
    server = sample_outline("yonote")
    await make_adapter(server).check()
    with pytest.raises(AdapterAuthError, match="admin_required"):
        await make_adapter(server, MEMBER_KEY).check()


async def test_rate_limit_honours_retry_after(server: FakeOutline) -> None:
    server.rate_limited["auth.info"] = 2
    sleeps = Sleeps()
    await make_adapter(server, sleep=sleeps).check()
    assert sleeps.calls == [7.0, 7.0]


async def test_rate_limit_wait_is_capped_and_bounded(server: FakeOutline) -> None:
    server.rate_limited["auth.info"] = 100
    server.retry_after = "600"
    sleeps = Sleeps()
    with pytest.raises(AdapterError) as caught:
        await make_adapter(server, sleep=sleeps).check()
    assert caught.value.code == "rate_limited" and caught.value.retryable
    assert sleeps.calls == [90.0] * 5


async def test_server_errors_are_retried(server: FakeOutline) -> None:
    server.server_errors = 2
    await make_adapter(server).check()


async def test_error_details_do_not_leak() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"ok": False, "error": "validation_error", "message": "secret!"}
        )

    from corp_ed.core.outbound import OutboundClient
    from tests.fake_connector import public_resolver

    client = OutlineClient(
        OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            resolver=public_resolver,
        ),
        base_url="https://wiki.example.com",
        token=API_KEY,
    )
    with pytest.raises(AdapterError) as caught:
        await client.call("auth.info")
    assert caught.value.code == "outline_validation_error"
    assert "secret" not in str(caught.value)


# --- документы и права ---------------------------------------------------------------


async def test_public_collection_is_company_wide(server: FakeOutline) -> None:
    documents = await listed(make_adapter(server))
    vacation = documents["doc:d-vacation"]
    assert vacation.visibility is MaterialVisibility.TENANT
    assert vacation.allowed_emails == frozenset()
    assert vacation.title == "Отпуск"
    assert vacation.kind is RemoteDocumentKind.PAGE
    assert vacation.module == "documents"
    assert vacation.url == f"{server.base}doc/отпуск-ud-vacation"
    assert vacation.path == "Кадры"
    assert vacation.version == "1:2026-09-01T10:00:00.000Z"
    assert vacation.modified_at is not None
    assert documents["doc:d-sick"].path == "Кадры/Отпуск"


async def test_private_collection_members_groups_and_document_members(
    server: FakeOutline,
) -> None:
    documents = await listed(make_adapter(server))
    budget = documents["doc:d-budget"]
    assert budget.visibility is MaterialVisibility.RESTRICTED
    # Участники коллекции и группы; заблокированный сотрудник — нет.
    assert budget.allowed_emails == {
        "admin@example.com",
        "anna@example.com",
        "boris@example.com",
    }
    bonus = documents["doc:d-bonus"]
    assert bonus.allowed_emails == budget.allowed_emails | {"vera@example.com"}


async def test_drafts_templates_and_loose_documents_are_skipped(
    server: FakeOutline,
) -> None:
    documents = await listed(make_adapter(server))
    assert set(documents) == {
        "doc:d-vacation",
        "doc:d-sick",
        "doc:d-budget",
        "doc:d-bonus",
    }
    # Участников документа спрашивают только у закрытых коллекций.
    asked = [b["id"] for m, b in server.calls if m == "documents.memberships"]
    assert sorted(asked) == ["d-bonus", "d-budget"]


async def test_document_members_unreadable_means_collection_only(
    server: FakeOutline,
) -> None:
    server.locked_documents.add("d-bonus")
    documents = await listed(make_adapter(server))
    assert "vera@example.com" not in documents["doc:d-bonus"].allowed_emails
    assert documents["doc:d-bonus"].allowed_emails


async def test_unknown_permission_is_treated_as_private(server: FakeOutline) -> None:
    server.collections["c-hr"]["permission"] = "something_new"
    documents = await listed(make_adapter(server))
    assert documents["doc:d-vacation"].visibility is MaterialVisibility.RESTRICTED
    assert documents["doc:d-vacation"].allowed_emails == frozenset()


async def test_yonote_dialect_uses_private_flag_without_document_members() -> None:
    server = sample_outline("yonote")
    documents = await listed(make_adapter(server))
    assert documents["doc:d-vacation"].visibility is MaterialVisibility.TENANT
    bonus = documents["doc:d-bonus"]
    assert bonus.visibility is MaterialVisibility.RESTRICTED
    assert bonus.allowed_emails == {
        "admin@example.com",
        "anna@example.com",
        "boris@example.com",
    }
    assert "documents.memberships" not in server.methods()


async def test_documents_and_members_are_paged(server: FakeOutline) -> None:
    for n in range(60):
        server.add_document(f"d-n{n}", "c-hr", f"Документ {n}", "x")
    server.collection_members["c-fin"] += [f"u-x{n}" for n in range(120)]
    documents = await listed(make_adapter(server))
    assert len(documents) == 64
    offsets = [b["offset"] for m, b in server.calls if m == "documents.list"]
    assert offsets == [0, 25, 50]
    members = [b["offset"] for m, b in server.calls if m == "collections.memberships"]
    assert members == [0, 100]


async def test_fetch_uses_listed_text_without_export(server: FakeOutline) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter)
    content = await adapter.fetch(documents["doc:d-vacation"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedMarkdown)
    assert content.markdown == "# Отпуск\n\nОтпуск — 28 дней."
    assert "documents.export" not in server.methods()


async def test_fetch_exports_markdown_when_listing_had_no_text(
    server: FakeOutline,
) -> None:
    server.list_text = False
    adapter = make_adapter(server)
    documents = await listed(adapter)
    content = await adapter.fetch(documents["doc:d-budget"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedMarkdown)
    # Заголовок не задвоился.
    assert content.markdown == "# Бюджет\n\nСмета на год."
    assert ("documents.export", {"id": "d-budget"}) in server.calls


async def test_text_cache_is_bounded(
    server: FakeOutline, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter_module, "MAX_CACHED_BYTES", 20)
    adapter = make_adapter(server)
    documents = await listed(adapter)
    await adapter.fetch(documents["doc:d-sick"], max_bytes=MAX_BYTES)
    assert ("documents.export", {"id": "d-sick"}) in server.calls


async def test_export_waits_out_the_per_minute_limit(server: FakeOutline) -> None:
    server.list_text = False
    server.rate_limited["documents.export"] = 1
    server.retry_after = "45"
    sleeps = Sleeps()
    adapter = make_adapter(server, sleep=sleeps)
    documents = await listed(adapter)
    await adapter.fetch(documents["doc:d-budget"], max_bytes=MAX_BYTES)
    assert sleeps.calls == [45.0]


async def test_fetch_limits(server: FakeOutline) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter)
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(documents["doc:d-vacation"], max_bytes=10)
    server.texts["d-budget"] = "   "
    server.list_text = False
    fresh = make_adapter(server)
    await listed(fresh)
    with pytest.raises(AdapterError, match="empty_page"):
        await fresh.fetch(documents["doc:d-budget"], max_bytes=MAX_BYTES)


async def test_foreign_document_url_falls_back_to_own_host(server: FakeOutline) -> None:
    server.documents["d-vacation"]["url"] = "https://evil.example.org/doc/x"
    documents = await listed(make_adapter(server))
    assert documents["doc:d-vacation"].url == f"{server.base}doc/ud-vacation"


async def test_non_admin_listing_is_refused(server: FakeOutline) -> None:
    with pytest.raises(AdapterAuthError, match="admin_required"):
        await listed(make_adapter(server, MEMBER_KEY))


def test_build_rejects_garbage_address(server: FakeOutline) -> None:
    with pytest.raises(AdapterConfigError, match="base_url_invalid"):
        build_adapter(
            OUTLINE_SPEC,
            {"base_url": "ftp://x"},
            {"token": API_KEY},
            server.client(),
            settings(),
        )
