"""Access-токен: подпись, срок, издатель, аудитория, тип (ASVS 9.x)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt
import pytest

from corp_ed.core.config import get_settings
from corp_ed.core.security import (
    ALGORITHM,
    AUDIENCE,
    ISSUER,
    create_access_token,
    decode_access_token,
    hash_refresh_token,
    new_refresh_token,
)


def _claims(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": str(uuid4()),
        "tenant_id": str(uuid4()),
        "role": "admin",
        "ver": 0,
        "typ": "access",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=5),
        "jti": uuid4().hex,
    }
    claims.update(overrides)
    return {key: value for key, value in claims.items() if value is not None}


def _sign(claims: dict[str, Any], key: str | None = None, alg: str = ALGORITHM) -> str:
    secret = key or get_settings().secret_key.get_secret_value()
    return jwt.encode(claims, secret, algorithm=alg)


def test_roundtrip() -> None:
    user_id, tenant_id = uuid4(), uuid4()
    token = create_access_token(user_id, tenant_id, "employee", token_version=3)

    payload = decode_access_token(token)

    assert payload["sub"] == str(user_id)
    assert payload["tenant_id"] == str(tenant_id)
    assert payload["ver"] == 3
    assert payload["iss"] == ISSUER
    assert payload["aud"] == AUDIENCE


def test_every_token_has_unique_jti() -> None:
    user_id, tenant_id = uuid4(), uuid4()
    first = decode_access_token(create_access_token(user_id, tenant_id, "admin", 0))
    second = decode_access_token(create_access_token(user_id, tenant_id, "admin", 0))
    assert first["jti"] != second["jti"]


def test_rejects_foreign_signature() -> None:
    with pytest.raises(jwt.InvalidSignatureError):
        decode_access_token(_sign(_claims(), key="another-key-" + "y" * 32))


def test_rejects_alg_none() -> None:
    """Классическая атака: токен без подписи с "alg": "none"."""
    unsigned = jwt.encode(_claims(), key=None, algorithm="none")  # type: ignore[arg-type]
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(unsigned)


def test_rejects_other_hmac_algorithm() -> None:
    with pytest.raises(jwt.InvalidAlgorithmError):
        decode_access_token(_sign(_claims(), alg="HS512"))


def test_rejects_expired() -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(_sign(_claims(exp=past, iat=past, nbf=past)))


def test_rejects_not_yet_valid() -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    with pytest.raises(jwt.ImmatureSignatureError):
        decode_access_token(_sign(_claims(nbf=future)))


def test_rejects_wrong_audience() -> None:
    with pytest.raises(jwt.InvalidAudienceError):
        decode_access_token(_sign(_claims(aud="another-service")))


def test_rejects_wrong_issuer() -> None:
    with pytest.raises(jwt.InvalidIssuerError):
        decode_access_token(_sign(_claims(iss="someone-else")))


@pytest.mark.parametrize("claim", ["exp", "iat", "nbf", "iss", "aud", "sub", "jti"])
def test_rejects_missing_required_claim(claim: str) -> None:
    with pytest.raises(jwt.MissingRequiredClaimError):
        decode_access_token(_sign(_claims(**{claim: None})))


def test_rejects_non_access_type() -> None:
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(_sign(_claims(typ="refresh")))


def test_refresh_tokens_are_random_and_stored_hashed() -> None:
    first, second = new_refresh_token(), new_refresh_token()

    assert first != second
    assert len(first) >= 43  # 32 байта в base64url
    assert hash_refresh_token(first) != first
    assert len(hash_refresh_token(first)) == 64


def test_weak_secret_key_is_rejected_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import ValidationError

    from corp_ed.core.config import Settings

    monkeypatch.setenv("SECRET_KEY", "short")
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_secret_key_is_masked_in_repr() -> None:
    settings = get_settings()
    assert settings.secret_key.get_secret_value() not in repr(settings)
