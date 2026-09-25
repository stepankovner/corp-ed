"""База знаний 2.0 Битрикс24 (note.*, REST 3.0): адреса вызовов, ошибки
нового формата, обход дерева, Markdown как есть, контракт по образцам."""

import json
from pathlib import Path
from typing import Any

import pytest

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    FetchedMarkdown,
)
from corp_ed.connectors.bitrix24 import SPEC
from corp_ed.connectors.bitrix24.notes import NotesModule
from corp_ed.ingest.extract import ExtractionError
from corp_ed.services.connector_sync_service import markdown_as_is
from tests.connectors.fake_portal import (
    ADMIN_ID,
    EMPLOYEE_ID,
    FakePortal,
    sample_portal,
)
from tests.connectors.test_bitrix24_adapter import listed, make_adapter
from tests.connectors.test_bitrix24_client import make_client

FIXTURES = Path(__file__).parent / "fixtures" / "bitrix24"
MODULE = "knowledge_base_v2"


@pytest.fixture
def portal() -> FakePortal:
    return sample_portal()


def test_module_is_in_the_catalog() -> None:
    assert MODULE in SPEC.module_names
    assert "note" in SPEC.extra["app_scopes_optional"]


# --- клиент: REST 3.0 --------------------------------------------------------


async def test_v3_webhook_and_oauth_paths(portal: FakePortal) -> None:
    await make_client(portal).call("note.collection.list", v3=True)
    await make_client(portal, auth="oauth").call("note.collection.list", v3=True)
    assert portal.paths == [
        f"/rest/api/{ADMIN_ID}/wh-code/note.collection.list",
        "/rest/api/note.collection.list",
    ]
    assert portal.auth_log[-1] == ("oauth", "tok-7")
    assert portal.calls[-1] == ("note.collection.list", {})


@pytest.mark.parametrize(
    ("status", "v3_code", "kind", "code"),
    [
        (403, "INSUFFICIENTSCOPEEXCEPTION", AdapterConfigError, "insufficient_scope"),
        (403, "ACCESSDENIEDEXCEPTION", AdapterError, "access_denied"),
        (400, "ENTITYNOTFOUNDEXCEPTION", AdapterError, "error_not_found"),
        (400, "VALIDATION_REQUESTVALIDATIONEXCEPTION", AdapterError, "error_argument"),
        (400, "SOMETHINGNEWEXCEPTION", AdapterError, "v3_somethingnewexception"),
    ],
)
async def test_v3_error_objects_are_mapped(
    portal: FakePortal, status: int, v3_code: str, kind: type[AdapterError], code: str
) -> None:
    portal.canned["note.document.get"] = (
        status,
        {"error": {"code": f"BITRIX_REST_V3_EXCEPTION_{v3_code}", "message": "x"}},
    )
    with pytest.raises(kind) as excinfo:
        await make_client(portal).call("note.document.get", {"id": 1}, v3=True)
    assert type(excinfo.value) is kind
    assert excinfo.value.code == code
    assert not excinfo.value.retryable


async def test_v3_auth_errors_still_refresh(portal: FakePortal) -> None:
    portal.expired.add("tok-7")
    client = make_client(portal, auth="oauth")
    data = await client.call("note.collection.list", v3=True)
    assert "items" in data["result"]
    assert client.refreshed_credentials is not None


async def test_v3_dead_token_is_auth_error(portal: FakePortal) -> None:
    portal.access_tokens.clear()
    with pytest.raises(AdapterAuthError, match="invalid_token"):
        await make_client(portal, auth="oauth").call("note.collection.list", v3=True)


# --- обход --------------------------------------------------------------------


async def test_walk_lists_documents_depth_first_with_paths(portal: FakePortal) -> None:
    documents = await listed(make_adapter(portal), MODULE)
    assert list(documents) == ["note:10", "note:11", "note:12", "note:20"]
    intro = documents["note:10"]
    assert intro.title == "Введение"
    assert intro.module == MODULE
    assert intro.path == "Продуктовая документация"
    assert intro.version == "2026-04-21T09:15:30Z"
    assert intro.url == f"{portal.portal}knowledge/"
    assert intro.size == len("# Введение\n\nКак устроен продукт.".encode())
    chapter = documents["note:11"]
    assert chapter.path == "Продуктовая документация/Введение"
    assert chapter.version == "2026-05-01T10:00:00Z"
    assert documents["note:20"].path == "Регламенты 2.0"
    methods = [m for m, _ in portal.calls]
    assert methods.count("note.collection.list") == 1
    assert methods.count("note.document.tree.list") == 2
    assert methods.count("note.document.get") == 4


async def test_fetch_uses_content_read_during_walk(portal: FakePortal) -> None:
    adapter = make_adapter(portal)
    documents = await listed(adapter, MODULE)
    before = len(portal.calls)
    content = await adapter.fetch(documents["note:11"], max_bytes=1024)
    assert isinstance(content, FetchedMarkdown)
    assert content.markdown == "# Глава 1\n\nПервые шаги."
    assert len(portal.calls) == before


