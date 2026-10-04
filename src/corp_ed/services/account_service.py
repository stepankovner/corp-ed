"""Учётная запись: регистрация, почта, пароль, удаление (ТЗ §2–3).

Ответы, по которым можно узнать, зарегистрирован ли адрес, одинаковые:
регистрация, повторное письмо и «забыли пароль» всегда отвечают «письмо
отправлено», а владельцу адреса уходит письмо по ситуации.

Ссылки из писем — с токеном после «#» (/verify-email#…): фрагмент не
уходит на сервер, в журналы доступа и в Referer; фронт отправляет его
телом запроса.
"""

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import get_registration_settings
from corp_ed.core.exceptions import (
    EmailTakenError,
    InvalidEmailCodeError,
    LastAdminError,
    NotFoundError,
    PermissionError,
)
from corp_ed.core.password_policy import validate_password
from corp_ed.core.profile import normalize_phone, normalize_telegram
from corp_ed.core.security import (
    hash_password,
    hash_refresh_token,
    hash_secret,
    new_numeric_code,
    new_refresh_token,
    verify_password,
)
from corp_ed.core.tenant_context import account_scope, tenant_scope
from corp_ed.domain.models import (
    Account,
    EmailToken,
    EmailTokenPurpose,
    MemberStatus,
    User,
    UserRole,
)
from corp_ed.repositories.account_repository import AccountRepository, normalize_email
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.email_token_repository import EmailTokenRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services import email_templates
from corp_ed.services.auth_service import (
    AuthService,
    TokenPair,
)
from corp_ed.services.email_service import EmailService
from corp_ed.services.mfa_service import (
    InvalidPasswordError,
    InvalidSecondFactorError,
    MfaService,
    mask_email,
)
from corp_ed.services.passwords import set_password

logger = structlog.get_logger()

VERIFY_TTL = timedelta(minutes=30)
RESET_TTL = timedelta(hours=1)
CHANGE_EMAIL_TTL = timedelta(hours=24)
CHANGE_EMAIL_CODE_TTL = timedelta(minutes=10)
REVERT_EMAIL_TTL = timedelta(days=7)
MAX_CODE_ATTEMPTS = 5
"""Попыток ввести код на одно письмо: 6 цифр — миллион вариантов,
пять попыток — шанс угадать 1 к 200 000; дальше — новое письмо."""


class RegistrationClosedError(PermissionError):
    """Открытая регистрация выключена: только по приглашению (ТЗ §11)."""

    code = "registration_closed"

    def __init__(self) -> None:
        super().__init__(
            "Регистрация пока только по приглашению — попросите ссылку у "
            "администратора компании"
        )


class SecondFactorRequiredError(PermissionError):
    """Сброс пароля учётки с приложением или ключом требует ещё и код
    приложения или резервный код (ТЗ §3): иначе взлом почты обходил бы
    второй фактор. HTTP 403."""

    code = "second_factor_required"

    def __init__(self) -> None:
        super().__init__("Введите код из приложения-аутентификатора или резервный код")


@dataclass(frozen=True)
class EmailChangeStep:
    """code_sent — код ушёл на прежний адрес, ждём его; link_sent —
    ссылка ушла на новый адрес."""

    status: Literal["code_sent", "link_sent"]
    email_hint: str | None


