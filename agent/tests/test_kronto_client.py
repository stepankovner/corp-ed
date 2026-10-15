"""Клиент протокола агент → kronto на поддельном сервере: повторы,
Retry-After, 401/403, страницы /state, пачки /access, 413/415."""

from datetime import UTC, datetime

import httpx
import pytest

from kronto_agent import kronto as kronto_mod
from kronto_agent.kronto import (
    KrontoAuthError,
    KrontoClient,
    KrontoForbiddenError,
    KrontoUnavailableError,
    retry_after_seconds,
)
from tests.fake_kronto import KEY, FakeKronto


def client(
    fake: FakeKronto, sleeps: list[float] | None = None, key: str = KEY
) -> KrontoClient:
    record = sleeps if sleeps is not None else []
    return KrontoClient(
        "https://kronto.test/", key, transport=fake.transport(), sleep=record.append
    )


def test_retry_after_parsing() -> None:
    now = datetime(2026, 10, 15, 9, 0, 0, tzinfo=UTC)
    assert retry_after_seconds("7") == 7.0
    assert retry_after_seconds("Thu, 15 Oct 2026 09:00:30 GMT", now) == 30.0
    assert retry_after_seconds("Thu, 15 Oct 2026 08:00:00 GMT", now) == 0.0
    assert retry_after_seconds("100000") == kronto_mod.MAX_RETRY_AFTER
    assert retry_after_seconds("soon") is None
    assert retry_after_seconds(None) is None


def test_429_waits_retry_after_then_succeeds() -> None:
    fake = FakeKronto(fail_next=[httpx.Response(429, headers={"Retry-After": "12"})])
    sleeps: list[float] = []
    run_id = client(fake, sleeps).start_run()
    assert run_id == fake.open_run
    assert sleeps == [12.0]


def test_5xx_and_network_errors_back_off() -> None:
    calls = {"n": 0}
    fake = FakeKronto()

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused", request=request)
        if calls["n"] in (2, 3):
            return httpx.Response(503)
        return fake.handle(request)

    sleeps: list[float] = []
    c = KrontoClient(
        "https://kronto.test",
        KEY,
        transport=httpx.MockTransport(flaky),
        sleep=sleeps.append,
    )
    assert c.state() == {}
    assert sleeps == [1.0, 2.0, 4.0]


def test_retries_exhausted() -> None:
    fake = FakeKronto(fail_next=[httpx.Response(502) for _ in range(10)])
    sleeps: list[float] = []
    with pytest.raises(KrontoUnavailableError, match="502"):
        client(fake, sleeps).start_run()
    assert len(sleeps) == 5  # 6 попыток


def test_auth_and_forbidden_are_not_retried() -> None:
    fake = FakeKronto()
    sleeps: list[float] = []
    with pytest.raises(KrontoAuthError):
        client(fake, sleeps, key="kag_wrong").state()
    fake.forbidden = True
    with pytest.raises(KrontoForbiddenError):
        client(fake, sleeps).start_run()
    assert sleeps == []
    assert len(fake.calls) == 2


def test_state_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kronto_mod, "STATE_PAGE", 2)
    fake = FakeKronto()
    for i in range(5):
        fake.seed(f"\\\\fs\\d\\{i}.txt", f"v{i}", [])
    state = client(fake).state()
    assert state == {f"\\\\fs\\d\\{i}.txt": f"v{i}" for i in range(5)}
    assert [c for c in fake.calls if c[1].endswith("/state")] == [
        ("GET", "/api/v1/agent/state")
    ] * 3


def test_access_batches_of_500() -> None:
    fake = FakeKronto()
    for i in range(1200):
        fake.seed(f"doc{i}", "v", [])
    c = client(fake)
    run_id = c.start_run()
    docs = [{"external_id": f"doc{i}", "allowed_emails": ["a@x"]} for i in range(1203)]
    unknown = c.access(run_id, docs)
    assert unknown == ["doc1200", "doc1201", "doc1202"]
    assert [len(batch) for batch in fake.access_calls] == [500, 500, 203]


@pytest.mark.parametrize(
    ("code", "status"), [(413, "too_large"), (415, "unsupported"), (422, "rejected")]
)
def test_put_rejections(code: int, status: str) -> None:
    fake = FakeKronto(reject_files={"a.docx": code})
    c = client(fake)
    run_id = c.start_run()
    meta = {
        "run_id": run_id,
        "external_id": "x",
        "version": "1-1",
        "title": "a",
        "path": "\\\\fs\\d\\a.docx",
        "filename": "a.docx",
        "modified_at": "2026-10-15T09:00:00Z",
        "allowed_emails": ["a@x"],
    }
    assert c.put_document(meta, "a.docx", b"1").status == status


def test_put_multipart_with_cyrillic_name() -> None:
    fake = FakeKronto()
    c = client(fake)
    run_id = c.start_run()
    meta = {
        "run_id": run_id,
        "external_id": "\\\\fs\\d\\положение.docx",
        "version": "1-3",
        "title": "Положение",
        "path": "\\\\fs\\d\\Положение.docx",
        "filename": "Положение.docx",
        "modified_at": "2026-10-15T09:00:00Z",
        "allowed_emails": ["a@x"],
    }
    result = c.put_document(meta, "Положение.docx", b"abc")
    assert result.status == "added"
    doc = fake.docs["\\\\fs\\d\\положение.docx"]
    assert doc.content == b"abc"
    assert doc.meta["title"] == "Положение"
    request_auth = fake.calls[-1]
    assert request_auth == ("PUT", "/api/v1/agent/documents")
