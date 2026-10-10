"""Адаптер Kaiten (режим per_user): каталог, клиент (ошибки, лимит 429),
обход дерева документов глубже двух уровней только по читаемому,
документы ProseMirror → Markdown, вложения карточек."""

from typing import Any

import pytest
from cryptography.fernet import Fernet

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    FetchedFile,
    FetchedMarkdown,
    RemoteDocument,
)
from corp_ed.connectors.common import counting_skips
from corp_ed.connectors.kaiten import KIND, SPEC
from corp_ed.connectors.kaiten import documents as documents_module
from corp_ed.connectors.kaiten.adapter import KaitenAdapter, build_adapter
from corp_ed.connectors.kaiten.client import KaitenClient
from corp_ed.connectors.registry import UserAuth, default_registry
from corp_ed.core.config import ConnectorSettings
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind
from tests.connectors.fake_kaiten import (
    ANNA_ID,
    ANNA_TOKEN,
    BORIS_TOKEN,
    NOW,
    FakeKaiten,
    pm,
    sample_kaiten,
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
        secrets_keys=KEY,
        max_document_bytes=MAX_BYTES,
        max_large_document_bytes=MAX_BYTES,
        **overrides,
    )


def make_adapter(
    server: FakeKaiten,
    token: str = ANNA_TOKEN,
    *,
    sleep: Sleeps | None = None,
    spaces: str = "",
) -> KaitenAdapter:
    client = KaitenClient(
        server.client(),
        base_url=server.base,
        token=token,
        sleep=sleep or Sleeps(),
        clock=lambda: NOW,
    )
    return KaitenAdapter(client, max_bytes=MAX_BYTES, spaces=spaces.split(","))


async def listed(adapter: KaitenAdapter, *modules: str) -> dict[str, RemoteDocument]:
    return {d.external_id: d async for d in adapter.list(list(modules))}


@pytest.fixture
def server() -> FakeKaiten:
    return sample_kaiten()


# --- каталог и сборка -------------------------------------------------------------


def test_spec_is_per_user_with_personal_token() -> None:
    assert SPEC.kind == KIND == "kaiten"
    assert SPEC.mode is ConnectorMode.PER_USER
    assert SPEC.user_auth is UserAuth.FIELDS and not SPEC.oauth
    assert SPEC.preview and SPEC.base
    assert [m.name for m in SPEC.modules] == ["documents", "card_files"]
    assert [(f.name, f.required) for f in SPEC.config_fields] == [
        ("base_url", True),
        ("spaces", False),
    ]
    assert [(f.name, f.secret) for f in SPEC.credential_fields] == [("token", True)]
    assert SPEC.app_credential_fields == ()
    assert SPEC.url_field == "base_url"


def test_spec_checks_space_ids() -> None:
    assert SPEC.config_check is not None
    assert SPEC.config_check({"base_url": "https://x.kaiten.ru"}) is None
    assert SPEC.config_check({"spaces": " 12, 7 "}) is None
    assert SPEC.config_check({"spaces": "12,abc"}) == "spaces_invalid"


def test_kind_is_offered_only_when_enabled(server: FakeKaiten) -> None:
    hidden = default_registry(settings())
    assert "kaiten" not in [spec.kind for spec in hidden.kinds()]
    assert hidden.spec("kaiten").preview
    enabled = default_registry(settings(preview_kinds="kaiten"))
    assert "kaiten" in [spec.kind for spec in enabled.kinds()]
    adapter = enabled.build(
        "kaiten", {"base_url": server.base}, {"token": ANNA_TOKEN}, server.client()
    )
    assert isinstance(adapter, KaitenAdapter)


def test_build_requires_address_and_token(server: FakeKaiten) -> None:
    with pytest.raises(AdapterAuthError, match="credentials_missing"):
        build_adapter({"base_url": server.base}, {}, server.client(), settings())
    with pytest.raises(AdapterConfigError, match="base_url_missing"):
        build_adapter({}, {"token": ANNA_TOKEN}, server.client(), settings())


@pytest.mark.parametrize(
    "address",
    [
        "https://company.kaiten.ru",
        "https://company.kaiten.ru/",
        "https://company.kaiten.ru/api/latest",
        "https://company.kaiten.ru/api/v1/",
    ],
)
async def test_api_address_is_normalized(server: FakeKaiten, address: str) -> None:
    adapter = build_adapter(
        {"base_url": address}, {"token": ANNA_TOKEN}, server.client(), settings()
    )
    await adapter.check()
    assert server.calls[-1][0] == "users/current"


# --- клиент -------------------------------------------------------------------------


