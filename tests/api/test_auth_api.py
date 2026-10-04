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
from argon2 import PasswordHasher
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.api.v1.session_cookie import REFRESH_COOKIE
from corp_ed.core import security
from corp_ed.core.config import get_settings
from corp_ed.core.tenant_context import current_tenant
from corp_ed.domain.models import MemberStatus, RefreshToken, Tenant, User
from corp_ed.repositories.audit_repository import AuditRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.tenant_service import TenantService
from tests.api.conftest import (
    PASSWORD,
    bearer,
    login,
    refresh_token_of,
    refresh_with,
)

GENERIC_LOGIN_ERROR = "Неверный логин или пароль"


# --- вход --------------------------------------------------------------------


async def test_login_returns_token_pair(api: httpx.AsyncClient, account: User) -> None:
    response = await login(api, account.email)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == get_settings().access_token_ttl_minutes * 60
    assert body["access_token"]
    # Refresh — только в httpOnly-cookie: скрипт страницы его не прочтёт.
    assert "refresh_token" not in body
    assert refresh_token_of(response)

    me = await api.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["email"] == account.email
    assert me.json()["company"]["name"] == "Test Co"
    assert me.json()["company"]["role"] == "employee"
    assert [c["company_name"] for c in me.json()["companies"]] == ["Test Co"]


async def test_login_is_case_insensitive_for_email(
    api: httpx.AsyncClient, account: User
) -> None:
    response = await api.post(
        "/api/v1/auth/login",
        json={"email": "Worker@Test.com", "password": PASSWORD},
    )
    assert response.status_code == 200


async def test_login_no_longer_takes_company_code(
    api: httpx.AsyncClient, account: User
) -> None:
    """Код компании убран из входа (ТЗ §2): лишнее поле — 422."""
    response = await api.post(
        "/api/v1/auth/login",
        json={"company_code": "test", "email": account.email, "password": PASSWORD},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("worker@test.com", "wrong-password-123"),
        ("nobody@test.com", PASSWORD),
    ],
)
async def test_login_failures_are_indistinguishable(
    api: httpx.AsyncClient,
    account: User,
    email: str,
    password: str,
) -> None:
    """Один статус и один текст — по ответу нельзя узнать, что не так."""
    response = await api.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
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
    await login(api, "a@b.ru")

    assert calls == [None, None]


