"""REST-клиент Битрикс24: адреса вызовов, темп, страницы, продление токена,
коды ошибок, скачивание только с хоста портала, запись фикстур."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from corp_ed.connectors.base import AdapterAuthError, AdapterConfigError, AdapterError
from corp_ed.connectors.bitrix24.client import (
    Bitrix24Client,
    OAuthAuth,
    WebhookAuth,
    redact,
)
from corp_ed.connectors.bitrix24.oauth import Bitrix24OAuth, TokenSet
from tests.connectors.fake_portal import (
    ACCESS_TOKEN,
    ADMIN_ID,
    CLIENT_ID,
    CLIENT_SECRET,
    EMPLOYEE_ID,
    OAUTH_SERVER,
    REFRESH_TOKEN,
    FakePortal,
    sample_portal,
)

FIXTURES = Path(__file__).parent / "fixtures" / "bitrix24"


async def _no_sleep(seconds: float) -> None:
    return None


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_client(
    portal: FakePortal,
    *,
    auth: str = "webhook",
    min_interval: float = 0.0,
    sleep: Callable[..., Any] = _no_sleep,
    clock: Callable[[], float] | None = None,
    recorder: Callable[..., None] | None = None,
    client_secret: str = CLIENT_SECRET,
) -> Bitrix24Client:
    http = portal.client()
    credentials: WebhookAuth | OAuthAuth
    if auth == "webhook":
        credentials = WebhookAuth(portal.webhook)
    else:
        credentials = OAuthAuth(
            TokenSet(ACCESS_TOKEN, REFRESH_TOKEN, 0),
            Bitrix24OAuth(
                http,
                portal=portal.portal,
                client_id=CLIENT_ID,
                client_secret=client_secret,
                server=OAUTH_SERVER,
            ),
        )
    kwargs: dict[str, Any] = {"min_interval": min_interval, "sleep": sleep}
    if clock is not None:
        kwargs["clock"] = clock
    return Bitrix24Client(
        http, portal=portal.portal, auth=credentials, recorder=recorder, **kwargs
    )


@pytest.fixture
def portal() -> FakePortal:
    return sample_portal()


# --- адреса и авторизация ------------------------------------------------------------


async def test_webhook_puts_code_in_path(portal: FakePortal) -> None:
    client = make_client(portal)
    data = await client.call("profile")
    assert data["result"]["ID"] == ADMIN_ID
    assert portal.auth_log == [("webhook", ADMIN_ID)]
    assert portal.calls == [("profile", {})]


async def test_oauth_token_goes_in_body(portal: FakePortal) -> None:
    client = make_client(portal, auth="oauth")
    data = await client.call("profile")
    assert data["result"]["ID"] == EMPLOYEE_ID
    assert portal.auth_log == [("oauth", ACCESS_TOKEN)]
    assert client.refreshed_credentials is None


async def test_webhook_host_must_match_portal(portal: FakePortal) -> None:
    with pytest.raises(AdapterConfigError, match="webhook_host_mismatch"):
        Bitrix24Client(
            portal.client(),
            portal=portal.portal,
            auth=WebhookAuth("https://other.example.com/rest/1/code/"),
        )


# --- страницы и темп ------------------------------------------------------------


async def test_iterate_follows_next(portal: FakePortal) -> None:
    portal.page_size = 2
    client = make_client(portal)
    storages = [s async for s in client.iterate("disk.storage.getlist")]
    assert [s["ID"] for s in storages] == ["10", "20", "30", "31"]
    starts = [body["start"] for method, body in portal.calls]
    assert starts == [0, 2]


async def test_pacing_waits_between_calls(portal: FakePortal) -> None:
    sleeps = Sleeps()
    client = make_client(portal, min_interval=0.5, sleep=sleeps, clock=lambda: 100.0)
    await client.call("profile")
    await client.call("profile")
    await client.call("profile")
    assert sleeps.calls == [0.5, 1.0]


async def test_rate_limit_backs_off_then_succeeds(portal: FakePortal) -> None:
    portal.rate_limit_hits = 2
    sleeps = Sleeps()
    client = make_client(portal, sleep=sleeps)
    data = await client.call("profile")
    assert data["result"]["ID"] == ADMIN_ID
    assert sleeps.calls == [1.0, 2.0]


async def test_rate_limit_exhausted_is_retryable(portal: FakePortal) -> None:
    portal.rate_limit_hits = 10
    sleeps = Sleeps()
    client = make_client(portal, sleep=sleeps)
    with pytest.raises(AdapterError) as excinfo:
        await client.call("profile")
    assert excinfo.value.code == "query_limit_exceeded"
    assert excinfo.value.retryable
    assert sleeps.calls == [1.0, 2.0, 4.0]


# --- продление токена ------------------------------------------------------------


async def test_expired_token_is_refreshed_once_and_call_retried(
    portal: FakePortal,
) -> None:
    portal.expired.add(ACCESS_TOKEN)
    client = make_client(portal, auth="oauth")
    data = await client.call("profile")
    assert data["result"]["ID"] == EMPLOYEE_ID
    refreshed = client.refreshed_credentials
    assert refreshed is not None
    assert refreshed["access_token"] != ACCESS_TOKEN
    assert refreshed["refresh_token"] != REFRESH_TOKEN
    assert refreshed["expires_at"].isdigit()
    assert refreshed["member_id"] == "member-1"
    # Старый refresh отозван порталом: второй такой же обмен не пройдёт.
    assert REFRESH_TOKEN not in portal.refresh_tokens
    methods = [m for m, _ in portal.calls]
    assert methods == ["profile", "oauth/token", "profile"]


async def test_still_expired_after_refresh_is_auth_error(portal: FakePortal) -> None:
    portal.expire_all = True
    client = make_client(portal, auth="oauth")
    with pytest.raises(AdapterAuthError, match="expired_token"):
        await client.call("profile")
    assert [m for m, _ in portal.calls].count("oauth/token") == 1


async def test_refresh_with_dead_refresh_token_is_auth_error(
    portal: FakePortal,
) -> None:
    portal.expired.add(ACCESS_TOKEN)
    portal.refresh_tokens.clear()
    client = make_client(portal, auth="oauth")
    with pytest.raises(AdapterAuthError, match="invalid_grant"):
        await client.call("profile")


async def test_refresh_with_wrong_secret_is_config_error(portal: FakePortal) -> None:
    portal.expired.add(ACCESS_TOKEN)
    client = make_client(portal, auth="oauth", client_secret="wrong")
    with pytest.raises(AdapterConfigError, match="invalid_client"):
        await client.call("profile")


async def test_webhook_expired_token_is_not_refreshed(portal: FakePortal) -> None:
    portal.canned["profile"] = (401, {"error": "expired_token"})
    client = make_client(portal)
    with pytest.raises(AdapterAuthError, match="expired_token"):
        await client.call("profile")
    assert "oauth/token" not in [m for m, _ in portal.calls]


# --- ошибки ------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "kind", "code", "retryable"),
    [
        ("invalid_token", AdapterAuthError, "invalid_token", False),
        ("no_auth_found", AdapterAuthError, "no_auth_found", False),
        ("invalid_credentials", AdapterAuthError, "invalid_credentials", False),
        ("insufficient_scope", AdapterConfigError, "insufficient_scope", False),
        ("method_not_found", AdapterConfigError, "error_method_not_found", False),
        ("access_denied_401", AdapterConfigError, "access_denied", False),
        ("access_denied_method", AdapterError, "access_denied", False),
        ("error_not_found", AdapterError, "error_not_found", False),
        ("operation_time_limit", AdapterError, "operation_time_limit", True),
        ("internal", AdapterError, "internal_server_error", True),
        ("portal_deleted", AdapterError, "portal_deleted", False),
    ],
)
async def test_documented_error_codes_are_mapped(
    portal: FakePortal, name: str, kind: type[AdapterError], code: str, retryable: bool
) -> None:
    errors = json.loads((FIXTURES / "errors.json").read_text())["response"]
    portal.canned["profile"] = (errors[name]["status"], errors[name]["body"])
    client = make_client(portal)
    with pytest.raises(kind) as excinfo:
        await client.call("profile")
    assert type(excinfo.value) is kind
    assert excinfo.value.code == code
    assert excinfo.value.retryable is retryable


async def test_redirect_means_portal_moved(portal: FakePortal) -> None:
    portal.redirect_to = "https://new.example.com/rest/profile.json"
    client = make_client(portal)
    with pytest.raises(AdapterError, match="portal_moved"):
        await client.call("profile")


async def test_non_json_5xx_is_retryable(portal: FakePortal) -> None:
    portal.canned["profile"] = (502, "<html>bad gateway</html>")
    client = make_client(portal)
    with pytest.raises(AdapterError) as excinfo:
        await client.call("profile")
    assert excinfo.value.code == "http_502"
    assert excinfo.value.retryable


async def test_unknown_method_error_keeps_code(portal: FakePortal) -> None:
    portal.canned["profile"] = (400, {"error": "LANDING_NOT_EXIST"})
    client = make_client(portal)
    with pytest.raises(AdapterError) as excinfo:
        await client.call("profile")
    assert excinfo.value.code == "landing_not_exist"
    assert not excinfo.value.retryable


# --- скачивание ------------------------------------------------------------


async def test_download_only_from_portal_host(portal: FakePortal) -> None:
    client = make_client(portal)
    with pytest.raises(AdapterError, match="download_url_foreign"):
        await client.download(
            "https://evil.example.com/rest/download.json?token=disk%7C102",
            max_bytes=1024,
        )
    with pytest.raises(AdapterError, match="download_url_foreign"):
        await client.download(
            f"http://{portal.host}/rest/download.json?token=disk%7C102", max_bytes=1024
        )
    assert portal.downloads == []


async def test_download_returns_bytes_with_browser_headers(portal: FakePortal) -> None:
    client = make_client(portal)
    data = await client.download(
        f"{portal.portal}rest/download.json?auth=x&token=disk%7C102", max_bytes=1024
    )
    assert data == "Отпуск — 28 дней.".encode()


async def test_download_html_page_is_failure(portal: FakePortal) -> None:
    portal.download_content_type = "text/html; charset=utf-8"
    client = make_client(portal)
    with pytest.raises(AdapterError, match="download_failed"):
        await client.download(
            f"{portal.portal}rest/download.json?token=disk%7C102", max_bytes=1024
        )


async def test_download_too_large(portal: FakePortal) -> None:
    client = make_client(portal)
    with pytest.raises(AdapterError, match="document_too_large"):
        await client.download(
            f"{portal.portal}rest/download.json?token=disk%7C102", max_bytes=3
        )


# --- запись фикстур ------------------------------------------------------------


async def test_recorder_receives_redacted_calls(portal: FakePortal) -> None:
    recorded: list[tuple[str, dict[str, Any], Any]] = []
    client = make_client(
        portal, auth="oauth", recorder=lambda m, p, r: recorded.append((m, p, r))
    )
    await client.call("disk.file.get", {"id": "102"})
    method, params, response = recorded[0]
    assert method == "disk.file.get"
    assert params == {"id": "102"}
    url = response["result"]["DOWNLOAD_URL"]
    assert "auth=%3Credacted%3E" in url
    assert "token=%3Credacted%3E" in url
    assert ACCESS_TOKEN not in json.dumps(response)


def test_redact_masks_secret_keys_and_query() -> None:
    value = {
        "auth": "t",
        "nested": {"refresh_token": "r", "items": [{"code": "c", "ok": "v"}]},
        "url": "https://p.example.com/x?auth=1&token=2&id=3",
        "plain": "no secrets",
    }
    assert redact(value) == {
        "auth": "<redacted>",
        # В ответе code — символьный код или код ошибки, не секрет.
        "nested": {"refresh_token": "<redacted>", "items": [{"code": "c", "ok": "v"}]},
        "url": "https://p.example.com/x?auth=%3Credacted%3E&token=%3Credacted%3E&id=3",
        "plain": "no secrets",
    }
    # В параметрах запроса code — одноразовый код авторизации.
    assert redact({"code": "c", "CODE": "site"}, request=True) == {
        "code": "<redacted>",
        "CODE": "<redacted>",
    }
    assert redact({"error": {"code": "BITRIX_REST_V3_EXCEPTION_X"}}) == {
        "error": {"code": "BITRIX_REST_V3_EXCEPTION_X"}
    }
