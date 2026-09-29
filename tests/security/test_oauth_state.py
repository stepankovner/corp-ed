"""state OAuth коннекторов: подписан, привязан к сотруднику и подключению,
не взаимозаменяем с access-токеном, живёт недолго."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from corp_ed.core.config import get_settings
from corp_ed.core.security import (
    ALGORITHM,
    AUDIENCE,
    ISSUER,
    OAUTH_STATE_TYPE,
    create_access_token,
    create_oauth_state,
    decode_access_token,
    decode_oauth_state,
)


def test_state_roundtrip_carries_user_tenant_connector() -> None:
    user_id, tenant_id, connector_id = uuid4(), uuid4(), uuid4()
    state = create_oauth_state(user_id, tenant_id, connector_id, ttl_minutes=5)
    payload = decode_oauth_state(state)
    assert payload["sub"] == str(user_id)
    assert payload["tenant_id"] == str(tenant_id)
    assert payload["connector_id"] == str(connector_id)
    assert payload["typ"] == OAUTH_STATE_TYPE


def test_access_token_is_not_a_state_and_vice_versa() -> None:
    access = create_access_token(uuid4(), uuid4(), "employee", 1)
    with pytest.raises(jwt.PyJWTError):
        decode_oauth_state(access)
    state = create_oauth_state(uuid4(), uuid4(), uuid4(), ttl_minutes=5)
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(state)


def test_tampered_or_expired_state_is_rejected() -> None:
    state = create_oauth_state(uuid4(), uuid4(), uuid4(), ttl_minutes=5)
    with pytest.raises(jwt.PyJWTError):
        decode_oauth_state(state[:-3] + "abc")
    now = datetime.now(UTC)
    expired = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
            "connector_id": str(uuid4()),
            "typ": OAUTH_STATE_TYPE,
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now - timedelta(minutes=20),
            "nbf": now - timedelta(minutes=20),
            "exp": now - timedelta(minutes=10),
            "jti": "x",
        },
        get_settings().secret_key.get_secret_value(),
        algorithm=ALGORITHM,
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_oauth_state(expired)


def test_state_without_connector_claim_is_rejected() -> None:
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
            "typ": OAUTH_STATE_TYPE,
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=5),
            "jti": "x",
        },
        get_settings().secret_key.get_secret_value(),
        algorithm=ALGORITHM,
    )
    with pytest.raises(jwt.MissingRequiredClaimError):
        decode_oauth_state(token)