async def test_unverified_email_cannot_login(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    """Верный пароль, но почта не подтверждена — отдельный код для фронта."""
    assert account.account is not None
    account.account.email_verified_at = None
    await session.commit()

    response = await login(api, account.email)
    assert response.status_code == 403
    assert response.json()["code"] == "email_not_verified"
    # Неверный пароль на неподтверждённой учётке — общий ответ.
    wrong = await login(api, account.email, "wrong-password-123")
    assert wrong.json() == {"detail": GENERIC_LOGIN_ERROR}


async def test_blocked_member_logs_in_without_that_company(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    """Учётка жива и без компании (ТЗ §2): вход есть, компании — нет."""
    account.status = MemberStatus.BLOCKED
    await session.commit()

    response = await login(api, account.email)
    assert response.status_code == 200
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    me = (await api.get("/api/v1/auth/me", headers=headers)).json()
    assert me["company"] is None
    assert me["companies"][0]["status"] == "blocked"
    ask = await api.post("/api/v1/faq/ask", json={"question": "?"}, headers=headers)
    assert ask.status_code == 403
    assert ask.json()["code"] == "no_company"


async def test_suspended_company_is_not_selected_at_login(
    api: httpx.AsyncClient, account: User, tenant_ctx: Tenant, session: AsyncSession
) -> None:
    tenant_ctx.is_active = False
    await session.commit()

    response = await login(api, account.email)
    assert response.status_code == 200
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    me = (await api.get("/api/v1/auth/me", headers=headers)).json()
    assert me["company"] is None
    assert me["companies"] == []


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
        "sid": str(uuid4()),
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
        {"sid": "not-a-uuid"},
        {"sid": 12345},
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

    assert account.account is not None
    token = _forge(
        sub=str(account.account.id),
        tenant_id=str(other.id),
        member_id=str(account.id),
        mver=0,
    )
    response = await api.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


async def test_token_of_deactivated_user_is_rejected(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    headers = bearer(account)
    account.status = MemberStatus.BLOCKED
    await session.commit()

    assert (await api.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def test_token_of_suspended_company_is_rejected(
    api: httpx.AsyncClient, account: User, tenant_ctx: Tenant, session: AsyncSession
) -> None:
    headers = bearer(account)
    tenant_ctx.is_active = False
    await session.commit()

    assert (await api.get("/api/v1/auth/me", headers=headers)).status_code == 401


@pytest.mark.parametrize("level", ["account", "member"])
async def test_old_token_version_is_rejected(
    api: httpx.AsyncClient, account: User, session: AsyncSession, level: str
) -> None:
    """И «выйти везде» учётки, и смена роли в компании отзывают токен."""
    headers = bearer(account)
    if level == "account":
        assert account.account is not None
        account.account.token_version += 1
    else:
        account.token_version += 1
    await session.commit()

    assert (await api.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def test_role_claim_is_not_trusted(api: httpx.AsyncClient, account: User) -> None:
    """В токене role=admin, в базе EMPLOYEE — права берутся из базы."""
    assert account.account is not None
    token = _forge(
        sub=str(account.account.id),
        tenant_id=str(account.tenant_id),
        member_id=str(account.id),
        mver=account.token_version,
        role="admin",
    )
    response = await api.get(
        "/api/v1/users", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


# --- refresh-токены ----------------------------------------------------------


def _set_cookie(response: httpx.Response) -> str:
    [header] = [
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith(f"{REFRESH_COOKIE}=")
    ]
    return header


async def test_refresh_cookie_attributes(api: httpx.AsyncClient, account: User) -> None:
    header = _set_cookie(await login(api, account.email))
    attributes = {part.strip().split("=")[0].lower() for part in header.split(";")}

    assert {"httponly", "path", "max-age", "samesite"} <= attributes
    assert "Path=/api/v1/auth" in header
    assert "SameSite=strict" in header or "SameSite=Strict" in header
    assert f"Max-Age={get_settings().refresh_token_ttl_days * 86400}" in header
    # В разработке фронт на http://localhost: Secure — только в production.
    assert "secure" not in attributes


async def test_refresh_cookie_is_secure_in_production(
    api: httpx.AsyncClient, account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from corp_ed.api.v1 import session_cookie

    class Production:
        is_production = True
        cors_origins: list[str] = []

    monkeypatch.setattr(session_cookie, "get_http_settings", lambda: Production())
    header = _set_cookie(await login(api, account.email))
    assert "Secure" in header


async def test_refresh_rotates_tokens(api: httpx.AsyncClient, account: User) -> None:
    first = await login(api, account.email)

    response = await refresh_with(api, refresh_token_of(first))

    assert response.status_code == 200
    assert "refresh_token" not in response.json()
    assert refresh_token_of(response) != refresh_token_of(first)
    assert response.json()["access_token"] != first.json()["access_token"]


async def test_refresh_without_cookie_is_401(api: httpx.AsyncClient) -> None:
    response = await api.post("/api/v1/auth/refresh")
    assert response.status_code == 401


async def test_refresh_from_foreign_origin_is_403(
    api: httpx.AsyncClient, account: User
) -> None:
    """CSRF: SameSite=Strict не пустит cookie с чужого сайта, Origin —
    второй рубеж для браузера, который SameSite не знает."""
    raw = refresh_token_of(await login(api, account.email))

    foreign = await refresh_with(api, raw, headers={"Origin": "https://evil.example"})
    assert foreign.status_code == 403

    own = await refresh_with(api, raw, headers={"Origin": "http://test"})
    assert own.status_code == 200


async def test_refresh_token_reuse_revokes_whole_family(
    api: httpx.AsyncClient, account: User
) -> None:
    """Украденный и уже использованный токен предъявлен повторно.

    Сервер не знает, кто из двоих легитимный, поэтому отзывает всю
    цепочку: новый токен, полученный честной ротацией, тоже умирает.
    """
    stolen = refresh_token_of(await login(api, account.email))
    rotated = refresh_token_of(await refresh_with(api, stolen))

    replay = await refresh_with(api, stolen)
    assert replay.status_code == 401

    after = await refresh_with(api, rotated)
    assert after.status_code == 401


async def test_unknown_refresh_token_is_401(api: httpx.AsyncClient) -> None:
    response = await refresh_with(api, "made-up-token")
    assert response.status_code == 401


async def test_oversized_refresh_cookie_is_401(api: httpx.AsyncClient) -> None:
    response = await refresh_with(api, "x" * 10_000)
    assert response.status_code == 401


async def test_expired_refresh_token_is_401(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    raw = refresh_token_of(await login(api, account.email))
    record = (
        await session.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == security.hash_refresh_token(raw)
            )
        )
    ).scalar_one()
    record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    response = await refresh_with(api, raw)
    assert response.status_code == 401


async def test_refresh_token_is_stored_only_as_hash(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    raw = refresh_token_of(await login(api, account.email))
    hashes = (await session.execute(select(RefreshToken.token_hash))).scalars().all()

    assert raw not in hashes
    assert security.hash_refresh_token(raw) in hashes


async def test_refresh_after_block_drops_the_company(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    """Заблокировали в компании — обновление даёт сессию без неё, а не
    выход: учётка жива (ТЗ §2)."""
    raw = refresh_token_of(await login(api, account.email))
    account.status = MemberStatus.BLOCKED
    await session.commit()

    response = await refresh_with(api, raw)
    assert response.status_code == 200
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    assert (await api.get("/api/v1/auth/me", headers=headers)).json()["company"] is None


async def test_refresh_for_deleted_account_is_401(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    raw = refresh_token_of(await login(api, account.email))
    assert account.account is not None
    await session.delete(account.account)
    await session.commit()

    assert (await refresh_with(api, raw)).status_code == 401


# --- выход -------------------------------------------------------------------


async def test_logout_revokes_refresh_token_and_clears_cookie(
    api: httpx.AsyncClient, account: User
) -> None:
    signed_in = await login(api, account.email)
    raw = refresh_token_of(signed_in)

    response = await api.post(
        "/api/v1/auth/logout",
        headers={
            "Authorization": f"Bearer {signed_in.json()['access_token']}",
            "Cookie": f"{REFRESH_COOKIE}={raw}",
        },
    )
    assert response.status_code == 204
    cleared = _set_cookie(response)
    assert "Max-Age=0" in cleared or "expires=" in cleared.lower()

    again = await refresh_with(api, raw)
    assert again.status_code == 401
    # Токен доступа этого входа больше не действует — сразу, а не через
    # 15 минут, когда истечёт (сеанс в нём — sid).
    me = await api.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {signed_in.json()['access_token']}"},
    )
    assert me.status_code == 401


async def test_logout_keeps_other_sessions_working(
    api: httpx.AsyncClient, account: User
) -> None:
    phone = (await login(api, account.email)).json()
    laptop = await login(api, account.email)

    await api.post(
        "/api/v1/auth/logout",
        headers={
            "Authorization": f"Bearer {laptop.json()['access_token']}",
            "Cookie": f"{REFRESH_COOKIE}={refresh_token_of(laptop)}",
        },
    )

    me = await api.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {phone['access_token']}"}
    )
    assert me.status_code == 200


async def test_logout_without_cookie_still_succeeds(
    api: httpx.AsyncClient, account: User
) -> None:
    api.cookies.clear()
    response = await api.post("/api/v1/auth/logout", headers=bearer(account))
    assert response.status_code == 204


async def test_logout_without_cookie_ends_the_token_session(
    api: httpx.AsyncClient, account: User
) -> None:
    """cookie потерялся — «Выйти» всё равно закрывает сеанс из токена."""
    signed_in = await login(api, account.email)
    raw = refresh_token_of(signed_in)
    headers = {"Authorization": f"Bearer {signed_in.json()['access_token']}"}
    api.cookies.clear()

    assert (await api.post("/api/v1/auth/logout", headers=headers)).status_code == 204

    assert (await api.get("/api/v1/auth/me", headers=headers)).status_code == 401
    assert (await refresh_with(api, raw)).status_code == 401


async def test_logout_from_foreign_origin_is_403(
    api: httpx.AsyncClient, account: User
) -> None:
    response = await api.post(
        "/api/v1/auth/logout",
        headers={**bearer(account), "Origin": "https://evil.example"},
    )
    assert response.status_code == 403


async def test_logout_cannot_revoke_someone_elses_token(
    api: httpx.AsyncClient, account: User, admin_account: User
) -> None:
    victim = refresh_token_of(await login(api, admin_account.email))
    attacker = (await login(api, account.email)).json()

    response = await api.post(
        "/api/v1/auth/logout",
        headers={
            "Authorization": f"Bearer {attacker['access_token']}",
            "Cookie": f"{REFRESH_COOKIE}={victim}",
        },
    )
    assert response.status_code == 204  # ответ не выдаёт, чей это токен

    still_valid = await refresh_with(api, victim)
    assert still_valid.status_code == 200


async def test_logout_everywhere_kills_access_and_refresh(
    api: httpx.AsyncClient, account: User
) -> None:
    laptop = await login(api, account.email)
    phone = await login(api, account.email)

    response = await api.post(
        "/api/v1/auth/logout-all",
        headers={"Authorization": f"Bearer {laptop.json()['access_token']}"},
    )
    assert response.status_code == 204

    for device in (laptop, phone):
        me = await api.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {device.json()['access_token']}"},
        )
        assert me.status_code == 401
        refreshed = await refresh_with(api, refresh_token_of(device))
        assert refreshed.status_code == 401


# --- смена пароля -------------------------------------------------------------


async def test_change_password_closes_other_sessions(
    api: httpx.AsyncClient, account: User
) -> None:
    other_signin = await login(api, account.email)
    other_device = other_signin.json()
    current = (await login(api, account.email)).json()

    response = await api.post(
        "/api/v1/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "new horse battery 2026"},
        headers={"Authorization": f"Bearer {current['access_token']}"},
    )
    assert response.status_code == 200
    # Текущее устройство получает новую cookie, у другого refresh отозван.
    assert (await refresh_with(api, refresh_token_of(response))).status_code == 200
    stale_refresh = await refresh_with(api, refresh_token_of(other_signin))
    assert stale_refresh.status_code == 401

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
        json={
            "current_password": "guess-guess-guess",
            "new_password": "new horse battery 2026",
        },
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


async def test_policy_rejections_do_not_lock_password_change(
    api: httpx.AsyncClient, account: User
) -> None:
    """Подбор пароля под правила не упирается в лимит (стенд 02.10: после
    пяти «слишком распространённый» человек ждал 15 минут)."""
    for weak in ["short", "aaaaaaaaaaaa", "password1234", "abababababab", "x", "y"]:
        rejected = await api.post(
            "/api/v1/auth/change-password",
            json={"current_password": PASSWORD, "new_password": weak},
            headers=bearer(account),
        )
        assert rejected.status_code == 422

    changed = await api.post(
        "/api/v1/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "new horse battery 2026"},
        headers=bearer(account),
    )
    assert changed.status_code == 200


async def test_wrong_current_password_is_still_limited(
    api: httpx.AsyncClient, account: User
) -> None:
    statuses = [
        (
            await api.post(
                "/api/v1/auth/change-password",
                json={
                    "current_password": f"guess-guess-{attempt}",
                    "new_password": "new horse battery 2026",
                },
                headers=bearer(account),
            )
        ).status_code
        for attempt in range(6)
    ]
    assert statuses == [400] * 5 + [429]


async def test_temporary_password_cannot_be_changed_back_to_the_old_one(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    """Пароль сбросила команда (cli reset-password) — прежний не вернуть:
    сбрасывают его, когда его мог узнать кто-то ещё."""
    temporary = "временный пароль от команды 2026"
    await TenantService(
        TenantRepository(session),
        UserRepository(session),
        AuditRepository(session),
        session,
    ).reset_password(account.email, temporary)
    signed_in = await login(api, account.email, temporary)
    headers = {"Authorization": f"Bearer {signed_in.json()['access_token']}"}

    back = await api.post(
        "/api/v1/auth/change-password",
        json={"current_password": temporary, "new_password": PASSWORD},
        headers=headers,
    )
    assert back.status_code == 422
    assert "уже был" in back.json()["detail"]

    fresh = await api.post(
        "/api/v1/auth/change-password",
        json={"current_password": temporary, "new_password": "new horse battery 2026"},
        headers=headers,
    )
    assert fresh.status_code == 200


async def test_rehash_on_login_does_not_touch_password_history(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    """Хеш со старыми параметрами пересчитывается при входе — пароль тот
    же, в историю он не идёт."""
    assert account.account is not None
    old = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1).hash(PASSWORD)
    account.account.hashed_password = old
    await session.commit()

    assert (await login(api, account.email)).status_code == 200

    await session.refresh(account.account)
    assert account.account.hashed_password != old
    assert security.verify_password(PASSWORD, account.account.hashed_password)
    assert account.account.previous_password_hashes == []


async def test_temporary_password_blocks_everything_but_change(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    assert account.account is not None
    account.account.must_change_password = True
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
