import asyncio

import httpx
import pytest

from corp_ed.llm.errors import LLMError
from corp_ed.llm.retry import call_with_retry


class FakeRequester:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def __call__(self) -> httpx.Response:
        result = self.responses[self.calls]
        self.calls += 1
        return result


async def test_success_with_first_try() -> None:
    fake = FakeRequester(httpx.Response(200))

    result = await call_with_retry(fake, max_attempts=3)

    assert result.status_code == 200
    assert fake.calls == 1


async def test_success_with_second_try(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(_: float) -> None:
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    fake = FakeRequester(httpx.Response(500), httpx.Response(200))

    result = await call_with_retry(fake, max_attempts=3)

    assert result.status_code == 200
    assert fake.calls == 2


async def test_not_retryable_error() -> None:
    fake = FakeRequester(httpx.Response(400))

    with pytest.raises(LLMError):
        await call_with_retry(fake, max_attempts=3)

    assert fake.calls == 1


async def test_attempts_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    fake = FakeRequester(
        httpx.Response(500),
        httpx.Response(500),
        httpx.Response(500),
    )

    with pytest.raises(LLMError):
        await call_with_retry(fake, max_attempts=3)

    assert fake.calls == 3
    assert len(sleeps) == 2
    assert sleeps[0] < sleeps[1]


async def test_invalid_max_attempts() -> None:
    fake = FakeRequester(httpx.Response(200))

    with pytest.raises(ValueError):
        await call_with_retry(fake, max_attempts=0)

    assert fake.calls == 0


class FailingRequester:
    def __init__(self, failures: int, final: httpx.Response) -> None:
        self.failures = failures
        self.final = final
        self.calls = 0

    async def __call__(self) -> httpx.Response:
        self.calls += 1
        if self.calls <= self.failures:
            raise httpx.ConnectError("connection refused")
        return self.final


async def test_transport_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(_: float) -> None:
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    fake = FailingRequester(1, httpx.Response(200))

    result = await call_with_retry(fake, max_attempts=3)

    assert result.status_code == 200
    assert fake.calls == 2
