"""Учётка kronto: регистрация, почта, пароль, компании, удаление (ТЗ §2–3).

Как и вход, эти ручки — первое, что пробует пентестер: перечисление
адресов по ответам регистрации и «забыли пароль», перебор кода из
письма, повтор ссылок, захват учётки сменой почты.
"""

import re
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.api.v1.dependencies import get_team_notifier
from corp_ed.core import totp
from corp_ed.core.config import RegistrationSettings, get_registration_settings
from corp_ed.core.security import verify_password
from corp_ed.core.tenant_context import account_scope, current_tenant, tenant_scope
from corp_ed.domain.models import (
    Account,
    CompanyRequest,
    MemberStatus,
    OutboxEmail,
    Tenant,
    User,
    UserRole,
)
from corp_ed.main import app
from corp_ed.repositories.user_repository import UserRepository
from tests.api.conftest import (
    PASSWORD,
    TEST_TOTP_SECRET,
    account_bearer,
    bearer,
    enable_test_totp,
    login,
    refresh_token_of,
    refresh_with,
)
from tests.factories import make_user
from tests.team_notify_helpers import RecordingNotifier

NEW_PASSWORD = "другая длинная фраза 2026"


def _register_body(
    email: str = "new@acme.ru", **overrides: object
) -> dict[str, object]:
    return {
        "first_name": "Анна",
        "last_name": "Петрова",
        "email": email,
        "password": PASSWORD,
        "terms": True,
        "consent": True,
        **overrides,
    }


async def _mails(
    session: AsyncSession, to: str, kind: str | None = None
) -> list[OutboxEmail]:
    query = select(OutboxEmail).where(OutboxEmail.to_email == to)
    if kind is not None:
        query = query.where(OutboxEmail.kind == kind)
    return list(await session.scalars(query.order_by(OutboxEmail.created_at)))


async def _last_mail(session: AsyncSession, to: str, kind: str) -> OutboxEmail:
    mails = await _mails(session, to, kind)
    assert mails, f"нет письма {kind} на {to}"
    return mails[-1]


def _code(mail: OutboxEmail) -> str:
    match = re.search(r"Код для подтверждения почты: (\d{6})", mail.text_body)
    assert match
    return match.group(1)


def _change_code(mail: OutboxEmail) -> str:
    match = re.search(r"Код для смены почты на \S+: (\d{6})", mail.text_body)
    assert match, mail.text_body
    return match.group(1)


def _link_token(mail: OutboxEmail, path: str) -> str:
    match = re.search(rf"{re.escape(path)}#token=([\w-]+)", mail.text_body)
    assert match, mail.text_body
    return match.group(1)


async def _account(session: AsyncSession, email: str) -> Account:
    account = await session.scalar(select(Account).where(Account.email == email))
    assert account is not None
    await session.refresh(account)
    return account


