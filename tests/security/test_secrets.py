"""Учётные данные источников в базе: шифрование, ротация, обязательность ключа."""

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from corp_ed.core.config import ConnectorSettings
from corp_ed.core.secrets import (
    SecretBox,
    SecretDecryptionError,
    SecretsNotConfiguredError,
)

KEY = Fernet.generate_key().decode()
OTHER_KEY = Fernet.generate_key().decode()
CREDENTIALS = {"token": "abc-123", "refresh_token": "r-456"}


def test_roundtrip_and_ciphertext_hides_values() -> None:
    box = SecretBox([KEY])
    token = box.encrypt(CREDENTIALS)
    assert box.decrypt(token) == CREDENTIALS
    assert "abc-123" not in token
    assert "token" not in token


def test_same_secret_encrypts_differently_each_time() -> None:
    """Fernet с IV: по шифротексту нельзя понять, что два клиента ввели
    один и тот же токен."""
    box = SecretBox([KEY])
    assert box.encrypt(CREDENTIALS) != box.encrypt(CREDENTIALS)


def test_wrong_key_fails_loudly() -> None:
    token = SecretBox([KEY]).encrypt(CREDENTIALS)
    with pytest.raises(SecretDecryptionError):
        SecretBox([OTHER_KEY]).decrypt(token)


def test_tampered_token_is_rejected() -> None:
    box = SecretBox([KEY])
    token = box.encrypt(CREDENTIALS)
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(SecretDecryptionError):
        box.decrypt(tampered)
    with pytest.raises(SecretDecryptionError):
        box.decrypt("not-a-token")


def test_rotation_decrypts_old_and_reencrypts_with_new() -> None:
    old = SecretBox([KEY])
    token = old.encrypt(CREDENTIALS)
    rotated_box = SecretBox([OTHER_KEY, KEY])
    assert rotated_box.decrypt(token) == CREDENTIALS
    fresh = rotated_box.rotate(token)
    assert SecretBox([OTHER_KEY]).decrypt(fresh) == CREDENTIALS
    with pytest.raises(SecretDecryptionError):
        SecretBox([KEY]).decrypt(fresh)


def test_without_key_nothing_is_stored() -> None:
    box = SecretBox([])
    assert not box.configured
    with pytest.raises(SecretsNotConfiguredError):
        box.encrypt(CREDENTIALS)
    with pytest.raises(SecretsNotConfiguredError):
        box.decrypt("x")


def test_only_string_values_are_accepted() -> None:
    with pytest.raises(TypeError):
        SecretBox([KEY]).encrypt({"token": 5})  # type: ignore[dict-item]


def test_invalid_key_fails_at_construction() -> None:
    with pytest.raises(ValueError):
        SecretBox(["short"])


def test_settings_parse_multiple_keys_and_require_key_in_production() -> None:
    settings = ConnectorSettings(secrets_keys=f"{KEY}, {OTHER_KEY}")  # type: ignore[arg-type]
    assert settings.keys == [KEY, OTHER_KEY]
    assert ConnectorSettings().keys == []
    with pytest.raises(ValidationError):
        ConnectorSettings(environment="production")
    assert ConnectorSettings(environment="production", secrets_keys=KEY).keys == [KEY]  # type: ignore[arg-type]