async def test_check_remembers_kaiten_user(server: FakeKaiten) -> None:
    adapter = make_adapter(server)
    await adapter.check()
    assert adapter.external_user_id == str(ANNA_ID)


async def test_rejected_token_is_an_auth_error(server: FakeKaiten) -> None:
    with pytest.raises(AdapterAuthError, match="unauthorized"):
        await make_adapter(server, "wrong").check()


async def test_rate_limit_waits_until_reset(server: FakeKaiten) -> None:
    server.rate_limit_hits = 2
    server.reset_header = str(int(NOW) + 3)
    sleeps = Sleeps()
    await make_adapter(server, sleep=sleeps).check()
    assert sleeps.calls == [3.0, 3.0]


async def test_rate_limit_without_reset_backs_off_then_gives_up(
    server: FakeKaiten,
) -> None:
    server.rate_limit_hits = 100
    sleeps = Sleeps()
    with pytest.raises(AdapterError) as caught:
        await make_adapter(server, sleep=sleeps).check()
    assert caught.value.code == "rate_limited" and caught.value.retryable
    assert sleeps.calls == [0.5, 1.0, 2.0, 4.0]


async def test_reset_in_the_past_or_far_future_is_clamped(server: FakeKaiten) -> None:
    server.rate_limit_hits = 2
    server.reset_header = str(int(NOW) + 10_000)
    sleeps = Sleeps()
    await make_adapter(server, sleep=sleeps).check()
    assert sleeps.calls == [30.0, 30.0]
    server.rate_limit_hits = 1
    server.reset_header = str(int(NOW) - 5)
    sleeps = Sleeps()
    await make_adapter(server, sleep=sleeps).check()
    assert sleeps.calls == [0.5]


async def test_server_error_is_retried(server: FakeKaiten) -> None:
    server.server_errors = 1
    await make_adapter(server).check()
    server.server_errors = 10
    with pytest.raises(AdapterError) as caught:
        await make_adapter(server).check()
    assert caught.value.code == "http_503" and caught.value.retryable


# --- документы ---------------------------------------------------------------------


async def test_documents_follow_the_readable_tree(server: FakeKaiten) -> None:
    anna = await listed(make_adapter(server), "documents")
    assert set(anna) == {
        "doc:d-vacation",
        "doc:d-salary",
        "doc:d-bonus",
        "doc:d-inside",
        "doc:d-root",
    }
    bonus = anna["doc:d-bonus"]
    assert bonus.title == "Премии"
    assert bonus.path == "Кадры/Закрытое/Глубже"
    assert bonus.kind is RemoteDocumentKind.PAGE
    assert bonus.module == "documents"
    assert bonus.url == f"{server.base}documents/d-bonus"
    assert bonus.version == "1:2026-09-01T10:00:00.000Z"
    assert bonus.modified_at is not None
    # Видимость считает ядро по листингу сотрудника.
    assert bonus.visibility is MaterialVisibility.RESTRICTED
    assert not bonus.allowed_emails


async def test_unreadable_documents_are_not_listed_even_if_api_lists_them(
    server: FakeKaiten,
) -> None:
    boris = await listed(make_adapter(server, BORIS_TOKEN), "documents")
    # /documents отдаёт и закрытые документы — их нет, раз нет в дереве.
    assert set(boris) == {"doc:d-vacation", "doc:d-inside", "doc:d-root"}
    # Папка, в которой он лежит, Борису не видна — и в пути её нет.
    assert boris["doc:d-inside"].path == ""
    assert boris["doc:d-vacation"].path == "Кадры"