async def _registered_and_verified(
    api: httpx.AsyncClient, session: AsyncSession, email: str = "new@acme.ru"
) -> dict[str, str]:
    assert (
        await api.post("/api/v1/auth/register", json=_register_body(email))
    ).status_code == 202
    mail = await _last_mail(session, email, "verify_email")
    response = await api.post(
        "/api/v1/auth/verify-email", json={"email": email, "code": _code(mail)}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# --- регистрация и подтверждение почты ----------------------------------------


async def test_register_creates_unverified_account_and_sends_code(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    response = await api.post(
        "/api/v1/auth/register", json=_register_body("New@Acme.ru")
    )

    assert response.status_code == 202
    assert response.json() == {"email": "new@acme.ru"}
    account = await _account(session, "new@acme.ru")
    assert account.email_verified_at is None
    assert (account.first_name, account.last_name) == ("Анна", "Петрова")
    # Два отдельных согласия (ч. 1 ст. 9 152-ФЗ): соглашение и обработка
    # персональных данных — каждое со своей версией текста.
    settings = get_registration_settings()
    assert account.terms_accepted_at is not None
    assert account.terms_version == settings.terms_version
    assert account.consented_at is not None
    assert account.consent_policy_version == settings.policy_version
    assert settings.terms_version != settings.policy_version
    mail = await _last_mail(session, "new@acme.ru", "verify_email")
    assert re.fullmatch(r"\d{6}", _code(mail))
    # Без подтверждения не войти.
    refused = await login(api, "new@acme.ru")
    assert refused.status_code == 403
    assert refused.json()["code"] == "email_not_verified"


async def test_register_on_existing_address_answers_the_same(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    """Ответ не выдаёт, что адрес занят; владельцу — письмо, пароль цел."""
    response = await api.post(
        "/api/v1/auth/register",
        json=_register_body(account.email, password="чужой пароль 12345"),
    )

    assert response.status_code == 202
    assert response.json() == {"email": account.email}
    assert await _mails(session, account.email, "account_exists")
    assert await _mails(session, account.email, "verify_email") == []
    assert (await login(api, account.email)).status_code == 200


async def test_reregistering_unverified_address_overwrites_it(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    """Неподтверждённый адрес не «занят»: иначе чужой занял бы его навсегда."""
    await api.post("/api/v1/auth/register", json=_register_body())
    await api.post(
        "/api/v1/auth/register",
        json=_register_body(first_name="Настоящая", password=NEW_PASSWORD),
    )

    account = await _account(session, "new@acme.ru")
    assert account.first_name == "Настоящая"
    assert verify_password(NEW_PASSWORD, account.hashed_password)
    first, second = await _mails(session, "new@acme.ru", "verify_email")
    # Действует только последнее письмо.
    stale = await api.post(
        "/api/v1/auth/verify-email", json={"email": "new@acme.ru", "code": _code(first)}
    )
    if _code(first) != _code(second):
        assert stale.status_code == 400


@pytest.mark.parametrize("missing", ["terms", "consent"])
async def test_register_needs_both_agreements(
    api: httpx.AsyncClient, session: AsyncSession, missing: str
) -> None:
    """Галочки две и обе обязательны: без любой — учётки нет."""
    body = _register_body()
    del body[missing]
    response = await api.post("/api/v1/auth/register", json=body)
    assert response.status_code == 422
    assert (
        await session.scalar(select(Account).where(Account.email == "new@acme.ru"))
        is None
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"consent": False},
        {"terms": False},
        {"terms": None},
        {"password": "short"},
        {"first_name": ""},
        {"last_name": "   "},
        {"email": "not-an-email"},
        {"role": "admin"},
    ],
)
async def test_register_validation(
    api: httpx.AsyncClient, overrides: dict[str, object]
) -> None:
    response = await api.post("/api/v1/auth/register", json=_register_body(**overrides))
    assert response.status_code == 422


async def test_closed_registration_lets_only_invited_in(
    api: httpx.AsyncClient,
    admin_account: User,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "corp_ed.services.account_service.get_registration_settings",
        lambda: RegistrationSettings(enabled=False),
    )
    closed = await api.post("/api/v1/auth/register", json=_register_body())
    assert closed.status_code == 403
    assert closed.json()["code"] == "registration_closed"

    invite = await api.post("/api/v1/invites", json={}, headers=bearer(admin_account))
    invited = await api.post(
        "/api/v1/auth/register",
        json=_register_body(invite=invite.json()["code"]),
    )
    assert invited.status_code == 202


async def test_verify_by_code_opens_a_session_without_company(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    headers = await _registered_and_verified(api, session)

    me = (await api.get("/api/v1/auth/me", headers=headers)).json()
    assert me["company"] is None
    assert me["companies"] == []
    assert me["first_name"] == "Анна"
    ask = await api.post("/api/v1/faq/ask", json={"question": "?"}, headers=headers)
    assert ask.status_code == 403
    assert ask.json()["code"] == "no_company"
    assert (await _account(session, "new@acme.ru")).email_verified_at is not None


async def test_wrong_codes_burn_the_letter(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    """5 попыток на письмо: дальше и верный код не поможет."""
    await api.post("/api/v1/auth/register", json=_register_body())
    right = _code(await _last_mail(session, "new@acme.ru", "verify_email"))
    wrong = f"{(int(right) + 1) % 1_000_000:06d}"

    statuses = [
        (
            await api.post(
                "/api/v1/auth/verify-email",
                json={"email": "new@acme.ru", "code": wrong},
            )
        ).status_code
        for _ in range(5)
    ]
    late = await api.post(
        "/api/v1/auth/verify-email", json={"email": "new@acme.ru", "code": right}
    )

    assert statuses == [400] * 5
    assert late.status_code == 400
    assert late.json()["code"] == "invalid_code"


async def test_verify_by_link_and_link_is_single_use(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    await api.post("/api/v1/auth/register", json=_register_body())
    token = _link_token(
        await _last_mail(session, "new@acme.ru", "verify_email"), "/verify-email"
    )

    first = await api.post("/api/v1/auth/verify-email/link", json={"token": token})
    again = await api.post("/api/v1/auth/verify-email/link", json={"token": token})

    assert first.status_code == 200
    assert again.status_code == 400


async def test_resend_is_silent_for_unknown_and_verified(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    for email in ("ghost@acme.ru", account.email):
        response = await api.post(
            "/api/v1/auth/verify-email/resend", json={"email": email}
        )
        assert response.status_code == 202
        assert await _mails(session, email) == []


# --- пароль -------------------------------------------------------------------


async def test_forgot_and_reset_password(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    old_session = await login(api, account.email)
    unknown = await api.post(
        "/api/v1/auth/forgot-password", json={"email": "ghost@acme.ru"}
    )
    known = await api.post(
        "/api/v1/auth/forgot-password", json={"email": account.email}
    )
    assert unknown.status_code == known.status_code == 202
    assert await _mails(session, "ghost@acme.ru") == []
    token = _link_token(
        await _last_mail(session, account.email, "reset_password"), "/reset-password"
    )

    reset = await api.post(
        "/api/v1/auth/reset-password",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    reused = await api.post(
        "/api/v1/auth/reset-password",
        json={"token": token, "new_password": NEW_PASSWORD},
    )

    assert reset.status_code == 200
    assert reused.status_code == 400
    assert (await login(api, account.email)).status_code == 401
    assert (await login(api, account.email, NEW_PASSWORD)).status_code == 200
    # Прежние сессии закрыты.
    stale = await refresh_with(api, refresh_token_of(old_session))
    assert stale.status_code == 401


async def test_reset_password_follows_policy(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    await api.post("/api/v1/auth/forgot-password", json={"email": account.email})
    token = _link_token(
        await _last_mail(session, account.email, "reset_password"), "/reset-password"
    )
    weak = await api.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": "short"}
    )
    assert weak.status_code == 422


async def test_reset_password_cannot_keep_the_old_one(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    """Сброс по ссылке на тот же пароль — отказ; ссылка остаётся рабочей."""
    await api.post("/api/v1/auth/forgot-password", json={"email": account.email})
    token = _link_token(
        await _last_mail(session, account.email, "reset_password"), "/reset-password"
    )

    same = await api.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": PASSWORD}
    )
    assert same.status_code == 422
    assert "уже был" in same.json()["detail"]

    fresh = await api.post(
        "/api/v1/auth/reset-password",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    assert fresh.status_code == 200


async def test_change_password_sends_a_notice(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    response = await api.post(
        "/api/v1/auth/change-password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=bearer(account),
    )
    assert response.status_code == 200
    assert await _mails(session, account.email, "password_changed")


# --- смена почты --------------------------------------------------------------


async def test_change_email_confirm_and_revert(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    """Без приложения второй фактор — код на прежний адрес (ТЗ §3)."""
    old_email = account.email
    assert old_email is not None
    headers = bearer(account)

    wrong = await api.post(
        "/api/v1/account/email",
        json={"new_email": "anna@new.ru", "password": "wrong-password-123"},
        headers=headers,
    )
    assert wrong.status_code == 400
    assert wrong.json()["code"] == "invalid_password"
    asked = await api.post(
        "/api/v1/account/email",
        json={"new_email": "Anna@New.ru", "password": PASSWORD},
        headers=headers,
    )
    assert asked.status_code == 202
    assert asked.json() == {
        "status": "code_sent",
        "email_hint": f"{old_email[0]}***@{old_email.split('@')[1]}",
    }
    # Ссылки на новый адрес ещё нет — только код на прежний.
    assert not await _mails(session, "anna@new.ru")
    code = _change_code(await _last_mail(session, old_email, "change_email_code"))

    # Код к одному адресу не подтверждает смену на другой.
    other = await api.post(
        "/api/v1/account/email",
        json={"new_email": "evil@new.ru", "password": PASSWORD, "code": code},
        headers=headers,
    )
    assert other.status_code == 400
    sent = await api.post(
        "/api/v1/account/email",
        json={"new_email": "anna@new.ru", "password": PASSWORD, "code": code},
        headers=headers,
    )
    assert sent.status_code == 202
    assert sent.json() == {"status": "link_sent", "email_hint": None}
    # Почта не меняется, пока новый адрес не подтверждён.
    assert (await _account(session, old_email)).email == old_email

    confirm = _link_token(
        await _last_mail(session, "anna@new.ru", "change_email"), "/confirm-email"
    )
    assert (
        await api.post("/api/v1/account/email/confirm", json={"token": confirm})
    ).status_code == 204
    assert (await login(api, "anna@new.ru")).status_code == 200

    revert = _link_token(
        await _last_mail(session, old_email, "email_changed"), "/revert-email"
    )
    assert (
        await api.post("/api/v1/account/email/revert", json={"token": revert})
    ).status_code == 204
    restored = await _account(session, old_email)
    assert restored.email == old_email
    # Захвативший мог знать пароль: сессии закрыты, ссылка на новый пароль — владельцу.
    assert (await api.get("/api/v1/auth/me", headers=headers)).status_code == 401
    assert await _mails(session, old_email, "reset_password")


async def _change_email(
    api: httpx.AsyncClient, session: AsyncSession, account: User, new_email: str
) -> httpx.Response:
    """Смена почты до письма со ссылкой: код на прежний адрес и ссылка."""
    old_email = account.email
    assert old_email is not None
    headers = bearer(account)
    body = {"new_email": new_email, "password": PASSWORD}
    asked = await api.post("/api/v1/account/email", json=body, headers=headers)
    assert asked.status_code == 202, asked.text
    code = _change_code(await _last_mail(session, old_email, "change_email_code"))
    return await api.post(
        "/api/v1/account/email", json={**body, "code": code}, headers=headers
    )


async def test_unverified_signup_does_not_block_email_change_or_revert(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    """Регистрация без подтверждения адрес не занимает (ТЗ §3): ни новый
    адрес при смене почты, ни прежний — для «это не я» из письма."""
    old_email = account.email
    assert old_email is not None
    assert (
        await api.post("/api/v1/auth/register", json=_register_body("anna@new.ru"))
    ).status_code == 202

    sent = await _change_email(api, session, account, "anna@new.ru")
    assert sent.status_code == 202, sent.text
    confirm = _link_token(
        await _last_mail(session, "anna@new.ru", "change_email"), "/confirm-email"
    )
    confirmed = await api.post("/api/v1/account/email/confirm", json={"token": confirm})
    assert confirmed.status_code == 204, confirmed.text

    # Кто-то регистрируется на прежний адрес и не подтверждает его.
    assert (
        await api.post("/api/v1/auth/register", json=_register_body(old_email))
    ).status_code == 202
    revert = _link_token(
        await _last_mail(session, old_email, "email_changed"), "/revert-email"
    )
    reverted = await api.post("/api/v1/account/email/revert", json={"token": revert})

    assert reverted.status_code == 204, reverted.text
    accounts = (
        await session.scalars(select(Account).where(Account.email == old_email))
    ).all()
    assert len(accounts) == 1
    assert accounts[0].email_verified_at is not None


async def test_change_email_code_attempts_are_limited(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    assert account.email is not None
    headers = bearer(account)
    body = {"new_email": "anna@new.ru", "password": PASSWORD}
    await api.post("/api/v1/account/email", json=body, headers=headers)
    code = _change_code(await _last_mail(session, account.email, "change_email_code"))

    for _ in range(5):
        wrong = await api.post(
            "/api/v1/account/email", json={**body, "code": "000000"}, headers=headers
        )
        assert wrong.status_code == 400
    # Попытки кончились — и верный код уже не сработает.
    late = await api.post(
        "/api/v1/account/email", json={**body, "code": code}, headers=headers
    )
    assert late.status_code == 400
    assert not await _mails(session, "anna@new.ru")


async def test_change_email_with_app_needs_app_code(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    """С приложением второй фактор — его код или резервный; письма с
    кодом на прежний адрес нет."""
    assert account.account is not None and account.email is not None
    enable_test_totp(account.account)
    await session.commit()
    headers = bearer(account)
    body = {"new_email": "anna@new.ru", "password": PASSWORD}

    missing = await api.post("/api/v1/account/email", json=body, headers=headers)
    assert missing.status_code == 403
    assert missing.json()["code"] == "second_factor_required"
    wrong = await api.post(
        "/api/v1/account/email", json={**body, "code": "000000"}, headers=headers
    )
    assert wrong.status_code == 400
    assert wrong.json()["code"] == "invalid_second_factor"
    code = totp.code_at(TEST_TOTP_SECRET, totp.current_step())
    sent = await api.post(
        "/api/v1/account/email", json={**body, "code": code}, headers=headers
    )
    assert sent.status_code == 202
    assert sent.json()["status"] == "link_sent"
    assert not await _mails(session, account.email, "change_email_code")
    assert await _mails(session, "anna@new.ru", "change_email")


async def test_change_email_to_taken_address_is_conflict(
    api: httpx.AsyncClient, account: User, admin_account: User
) -> None:
    response = await api.post(
        "/api/v1/account/email",
        json={"new_email": admin_account.email, "password": PASSWORD},
        headers=bearer(account),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "email_taken"


# --- компании -----------------------------------------------------------------


async def _second_company(session: AsyncSession, member: User) -> Tenant:
    other = Tenant(id=uuid4(), company_code="second", name="Second Co")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        session.add(make_user(email="x", account=member.account))
        await session.commit()
    return other


async def test_switch_between_own_companies(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    other = await _second_company(session, account)
    signed_in = await login(api, account.email)
    headers = {"Authorization": f"Bearer {signed_in.json()['access_token']}"}

    me = (await api.get("/api/v1/auth/me", headers=headers)).json()
    assert {c["company_name"] for c in me["companies"]} == {"Test Co", "Second Co"}

    switched = await api.post(
        "/api/v1/auth/switch-company",
        json={"tenant_id": str(other.id)},
        headers=headers,
    )
    assert switched.status_code == 200
    new = {"Authorization": f"Bearer {switched.json()['access_token']}"}
    assert (await api.get("/api/v1/auth/me", headers=new)).json()["company"][
        "name"
    ] == ("Second Co")
    # Следующий вход открывает последнюю выбранную компанию.
    again = await login(api, account.email)
    again_headers = {"Authorization": f"Bearer {again.json()['access_token']}"}
    me_again = (await api.get("/api/v1/auth/me", headers=again_headers)).json()
    assert me_again["company"]["name"] == "Second Co"


async def test_cannot_switch_into_a_foreign_company(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    stranger = Tenant(id=uuid4(), company_code="foreign", name="Foreign")
    session.add(stranger)
    await session.commit()

    response = await api.post(
        "/api/v1/auth/switch-company",
        json={"tenant_id": str(stranger.id)},
        headers=bearer(account),
    )
    assert response.status_code == 404


async def test_own_memberships_are_visible_across_companies_only_to_owner(
    session: AsyncSession, account: User, admin_account: User
) -> None:
    """RLS own_membership: свои членства во всех компаниях, чужие — нет."""
    await _second_company(session, account)
    assert account.account is not None and admin_account.account is not None
    repo = UserRepository(session)

    # Без компании в контексте — как в ручках учётки: tenant_isolation не
    # показывает ничего, own_membership — только своё.
    no_company = current_tenant.set(None)
    try:
        with account_scope(account.account.id):
            mine = await repo.memberships_of_account(account.account.id)
            theirs = await repo.memberships_of_account(admin_account.account.id)
    finally:
        current_tenant.reset(no_company)

    assert len(mine) == 2
    assert theirs == []


async def test_leave_company_keeps_the_account(
    api: httpx.AsyncClient, account: User, admin_account: User, session: AsyncSession
) -> None:
    headers = bearer(account)
    response = await api.post(
        "/api/v1/account/leave",
        json={"tenant_id": str(account.tenant_id)},
        headers=headers,
    )
    assert response.status_code == 204

    assert (await api.get("/api/v1/auth/me", headers=headers)).status_code == 401
    signed_in = await login(api, account.email)
    me = await api.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {signed_in.json()['access_token']}"},
    )
    assert me.json()["company"] is None
    await session.refresh(account)
    assert account.status is MemberStatus.LEFT


async def test_last_admin_cannot_leave(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    response = await api.post(
        "/api/v1/account/leave",
        json={"tenant_id": str(admin_account.tenant_id)},
        headers=bearer(admin_account),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "last_admin"


# --- удаление учётки ----------------------------------------------------------


async def test_delete_account(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    headers = bearer(account)
    wrong = await api.post(
        "/api/v1/account/delete",
        json={"password": "wrong-password-123"},
        headers=headers,
    )
    assert wrong.status_code == 400

    response = await api.post(
        "/api/v1/account/delete", json={"password": PASSWORD}, headers=headers
    )
    assert response.status_code == 204
    assert (
        await session.scalar(select(Account).where(Account.email == account.email))
        is None
    )
    with tenant_scope(account.tenant_id):
        member = await session.scalar(
            select(User)
            .where(User.id == account.id)
            .execution_options(populate_existing=True)
        )
    assert member is not None
    assert member.status is MemberStatus.LEFT
    assert member.account_id is None
    assert (await login(api, "worker@test.com")).status_code == 401


async def test_last_admin_cannot_delete_account(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    response = await api.post(
        "/api/v1/account/delete",
        json={"password": PASSWORD},
        headers=bearer(admin_account),
    )
    assert response.status_code == 409
    assert "Test Co" in response.json()["detail"]


async def test_update_name(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    response = await api.patch(
        "/api/v1/account",
        json={"first_name": "  Мария ", "last_name": "Иванова"},
        headers=bearer(account),
    )
    assert response.status_code == 204
    assert account.email is not None
    updated = await _account(session, account.email)
    assert (updated.first_name, updated.last_name) == ("Мария", "Иванова")


# --- заявки «Подключить компанию» ---------------------------------------------


async def test_company_request_lifecycle(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    sent: list[str] = []
    app.dependency_overrides[get_team_notifier] = lambda: RecordingNotifier(sent)
    headers = await _registered_and_verified(api, session, "founder@acme.ru")

    created = await api.post(
        "/api/v1/account/company-requests",
        json={"company_name": "ООО «Ромашка»", "seats": 25, "comment": "Пилот"},
        headers=headers,
    )
    duplicate = await api.post(
        "/api/v1/account/company-requests",
        json={"company_name": "Вторая"},
        headers=headers,
    )

    assert created.status_code == 201
    assert created.json()["status"] == "new"
    assert duplicate.status_code == 409
    # В Telegram — без названия и контактов.
    assert len(sent) == 1
    assert "Ромашка" not in sent[0] and "founder" not in sent[0]

    from corp_ed.repositories.audit_repository import AuditRepository
    from corp_ed.services.company_request_service import CompanyRequestService

    tenant = await CompanyRequestService(session, AuditRepository(session)).approve(
        created.json()["id"]
    )

    assert tenant.name == "ООО «Ромашка»"
    assert tenant.seats == 25
    assert tenant.company_code.startswith("ooo-romashka-")
    assert await _mails(session, "founder@acme.ru", "company_approved")
    signed_in = await login(api, "founder@acme.ru")
    me = await api.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {signed_in.json()['access_token']}"},
    )
    assert me.json()["company"]["name"] == "ООО «Ромашка»"
    assert me.json()["company"]["role"] == "admin"
    listed = await api.get("/api/v1/account/company-requests", headers=headers)
    assert [r["status"] for r in listed.json()] == ["approved"]


async def test_company_request_can_be_cancelled_and_rejected(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    headers = await _registered_and_verified(api, session, "founder@acme.ru")
    first = await api.post(
        "/api/v1/account/company-requests",
        json={"company_name": "Первая"},
        headers=headers,
    )
    cancelled = await api.post(
        f"/api/v1/account/company-requests/{first.json()['id']}/cancel", headers=headers
    )
    assert cancelled.json()["status"] == "cancelled"

    second = await api.post(
        "/api/v1/account/company-requests",
        json={"company_name": "Вторая"},
        headers=headers,
    )
    from corp_ed.repositories.audit_repository import AuditRepository
    from corp_ed.services.company_request_service import CompanyRequestService

    await CompanyRequestService(session, AuditRepository(session)).reject(
        second.json()["id"]
    )
    assert await _mails(session, "founder@acme.ru", "company_rejected")
    request = await session.get(CompanyRequest, second.json()["id"])
    assert request is not None and request.status == "rejected"


async def test_foreign_company_request_cannot_be_cancelled(
    api: httpx.AsyncClient, session: AsyncSession, account: User
) -> None:
    headers = await _registered_and_verified(api, session, "founder@acme.ru")
    created = await api.post(
        "/api/v1/account/company-requests",
        json={"company_name": "Своя"},
        headers=headers,
    )
    assert account.account is not None
    response = await api.post(
        f"/api/v1/account/company-requests/{created.json()['id']}/cancel",
        headers=account_bearer(account.account),
    )
    assert response.status_code == 404


# --- лимиты -------------------------------------------------------------------


async def test_mail_to_one_address_is_rate_limited(api: httpx.AsyncClient) -> None:
    """«Почтовая бомба» на чужой адрес упирается в лимит по адресу."""
    statuses = [
        (
            await api.post(
                "/api/v1/auth/forgot-password", json={"email": "victim@acme.ru"}
            )
        ).status_code
        for _ in range(6)
    ]
    assert statuses == [202] * 5 + [429]


async def test_admin_role_is_per_company(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    """Админ в одной компании — сотрудник в другой: роль у членства."""
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        elsewhere = make_user(
            email="x", account=admin_account.account, role=UserRole.EMPLOYEE
        )
        session.add(elsewhere)
        await session.commit()

    assert (
        await api.get("/api/v1/users", headers=bearer(admin_account))
    ).status_code == 200
    assert (
        await api.get("/api/v1/users", headers=bearer(elsewhere))
    ).status_code == 403
