from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, StringConstraints

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.core.password_policy import MAX_PASSWORD_LENGTH
from corp_ed.domain.models import MemberStatus, UserRole

# Верхние границы у всех строк: argon2 и JWT считают от всей строки,
# мегабайтное поле — дешёвый способ занять CPU.
MAX_TOKEN_LENGTH = 128
MAX_NAME_LENGTH = 100

# Имя и фамилия: пробелы по краям не считаются, из одних пробелов — пусто.
PersonName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NAME_LENGTH),
]
# Необязательное поле профиля: отчество, должность. Пустое — очистить.
ProfileText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=MAX_NAME_LENGTH),
]


class LoginRequest(RequestModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    remember: bool = True
    """«Запомнить это устройство»: 30 дней; иначе — до закрытия браузера."""


class RegisterRequest(RequestModel):
    first_name: PersonName
    last_name: PersonName
    email: EmailStr
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    consent: Literal[True]
    """Согласие на обработку персональных данных (152-ФЗ) — обязательно."""
    invite: str | None = Field(
        default=None,
        min_length=8,
        max_length=MAX_TOKEN_LENGTH,
        description="Приглашение, по которому человек пришёл: при закрытой "
        "регистрации пускает зарегистрироваться. В компанию не вступает — после "
        "подтверждения почты это отдельный шаг POST /invites/accept («Вступить»).",
    )
    """Ссылка или код приглашения: при закрытой регистрации пускает только с ним."""


class EmailSentResponse(BaseModel):
    """Ответ один и тот же, есть учётка или нет (по нему не перебрать адреса)."""

    email: EmailStr


class EmailRequest(RequestModel):
    email: EmailStr


class VerifyCodeRequest(RequestModel):
    email: EmailStr
    code: str = Field(pattern=r"^\s*\d{6}\s*$")


class TokenRequest(RequestModel):
    token: str = Field(min_length=16, max_length=MAX_TOKEN_LENGTH)


class ResetPasswordRequest(TokenRequest):
    new_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    second_factor: str | None = Field(default=None, max_length=32)
    """Код приложения или резервный — если у учётки приложение или ключ."""


class ChangePasswordRequest(RequestModel):
    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class SwitchCompanyRequest(RequestModel):
    tenant_id: UUID | None
    """None — выйти в «без компании» (например, перед вступлением в новую)."""


class TokenResponse(BaseModel):
    """Access-токен для заголовка Authorization. Refresh-токен в тело не
    попадает: он уходит в httpOnly-cookie (api/v1/session_cookie.py)."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105 — схема токена (RFC 6750), не пароль
    expires_in: int


class MfaChallenge(BaseModel):
    token: str
    """Шаг входа — вернуть в /auth/mfa/verify. Действует 10 минут."""
    methods: list[Literal["email", "totp", "passkey", "backup"]]
    email_hint: str | None
    """Куда ушёл код: a***@acme.ru (только для способа email)."""


class LoginResponse(BaseModel):
    """Вход: сразу сессия (доверенное устройство) или второй фактор."""

    status: Literal["ok", "mfa_required"]
    access_token: str | None = None
    token_type: str = "bearer"  # noqa: S105 — схема токена (RFC 6750), не пароль
    expires_in: int | None = None
    mfa: MfaChallenge | None = None


class MfaTokenRequest(RequestModel):
    token: str = Field(min_length=16, max_length=MAX_TOKEN_LENGTH)


class MfaVerifyRequest(MfaTokenRequest):
    method: Literal["email", "totp", "passkey", "backup"]
    code: str | None = Field(default=None, max_length=32)
    credential: dict[str, Any] | None = None
    """Ответ navigator.credentials.get() в JSON (только для passkey)."""


class PasskeyOptionsResponse(BaseModel):
    options: dict[str, Any]
    """PublicKeyCredentialRequestOptions / CreationOptions в JSON (base64url)."""


class MfaState(BaseModel):
    strong: bool
    """Включено приложение или есть ключ доступа."""
    strong_required: bool
    """Администратор или компания требует надёжный фактор."""


class MembershipItem(BaseModel):
    tenant_id: UUID
    company_name: str
    role: UserRole
    status: MemberStatus
    logo_url: str | None = None
    """Логотип компании (ТЗ §7) — подписанная ссылка до конца суток."""


class DepartmentRef(BaseModel):
    id: UUID
    name: str


class CurrentCompany(BaseModel):
    tenant_id: UUID
    member_id: UUID
    name: str
    role: UserRole
    position: str | None
    """Должность в этой компании (ТЗ §4)."""
    department: DepartmentRef | None
    department_confirmed: bool = False
    """Отдел подтверждён администратором (ТЗ §7): закрытые папки отдела
    открыты только тогда."""
    logo_url: str | None = None


class MeResponse(BaseModel):
    id: UUID
    """Учётка (не членство)."""
    email: EmailStr
    first_name: str | None
    last_name: str | None
    patronymic: str | None
    full_name: str | None
    phone: str | None
    """+79991234567."""
    telegram: str | None
    """Имя пользователя без «@»."""
    avatar_url: str | None
    """Подписанная ссылка на фото, действует до конца следующих суток."""
    must_change_password: bool
    last_login_at: datetime | None
    company: CurrentCompany | None
    """Выбранная компания; null — человек без компании."""
    companies: list[MembershipItem]
    """Все компании человека (кроме тех, откуда он ушёл)."""
    mfa: MfaState
    staff: bool = False
    """Команда kronto: открыта наша панель (/staff, ТЗ §9)."""