async def test_fetch_without_walk_reads_document(portal: FakePortal) -> None:
    adapter = make_adapter(portal)
    documents = await listed(make_adapter(portal), MODULE)
    content = await adapter.fetch(documents["note:20"], max_bytes=1024)
    assert isinstance(content, FetchedMarkdown)
    assert "28 дней" in content.markdown
    assert portal.calls[-1] == ("note.document.get", {"id": 20})


async def test_empty_document_is_error(portal: FakePortal) -> None:
    adapter = make_adapter(portal)
    documents = await listed(adapter, MODULE)
    with pytest.raises(AdapterError, match="empty_page"):
        await adapter.fetch(documents["note:12"], max_bytes=1024)


async def test_collections_are_paged_by_cursor(portal: FakePortal) -> None:
    for index in range(3, 6):
        portal.add_collection(index, f"База {index}")
    portal.canned.clear()
    module = NotesModule(make_client(portal))
    names = (
        [c["name"] async for c in module._collections()]
        if hasattr(module, "_collections")
        else []
    )
    assert len(names) == 5
    # Страница — 50; чтобы увидеть курсор, режем лимит через поддельный портал.
    portal.calls.clear()
    small = FakePortal()
    for index in range(1, 4):
        small.add_collection(index, f"База {index}")
    small_module = NotesModule(make_client(small))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("corp_ed.connectors.bitrix24.notes.PAGE_LIMIT", 2)
        listed_names = [c["name"] async for c in small_module._collections()]
    assert listed_names == ["База 1", "База 2", "База 3"]
    cursors = [body["pagination"].get("afterCursor") for _, body in small.calls]
    assert cursors == [None, {"position": 200, "id": 2}]


async def test_missing_note_scope_is_config_error(portal: FakePortal) -> None:
    portal.note_scope = False
    with pytest.raises(AdapterConfigError, match="insufficient_scope"):
        await listed(make_adapter(portal), MODULE)


async def test_document_closed_between_tree_and_read_is_skipped(
    portal: FakePortal,
) -> None:
    portal.note_denied.add("11")
    documents = await listed(make_adapter(portal), MODULE)
    assert "note:11" not in documents
    assert {"note:10", "note:12", "note:20"} <= set(documents)


async def test_truncated_tree_still_lists(portal: FakePortal) -> None:
    portal.note_truncated = True
    documents = await listed(make_adapter(portal), MODULE)
    assert "note:10" in documents


async def test_notes_combine_with_other_modules(portal: FakePortal) -> None:
    documents = await listed(make_adapter(portal), "disk", "knowledge_base", MODULE)
    assert {"disk:102", "kb:KNOWLEDGE:985", "note:10"} <= set(documents)


async def test_webhook_sees_notes_too(portal: FakePortal) -> None:
    documents = await listed(make_adapter(portal, auth="webhook"), MODULE)
    assert "note:20" in documents
    assert portal.auth_log[0] == ("webhook", ADMIN_ID)
    assert EMPLOYEE_ID not in {u for _, u in portal.auth_log}


# --- Markdown как есть -------------------------------------------------------------


def test_markdown_as_is_limits() -> None:
    assert markdown_as_is("  # Заголовок\n\nтекст  ") == "# Заголовок\n\nтекст"
    with pytest.raises(ExtractionError, match="no_text"):
        markdown_as_is(" \n---\n ")
    with pytest.raises(ExtractionError, match="document_too_large"):
        markdown_as_is("a" * 2_000_001)


# --- контракт по образцам документации ---------------------------------------


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))[
        "response"
    ]


async def test_documented_note_responses_parse(portal: FakePortal) -> None:
    portal.canned["note.collection.list"] = [
        fixture("note.collection.list"),
        {"result": {"items": [], "nextCursor": None}},
    ]
    portal.canned["note.document.tree.list"] = fixture("note.document.tree.list")
    portal.canned["note.document.get"] = fixture("note.document.get")
    adapter = make_adapter(portal, auth="webhook")
    documents = await listed(adapter, MODULE)
    # В образце дерева два узла; get отдаёт один и тот же документ 42.
    assert list(documents) == ["note:10", "note:11"]
    assert documents["note:10"].path == "Продуктовая документация"
    assert documents["note:11"].path == "Продуктовая документация/Введение"
    assert documents["note:10"].version == "2026-04-21T09:15:30Z"
    content = await adapter.fetch(documents["note:10"], max_bytes=1024)
    assert isinstance(content, FetchedMarkdown)
    assert markdown_as_is(content.markdown) == "# Глава 1\n\nТекст в Markdown..."
    second_page = [b for m, b in portal.calls if m == "note.collection.list"][1]
    assert second_page["pagination"]["afterCursor"] == {"position": 100, "id": 123}
