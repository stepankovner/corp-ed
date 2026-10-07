"""Второй фактор, доверенные устройства и сеансы (ТЗ §3, решение 03.10).

Вход — в два шага: верный пароль даёт не сессию, а одноразовый шаг входа
(AuthChallenge, 10 минут), который закрывается вторым фактором:

- код на почту — по умолчанию, у кого нет ничего надёжнее;
- приложение-аутентификатор (TOTP) или ключ доступа (WebAuthn); если
  включено одно из них, код на почту для входа больше не принимается —
  иначе взлом почты обходил бы защиту;
- резервный код — на случай потерянного телефона.

«Запомнить это устройство» — 30 дней без второго фактора на этом
браузере (TrustedDevice, cookie). Администраторам и компаниям с правилом
strong нужен надёжный фактор: без него ручки компании отвечают
mfa_setup_required (api/v1/dependencies.py).
"""

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from corp_ed.core import totp
from corp_ed.core.exceptions import (
    ConflictError,
    DomainError,
    NotFoundError,
)
from corp_ed.core.request_context import current_client_ip, current_user_agent
from corp_ed.core.secrets import SecretBox
from corp_ed.core.security import (
    hash_refresh_token,
    hash_secret,
    new_invite_code,
    new_numeric_code,
    new_refresh_token,
    normalize_invite_code,
    verify_password,
)
from corp_ed.core.tenant_context import account_scope
from corp_ed.core.useragent import describe
from corp_ed.domain.models import (
    Account,
    AuthChallenge,
    BackupCode,
    MemberStatus,
    Passkey,
    RefreshToken,
    Tenant,
    TrustedDevice,
    User,
    UserRole,
)
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.services import email_templates
from corp_ed.services.email_service import EmailService

logger = structlog.get_logger()

LOGIN_TTL = timedelta(minutes=10)
SETUP_TTL = timedelta(minutes=15)
TRUSTED_DEVICE_TTL = timedelta(days=30)
MAX_ATTEMPTS = 5
BACKUP_CODES = 10

Method = Literal["email", "totp", "passkey", "backup"]


class InvalidSecondFactorError(DomainError):
    """Код или ключ не подошёл, шаг входа истёк или исчерпан. HTTP 400."""

    code = "invalid_second_factor"

    def __init__(self) -> None:
        super().__init__("Код не подошёл или устарел — попробуйте ещё раз")


class LoginExpiredError(DomainError):
    """Шаг входа истёк или попытки кончились — войти заново. HTTP 400."""

    code = "login_expired"

    def __init__(self) -> None:
        super().__init__("Время на подтверждение вышло — войдите заново")


class SetupExpiredError(DomainError):
    """Настройка приложения или ключа истекла или исчерпала попытки —
    начать заново. HTTP 400."""

    code = "setup_expired"

    def __init__(self) -> None:
        super().__init__("Время настройки вышло — начните заново")


class InvalidPasswordError(DomainError):
    """Пароль для подтверждения действия неверен. HTTP 400."""

    code = "invalid_password"

    def __init__(self) -> None:
        super().__init__("Пароль указан неверно")


@dataclass(frozen=True)
class RelyingParty:
    """Сайт для WebAuthn: имя домена и разрешённые адреса страниц."""

    id: str
    origins: list[str]


@dataclass(frozen=True)
class LoginStep:
    token: str
    """Показывается браузеру один раз; в базе — sha256."""
    methods: list[Method]
    email_hint: str | None


@dataclass(frozen=True)
class CompletedLogin:
    account: Account
    remember: bool
    method: Method


@dataclass(frozen=True)
class SessionInfo:
    family_id: UUID
    device: str | None
    ip: str | None
    started_at: datetime
    last_active_at: datetime
    current: bool


