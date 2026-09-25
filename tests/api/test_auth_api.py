"""Аутентификация через HTTP с настоящими токенами.

Проверяются не только «счастливые» сценарии, но и то, что пентестер
попробует первым: перечисление пользователей, подделка и повтор
токенов, отзыв сессий, утечка контекста тенанта.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core import security
from corp_ed.core.config import get_settings
from corp_ed.core.tenant_context import current_tenant
from corp_ed.domain.models import RefreshToken, Tenant, User
from tests.api.conftest import PASSWORD, bearer, login

GENERIC_LOGIN_ERROR = "Неверный логин или пароль"


# --- вход --------------------------------------------------------------------


async def test_login_returns_token_pair(api: httpx.AsyncClient, account: User) -> None:
    response = await login(api, account.email)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == get_settings().access_token_ttl_minutes * 60
    assert body["access_token"] and body["refresh_token"]

    me = await api.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["email"] == account.email
    assert me.json()["company_name"] == "Test Co"


async def test_login_is_case_insensitive_for_email_and_company(
    api: httpx.AsyncClient, account: User
) -> None:
    response = await api.post(
        "/api/v1/auth/login",
        json={"company_code": "TEST", "email": "Worker@Test.com", "password": PASSWORD},
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("company", "email", "password"),
    [
        ("test", "worker@test.com", "wrong-password-123"),
        ("test", "nobody@test.com", PASSWORD),
        ("no-such-company", "worker@test.com", PASSWORD),
    ],
)
async def test_login_failures_are_indistinguishable(
    api: httpx.AsyncClient,
    account: User,
    company: str,
    email: str,
    password: str,
) -> None:
    """Один статус и один текст — по ответу нельзя узнать, что не так."""
    response = await api.post(
        "/api/v1/auth/login",
        json={"company_code": company, "email": email, "password": password},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": GENERIC_LOGIN_ERROR}


async def test_password_is_checked_even_for_unknown_user(
    api: httpx.AsyncClient,
    tenant_ctx: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Выравнивание по времени: argon2 считается и без пользователя.

    Проверяется детерминированно — числом вызовов, а не секундомером.
    """
    calls: list[str | None] = []
    original = security.verify_password

    def spy(plain: str, hashed: str | None) -> bool:
        calls.append(hashed)
        return original(plain, hashed)

    monkeypatch.setattr("corp_ed.services.auth_service.verify_password", spy)

    await login(api, "nobody@test.com")
    await api.post(
        "/api/v1/auth/login",
        json={"company_code": "missing", "email": "a@b.ru", "password": PASSWORD},
    )

    assert calls == [None, None]


async def test_inactive_user_cannot_login(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    account.is_active = False
    await session.commit()

    response = await login(api, account.email)
    assert response.status_code == 401
    assert response.json() == {"detail": GENERIC_LOGIN_ERROR}


async def test_suspended_company_cannot_login(
    api: httpx.AsyncClient, account: User, tenant_ctx: Tenant, session: AsyncSession
) -> None:
    tenant_ctx.is_active = False
    await session.commit()

    assert (await login(api, account.email)).status_code == 401


async def test_login_does_not_leak_tenant_context(
    api: httpx.AsyncClient, account: User
) -> None:
    """RISKS.md №5: логин ставил тенанта из тела запроса и не сбрасывал."""
    token = current_tenant.set(None)
    try:
        await login(api, account.email)
        assert current_tenant.get() is None
    finally:
        current_tenant.reset(token)


async def test_login_rejects_unknown_fields(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/api/v1/auth/login",
        json={
            "company_code": "test",
            "email": "a@b.ru",
            "password": "x",
            "tenant_id": str(uuid4()),
        },
    )
    assert response.status_code == 422


async def test_login_rejects_oversized_password(api: httpx.AsyncClient) -> None:
    """Мегабайтный пароль не должен доходить до argon2."""
    response = await api.post(
        "/api/v1/auth/login",
        json={"company_code": "test", "email": "a@b.ru", "password": "x" * 10_000},
    )
    assert response.status_code == 422


# --- проверка access-токена -------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        None,
        "Bearer",
        "Bearer not-a-jwt",
        "Basic dXNlcjpwYXNz",
    ],
)
async def test_bad_authorization_header_is_401(
    api: httpx.AsyncClient, header: str | None
) -> None:
    headers = {"Authorization": header} if header else {}
    response = await api.get("/api/v1/auth/me", headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def _forge(**claims: object) -> str:
    """Токен, подписанный НАШИМ ключом, но с произвольным содержимым.

    Моделирует утечку ключа или ошибку в коде выпуска: даже тогда
    сервер обязан ответить 401, а не 500.
    """
    now = datetime.now(UTC)
    payload = {
        "sub": str(uuid4()),
        "tenant_id": str(uuid4()),
        "ver": 0,
        "typ": "access",
        "iss": security.ISSUER,
        "aud": security.AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=5),
        "jti": uuid4().hex,
        **claims,
    }
    return jwt.encode(
        payload, get_settings().secret_key.get_secret_value(), algorithm="HS256"
    )


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": "not-a-uuid"},
        {"sub": 12345},
        {"tenant_id": "../../etc/passwd"},
        {"ver": "abc"},
        {"ver": None},
    ],
)
async def test_malformed_claims_are_401_not_500(
    api: httpx.AsyncClient, claims: dict[str, object]
) -> None:
    response = await api.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {_forge(**claims)}"}
    )
    assert response.status_code == 401


