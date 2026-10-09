"""Второй фактор, доверенные устройства и сеансы (ТЗ §3, решение 03.10)."""

import re

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.api.v1.session_cookie import DEVICE_COOKIE, REFRESH_COOKIE
from corp_ed.core import totp
from corp_ed.domain.models import OutboxEmail, Tenant, User
from tests.api.conftest import (
    PASSWORD,
    TEST_TOTP_SECRET,
    bearer,
    last_login_code,
    login,
    login_step,
    refresh_token_of,
    refresh_with,
)
from tests.soft_authenticator import SoftAuthenticator


def _auth(response: httpx.Response) -> dict[str, str]:
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _mails(session: AsyncSession, to: str, kind: str) -> list[OutboxEmail]:
    return list(
        await session.scalars(
            select(OutboxEmail).where(
                OutboxEmail.to_email == to, OutboxEmail.kind == kind
            )
        )
    )


async def _verify(
    api: httpx.AsyncClient, token: str, method: str, code: str
) -> httpx.Response:
    return await api.post(
        "/api/v1/auth/mfa/verify",
        json={"token": token, "method": method, "code": code},
    )


async def _enable_totp(
    api: httpx.AsyncClient, headers: dict[str, str]
) -> tuple[str, list[str]]:
    setup = (
        await api.post(
            "/api/v1/account/totp/setup", json={"password": PASSWORD}, headers=headers
        )
    ).json()
    code = totp.code_at(setup["secret"], totp.current_step())
    enabled = await api.post(
        "/api/v1/account/totp/enable",
        json={"setup_token": setup["setup_token"], "code": code},
        headers=headers,
    )
    assert enabled.status_code == 200, enabled.text
    return setup["secret"], enabled.json()["backup_codes"]


# --- вход: код на почту и доверенное устройство -----------------------------


async def test_password_alone_does_not_open_a_session(
    api: httpx.AsyncClient, account: User
) -> None:
    response = await login_step(api, account.email or "")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "mfa_required"
    assert body["access_token"] is None
    assert body["mfa"]["methods"] == ["email"]
    assert body["mfa"]["email_hint"] == "w***@test.com"
    assert REFRESH_COOKIE not in response.cookies


