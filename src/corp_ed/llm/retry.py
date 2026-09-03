import asyncio
import random
from collections.abc import Awaitable, Callable

import httpx
import structlog

from corp_ed.llm.errors import LLMError, classify

logger = structlog.get_logger()


async def call_with_retry(
    do_request: Callable[[], Awaitable[httpx.Response]],
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> httpx.Response:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    last_error: LLMError | None = None

    for attempt in range(max_attempts):
        try:
            response = await do_request()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = LLMError(f"transport: {exc}", retryable=True)
        else:
            error = classify(response)
            if error is None:
                return response
            last_error = error

        if not last_error.retryable:
            raise last_error

        if attempt < max_attempts - 1:
            delay = base_delay * (2**attempt) + random.uniform(0, 1)
            logger.warning(
                "llm_retry",
                attempt=attempt + 1,
                max_attempts=max_attempts,
                delay=round(delay, 2),
                error=str(last_error),
            )
            await asyncio.sleep(delay)

    if last_error is None:
        raise RuntimeError("unreachable: loop must set last_error")

    logger.error(
        "llm_call_failed",
        attempts=max_attempts,
        error=str(last_error),
    )
    raise last_error