async def test_token_with_other_tenant_id_does_not_find_user(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    """Пользователь есть, но в токене чужой тенант — хук изоляции его скроет."""
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()

    token = _forge(sub=str(account.id), tenant_id=str(other.id))
    response = await api.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


async def test_token_of_deactivated_user_is_rejected(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    headers = bearer(account)
    account.is_active = False
    await session.commit()

    assert (await api.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def test_token_of_suspended_company_is_rejected(
    api: httpx.AsyncClient, account: User, tenant_ctx: Tenant, session: AsyncSession
) -> None:
    headers = bearer(account)
    tenant_ctx.is_active = False
    await session.commit()

    assert (await api.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def test_old_token_version_is_rejected(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    headers = bearer(account)
    account.token_version += 1
    await session.commit()

    assert (await api.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def test_role_claim_is_not_trusted(api: httpx.AsyncClient, account: User) -> None:
    """В токене role=admin, в базе EMPLOYEE — права берутся из базы."""
    token = _forge(sub=str(account.id), tenant_id=str(account.tenant_id), role="admin")
    response = await api.get(
        "/api/v1/users", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


# --- refresh-токены ----------------------------------------------------------


async def test_refresh_rotates_tokens(api: httpx.AsyncClient, account: User) -> None:
    first = (await login(api, account.email)).json()

    response = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )

    assert response.status_code == 200
    second = response.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert second["access_token"] != first["access_token"]


async def test_refresh_token_reuse_revokes_whole_family(
    api: httpx.AsyncClient, account: User
) -> None:
    """Украденный и уже использованный токен предъявлен повторно.

    Сервер не знает, кто из двоих легитимный, поэтому отзывает всю
    цепочку: новый токен, полученный честной ротацией, тоже умирает.
    """
    stolen = (await login(api, account.email)).json()["refresh_token"]
    rotated = (
        await api.post("/api/v1/auth/refresh", json={"refresh_token": stolen})
    ).json()["refresh_token"]

    replay = await api.post("/api/v1/auth/refresh", json={"refresh_token": stolen})
    assert replay.status_code == 401

    after = await api.post("/api/v1/auth/refresh", json={"refresh_token": rotated})
    assert after.status_code == 401


async def test_unknown_refresh_token_is_401(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": "made-up-token"}
    )
    assert response.status_code == 401


async def test_expired_refresh_token_is_401(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    raw = (await login(api, account.email)).json()["refresh_token"]
    record = (
        await session.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == security.hash_refresh_token(raw)
            )
        )
    ).scalar_one()
    record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    response = await api.post("/api/v1/auth/refresh", json={"refresh_token": raw})
    assert response.status_code == 401


async def test_refresh_token_is_stored_only_as_hash(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    raw = (await login(api, account.email)).json()["refresh_token"]
    hashes = (await session.execute(select(RefreshToken.token_hash))).scalars().all()

    assert raw not in hashes
    assert security.hash_refresh_token(raw) in hashes


async def test_refresh_for_deactivated_user_is_401(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    raw = (await login(api, account.email)).json()["refresh_token"]
    account.is_active = False
    await session.commit()

    response = await api.post("/api/v1/auth/refresh", json={"refresh_token": raw})
    assert response.status_code == 401


# --- выход -------------------------------------------------------------------


async def test_logout_revokes_refresh_token(
    api: httpx.AsyncClient, account: User
) -> None:
    tokens = (await login(api, account.email)).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    response = await api.post(
        "/api/v1/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers=headers,
    )
    assert response.status_code == 204

    again = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert again.status_code == 401


async def test_logout_cannot_revoke_someone_elses_token(
    api: httpx.AsyncClient, account: User, admin_account: User
) -> None:
    victim = (await login(api, admin_account.email)).json()
    attacker = (await login(api, account.email)).json()

    response = await api.post(
        "/api/v1/auth/logout",
        json={"refresh_token": victim["refresh_token"]},
        headers={"Authorization": f"Bearer {attacker['access_token']}"},
    )
    assert response.status_code == 204  # ответ не выдаёт, чей это токен

    still_valid = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": victim["refresh_token"]}
    )
    assert still_valid.status_code == 200


async def test_logout_everywhere_kills_access_and_refresh(
    api: httpx.AsyncClient, account: User
) -> None:
    laptop = (await login(api, account.email)).json()
    phone = (await login(api, account.email)).json()

    response = await api.post(
        "/api/v1/auth/logout-all",
        headers={"Authorization": f"Bearer {laptop['access_token']}"},
    )
    assert response.status_code == 204

    for tokens in (laptop, phone):
        me = await api.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert me.status_code == 401
        refreshed = await api.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert refreshed.status_code == 401


# --- смена пароля -------------------------------------------------------------


async def test_change_password_closes_other_sessions(
    api: httpx.AsyncClient, account: User
) -> None:
    other_device = (await login(api, account.email)).json()
    current = (await login(api, account.email)).json()

    response = await api.post(
        "/api/v1/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "new horse battery 2026"},
        headers={"Authorization": f"Bearer {current['access_token']}"},
    )
    assert response.status_code == 200

    new_access = response.json()["access_token"]
    ok = await api.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {new_access}"}
    )
    assert ok.status_code == 200

    stale = await api.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {other_device['access_token']}"},
    )
    assert stale.status_code == 401
    assert (await login(api, account.email)).status_code == 401
    assert (
        await login(api, account.email, "new horse battery 2026")
    ).status_code == 200


async def test_change_password_requires_current_password(
    api: httpx.AsyncClient, account: User
) -> None:
    response = await api.post(
        "/api/v1/auth/change-password",
        json={"current_password": "guess-guess-guess", "new_password": "x" * 20},
        headers=bearer(account),
    )
    # Не 401: иначе клиент решит, что сессия истекла.
    assert response.status_code == 400


async def test_change_password_enforces_policy(
    api: httpx.AsyncClient, account: User
) -> None:
    response = await api.post(
        "/api/v1/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "short"},
        headers=bearer(account),
    )
    assert response.status_code == 422


async def test_temporary_password_blocks_everything_but_change(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    account.must_change_password = True
    await session.commit()
    headers = bearer(account)

    me = await api.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["must_change_password"] is True

    ask = await api.post(
        "/api/v1/faq/ask", json={"question": "Сколько дней отпуска?"}, headers=headers
    )
    assert ask.status_code == 403

    changed = await api.post(
        "/api/v1/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "new horse battery 2026"},
        headers=headers,
    )
    assert changed.status_code == 200

    new_headers = {"Authorization": f"Bearer {changed.json()['access_token']}"}
    ask = await api.post(
        "/api/v1/faq/ask",
        json={"question": "Сколько дней отпуска?"},
        headers=new_headers,
    )
    assert ask.status_code == 200
