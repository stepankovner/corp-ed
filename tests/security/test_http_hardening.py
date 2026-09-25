"""HTTP-периметр: заголовки, ошибки без утечек, лимиты, документация.

Это то, что сканеры (OWASP ZAP, Burp) проверяют в первые минуты.
"""

import json
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import ValidationError

from corp_ed.api.v1.dependencies import get_faq_service
from corp_ed.core.config import HttpSettings
from corp_ed.core.exceptions import TenantContextMissingError
from corp_ed.core.logging import REDACTED, redact_sensitive
from corp_ed.domain.models import User
from corp_ed.main import app
from tests.api.conftest import bearer

REQUIRED_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}


def _assert_security_headers(response: httpx.Response) -> None:
    for name, value in REQUIRED_HEADERS.items():
        assert response.headers.get(name) == value, name
    uuid.UUID(response.headers["x-request-id"])


async def test_headers_on_success(api: httpx.AsyncClient) -> None:
    response = await api.get("/")
    assert response.status_code == 200
    _assert_security_headers(response)


async def test_headers_on_auth_error(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/v1/auth/me")
    assert response.status_code == 401
    _assert_security_headers(response)


async def test_headers_on_not_found(api: httpx.AsyncClient) -> None:
    response = await api.get("/nope")
    assert response.status_code == 404
    _assert_security_headers(response)


async def test_no_hsts_outside_production(api: httpx.AsyncClient) -> None:
    response = await api.get("/")
    assert "strict-transport-security" not in response.headers


async def test_request_id_is_generated_not_trusted(api: httpx.AsyncClient) -> None:
    """Входящий X-Request-ID не принимается — это подделка записей лога."""
    forged = "forged\nlevel=critical msg=admin_logged_in"
    response = await api.get("/", headers={"X-Request-ID": "abc"})
    assert response.headers["x-request-id"] != "abc"
    assert forged not in response.headers["x-request-id"]


async def test_validation_error_does_not_echo_input(api: httpx.AsyncClient) -> None:
    secret = "super-secret-password-value"
    response = await api.post(
        "/api/v1/auth/login",
        json={"company_code": "test", "email": "not-an-email", "password": secret},
    )

    assert response.status_code == 422
    _assert_security_headers(response)
    assert secret not in response.text
    assert "not-an-email" not in response.text
    assert all(
        set(error) == {"loc", "msg", "type"} for error in response.json()["detail"]
    )


async def test_unhandled_error_is_generic_500(
    api: httpx.AsyncClient, admin: User
) -> None:
    """Трассировка — в лог, клиенту — общий текст и request_id."""

    def explode() -> None:
        raise RuntimeError("SELECT secret FROM internal_table")

    app.dependency_overrides[get_faq_service] = explode
    response = await api.post(
        "/api/v1/faq/ask", json={"question": "Вопрос"}, headers=bearer(admin)
    )

    assert response.status_code == 500
    _assert_security_headers(response)
    body = response.json()
    assert body["detail"] == "Внутренняя ошибка сервера"
    assert body["request_id"] == response.headers["x-request-id"]
    assert "internal_table" not in response.text
    assert "Traceback" not in response.text


async def test_isolation_error_does_not_mention_tenant(
    api: httpx.AsyncClient, admin: User
) -> None:
    def explode() -> None:
        raise TenantContextMissingError("В контексте отсутствует tenant_id")

    app.dependency_overrides[get_faq_service] = explode
    response = await api.post(
        "/api/v1/faq/ask", json={"question": "Вопрос"}, headers=bearer(admin)
    )

    assert response.status_code == 500
    assert "tenant" not in response.text.lower()


async def test_declared_oversized_body_is_413(api: httpx.AsyncClient) -> None:
    payload = json.dumps({"question": "x" * (2 * 1024 * 1024)})
    response = await api.post(
        "/api/v1/faq/ask",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    _assert_security_headers(response)


async def test_streamed_oversized_body_is_413(api: httpx.AsyncClient) -> None:
    """Chunked-тело без Content-Length тоже упирается в лимит."""

    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"question": "'
        for _ in range(40):
            yield b"x" * 64 * 1024
        yield b'"}'

    response = await api.post(
        "/api/v1/faq/ask",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_production_rejects_wildcard_hosts() -> None:
    with pytest.raises(ValidationError):
        HttpSettings(environment="production", allowed_hosts="*")


def test_production_rejects_wildcard_cors() -> None:
    with pytest.raises(ValidationError):
        HttpSettings(
            environment="production",
            allowed_hosts="api.example.ru",
            cors_allowed_origins="*",
        )


def test_docs_are_disabled_in_production() -> None:
    """Приложение собирается при импорте, поэтому — отдельным процессом."""
    env = {
        **os.environ,
        "ENVIRONMENT": "production",
        "ALLOWED_HOSTS": "api.example.ru",
    }
    code = (
        "from corp_ed.main import app;"
        "print(app.docs_url, app.redoc_url, app.openapi_url)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "None None None"


def test_log_redaction_hides_secrets() -> None:
    event = {
        "event": "debug",
        "password": "p",
        "new_password": "p2",
        "refresh_token": "t",
        "Authorization": "Bearer x",
        "api_key": "k",
        "user_id": "42",
    }
    redacted = redact_sensitive(None, "info", event)

    assert redacted["user_id"] == "42"
    for key in (
        "password",
        "new_password",
        "refresh_token",
        "Authorization",
        "api_key",
    ):
        assert redacted[key] == REDACTED
