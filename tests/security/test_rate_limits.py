"""Ограничение частоты: перебор паролей, расход LLM, отказ Redis."""

import os
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis

from corp_ed.api.v1.rate_limits import (
    FAQ_PER_USER,
    LOGIN_FAILURES_PER_ACCOUNT,
    LOGIN_PER_IP,
    REFRESH_PER_IP,
)
from corp_ed.core.rate_limit import (
    InMemoryRateLimiter,
    RateDecision,
    RateLimiter,
    RateLimiterUnavailableError,
    RedisRateLimiter,
)
from corp_ed.domain.models import User
from corp_ed.main import app
from tests.api.conftest import PASSWORD, bearer, login


class DownLimiter(RateLimiter):
    """Redis недоступен."""

    async def hit(self, key: str, *, limit: int, window: int) -> RateDecision:
        raise RateLimiterUnavailableError("connection refused")

    async def peek(self, key: str, *, limit: int) -> RateDecision:
        raise RateLimiterUnavailableError("connection refused")

    async def reset(self, key: str) -> None:
        raise RateLimiterUnavailableError("connection refused")


async def _fail(api: httpx.AsyncClient, email: str, times: int) -> None:
    for _ in range(times):
        response = await login(api, email, "wrong-password-123")
        assert response.status_code == 401


async def test_account_locks_after_repeated_failures(
    api: httpx.AsyncClient, account: User
) -> None:
    await _fail(api, account.email, LOGIN_FAILURES_PER_ACCOUNT.limit)

    # Теперь и верный пароль не пускает — окно блокировки.
    response = await login(api, account.email, PASSWORD)
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


async def test_lock_applies_to_nonexistent_accounts_too(
    api: httpx.AsyncClient, account: User
) -> None:
    """Иначе по 429 можно было бы узнать, что учётка существует."""
    await _fail(api, "ghost@test.com", LOGIN_FAILURES_PER_ACCOUNT.limit)

    assert (await login(api, "ghost@test.com")).status_code == 429


async def test_lock_is_per_account(api: httpx.AsyncClient, account: User) -> None:
    await _fail(api, "ghost@test.com", LOGIN_FAILURES_PER_ACCOUNT.limit)

    assert (await login(api, account.email)).status_code == 200


async def test_success_resets_failure_counter(
    api: httpx.AsyncClient, account: User
) -> None:
    await _fail(api, account.email, LOGIN_FAILURES_PER_ACCOUNT.limit - 1)
    assert (await login(api, account.email)).status_code == 200

    await _fail(api, account.email, LOGIN_FAILURES_PER_ACCOUNT.limit - 1)
    assert (await login(api, account.email)).status_code == 200


async def test_ip_limit_stops_spraying_many_accounts(
    api: httpx.AsyncClient, account: User
) -> None:
    """Перебор «один пароль по многим адресам» с одного IP."""
    for number in range(LOGIN_PER_IP.limit):
        await login(api, f"user{number}@test.com", "Summer2026!!")

    response = await login(api, account.email)
    assert response.status_code == 429


async def test_refresh_is_limited_per_ip(api: httpx.AsyncClient) -> None:
    for _ in range(REFRESH_PER_IP.limit):
        await api.post("/api/v1/auth/refresh", json={"refresh_token": "x"})

    response = await api.post("/api/v1/auth/refresh", json={"refresh_token": "x"})
    assert response.status_code == 429


async def test_faq_is_limited_per_user(api: httpx.AsyncClient, account: User) -> None:
    headers = bearer(account)
    for _ in range(FAQ_PER_USER.limit):
        ok = await api.post(
            "/api/v1/faq/ask", json={"question": "Вопрос"}, headers=headers
        )
        assert ok.status_code == 200

    response = await api.post(
        "/api/v1/faq/ask", json={"question": "Вопрос"}, headers=headers
    )
    assert response.status_code == 429


async def test_login_fails_closed_when_limiter_is_down(
    api: httpx.AsyncClient, account: User
) -> None:
    """Без счётчиков пароль перебирается — лучше 503, чем открытый перебор."""
    app.state.rate_limiter = DownLimiter()

    response = await login(api, account.email)
    assert response.status_code == 503


async def test_faq_fails_open_when_limiter_is_down(
    api: httpx.AsyncClient, account: User
) -> None:
    app.state.rate_limiter = DownLimiter()

    response = await api.post(
        "/api/v1/faq/ask", json={"question": "Вопрос"}, headers=bearer(account)
    )
    assert response.status_code == 200


# --- реализации -------------------------------------------------------------


async def test_in_memory_window_counts_and_resets() -> None:
    limiter = InMemoryRateLimiter()

    first = await limiter.hit("k", limit=2, window=60)
    second = await limiter.hit("k", limit=2, window=60)
    third = await limiter.hit("k", limit=2, window=60)

    assert (first.allowed, second.allowed, third.allowed) == (True, True, False)
    assert (await limiter.peek("k", limit=2)).allowed is False

    await limiter.reset("k")
    assert (await limiter.peek("k", limit=2)).allowed is True


REDIS_URL = os.environ.get("TEST_REDIS_URL")


@pytest.mark.skipif(REDIS_URL is None, reason="TEST_REDIS_URL не задан (в CI задан)")
async def test_redis_limiter_counts_expires_and_resets() -> None:
    assert REDIS_URL is not None
    redis = Redis.from_url(REDIS_URL)
    limiter = RedisRateLimiter(redis)
    key = f"test:{uuid4()}"
    try:
        decisions = [await limiter.hit(key, limit=2, window=30) for _ in range(3)]
        assert [d.allowed for d in decisions] == [True, True, False]
        assert 0 < decisions[-1].retry_after <= 30

        peek = await limiter.peek(key, limit=2)
        assert peek.allowed is False and peek.count == 3

        await limiter.reset(key)
        assert (await limiter.peek(key, limit=2)).count == 0
    finally:
        await redis.aclose()


async def test_redis_limiter_reports_unavailability() -> None:
    redis = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    limiter = RedisRateLimiter(redis)
    try:
        with pytest.raises(RateLimiterUnavailableError):
            await limiter.hit("k", limit=1, window=1)
    finally:
        await redis.aclose()
