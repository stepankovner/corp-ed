"""Настройки коннекторов: https обязателен только в production."""

import pytest
from cryptography.fernet import Fernet

from corp_ed.core.config import ConnectorSettings

KEY = Fernet.generate_key().decode()


def test_outbound_goes_to_pinned_addresses_by_default() -> None:
    settings = ConnectorSettings(environment="development")  # type: ignore[call-arg]
    assert settings.outbound_via_proxy is False


def test_development_allows_plain_http_return_url() -> None:
    settings = ConnectorSettings(
        environment="development",
        oauth_return_url="http://localhost:5173/sources",
    )  # type: ignore[call-arg]
    assert settings.oauth_return_url == "http://localhost:5173/sources"


@pytest.mark.parametrize(
    "field",
    [
        "oauth_callback_url",
        "oauth_return_url",
        "bitrix24_oauth_server",
        "yandex_oauth_server",
        "yandex_disk_api",
    ],
)
def test_production_requires_https(field: str) -> None:
    with pytest.raises(ValueError, match="https://"):
        ConnectorSettings(
            environment="production",
            secrets_keys=KEY,
            **{field: "http://example.com/x"},
        )  # type: ignore[arg-type]


def test_production_requires_secret_keys() -> None:
    with pytest.raises(ValueError, match="CONNECTOR_SECRETS_KEYS"):
        ConnectorSettings(environment="production")  # type: ignore[call-arg]