async def test_tree_is_paged_and_descended_below_two_levels(
    server: FakeKaiten, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(documents_module, "TREE_LIMIT", 2)
    anna = await listed(make_adapter(server), "documents")
    assert "doc:d-bonus" in anna
    tree_calls = [q for route, q in server.calls if route == "tree-entities"]
    assert {q.get("levels_count") for q in tree_calls} == {"2"}
    assert any(q.get("offset") == "2" for q in tree_calls)
    assert any(q.get("parent_entity_uid") == "g-closed" for q in tree_calls)


async def test_documents_list_is_paged(server: FakeKaiten) -> None:
    for n in range(130):
        server.add_document(f"d-many-{n}", f"Документ {n}", pm("x"), parent="g-hr")
    anna = await listed(make_adapter(server), "documents")
    assert len(anna) == 135
    offsets = [q["offset"] for route, q in server.calls if route == "documents"]
    assert offsets == ["0", "100"]


async def test_document_missing_from_api_list_is_read_by_uid(
    server: FakeKaiten,
) -> None:
    server.unlisted.add("d-root")
    anna = await listed(make_adapter(server), "documents")
    assert anna["doc:d-root"].version == "1:2026-09-01T10:00:00.000Z"
    assert ("documents/d-root", {}) in server.calls


async def test_oversized_tree_stops_instead_of_truncating(
    server: FakeKaiten, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(documents_module, "MAX_ENTITIES", 3)
    with pytest.raises(AdapterError, match="tree_too_large"):
        await listed(make_adapter(server), "documents")


async def test_tree_endpoint_missing_is_a_clear_error(server: FakeKaiten) -> None:
    server.tree_missing = True
    with pytest.raises(AdapterError) as caught:
        await listed(make_adapter(server), "documents")
    assert caught.value.code == "tree_unavailable" and not caught.value.retryable


async def test_fetch_converts_prosemirror_to_markdown(server: FakeKaiten) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter, "documents")
    content = await adapter.fetch(documents["doc:d-vacation"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedMarkdown)
    assert content.markdown == "# Отпуск\n\nОтпуск — 28 календарных дней."


async def test_fetch_limits_and_empty_documents(server: FakeKaiten) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter, "documents")
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(documents["doc:d-vacation"], max_bytes=10)
    server.documents["d-root"]["data"] = {"type": "doc", "content": []}
    with pytest.raises(AdapterError, match="empty_page"):
        await adapter.fetch(documents["doc:d-root"], max_bytes=MAX_BYTES)


async def test_fetch_of_document_closed_since_listing_is_forbidden(
    server: FakeKaiten,
) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter, "documents")
    server.readers["d-root"] = {999}
    with pytest.raises(AdapterError, match="forbidden"):
        await adapter.fetch(documents["doc:d-root"], max_bytes=MAX_BYTES)


# --- вложения карточек ------------------------------------------------------------


async def test_card_files_of_supported_formats(server: FakeKaiten) -> None:
    with counting_skips() as tally:
        anna = await listed(make_adapter(server), "card_files")
    assert set(anna) == {"file:f-guide", "file:7001", "file:f-budget"}
    guide = anna["file:f-guide"]
    assert guide.kind is RemoteDocumentKind.FILE
    assert guide.module == "card_files"
    assert guide.filename == "Гайд.txt"
    assert guide.size == len("Первый день: пропуск.".encode())
    assert guide.path == "Доска 1/Онбординг"
    assert guide.url == f"{server.base}501"
    assert guide.version == f"2026-09-03T10:00:00.000Z:{guide.size}"
    assert tally.formats() == {".png": 1}
    assert tally.too_large == {"f-big"}


async def test_card_files_respect_board_access(server: FakeKaiten) -> None:
    boris = await listed(make_adapter(server, BORIS_TOKEN), "card_files")
    assert set(boris) == {"file:f-guide", "file:7001"}


async def test_card_files_from_listing_skip_card_requests(server: FakeKaiten) -> None:
    server.cards_list_files = True
    anna = await listed(make_adapter(server), "card_files")
    assert "file:f-guide" in anna
    assert not [route for route, _ in server.calls if route.startswith("cards/")]


async def test_spaces_setting_filters_cards(server: FakeKaiten) -> None:
    anna = await listed(make_adapter(server, spaces="2"), "card_files")
    assert set(anna) == {"file:f-budget"}
    assert {q.get("space_id") for r, q in server.calls if r == "cards"} == {"2"}


async def test_restricted_file_is_downloaded_by_signed_link_without_token(
    server: FakeKaiten,
) -> None:
    adapter = make_adapter(server)
    files = await listed(adapter, "card_files")
    content = await adapter.fetch(files["file:f-guide"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedFile)
    assert content.data == "Первый день: пропуск.".encode()
    assert content.filename == "Гайд.txt"
    old = await adapter.fetch(files["file:7001"], max_bytes=MAX_BYTES)
    assert old.data.startswith("# Заметки".encode())  # type: ignore[union-attr]
    # Токен сотрудника не уходит на хост хранилища.
    assert server.storage_auth == ["", ""]


async def test_file_limits_and_malicious_files(server: FakeKaiten) -> None:
    adapter = make_adapter(server)
    files = await listed(adapter, "card_files")
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(files["file:f-guide"], max_bytes=5)
    server.malicious.add("f-guide")
    with pytest.raises(AdapterError, match="file_rejected"):
        await adapter.fetch(files["file:f-guide"], max_bytes=MAX_BYTES)


async def test_unknown_document_is_refused(server: FakeKaiten) -> None:
    stray = RemoteDocument(
        external_id="other:1",
        title="x",
        url="",
        version="",
        kind=RemoteDocumentKind.FILE,
        module="documents",
    )
    with pytest.raises(AdapterError, match="unknown_document"):
        await make_adapter(server).fetch(stray, max_bytes=MAX_BYTES)
