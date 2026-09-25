"""Политики ограничения частоты для ручек API.

Числа — стартовые, подбираются по логам `rate_limited`. Главное в них
не точность, а порядок: человек не входит 30 раз за 15 минут и не
задаёт 30 вопросов в минуту, скрипт — легко.

Fail-closed или fail-open, когда Redis недоступен:
- вход, обновление токена, смена пароля — fail-closed (503): без
  лимита пароль перебирается, и лучше временно не пустить никого,
  чем пустить перебор;
- вопросы, поиск, загрузка — fail-open с ошибкой в логе: их стоимость
  ограничена ещё и пулом кредитов компании, а отказ в ответах всем
  сотрудникам из-за упавшего счётчика — непропорционально.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, Request

from corp_ed.api.v1.dependencies import get_current_user
from corp_ed.core.exceptions import ServiceUnavailableError
from corp_ed.core.rate_limit import (
    RateLimitedError,
    RateLimiter,
    RateLimiterUnavailableError,
)
from corp_ed.domain.models import User

logger = structlog.get_logger()


@dataclass(frozen=True)
class RatePolicy:
    name: str
    limit: int
    window: int
    """Длина окна в секундах."""
    fail_open: bool


LOGIN_PER_IP = RatePolicy("login-ip", limit=30, window=900, fail_open=False)
# Считаются только НЕУДАЧНЫЕ попытки на пару «компания + почта», в том
# числе несуществующую: блокировка есть для любого адреса, поэтому по
# ней нельзя узнать, существует ли учётка.
LOGIN_FAILURES_PER_ACCOUNT = RatePolicy(
    "login-account", limit=10, window=900, fail_open=False
)
REFRESH_PER_IP = RatePolicy("refresh-ip", limit=60, window=60, fail_open=False)
PASSWORD_CHANGE_PER_USER = RatePolicy(
    "password-user", limit=5, window=900, fail_open=False
)
FAQ_PER_USER = RatePolicy("faq-user", limit=30, window=60, fail_open=True)
SEARCH_PER_USER = RatePolicy("search-user", limit=60, window=60, fail_open=True)
UPLOAD_PER_TENANT = RatePolicy("upload-tenant", limit=60, window=3600, fail_open=True)
# Создание и переиндексация материалов: каждый запрос — пачка платных
# эмбеддингов в воркере.
INGEST_PER_TENANT = RatePolicy("ingest-tenant", limit=120, window=3600, fail_open=True)
# Правки словаря — руками админа; сотни в час — уже скрипт.
GLOSSARY_PER_TENANT = RatePolicy(
    "glossary-tenant", limit=300, window=3600, fail_open=True
)
# Коннекторы: настройка — руками админа; «синхронизировать сейчас» и
# проверка учётных данных — запросы к системе клиента, их темп держим
# ниже её лимитов API.
CONNECTOR_WRITE_PER_TENANT = RatePolicy(
    "connector-tenant", limit=120, window=3600, fail_open=True
)
CONNECTOR_SYNC_PER_TENANT = RatePolicy(
    "connector-sync-tenant", limit=12, window=3600, fail_open=True
)
CONNECTOR_TEST_PER_TENANT = RatePolicy(
    "connector-test-tenant", limit=30, window=3600, fail_open=True
)
CONNECTOR_GRANT_PER_USER = RatePolicy(
    "connector-grant-user", limit=20, window=3600, fail_open=True
)
# Обратный вызов OAuth приходит без нашего токена — лимит по IP, как у
# входа: подбор state или кода не должен быть бесплатным.
CONNECTOR_OAUTH_CALLBACK_PER_IP = RatePolicy(
    "connector-oauth-ip", limit=30, window=900, fail_open=False
)


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.rate_limiter
    return limiter


def client_ip(request: Request) -> str:
    """IP клиента.

    X-Forwarded-For здесь не читается: подделать его может любой. За
    reverse proxy адрес подставляет uvicorn (--proxy-headers и
    --forwarded-allow-ips с адресом прокси), и тогда request.client —
    уже настоящий клиент.
    """
    return request.client.host if request.client else "unknown"


async def enforce(limiter: RateLimiter, policy: RatePolicy, subject: str) -> None:
    try:
        decision = await limiter.hit(
            f"{policy.name}:{subject}", limit=policy.limit, window=policy.window
        )
    except RateLimiterUnavailableError:
        logger.error("rate_limiter_unavailable", policy=policy.name)
        if policy.fail_open:
            return
        raise ServiceUnavailableError() from None

    if not decision.allowed:
        logger.warning("rate_limited", policy=policy.name, count=decision.count)
        raise RateLimitedError(decision.retry_after)


async def ensure_not_locked(
    limiter: RateLimiter, policy: RatePolicy, subject: str
) -> None:
    """Отказать заранее, если неудачных попыток уже слишком много."""
    try:
        decision = await limiter.peek(f"{policy.name}:{subject}", limit=policy.limit)
    except RateLimiterUnavailableError:
        logger.error("rate_limiter_unavailable", policy=policy.name)
        if policy.fail_open:
            return
        raise ServiceUnavailableError() from None

    if not decision.allowed:
        logger.warning("account_locked", policy=policy.name)
        raise RateLimitedError(decision.retry_after)


async def record(limiter: RateLimiter, policy: RatePolicy, subject: str) -> None:
    """Засчитать событие, не отказывая сейчас.

    Для неудачного входа: ответ на эту попытку уже решён (401), и 429
    отсюда сделал бы его отличимым от остальных. Блокировку проверит
    ensure_not_locked на следующей попытке.
    """
    try:
        await limiter.hit(
            f"{policy.name}:{subject}", limit=policy.limit, window=policy.window
        )
    except RateLimiterUnavailableError:
        logger.error("rate_limiter_unavailable", policy=policy.name)


async def forget(limiter: RateLimiter, policy: RatePolicy, subject: str) -> None:
    try:
        await limiter.reset(f"{policy.name}:{subject}")
    except RateLimiterUnavailableError:
        logger.error("rate_limiter_unavailable", policy=policy.name)


def limit_by_ip(policy: RatePolicy) -> Callable[..., Awaitable[None]]:
    async def dependency(
        request: Request,
        limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    ) -> None:
        await enforce(limiter, policy, client_ip(request))

    return dependency


def limit_by_user(policy: RatePolicy) -> Callable[..., Awaitable[None]]:
    async def dependency(
        user: Annotated[User, Depends(get_current_user)],
        limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    ) -> None:
        await enforce(limiter, policy, str(user.id))

    return dependency


def limit_by_tenant(policy: RatePolicy) -> Callable[..., Awaitable[None]]:
    async def dependency(
        user: Annotated[User, Depends(get_current_user)],
        limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    ) -> None:
        await enforce(limiter, policy, str(user.tenant_id))

    return dependency