async def test_email_code_opens_a_session_and_remembers_the_device(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    first = await login(api, account.email or "", remember=True)
    assert first.status_code == 200
    assert first.cookies.get(DEVICE_COOKIE)

    # Тот же браузер: второй фактор не нужен, нового письма с кодом нет.
    codes_before = len(await _mails(session, account.email or "", "login_code"))
    again = await login_step(api, account.email or "")
    assert again.json()["status"] == "ok"
    assert again.json()["access_token"]
    assert len(await _mails(session, account.email or "", "login_code")) == codes_before


async def test_without_remember_the_device_is_not_trusted(
    api: httpx.AsyncClient, account: User
) -> None:
    first = await login(api, account.email or "", remember=False)
    assert first.status_code == 200
    assert DEVICE_COOKIE not in first.cookies
    assert (await login_step(api, account.email or "")).json()["status"] == (
        "mfa_required"
    )


async def test_company_can_forbid_remembering(
    api: httpx.AsyncClient, account: User, tenant_ctx: Tenant, session: AsyncSession
) -> None:
    tenant_ctx.allow_remember_device = False
    await session.commit()

    response = await login(api, account.email or "", remember=True)
    assert response.status_code == 200
    assert DEVICE_COOKIE not in response.cookies


async def test_wrong_email_codes_expire_the_login(
    api: httpx.AsyncClient, account: User
) -> None:
    step = (await login_step(api, account.email or "")).json()["mfa"]
    right = await last_login_code(api, account.email or "")
    wrong = f"{(int(right) + 1) % 1_000_000:06d}"

    statuses = [
        (await _verify(api, step["token"], "email", wrong)).status_code
        for _ in range(5)
    ]
    late = await _verify(api, step["token"], "email", right)

    assert statuses == [400] * 5
    assert late.status_code == 400
    assert late.json()["code"] == "login_expired"


async def test_resend_replaces_the_email_code(
    api: httpx.AsyncClient, account: User
) -> None:
    step = (await login_step(api, account.email or "")).json()["mfa"]
    old = await last_login_code(api, account.email or "")
    resent = await api.post("/api/v1/auth/mfa/resend", json={"token": step["token"]})
    new = await last_login_code(api, account.email or "")

    assert resent.status_code == 202
    if old != new:
        assert (await _verify(api, step["token"], "email", old)).status_code == 400
    assert (await _verify(api, step["token"], "email", new)).status_code == 200


async def test_login_step_is_single_use(api: httpx.AsyncClient, account: User) -> None:
    step = (await login_step(api, account.email or "")).json()["mfa"]
    code = await last_login_code(api, account.email or "")
    assert (await _verify(api, step["token"], "email", code)).status_code == 200
    again = await _verify(api, step["token"], "email", code)
    assert again.json()["code"] == "login_expired"


async def test_logout_everywhere_forgets_trusted_devices(
    api: httpx.AsyncClient, account: User
) -> None:
    signed_in = await login(api, account.email or "", remember=True)
    await api.post("/api/v1/auth/logout-all", headers=_auth(signed_in))
    assert (await login_step(api, account.email or "")).json()["status"] == (
        "mfa_required"
    )


async def test_ending_a_session_forgets_trusted_devices(
    api: httpx.AsyncClient, account: User
) -> None:
    """«Завершить сеанс» — обычно из-за подозрения: «запомненные»
    устройства снова проходят второй фактор, как после «выйти везде»."""
    trusted = await login(api, account.email or "", remember=True)
    device = api.cookies.get(DEVICE_COOKIE)
    assert device
    other = await login(api, account.email or "", remember=False)
    sessions = (await api.get("/api/v1/auth/sessions", headers=_auth(other))).json()
    trusted_session = next(item for item in sessions if not item["current"])

    ended = await api.post(
        f"/api/v1/auth/sessions/{trusted_session['id']}/end", headers=_auth(other)
    )

    assert ended.status_code == 204
    assert trusted.status_code == 200
    api.cookies.set(DEVICE_COOKIE, device)
    assert (await login_step(api, account.email or "")).json()["status"] == (
        "mfa_required"
    )


# --- приложение-аутентификатор и резервные коды -----------------------------


async def test_totp_replaces_email_and_backup_codes_work_once(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    signed_in = await login(api, account.email or "", remember=False)
    secret, backup = await _enable_totp(api, _auth(signed_in))

    assert len(backup) == 10
    assert all(re.fullmatch(r"[0-9A-Z]{4}-[0-9A-Z]{4}", code) for code in backup)
    assert await _mails(session, account.email or "", "security_changed")

    step = (await login_step(api, account.email or "", remember=False)).json()["mfa"]
    assert step["methods"] == ["totp", "backup"]
    # Код на почту больше не принимается: взлом почты не обходит приложение.
    assert (await _verify(api, step["token"], "email", "000000")).status_code == 400

    by_backup = await _verify(api, step["token"], "backup", backup[0].lower())
    assert by_backup.status_code == 200
    assert await _mails(session, account.email or "", "new_device_login")

    step = (await login_step(api, account.email or "", remember=False)).json()["mfa"]
    reused = await _verify(api, step["token"], "backup", backup[0])
    assert reused.status_code == 400
    code = totp.code_at(secret, totp.current_step() + 1)
    assert (await _verify(api, step["token"], "totp", code)).status_code == 200


async def test_same_totp_code_is_not_accepted_twice(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    code = totp.code_at(TEST_TOTP_SECRET, totp.current_step())
    first = (await login_step(api, "boss@test.com", remember=False)).json()["mfa"]
    second = (await login_step(api, "boss@test.com", remember=False)).json()["mfa"]

    assert (await _verify(api, first["token"], "totp", code)).status_code == 200
    replay = await _verify(api, second["token"], "totp", code)
    assert replay.status_code == 400


async def test_totp_setup_needs_a_valid_code(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    headers = bearer(account)
    setup = (
        await api.post(
            "/api/v1/account/totp/setup", json={"password": PASSWORD}, headers=headers
        )
    ).json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/kronto%3Aworker%40test.com")

    wrong = await api.post(
        "/api/v1/account/totp/enable",
        json={"setup_token": setup["setup_token"], "code": "000000"},
        headers=headers,
    )
    assert wrong.status_code == 400
    assert wrong.json()["code"] == "invalid_second_factor"
    assert account.account is not None
    await session.refresh(account.account)
    assert account.account.totp_enabled_at is None


async def test_totp_setup_ends_after_attempts(
    api: httpx.AsyncClient, account: User
) -> None:
    """Попытки кончились — отдельный код: фронт предлагает начать заново,
    а не «код не подошёл» без конца."""
    headers = bearer(account)
    setup = (
        await api.post(
            "/api/v1/account/totp/setup", json={"password": PASSWORD}, headers=headers
        )
    ).json()
    body = {"setup_token": setup["setup_token"], "code": "000000"}
    for _ in range(5):
        await api.post("/api/v1/account/totp/enable", json=body, headers=headers)
    code = totp.code_at(setup["secret"], totp.current_step())
    late = await api.post(
        "/api/v1/account/totp/enable",
        json={**body, "code": code},
        headers=headers,
    )
    assert late.status_code == 400
    assert late.json()["code"] == "setup_expired"


async def test_disable_totp_needs_password_and_code(
    api: httpx.AsyncClient, account: User
) -> None:
    headers = bearer(account)
    secret, _ = await _enable_totp(api, headers)
    code = totp.code_at(secret, totp.current_step() + 1)

    wrong_password = await api.post(
        "/api/v1/account/totp/disable",
        json={"password": "wrong-password-123", "code": code},
        headers=headers,
    )
    done = await api.post(
        "/api/v1/account/totp/disable",
        json={"password": PASSWORD, "code": code},
        headers=headers,
    )

    assert wrong_password.status_code == 400
    assert done.status_code == 204
    security = (await api.get("/api/v1/account/security", headers=headers)).json()
    assert security["totp_enabled"] is False
    # Без надёжного фактора резервные коды не нужны.
    assert security["backup_codes_left"] == 0


async def test_admin_cannot_drop_the_last_strong_factor(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    code = totp.code_at(TEST_TOTP_SECRET, totp.current_step())
    response = await api.post(
        "/api/v1/account/totp/disable",
        json={"password": PASSWORD, "code": code},
        headers=bearer(admin_account),
    )
    assert response.status_code == 409


# --- правила: администраторам и компаниям strong ------------------------------


async def test_admin_without_strong_factor_must_set_it_up(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    assert admin_account.account is not None
    admin_account.account.totp_enabled_at = None
    await session.commit()
    headers = bearer(admin_account)

    blocked = await api.get("/api/v1/users", headers=headers)
    me = await api.get("/api/v1/auth/me", headers=headers)

    assert blocked.status_code == 403
    assert blocked.json()["code"] == "mfa_setup_required"
    assert me.json()["mfa"] == {"strong": False, "strong_required": True}

    await _enable_totp(api, headers)
    assert (await api.get("/api/v1/users", headers=headers)).status_code == 200


async def test_strong_company_policy_applies_to_employees(
    api: httpx.AsyncClient, account: User, tenant_ctx: Tenant, session: AsyncSession
) -> None:
    tenant_ctx.mfa_policy = "strong"
    await session.commit()

    response = await api.post(
        "/api/v1/faq/ask", json={"question": "?"}, headers=bearer(account)
    )
    assert response.status_code == 403
    assert response.json()["code"] == "mfa_setup_required"


# --- сброс пароля с приложением ---------------------------------------------


async def test_reset_password_needs_second_factor_when_app_is_on(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    _, backup = await _enable_totp(api, bearer(account))
    await api.post("/api/v1/auth/forgot-password", json={"email": account.email})
    mail = (await _mails(session, account.email or "", "reset_password"))[-1]
    match = re.search(r"/reset-password#token=([\w-]+)", mail.text_body)
    assert match
    body = {"token": match.group(1), "new_password": "совсем новый пароль 2026"}

    missing = await api.post("/api/v1/auth/reset-password", json=body)
    wrong = await api.post(
        "/api/v1/auth/reset-password", json={**body, "second_factor": "AAAA-AAAA"}
    )
    done = await api.post(
        "/api/v1/auth/reset-password", json={**body, "second_factor": backup[1]}
    )

    assert missing.status_code == 403
    assert missing.json()["code"] == "second_factor_required"
    assert wrong.status_code == 400
    assert done.status_code == 200


@pytest.mark.parametrize(
    "path", ["/api/v1/account/totp/setup", "/api/v1/account/passkeys/options"]
)
async def test_adding_a_second_factor_asks_for_the_password(
    api: httpx.AsyncClient, account: User, path: str
) -> None:
    """Подключить приложение или ключ — как и отключить: только с паролем.
    Одного access-токена мало, иначе чужой фактор привязывается к учётке."""
    headers = bearer(account)

    missing = await api.post(path, headers=headers)
    wrong = await api.post(
        path, json={"password": "wrong-password-123"}, headers=headers
    )
    done = await api.post(path, json={"password": PASSWORD}, headers=headers)

    assert missing.status_code == 422
    assert wrong.status_code == 400
    assert wrong.json()["code"] == "invalid_password"
    assert done.status_code == 200, done.text
    assert done.json()["setup_token"]


# --- ключи доступа ------------------------------------------------------------


async def test_passkey_registration_and_login(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    headers = bearer(account)
    device = SoftAuthenticator()

    setup = (
        await api.post(
            "/api/v1/account/passkeys/options",
            json={"password": PASSWORD},
            headers=headers,
        )
    ).json()
    assert setup["options"]["rp"]["id"] == "test"
    created = await api.post(
        "/api/v1/account/passkeys",
        json={
            "setup_token": setup["setup_token"],
            "credential": device.register(setup["options"]),
            "name": "Ноутбук",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert len(created.json()["backup_codes"]) == 10

    step = (await login_step(api, account.email or "", remember=False)).json()["mfa"]
    assert step["methods"] == ["passkey", "backup"]
    options = (
        await api.post(
            "/api/v1/auth/mfa/passkey-options", json={"token": step["token"]}
        )
    ).json()["options"]
    signed = await api.post(
        "/api/v1/auth/mfa/verify",
        json={
            "token": step["token"],
            "method": "passkey",
            "credential": device.sign(options),
        },
    )
    assert signed.status_code == 200, signed.text


async def test_foreign_passkey_signature_is_rejected(
    api: httpx.AsyncClient, account: User
) -> None:
    headers = bearer(account)
    device = SoftAuthenticator()
    setup = (
        await api.post(
            "/api/v1/account/passkeys/options",
            json={"password": PASSWORD},
            headers=headers,
        )
    ).json()
    await api.post(
        "/api/v1/account/passkeys",
        json={
            "setup_token": setup["setup_token"],
            "credential": device.register(setup["options"]),
        },
        headers=headers,
    )
    step = (await login_step(api, account.email or "", remember=False)).json()["mfa"]
    options = (
        await api.post(
            "/api/v1/auth/mfa/passkey-options", json={"token": step["token"]}
        )
    ).json()["options"]
    # Тот же идентификатор ключа, но подпись другого устройства.
    impostor = SoftAuthenticator()
    impostor.credential_id = device.credential_id
    forged = await api.post(
        "/api/v1/auth/mfa/verify",
        json={
            "token": step["token"],
            "method": "passkey",
            "credential": impostor.sign(options),
        },
    )
    assert forged.status_code == 400


async def test_passkey_for_another_site_is_rejected(
    api: httpx.AsyncClient, account: User
) -> None:
    headers = bearer(account)
    phishing = SoftAuthenticator(rp_id="evil.example", origin="https://evil.example")
    setup = (
        await api.post(
            "/api/v1/account/passkeys/options",
            json={"password": PASSWORD},
            headers=headers,
        )
    ).json()
    response = await api.post(
        "/api/v1/account/passkeys",
        json={
            "setup_token": setup["setup_token"],
            "credential": phishing.register(setup["options"]),
        },
        headers=headers,
    )
    assert response.status_code == 400


async def test_passkey_options_require_user_verification(
    api: httpx.AsyncClient, account: User
) -> None:
    headers = bearer(account)
    device = SoftAuthenticator()
    setup = (
        await api.post(
            "/api/v1/account/passkeys/options",
            json={"password": PASSWORD},
            headers=headers,
        )
    ).json()
    selection = setup["options"]["authenticatorSelection"]
    assert selection["userVerification"] == "required"
    await api.post(
        "/api/v1/account/passkeys",
        json={
            "setup_token": setup["setup_token"],
            "credential": device.register(setup["options"]),
        },
        headers=headers,
    )
    step = (await login_step(api, account.email or "", remember=False)).json()["mfa"]
    options = (
        await api.post(
            "/api/v1/auth/mfa/passkey-options", json={"token": step["token"]}
        )
    ).json()["options"]
    assert options["userVerification"] == "required"


async def test_passkey_without_user_verification_is_not_registered(
    api: httpx.AsyncClient, account: User
) -> None:
    # Ключ подтвердил только присутствие (касание), но не спросил PIN,
    # отпечаток или лицо: такой ключ не принимается.
    headers = bearer(account)
    device = SoftAuthenticator(user_verified=False)
    setup = (
        await api.post(
            "/api/v1/account/passkeys/options",
            json={"password": PASSWORD},
            headers=headers,
        )
    ).json()
    response = await api.post(
        "/api/v1/account/passkeys",
        json={
            "setup_token": setup["setup_token"],
            "credential": device.register(setup["options"]),
        },
        headers=headers,
    )
    assert response.status_code == 400
    overview = await api.get("/api/v1/account/security", headers=headers)
    assert overview.json()["passkeys"] == []


async def test_passkey_login_without_user_verification_is_rejected(
    api: httpx.AsyncClient, account: User
) -> None:
    headers = bearer(account)
    device = SoftAuthenticator()
    setup = (
        await api.post(
            "/api/v1/account/passkeys/options",
            json={"password": PASSWORD},
            headers=headers,
        )
    ).json()
    created = await api.post(
        "/api/v1/account/passkeys",
        json={
            "setup_token": setup["setup_token"],
            "credential": device.register(setup["options"]),
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    step = (await login_step(api, account.email or "", remember=False)).json()["mfa"]
    options = (
        await api.post(
            "/api/v1/auth/mfa/passkey-options", json={"token": step["token"]}
        )
    ).json()["options"]
    # Тот же ключ, но на входе владелец не подтверждён.
    device.user_verified = False
    response = await api.post(
        "/api/v1/auth/mfa/verify",
        json={
            "token": step["token"],
            "method": "passkey",
            "credential": device.sign(options),
        },
    )
    assert response.status_code == 400


# --- сеансы -------------------------------------------------------------------


async def test_sessions_list_and_end_one(api: httpx.AsyncClient, account: User) -> None:
    phone = await login(api, account.email or "", remember=False)
    laptop = await login(api, account.email or "", remember=False)

    # Cookie — из хранилища клиента, с её путём (/api/v1/auth), как в
    # браузере: текущий сеанс сервер узнаёт по ней.
    assert api.cookies.get(REFRESH_COOKIE) == refresh_token_of(laptop)
    listed = await api.get("/api/v1/auth/sessions", headers=_auth(laptop))
    sessions = listed.json()
    assert len(sessions) == 2
    assert sum(item["current"] for item in sessions) == 1
    other = next(item for item in sessions if not item["current"])

    ended = await api.post(
        f"/api/v1/auth/sessions/{other['id']}/end", headers=_auth(laptop)
    )
    assert ended.status_code == 204
    # Завершённый сеанс гаснет сразу — и его токен доступа тоже.
    assert (await api.get("/api/v1/auth/me", headers=_auth(phone))).status_code == 401
    assert (await api.get("/api/v1/auth/me", headers=_auth(laptop))).status_code == 200
    assert (await refresh_with(api, refresh_token_of(phone))).status_code == 401
    assert (await refresh_with(api, refresh_token_of(laptop))).status_code == 200


async def test_security_overview(api: httpx.AsyncClient, account: User) -> None:
    response = await api.get("/api/v1/account/security", headers=bearer(account))
    assert response.json() == {
        "totp_enabled": False,
        "passkeys": [],
        "backup_codes_left": 0,
        "strong_required": False,
    }


@pytest.mark.parametrize("method", ["sms", ""])
async def test_unknown_method_is_rejected(
    api: httpx.AsyncClient, account: User, method: str
) -> None:
    step = (await login_step(api, account.email or "")).json()["mfa"]
    response = await _verify(api, step["token"], method, "123456")
    assert response.status_code == 422
