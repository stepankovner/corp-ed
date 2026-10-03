"""TOTP против векторов RFC 6238 (приложение B, SHA-1)."""

import base64

import pytest

from corp_ed.core import totp

# Ключ из RFC: ASCII «12345678901234567890».
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")


@pytest.mark.parametrize(
    ("unix_time", "expected"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
    ],
)
def test_rfc6238_vectors(unix_time: int, expected: str) -> None:
    # RFC даёт 8 цифр; у нас 6 — это последние 6 того же числа.
    assert totp.code_at(RFC_SECRET, unix_time // 30) == expected[-6:]


def test_window_accepts_neighbour_steps_only() -> None:
    secret = totp.new_secret()
    now = 1_700_000_000.0
    step = totp.current_step(now)
    for delta in (-1, 0, 1):
        code = totp.code_at(secret, step + delta)
        assert totp.matching_step(secret, code, now) == step + delta
    assert totp.matching_step(secret, totp.code_at(secret, step + 2), now) is None


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", "12 34 5x"])
def test_malformed_codes_never_match(code: str) -> None:
    assert totp.matching_step(totp.new_secret(), code) is None


def test_code_with_spaces_is_accepted() -> None:
    secret = totp.new_secret()
    code = totp.code_at(secret, totp.current_step())
    assert totp.matching_step(secret, f"{code[:3]} {code[3:]}") is not None


def test_provisioning_uri() -> None:
    uri = totp.provisioning_uri("ABCDEF", account="anna@acme.ru")
    assert uri.startswith("otpauth://totp/kronto%3Aanna%40acme.ru?")
    assert "secret=ABCDEF" in uri and "issuer=kronto" in uri
