"""Политика паролей (ASVS 6.2) и хеширование."""

import pytest

from corp_ed.core.exceptions import WeakPasswordError
from corp_ed.core.password_policy import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    validate_password,
)
from corp_ed.core.security import (
    hash_password,
    password_needs_rehash,
    verify_password,
)


def test_accepts_long_passphrase() -> None:
    validate_password("correct horse battery staple", email="a@b.ru")


def test_rejects_short_password() -> None:
    with pytest.raises(WeakPasswordError):
        validate_password("a" * (MIN_PASSWORD_LENGTH - 1) + "Z")


def test_rejects_overlong_password() -> None:
    """Защита от DoS: argon2 хеширует всю строку целиком."""
    with pytest.raises(WeakPasswordError):
        validate_password("Ab1-" * (MAX_PASSWORD_LENGTH // 4 + 1))


@pytest.mark.parametrize("password", ["password1234", "Qwerty123456", "Пароль123456"])
def test_rejects_common_password_case_insensitive(password: str) -> None:
    with pytest.raises(WeakPasswordError):
        validate_password(password)


def test_rejects_repeated_characters() -> None:
    with pytest.raises(WeakPasswordError):
        validate_password("abababababababab")


def test_rejects_whitespace_only() -> None:
    with pytest.raises(WeakPasswordError):
        validate_password(" " * 20)


def test_rejects_password_containing_email_local_part() -> None:
    with pytest.raises(WeakPasswordError):
        validate_password("ivan.petrov-2026-long", email="Ivan.Petrov@corp.ru")


def test_error_message_does_not_echo_password() -> None:
    secret = "password1234"
    with pytest.raises(WeakPasswordError) as info:
        validate_password(secret)
    assert secret not in str(info.value)


def test_hash_is_argon2id_and_salted() -> None:
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")

    assert first.startswith("$argon2id$")
    assert first != second
    assert verify_password("correct horse battery staple", first)
    assert not verify_password("wrong horse battery staple", first)


def test_verify_without_hash_is_false() -> None:
    """Для несуществующего пользователя проверка идёт по фиктивному хешу."""
    assert verify_password("anything", None) is False


def test_verify_tolerates_corrupted_hash() -> None:
    """Битый хеш в базе — не 500, а обычный отказ."""
    assert verify_password("anything", "not-a-hash") is False
    assert password_needs_rehash("not-a-hash") is True


def test_legacy_passlib_hash_still_verifies() -> None:
    """Хеши, записанные passlib до перехода на argon2-cffi, продолжают работать."""
    legacy = (
        "$argon2id$v=19$m=65536,t=3,p=4$Wav1Xiul1Lp3LgUg5BzDeA$"
        "WMu6rqAo10rkZHo7Gskn75BZrQzIwObeaACxpj8l4O8"
    )
    assert verify_password("secret-password", legacy)
    assert not password_needs_rehash(legacy)
