"""Контракт с настоящим Outline: адаптер на ответах, записанных
`cli connector-check --record` со стенда Outline 1.10.1
(fixtures/outline/live, tests/live/outline/README.md).

Поддельный сервер (fake_outline.py) написан по OpenAPI и исходникам; эти
ответы — то, что сервер отдал на самом деле. Расхождение с ними поймали
бы раньше: группы коллекции Outline 1.10 отдаёт в `groupMemberships`, а
не в `collectionGroupMemberships` (стенд 09.10).
"""

import json
from pathlib import Path
from typing import Any

import httpx

from corp_ed.connectors.base import RemoteDocument
from corp_ed.connectors.outline.adapter import OutlineAdapter
from corp_ed.connectors.outline.client import OutlineClient
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import MaterialVisibility
from tests.fake_connector import public_resolver

FIXTURES = Path(__file__).parent / "fixtures" / "outline" / "live"
BASE = "https://wiki.example.com/"
EMAIL = "{}@example.com"

EXPECTED: dict[str, tuple[str, set[str] | None]] = {
    "Отпуск": ("Общая", None),
    "Заявление": ("Общая/Отпуск", None),
    "Зарплаты": ("Кадры", {"admin", "maria", "petr"}),
    "План": ("Проекты", {"admin", "ivan"}),
    "Бюджет": ("Проекты/План", {"admin", "ivan", "maria"}),
}
"""Как засеян стенд (tests/live/outline/seed.py): «Общая» открыта рабочей
области, «Кадры» — группе hr (maria, petr), «Проекты» — ivan, у «Бюджета»
ещё и участник документа maria; admin — создатель коллекций."""


def _recorded() -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURES.glob("*.json"))
    ]


def _replay() -> OutboundClient:
    """Ответ на вызов — записанный ответ того же метода с тем же id и offset."""
    recorded = _recorded()

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.removeprefix("/api/")
        body = json.loads(request.content or b"{}")
        for entry in recorded:
            params = entry["params"]
            if (
                entry["method"] == method
                and params.get("id") == body.get("id")
                and params.get("offset", 0) == body.get("offset", 0)
            ):
                return httpx.Response(200, json=entry["response"])
        return httpx.Response(404, json={"ok": False, "error": "not_found"})

    return OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        resolver=public_resolver,
    )


async def _listed() -> dict[str, RemoteDocument]:
    adapter = OutlineAdapter(
        OutlineClient(_replay(), base_url=BASE, token="recorded"),
        document_memberships=True,
        max_bytes=1024 * 1024,
    )
    await adapter.check()
    return {d.title: d async for d in adapter.list(["documents"])}


def test_recordings_hold_no_secrets() -> None:
    dumped = "\n".join(
        path.read_text(encoding="utf-8") for path in FIXTURES.glob("*.json")
    )
    assert "ol_api_" not in dumped
    assert "Bearer" not in dumped


async def test_real_responses_give_the_same_rights_as_outline() -> None:
    documents = await _listed()

    assert set(documents) == set(EXPECTED)
    for title, (path, readers) in EXPECTED.items():
        document = documents[title]
        assert document.path == path, title
        if readers is None:
            assert document.visibility is MaterialVisibility.TENANT, title
            assert document.allowed_emails == frozenset(), title
        else:
            assert document.visibility is MaterialVisibility.RESTRICTED, title
            assert document.allowed_emails == {EMAIL.format(u) for u in readers}, title
