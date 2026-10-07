"""Ошибка модели в логе — без текста от провайдера.

Текст LLMError собирается из тела ответа провайдера (llm/errors.py): если
провайдер повторит фрагмент запроса, в лог попадёт вопрос сотрудника.
В логе — только код ответа, класс ошибки и признак повтора.
"""

import httpx
from starlette.requests import Request
from structlog.testing import capture_logs

from corp_ed.core.exception_handlers import llm_error_handler
from corp_ed.llm.errors import LLMError

QUESTION = "вопрос сотрудника о зарплате Анны"


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/faq/ask",
            "headers": [],
            "query_string": b"",
        }
    )


async def test_provider_error_is_logged_without_its_text() -> None:
    error = LLMError(f"400 BadRequest: prompt «{QUESTION}» rejected", retryable=False)

    with capture_logs() as logs:
        response = await llm_error_handler(_request(), error)

    assert response.status_code == 502
    [entry] = logs
    assert entry["event"] == "llm_unavailable"
    assert entry["error_type"] == "LLMError"
    assert entry["status"] == 400
    assert entry["retryable"] is False
    assert entry["path"] == "/api/v1/faq/ask"
    assert QUESTION not in repr(entry)


async def test_transport_error_is_logged_by_cause_class() -> None:
    try:
        try:
            raise httpx.ReadTimeout(f"timed out reading {QUESTION}")
        except httpx.ReadTimeout as exc:
            raise LLMError(f"transport: {exc}", retryable=True) from exc
    except LLMError as error:
        with capture_logs() as logs:
            await llm_error_handler(_request(), error)

    [entry] = logs
    assert entry["error_type"] == "LLMError"
    assert entry["cause"] == "ReadTimeout"
    assert entry["retryable"] is True
    assert "status" not in entry
    assert QUESTION not in repr(entry)
