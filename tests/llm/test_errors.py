import httpx
import pytest

from corp_ed.llm.errors import classify


def _response(status: int, body: dict | None = None) -> httpx.Response:
    """Собирает ответ так, как его увидит classify."""
    return httpx.Response(status, json=body or {})


def test_success_returns_none() -> None:
    assert classify(_response(200)) is None


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_statuses(status: int) -> None:
    error = classify(_response(status))
    assert error is not None
    assert error.retryable is True


@pytest.mark.parametrize("status", [400, 401])
def test_not_retryable_statuses(status: int) -> None:
    error = classify(_response(status))
    assert error is not None
    assert error.retryable is False


def test_non_json_body_on_502() -> None:
    """Балансер отдаёт HTML вместо JSON — типично для 502/503."""
    response = httpx.Response(502, text="<html>502 Bad Gateway</html>")
    error = classify(response)
    assert error is not None
    assert error.retryable is True