def mask_email(email: str) -> str:
    """anna@acme.ru → a***@acme.ru: подсказка, куда ушёл код."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


class MfaService:
    def __init__(
        self,
        session: AsyncSession,
        secrets: SecretBox,
        audit: AuditRepository,
    ) -> None:
        self.session = session
        self.secrets = secrets
        self.audit = audit
        self.mail = EmailService(session)

    # --- состояние --------------------------------------------------------

    async def passkeys(self, account: Account) -> list[Passkey]:
        result = await self.session.scalars(
            select(Passkey)
            .where(Passkey.account_id == account.id)
            .order_by(Passkey.created_at)
        )
        return list(result)

    async def has_strong(self, account: Account) -> bool:
        if account.totp_enabled_at is not None:
            return True
        count = await self.session.scalar(
            select(func.count())
            .select_from(Passkey)
            .where(Passkey.account_id == account.id)
        )
        return bool(count)

    async def backup_codes_left(self, account: Account) -> int:
        count = await self.session.scalar(
            select(func.count())
            .select_from(BackupCode)
            .where(BackupCode.account_id == account.id, BackupCode.used_at.is_(None))
        )
        return int(count or 0)

    async def methods(self, account: Account) -> list[Method]:
        methods: list[Method] = []
        if account.totp_enabled_at is not None:
            methods.append("totp")
        if await self.passkeys(account):
            methods.append("passkey")
        if not methods:
            return ["email"]
        if await self.backup_codes_left(account):
            methods.append("backup")
        return methods

    # --- вход ---------------------------------------------------------------

    async def start_login(self, account: Account, *, remember: bool) -> LoginStep:
        """Пароль верный — шаг входа. Код на почту уходит сразу."""
        raw = new_refresh_token()
        challenge = AuthChallenge(
            account_id=account.id,
            purpose="login",
            token_hash=hash_refresh_token(raw),
            remember=remember,
            expires_at=_now() + LOGIN_TTL,
        )
        self.session.add(challenge)
        methods = await self.methods(account)
        hint = None
        if methods == ["email"]:
            self._send_login_code(account, challenge)
            hint = mask_email(account.email)
        await self.session.commit()
        return LoginStep(token=raw, methods=methods, email_hint=hint)

    async def login_email(self, raw: str) -> str:
        """Почта учётки незавершённого шага входа — ключ лимита писем."""
        _, account = await self._login_challenge(raw)
        return account.email

    async def resend_login_code(self, raw: str) -> None:
        challenge, account = await self._login_challenge(raw)
        if await self.methods(account) != ["email"]:
            raise InvalidSecondFactorError()
        self._send_login_code(account, challenge)
        await self.session.commit()

    async def passkey_login_options(self, raw: str, rp: RelyingParty) -> str:
        challenge, account = await self._login_challenge(raw)
        keys = await self.passkeys(account)
        if not keys:
            raise InvalidSecondFactorError()
        options = generate_authentication_options(
            rp_id=rp.id,
            allow_credentials=[
                PublicKeyCredentialDescriptor(
                    id=key.credential_id,
                    transports=_transports(key.transports),
                )
                for key in keys
            ],
            user_verification=UserVerificationRequirement.PREFERRED,
        )
        challenge.webauthn_challenge = options.challenge
        await self.session.commit()
        return options_to_json(options)

    async def complete_login(
        self,
        raw: str,
        method: Method,
        *,
        code: str | None = None,
        credential: dict[str, Any] | None = None,
        rp: RelyingParty | None = None,
    ) -> CompletedLogin:
        challenge, account = await self._login_challenge(raw)
        allowed = await self.methods(account)
        if method not in allowed:
            raise InvalidSecondFactorError()
        challenge.attempts += 1
        if challenge.attempts > MAX_ATTEMPTS:
            challenge.used_at = _now()
            await self.session.commit()
            raise LoginExpiredError()

        ok = False
        if method == "email" and code is not None:
            expected = hash_secret(f"{account.id}:login:{code.strip()}")
            ok = challenge.email_code_hash is not None and hmac.compare_digest(
                expected, challenge.email_code_hash
            )
        elif method == "totp" and code is not None:
            ok = self._accept_totp(account, code)
        elif method == "backup" and code is not None:
            ok = await self._use_backup_code(account, code)
        elif method == "passkey" and credential is not None and rp is not None:
            ok = await self._verify_passkey(account, challenge, credential, rp)

        if not ok:
            await self.session.commit()
            raise InvalidSecondFactorError()
        challenge.used_at = _now()
        if method != "email":
            # Код из почты сам пришёл письмом с устройством; для остальных
            # способов — отдельное письмо о входе с нового устройства.
            self.mail.enqueue(
                account.email,
                email_templates.new_device_login(
                    name=account.first_name,
                    device=describe(current_user_agent.get()),
                    ip=current_client_ip.get(),
                    reset_url=self.mail.url("/forgot-password"),
                ),
            )
        return CompletedLogin(
            account=account, remember=challenge.remember, method=method
        )

    async def verify_code(self, account: Account, code: str) -> bool:
        """Код приложения или резервный — для сброса пароля и отключения
        защиты. Не коммитит."""
        if account.totp_enabled_at is not None and self._accept_totp(account, code):
            return True
        return await self._use_backup_code(account, code)

    def _send_login_code(self, account: Account, challenge: AuthChallenge) -> None:
        code = new_numeric_code()
        challenge.email_code_hash = hash_secret(f"{account.id}:login:{code}")
        self.mail.enqueue(
            account.email,
            email_templates.login_code(
                name=account.first_name,
                code=code,
                minutes=int(LOGIN_TTL.total_seconds() // 60),
                device=describe(current_user_agent.get()),
                ip=current_client_ip.get(),
            ),
        )

    async def _login_challenge(self, raw: str) -> tuple[AuthChallenge, Account]:
        challenge = (
            await self.session.scalars(
                select(AuthChallenge)
                .where(
                    AuthChallenge.token_hash == hash_refresh_token(raw),
                    AuthChallenge.purpose == "login",
                )
                .with_for_update()
            )
        ).first()
        if (
            challenge is None
            or challenge.used_at is not None
            or challenge.expires_at <= _now()
        ):
            raise LoginExpiredError()
        account = await self.session.get(Account, challenge.account_id)
        if account is None:
            raise LoginExpiredError()
        return challenge, account

    def _accept_totp(self, account: Account, code: str) -> bool:
        if account.totp_secret is None:
            return False
        secret = self.secrets.decrypt(account.totp_secret)["secret"]
        step = totp.matching_step(secret, code)
        if step is None:
            return False
        if account.totp_last_step is not None and step <= account.totp_last_step:
            # Тот же код второй раз — перехваченный код не войдёт повторно.
            return False
        account.totp_last_step = step
        return True

    async def _use_backup_code(self, account: Account, code: str) -> bool:
        normalized = normalize_invite_code(code)
        if normalized is None:
            return False
        found = (
            await self.session.scalars(
                select(BackupCode)
                .where(
                    BackupCode.account_id == account.id,
                    BackupCode.code_hash == _backup_hash(account.id, normalized),
                    BackupCode.used_at.is_(None),
                )
                .with_for_update()
            )
        ).first()
        if found is None:
            return False
        found.used_at = _now()
        return True

    async def _verify_passkey(
        self,
        account: Account,
        challenge: AuthChallenge,
        credential: dict[str, Any],
        rp: RelyingParty,
    ) -> bool:
        if challenge.webauthn_challenge is None:
            return False
        try:
            raw_id = base64url_to_bytes(str(credential.get("rawId", "")))
        except (ValueError, TypeError):
            return False
        key = (
            await self.session.scalars(
                select(Passkey).where(
                    Passkey.account_id == account.id, Passkey.credential_id == raw_id
                )
            )
        ).first()
        if key is None:
            return False
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge.webauthn_challenge,
                expected_rp_id=rp.id,
                expected_origin=rp.origins,
                credential_public_key=key.public_key,
                credential_current_sign_count=key.sign_count,
            )
        except (InvalidAuthenticationResponse, ValueError, KeyError, TypeError):
            return False
        key.sign_count = verified.new_sign_count
        key.last_used_at = _now()
        challenge.webauthn_challenge = None
        return True

    # --- доверенные устройства ---------------------------------------------

    async def is_trusted(self, account: Account, raw: str | None) -> bool:
        if not raw:
            return False
        device = (
            await self.session.scalars(
                select(TrustedDevice).where(
                    TrustedDevice.token_hash == hash_refresh_token(raw),
                    TrustedDevice.account_id == account.id,
                    TrustedDevice.expires_at > _now(),
                )
            )
        ).first()
        if device is None:
            return False
        device.last_used_at = _now()
        return True

    def trust_device(self, account: Account) -> str:
        raw = new_refresh_token()
        self.session.add(
            TrustedDevice(
                account_id=account.id,
                token_hash=hash_refresh_token(raw),
                user_agent=current_user_agent.get(),
                expires_at=_now() + TRUSTED_DEVICE_TTL,
            )
        )
        return raw

    async def forget_devices(self, account: Account) -> None:
        await self.session.execute(
            delete(TrustedDevice).where(TrustedDevice.account_id == account.id)
        )

    async def remember_allowed(self, account: Account) -> bool:
        """Галочку «запомнить» можно запретить в компании (ТЗ §3): если
        запретила хоть одна компания человека — не запоминаем."""
        with account_scope(account.id):
            result = await self._count_memberships(
                account, Tenant.allow_remember_device.is_(False)
            )
        return not result

    async def _count_memberships(self, account: Account, condition: Any) -> int:
        """Действующие членства учётки, подходящие под условие (RLS
        own_membership: вызывать внутри account_scope)."""
        result = await self.session.scalar(
            select(func.count())
            .select_from(User)
            .join(Tenant, Tenant.id == User.tenant_id)
            .where(
                User.account_id == account.id,
                User.status == MemberStatus.ACTIVE,
                Tenant.is_active.is_(True),
                condition,
            )
            .execution_options(account_memberships=True)
        )
        return int(result or 0)

    # --- приложение-аутентификатор --------------------------------------------

    async def start_totp_setup(
        self, account: Account, password: str
    ) -> tuple[str, str, str]:
        """Секрет, otpauth:// для QR и токен настройки. Секрет действует
        только после подтверждения кодом из приложения. Токен настройки
        выдаётся только по паролю."""
        if not verify_password(password, account.hashed_password):
            raise InvalidPasswordError()
        secret = totp.new_secret()
        raw = new_refresh_token()
        self.session.add(
            AuthChallenge(
                account_id=account.id,
                purpose="totp_setup",
                token_hash=hash_refresh_token(raw),
                payload=self.secrets.encrypt({"secret": secret}),
                expires_at=_now() + SETUP_TTL,
            )
        )
        await self.session.commit()
        return secret, totp.provisioning_uri(secret, account=account.email), raw

    async def enable_totp(
        self, account: Account, raw: str, code: str
    ) -> list[str] | None:
        """Включить приложение. Первый надёжный фактор — выдать резервные
        коды (показываются один раз)."""
        challenge = await self._setup_challenge(account, raw, "totp_setup")
        if challenge.payload is None:
            raise InvalidSecondFactorError()
        secret = self.secrets.decrypt(challenge.payload)["secret"]
        step = totp.matching_step(secret, code)
        if step is None:
            challenge.attempts += 1
            await self.session.commit()
            raise InvalidSecondFactorError()
        first_strong = not await self.has_strong(account)
        account.totp_secret = self.secrets.encrypt({"secret": secret})
        account.totp_enabled_at = _now()
        account.totp_last_step = step
        challenge.used_at = _now()
        codes = await self._new_backup_codes(account) if first_strong else None
        self._security_notice(account, "Включён вход через приложение-аутентификатор.")
        self.audit.record(
            AuditAction.MFA_ENABLED,
            details={"account_id": str(account.id), "method": "totp"},
        )
        await self.session.commit()
        return codes

    async def disable_totp(self, account: Account, password: str, code: str) -> None:
        if not verify_password(password, account.hashed_password):
            raise InvalidPasswordError()
        if account.totp_enabled_at is None:
            return
        if not self._accept_totp(account, code) and not await self._use_backup_code(
            account, code
        ):
            raise InvalidSecondFactorError()
        await self._ensure_can_drop_strong(account, removing="totp")
        account.totp_secret = None
        account.totp_enabled_at = None
        account.totp_last_step = None
        await self._after_strong_removed(account)
        self._security_notice(account, "Вход через приложение-аутентификатор выключен.")
        self.audit.record(
            AuditAction.MFA_DISABLED,
            details={"account_id": str(account.id), "method": "totp"},
        )
        await self.session.commit()

    # --- ключи доступа ---------------------------------------------------------

    async def passkey_registration_options(
        self, account: Account, rp: RelyingParty, password: str
    ) -> tuple[str, str]:
        if not verify_password(password, account.hashed_password):
            raise InvalidPasswordError()
        keys = await self.passkeys(account)
        options = generate_registration_options(
            rp_id=rp.id,
            rp_name="kronto",
            user_id=account.id.bytes,
            user_name=account.email,
            user_display_name=account.full_name or account.email,
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=key.credential_id) for key in keys
            ],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.PREFERRED,
            ),
        )
        raw = new_refresh_token()
        self.session.add(
            AuthChallenge(
                account_id=account.id,
                purpose="passkey_setup",
                token_hash=hash_refresh_token(raw),
                webauthn_challenge=options.challenge,
                expires_at=_now() + SETUP_TTL,
            )
        )
        await self.session.commit()
        return options_to_json(options), raw

    async def register_passkey(
        self,
        account: Account,
        raw: str,
        credential: dict[str, Any],
        name: str,
        rp: RelyingParty,
    ) -> tuple[Passkey, list[str] | None]:
        challenge = await self._setup_challenge(account, raw, "passkey_setup")
        if challenge.webauthn_challenge is None:
            raise InvalidSecondFactorError()
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=challenge.webauthn_challenge,
                expected_rp_id=rp.id,
                expected_origin=rp.origins,
            )
        except (InvalidRegistrationResponse, ValueError, KeyError, TypeError) as exc:
            raise InvalidSecondFactorError() from exc
        first_strong = not await self.has_strong(account)
        transports = credential.get("response", {}).get("transports") or []
        key = Passkey(
            account_id=account.id,
            credential_id=verified.credential_id,
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
            transports=[str(t) for t in transports][:8],
            name=" ".join(name.split())[:100] or "Ключ доступа",
        )
        self.session.add(key)
        challenge.used_at = _now()
        codes = await self._new_backup_codes(account) if first_strong else None
        self._security_notice(account, f"Добавлен ключ доступа «{key.name}».")
        self.audit.record(
            AuditAction.MFA_ENABLED,
            details={"account_id": str(account.id), "method": "passkey"},
        )
        await self.session.commit()
        return key, codes

    async def delete_passkey(
        self, account: Account, passkey_id: UUID, password: str
    ) -> None:
        if not verify_password(password, account.hashed_password):
            raise InvalidPasswordError()
        key = (
            await self.session.scalars(
                select(Passkey).where(
                    Passkey.id == passkey_id, Passkey.account_id == account.id
                )
            )
        ).first()
        if key is None:
            raise NotFoundError("Ключ не найден")
        await self._ensure_can_drop_strong(account, removing="passkey")
        await self.session.delete(key)
        await self.session.flush()
        await self._after_strong_removed(account)
        self._security_notice(account, f"Удалён ключ доступа «{key.name}».")
        self.audit.record(
            AuditAction.MFA_DISABLED,
            details={"account_id": str(account.id), "method": "passkey"},
        )
        await self.session.commit()

    # --- резервные коды ---------------------------------------------------------

    async def regenerate_backup_codes(
        self, account: Account, password: str
    ) -> list[str]:
        if not verify_password(password, account.hashed_password):
            raise InvalidPasswordError()
        if not await self.has_strong(account):
            raise ConflictError(
                "Резервные коды нужны к приложению или ключу доступа — "
                "сначала включите одно из них"
            )
        codes = await self._new_backup_codes(account)
        self._security_notice(
            account, "Выпущены новые резервные коды, старые не действуют."
        )
        await self.session.commit()
        return codes

    async def _new_backup_codes(self, account: Account) -> list[str]:
        await self.session.execute(
            delete(BackupCode).where(BackupCode.account_id == account.id)
        )
        codes = [new_invite_code() for _ in range(BACKUP_CODES)]
        for code in codes:
            # Формат кода приглашения: 8 знаков base32 Крокфорда, XXXX-XXXX.
            normalized = normalize_invite_code(code) or code
            self.session.add(
                BackupCode(
                    account_id=account.id,
                    code_hash=_backup_hash(account.id, normalized),
                )
            )
        return codes

    # --- сеансы --------------------------------------------------------------------

    async def sessions(
        self, account: Account, current_raw: str | None
    ) -> list[SessionInfo]:
        """Сеансы — живые цепочки refresh-токенов учётки."""
        current_family = None
        if current_raw:
            row = (
                await self.session.scalars(
                    select(RefreshToken.family_id).where(
                        RefreshToken.token_hash == hash_refresh_token(current_raw)
                    )
                )
            ).first()
            current_family = row
        tokens = list(
            await self.session.scalars(
                select(RefreshToken)
                .where(
                    RefreshToken.account_id == account.id,
                    RefreshToken.revoked_at.is_(None),
                    RefreshToken.used_at.is_(None),
                    RefreshToken.expires_at > _now(),
                )
                .order_by(RefreshToken.created_at.desc())
            )
        )
        firsts: dict[UUID, datetime] = {
            family: started
            for family, started in (
                await self.session.execute(
                    select(RefreshToken.family_id, func.min(RefreshToken.created_at))
                    .where(RefreshToken.account_id == account.id)
                    .group_by(RefreshToken.family_id)
                )
            ).all()
        }
        return [
            SessionInfo(
                family_id=token.family_id,
                device=describe(token.user_agent),
                ip=token.ip,
                started_at=firsts.get(token.family_id, token.created_at),
                last_active_at=token.created_at,
                current=token.family_id == current_family,
            )
            for token in tokens
        ]

    async def end_session(self, account: Account, family_id: UUID) -> None:
        """Завершить свой сеанс. Чужой или несуществующий — «не найден»
        (одинаково: по ответу не узнать, есть ли такой сеанс у другого);
        уже завершённый свой — без ошибки, повторное нажатие.

        Сеанс завершают обычно из-за подозрения: «запомненные» устройства
        забываются (доверенное устройство не привязано к сеансу), и при
        следующем входе везде снова нужен второй фактор."""
        known = await self.session.scalar(
            select(func.count())
            .select_from(RefreshToken)
            .where(
                RefreshToken.account_id == account.id,
                RefreshToken.family_id == family_id,
            )
        )
        if not known:
            raise NotFoundError("Сеанс не найден")
        await self.session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.account_id == account.id,
                RefreshToken.family_id == family_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=_now())
        )
        await self.forget_devices(account)
        await self.session.commit()

    # --- правила -------------------------------------------------------------------

    async def strong_required(self, account: Account) -> bool:
        """Нужен ли надёжный фактор: администратор хоть в одной компании
        или компания с правилом strong (ТЗ §3)."""
        with account_scope(account.id):
            result = await self._count_memberships(
                account, (User.role == UserRole.ADMIN) | (Tenant.mfa_policy == "strong")
            )
        return bool(result)

    async def _ensure_can_drop_strong(self, account: Account, *, removing: str) -> None:
        remaining_totp = account.totp_enabled_at is not None and removing != "totp"
        keys = await self.passkeys(account)
        remaining_keys = len(keys) - (1 if removing == "passkey" else 0)
        if remaining_totp or remaining_keys > 0:
            return
        if await self.strong_required(account):
            raise ConflictError(
                "Администраторам нужен надёжный второй фактор: сначала добавьте "
                "другой ключ или включите приложение-аутентификатор"
            )

    async def _after_strong_removed(self, account: Account) -> None:
        if not await self.has_strong(account):
            # Без надёжного фактора резервные коды не нужны: вход — по почте.
            await self.session.execute(
                delete(BackupCode).where(BackupCode.account_id == account.id)
            )

    async def _setup_challenge(
        self, account: Account, raw: str, purpose: str
    ) -> AuthChallenge:
        challenge = (
            await self.session.scalars(
                select(AuthChallenge)
                .where(
                    AuthChallenge.token_hash == hash_refresh_token(raw),
                    AuthChallenge.purpose == purpose,
                    AuthChallenge.account_id == account.id,
                )
                .with_for_update()
            )
        ).first()
        if (
            challenge is None
            or challenge.used_at is not None
            or challenge.expires_at <= _now()
            or challenge.attempts >= MAX_ATTEMPTS
        ):
            raise SetupExpiredError()
        return challenge

    def _security_notice(self, account: Account, what: str) -> None:
        self.mail.enqueue(
            account.email,
            email_templates.security_changed(
                name=account.first_name,
                what=what,
                reset_url=self.mail.url("/forgot-password"),
            ),
        )


def _transports(values: list[str]) -> list[AuthenticatorTransport] | None:
    known = {t.value: t for t in AuthenticatorTransport}
    picked = [known[v] for v in values if v in known]
    return picked or None


def _backup_hash(account_id: UUID, normalized: str) -> str:
    return hash_secret(f"{account_id}:backup:{normalized}")


def _now() -> datetime:
    return datetime.now(UTC)