class AccountService:
    def __init__(
        self,
        session: AsyncSession,
        auth: AuthService,
        audit: AuditRepository,
        mfa: MfaService,
    ) -> None:
        self.session = session
        self.auth = auth
        self.audit = audit
        self.mfa = mfa
        self.accounts = AccountRepository(session)
        self.tokens = EmailTokenRepository(session)
        self.users = UserRepository(session)
        self.tenants = TenantRepository(session)
        self.mail = EmailService(session)

    # --- регистрация и подтверждение почты ------------------------------------

    async def register(
        self,
        *,
        first_name: str,
        last_name: str,
        email: str,
        password: str,
        invited: bool,
    ) -> None:
        """Завести учётку и отправить код подтверждения.

        invited — человек пришёл по действующему приглашению (проверено
        вызывающим): при выключенной открытой регистрации пускаем только
        его. Учётка без подтверждённой почты войти не может.
        """
        settings = get_registration_settings()
        if not settings.enabled and not invited:
            raise RegistrationClosedError()
        email = normalize_email(email)
        validate_password(password, email=email)
        # Хеш считается всегда: время ответа не должно выдавать, есть ли
        # уже учётка с этой почтой.
        hashed = hash_password(password)
        now = _now()

        account = await self.accounts.get_by_email(email)
        if account is not None and account.email_verified_at is not None:
            self.mail.enqueue(
                account.email,
                email_templates.account_exists(
                    name=account.first_name,
                    login_url=self.mail.url("/login"),
                    reset_url=self.mail.url("/forgot-password"),
                ),
            )
            await self.session.commit()
            logger.info("register_existing_account", account_id=str(account.id))
            return

        if account is None:
            account = await self.accounts.add(
                Account(email=email, hashed_password=hashed)
            )
        # Неподтверждённую учётку перезаписывает тот, кто регистрируется
        # снова: иначе чужой человек занял бы адрес, не владея им.
        account.hashed_password = hashed
        account.first_name = _clean(first_name)
        account.last_name = _clean(last_name)
        account.consented_at = now
        account.consent_policy_version = settings.policy_version
        await self._send_verification(account)
        self.audit.record(
            AuditAction.ACCOUNT_REGISTERED, details={"account_id": str(account.id)}
        )
        await self.session.commit()
        logger.info("account_registered", account_id=str(account.id))

    async def resend_verification(self, email: str) -> None:
        account = await self.accounts.get_by_email(email)
        if account is None or account.email_verified_at is not None:
            return
        await self._send_verification(account)
        await self.session.commit()

    async def verify_by_code(self, email: str, code: str) -> TokenPair:
        account = await self.accounts.get_by_email(email)
        if account is None or account.email_verified_at is not None:
            raise InvalidEmailCodeError()
        token = await self.tokens.latest_active(
            account.id, EmailTokenPurpose.VERIFY_EMAIL.value, _now()
        )
        if token is None or token.code_hash is None:
            raise InvalidEmailCodeError()
        token.attempts += 1
        if token.attempts > MAX_CODE_ATTEMPTS:
            token.used_at = _now()
            await self.session.commit()
            raise InvalidEmailCodeError()
        expected = hash_secret(f"{account.id}:{code.strip()}")
        if not hmac.compare_digest(expected, token.code_hash):
            await self.session.commit()
            raise InvalidEmailCodeError()
        return await self._verified(account, token)

    async def verify_by_link(self, raw_token: str) -> TokenPair:
        token = await self._active_token(raw_token, EmailTokenPurpose.VERIFY_EMAIL)
        account = await self.accounts.get(token.account_id)
        if account is None:
            raise InvalidEmailCodeError()
        return await self._verified(account, token)

    async def _verified(self, account: Account, token: EmailToken) -> TokenPair:
        token.used_at = _now()
        if account.email_verified_at is None:
            account.email_verified_at = _now()
        self.audit.record(
            AuditAction.ACCOUNT_EMAIL_VERIFIED, details={"account_id": str(account.id)}
        )
        return await self.auth.open_session(account, None)

    async def _send_verification(self, account: Account) -> None:
        now = _now()
        await self.tokens.invalidate(
            account.id, EmailTokenPurpose.VERIFY_EMAIL.value, now
        )
        raw = new_refresh_token()
        code = new_numeric_code()
        await self.tokens.add(
            EmailToken(
                account_id=account.id,
                purpose=EmailTokenPurpose.VERIFY_EMAIL.value,
                token_hash=hash_refresh_token(raw),
                code_hash=hash_secret(f"{account.id}:{code}"),
                expires_at=now + VERIFY_TTL,
            )
        )
        self.mail.enqueue(
            account.email,
            email_templates.verify_email(
                name=account.first_name,
                code=code,
                url=self.mail.url(f"/verify-email#token={raw}"),
                minutes=int(VERIFY_TTL.total_seconds() // 60),
            ),
        )

    # --- пароль ---------------------------------------------------------------

    async def forgot_password(self, email: str) -> None:
        account = await self.accounts.get_by_email(email)
        if account is None:
            return
        await self._send_reset(account)
        self.audit.record(
            AuditAction.PASSWORD_RESET_REQUESTED,
            details={"account_id": str(account.id)},
        )
        await self.session.commit()

    async def reset_password(
        self, raw_token: str, new_password: str, second_factor: str | None = None
    ) -> TokenPair:
        """Новый пароль по ссылке из письма. Все прежние сессии закрываются;
        ссылка доказывает владение почтой — неподтверждённая почта
        становится подтверждённой. С приложением или ключом — ещё и код
        приложения или резервный (second_factor)."""
        token = await self._active_token(raw_token, EmailTokenPurpose.RESET_PASSWORD)
        account = await self.accounts.get(token.account_id)
        if account is None:
            raise InvalidEmailCodeError()
        validate_password(new_password, email=account.email)
        if await self.mfa.has_strong(account):
            if not second_factor:
                raise SecondFactorRequiredError()
            if not await self.mfa.verify_code(account, second_factor):
                token.attempts += 1
                if token.attempts >= MAX_CODE_ATTEMPTS:
                    token.used_at = _now()
                await self.session.commit()
                raise InvalidSecondFactorError()
        set_password(account, new_password)
        account.must_change_password = False
        if account.email_verified_at is None:
            account.email_verified_at = _now()
        token.used_at = _now()
        await self.auth.invalidate_sessions(account)
        self.audit.record(
            AuditAction.PASSWORD_RESET_DONE, details={"account_id": str(account.id)}
        )
        member = await self.auth.default_membership(account)
        return await self.auth.open_session(account, member)

    async def _send_reset(self, account: Account) -> None:
        now = _now()
        await self.tokens.invalidate(
            account.id, EmailTokenPurpose.RESET_PASSWORD.value, now
        )
        raw = new_refresh_token()
        await self.tokens.add(
            EmailToken(
                account_id=account.id,
                purpose=EmailTokenPurpose.RESET_PASSWORD.value,
                token_hash=hash_refresh_token(raw),
                expires_at=now + RESET_TTL,
            )
        )
        self.mail.enqueue(
            account.email,
            email_templates.reset_password(
                name=account.first_name,
                url=self.mail.url(f"/reset-password#token={raw}"),
                minutes=int(RESET_TTL.total_seconds() // 60),
            ),
        )

    # --- смена почты ----------------------------------------------------------

    async def request_email_change(
        self,
        account: Account,
        new_email: str,
        password: str,
        second_factor: str | None = None,
    ) -> EmailChangeStep:
        """Смена почты (ТЗ §3): пароль и второй фактор, затем письмо со
        ссылкой на новый адрес. Почта меняется только после перехода по
        ней — опечатка в адресе не отрежет человека от учётки.

        Второй фактор — код приложения или резервный, если они включены;
        иначе код на прежний адрес: первый вызов без кода его отправляет
        (code_sent), второй — с кодом — отправляет ссылку (link_sent).
        """
        if not verify_password(password, account.hashed_password):
            raise InvalidPasswordError()
        new_email = normalize_email(new_email)
        if new_email == account.email:
            raise EmailTakenError()
        if await self.accounts.get_by_email(new_email) is not None:
            raise EmailTakenError()
        if await self.mfa.has_strong(account):
            if not second_factor:
                raise SecondFactorRequiredError()
            if not await self.mfa.verify_code(account, second_factor):
                await self.session.commit()
                raise InvalidSecondFactorError()
        elif not second_factor:
            await self._send_change_code(account, new_email)
            return EmailChangeStep(
                status="code_sent", email_hint=mask_email(account.email)
            )
        else:
            await self._check_change_code(account, new_email, second_factor)
        now = _now()
        await self.tokens.invalidate(
            account.id, EmailTokenPurpose.CHANGE_EMAIL.value, now
        )
        raw = new_refresh_token()
        await self.tokens.add(
            EmailToken(
                account_id=account.id,
                purpose=EmailTokenPurpose.CHANGE_EMAIL.value,
                token_hash=hash_refresh_token(raw),
                email=new_email,
                expires_at=now + CHANGE_EMAIL_TTL,
            )
        )
        self.mail.enqueue(
            new_email,
            email_templates.confirm_new_email(
                name=account.first_name,
                new_email=new_email,
                url=self.mail.url(f"/confirm-email#token={raw}"),
                hours=int(CHANGE_EMAIL_TTL.total_seconds() // 3600),
            ),
        )
        await self.session.commit()
        return EmailChangeStep(status="link_sent", email_hint=None)

    async def _send_change_code(self, account: Account, new_email: str) -> None:
        now = _now()
        await self.tokens.invalidate(
            account.id, EmailTokenPurpose.CHANGE_EMAIL_CODE.value, now
        )
        code = new_numeric_code()
        await self.tokens.add(
            EmailToken(
                account_id=account.id,
                purpose=EmailTokenPurpose.CHANGE_EMAIL_CODE.value,
                # Ссылки у кода нет: хеш случайного значения, которое не
                # уходит никуда, — только чтобы строка была уникальной.
                token_hash=hash_refresh_token(new_refresh_token()),
                code_hash=hash_secret(f"{account.id}:{code}"),
                email=new_email,
                expires_at=now + CHANGE_EMAIL_CODE_TTL,
            )
        )
        self.mail.enqueue(
            account.email,
            email_templates.email_change_code(
                name=account.first_name,
                code=code,
                new_email=new_email,
                minutes=int(CHANGE_EMAIL_CODE_TTL.total_seconds() // 60),
            ),
        )
        await self.session.commit()

    async def _check_change_code(
        self, account: Account, new_email: str, code: str
    ) -> None:
        """Код с прежнего адреса — для того же нового адреса, что в запросе:
        иначе код к одному адресу подтвердил бы смену на другой."""
        token = await self.tokens.latest_active(
            account.id, EmailTokenPurpose.CHANGE_EMAIL_CODE.value, _now()
        )
        if token is None or token.code_hash is None or token.email != new_email:
            raise InvalidEmailCodeError()
        token.attempts += 1
        if token.attempts > MAX_CODE_ATTEMPTS:
            token.used_at = _now()
            await self.session.commit()
            raise InvalidEmailCodeError()
        expected = hash_secret(f"{account.id}:{code.strip()}")
        if not hmac.compare_digest(expected, token.code_hash):
            await self.session.commit()
            raise InvalidEmailCodeError()
        token.used_at = _now()

    async def confirm_email_change(self, raw_token: str) -> None:
        """Сменить почту; на старую — письмо со ссылкой «это не я»."""
        token = await self._active_token(raw_token, EmailTokenPurpose.CHANGE_EMAIL)
        account = await self.accounts.get(token.account_id)
        if account is None or token.email is None:
            raise InvalidEmailCodeError()
        if await self.accounts.get_by_email(token.email) is not None:
            raise EmailTakenError()
        now = _now()
        old_email = account.email
        account.email = token.email
        account.email_verified_at = now
        token.used_at = now
        raw = new_refresh_token()
        await self.tokens.add(
            EmailToken(
                account_id=account.id,
                purpose=EmailTokenPurpose.REVERT_EMAIL.value,
                token_hash=hash_refresh_token(raw),
                email=old_email,
                expires_at=now + REVERT_EMAIL_TTL,
            )
        )
        self.mail.enqueue(
            old_email,
            email_templates.email_changed(
                name=account.first_name,
                new_email=account.email,
                revert_url=self.mail.url(f"/revert-email#token={raw}"),
                days=REVERT_EMAIL_TTL.days,
            ),
        )
        self.audit.record(
            AuditAction.ACCOUNT_EMAIL_CHANGED, details={"account_id": str(account.id)}
        )
        await self.session.commit()

    async def revert_email_change(self, raw_token: str) -> None:
        """«Это не я»: вернуть прежнюю почту, закрыть все сеансы и прислать
        ссылку для нового пароля — тот, кто сменил почту, мог знать и
        пароль."""
        token = await self._active_token(raw_token, EmailTokenPurpose.REVERT_EMAIL)
        account = await self.accounts.get(token.account_id)
        if account is None or token.email is None:
            raise InvalidEmailCodeError()
        taken = await self.accounts.get_by_email(token.email)
        if taken is not None and taken.id != account.id:
            raise EmailTakenError()
        account.email = token.email
        token.used_at = _now()
        await self.auth.invalidate_sessions(account)
        await self._send_reset(account)
        self.audit.record(
            AuditAction.ACCOUNT_EMAIL_REVERTED, details={"account_id": str(account.id)}
        )
        await self.session.commit()

    # --- профиль, компании, удаление ------------------------------------------

    async def update_profile(
        self, account: Account, changes: dict[str, Any]
    ) -> Account:
        """Личное в профиле (ТЗ §4): имя и фамилия обязательны, остальное —
        по желанию; пришедшее null — очистить, не пришедшее — не трогать.
        Телефон и Telegram — в одном виде (core/profile.py)."""
        if "first_name" in changes:
            account.first_name = _clean(changes["first_name"])
        if "last_name" in changes:
            account.last_name = _clean(changes["last_name"])
        if "patronymic" in changes:
            value = changes["patronymic"]
            account.patronymic = _clean(value) if value else None
        if "phone" in changes:
            account.phone = normalize_phone(changes["phone"])
        if "telegram" in changes:
            account.telegram = normalize_telegram(changes["telegram"])
        await self.session.commit()
        return account

    async def leave_company(self, account: Account, tenant_id: UUID) -> None:
        """Выйти из компании самому. Последний администратор не может —
        сначала назначить другого (компания не должна остаться без админа)."""
        with tenant_scope(tenant_id):
            member = await self.users.get_by_account(account.id)
            if member is None or member.status is MemberStatus.LEFT:
                raise NotFoundError("Компания не найдена")
            await _ensure_not_last_admin(
                self.users,
                member,
                "Вы единственный администратор компании — назначьте другого, "
                "прежде чем уйти",
            )
            _mark_left(member)
            self.audit.record(
                AuditAction.USER_LEFT,
                tenant_id=tenant_id,
                actor_id=member.id,
                target_type="user",
                target_id=member.id,
            )
            # В контексте компании: вне его RLS не найдёт строку членства.
            await self.session.flush()
        if account.last_tenant_id == tenant_id:
            account.last_tenant_id = None
        await self.session.commit()
        logger.info("member_left", account_id=str(account.id), tenant_id=str(tenant_id))

    async def delete_account(self, account: Account, password: str) -> None:
        """Удалить учётку (152-ФЗ). Членства становятся «ушёл», ссылки на
        них в журнале вопросов остаются без личных данных; токены, письма
        и заявки удаляются вместе с учёткой."""
        if not verify_password(password, account.hashed_password):
            raise InvalidPasswordError()
        with account_scope(account.id):
            memberships = await self.users.memberships_of_account(account.id)
        blocking: list[str] = []
        for membership in memberships:
            with tenant_scope(membership.tenant_id):
                member = await self.users.get_by_id(membership.id)
                if member is None:
                    continue
                if (
                    member.role is UserRole.ADMIN
                    and member.status is MemberStatus.ACTIVE
                    and await self.users.count_active_admins() <= 1
                ):
                    tenant = await self.tenants.get_by_id(member.tenant_id)
                    blocking.append(tenant.name if tenant else str(member.tenant_id))
        if blocking:
            raise LastAdminError(
                "Вы единственный администратор: "
                + ", ".join(f"«{name}»" for name in blocking)
                + ". Назначьте другого администратора, затем удалите учётку"
            )
        for membership in memberships:
            with tenant_scope(membership.tenant_id):
                member = await self.users.get_by_id(membership.id)
                if member is not None:
                    _mark_left(member)
                    await self.session.flush()
        self.audit.record(
            AuditAction.ACCOUNT_DELETED, details={"account_id": str(account.id)}
        )
        await self.accounts.delete(account)
        await self.session.commit()
        logger.info("account_deleted", account_id=str(account.id))

    async def _active_token(self, raw: str, purpose: EmailTokenPurpose) -> EmailToken:
        token = await self.tokens.get_by_hash(hash_refresh_token(raw), purpose.value)
        if token is None or token.used_at is not None or token.expires_at <= _now():
            raise InvalidEmailCodeError()
        return token


async def _ensure_not_last_admin(
    users: UserRepository, member: User, message: str
) -> None:
    if (
        member.role is UserRole.ADMIN
        and member.status is MemberStatus.ACTIVE
        and await users.count_active_admins() <= 1
    ):
        raise LastAdminError(message)


def _mark_left(member: User) -> None:
    member.status = MemberStatus.LEFT
    member.left_at = _now()
    member.token_version += 1


def _clean(value: str) -> str | None:
    return " ".join(value.split()) or None


def _now() -> datetime:
    return datetime.now(UTC)
