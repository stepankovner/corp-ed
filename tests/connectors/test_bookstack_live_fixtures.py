"""Контракт с настоящим BookStack: адаптер на ответах, записанных
`cli connector-check --record` со стенда BookStack v26.09.1
(fixtures/bookstack/live, tests/live/bookstack/README.md).

Поддельный сервер (fake_bookstack.py) написан по документации API и
исходникам; эти ответы — то, что сервер отдал на самом деле (гость в
списке пользователей, общая последовательность id книг, глав и страниц,
`fallback_permissions` с null при наследовании).
"""

import json
from pathlib import Path
from typing import Any

import httpx

from corp_ed.connectors.base import FetchedFile, FetchedPage, RemoteDocument
from corp_ed.connectors.bookstack.adapter import BookStackAdapter
from corp_ed.connectors.bookstack.client import BookStackClient
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import MaterialVisibility
from tests.fake_connector import public_resolver

FIXTURES = Path(__file__).parent / "fixtures" / "bookstack" / "live"
BASE = "https://wiki.example.com/"

EXPECTED: dict[str, tuple[str, set[str] | None]] = {
    "Отпуск": ("Общая", None),
    "Суточные": ("Общая/Командировки", None),
    "Премии": ("Общая", {"admin", "maria"}),
    "Зарплаты": ("Кадры", {"admin", "maria"}),
    "Оргструктура": ("Кадры", None),
    "Увольнения": ("Кадры", {"admin", "maria"}),
    "План": ("Проекты", {"admin", "ivan"}),
    "Заметки автора": ("Проекты", {"admin", "ivan", "olga"}),
    "Смета": ("Проекты", {"admin"}),
    "памятка.txt": ("Общая/Отпуск", None),
    "ведомость.txt": ("Кадры/Зарплаты", {"admin", "maria"}),
}
"""Как засеян стенд (tests/live/bookstack/seed.py) и что BookStack
отдавал каждому по его токену (test_bookstack_live.py)."""


def _recorded() -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURES.glob("*.json"))
    ]


def _replay() -> OutboundClient:
    """Ответ на вызов — записанный ответ того же пути с тем же count и
    offset."""
    recorded = _recorded()

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/")
        query = dict(request.url.params)
        for entry in recorded:
            params = {k: str(v) for k, v in entry["params"].items()}
            if (
                entry["method"] == path
                and params.get("count") == query.get("count")
                and params.get("offset") == query.get("offset")
            ):
                return httpx.Response(200, json=entry["response"])
        return httpx.Response(404, json={"error": {"code": 404, "message": "x"}})

    return OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        resolver=public_resolver,
    )


def _adapter() -> BookStackAdapter:
    return BookStackAdapter(
        BookStackClient(
            _replay(), base_url=BASE, token_id="recorded", token_secret="recorded"
        ),
        max_bytes=1024 * 1024,
    )


async def _listed(adapter: BookStackAdapter) -> dict[str, RemoteDocument]:
    await adapter.check()
    return {d.title: d async for d in adapter.list(["pages", "attachments"])}


def test_recordings_hold_no_secrets() -> None:
    dumped = "\n".join(p.read_text(encoding="utf-8") for p in FIXTURES.glob("*.json"))
    assert "Token " not in dumped
    assert '"token' not in dumped.lower()
    # Файл вложения — base64 внутри JSON: в записи только начало.
    assert all(len(json.dumps(entry)) < 20_000 for entry in _recorded())


async def test_recorded_rights_are_mirrored() -> None:
    documents = await _listed(_adapter())
    assert set(documents) == set(EXPECTED)
    for title, (path, allowed) in EXPECTED.items():
        document = documents[title]
        assert document.path == path, title
        if allowed is None:
            assert document.visibility is MaterialVisibility.TENANT, title
        else:
            assert document.visibility is MaterialVisibility.RESTRICTED, title
            assert document.allowed_emails == {
                f"{login}@example.com" for login in allowed
            }, title


async def test_recorded_content_is_read() -> None:
    adapter = _adapter()
    documents = await _listed(adapter)
    page = await adapter.fetch(documents["Отпуск"], max_bytes=1024 * 1024)
    assert isinstance(page, FetchedPage)
    assert "Отпуск — 28 календарных дней." in page.html
    memo = await adapter.fetch(documents["памятка.txt"], max_bytes=1024 * 1024)
    assert isinstance(memo, FetchedFile)
    assert memo.data.decode() == "Памятка: заявление за две недели."
