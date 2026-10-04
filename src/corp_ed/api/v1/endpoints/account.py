"""Учётка человека: профиль, почта, компании, заявки, удаление (ТЗ §2–4).

Ручки работают без выбранной компании: учётка существует сама по себе.
"""

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile, status

from corp_ed.api.v1.dependencies import (
    get_account_service,
    get_avatar_service,
    get_company_request_service,
    get_current_account,
    get_mfa_service,
    get_relying_party,
)
from corp_ed.api.v1.rate_limits import (
    AVATAR_PER_ACCOUNT,
    COMPANY_REQUEST_PER_ACCOUNT,
    EMAIL_CHANGE_PER_ACCOUNT,
    VERIFY_PER_IP,
    client_ip,
    enforce,
    get_rate_limiter,
)
from corp_ed.api.v1.schemas.account import (
    AvatarResponse,
    BackupCodesResponse,
    CompanyRequestCreate,
    CompanyRequestResponse,
    EmailChangeRequest,
    EmailChangeResponse,
    LeaveCompanyRequest,
    PasskeyCreatedResponse,
    PasskeyRegisterRequest,
    PasskeyResponse,
    PasskeySetupResponse,
    PasswordConfirmRequest,
    ProfileUpdateRequest,
    SecondFactorConfirmRequest,
    SecurityResponse,
    SessionResponse,
    TotpEnableRequest,
    TotpSetupResponse,
)
from corp_ed.api.v1.schemas.auth import TokenRequest
from corp_ed.api.v1.session_cookie import clear_refresh_cookie, read_refresh_cookie
from corp_ed.core.rate_limit import RateLimiter
from corp_ed.domain.models import Account
from corp_ed.services.account_service import AccountService
from corp_ed.services.avatar_service import (
    MAX_AVATAR_BYTES,
    AvatarService,
    InvalidAvatarError,
)
from corp_ed.services.company_request_service import CompanyRequestService
from corp_ed.services.mfa_service import MfaService, RelyingParty

router = APIRouter(prefix="/account", tags=["account"])

CurrentAccount = Annotated[Account, Depends(get_current_account)]
Service = Annotated[AccountService, Depends(get_account_service)]


@router.patch("", status_code=status.HTTP_204_NO_CONTENT)
async def update_profile(
    data: ProfileUpdateRequest, account: CurrentAccount, service: Service
) -> None:
    """Имя, отчество, телефон, Telegram (ТЗ §4). Пришедшее null — очистить."""
    await service.update_profile(account, data.model_dump(exclude_unset=True))


@router.put("/avatar", response_model=AvatarResponse)
async def upload_avatar(
    file: Annotated[UploadFile, File(description="JPEG, PNG или WebP до 5 МБ")],
    account: CurrentAccount,
    avatars: Annotated[AvatarService, Depends(get_avatar_service)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> AvatarResponse:
    """Фото профиля: сервер вырезает квадрат 256×256 и убирает метаданные
    снимка. Формат определяется по содержимому, а не по имени файла."""
    await enforce(limiter, AVATAR_PER_ACCOUNT, str(account.id))
    raw = await file.read(MAX_AVATAR_BYTES + 1)
    if len(raw) > MAX_AVATAR_BYTES:
        raise InvalidAvatarError()
    return AvatarResponse(avatar_url=await avatars.save(account, raw))


@router.delete("/avatar", status_code=status.HTTP_204_NO_CONTENT)
async def delete_avatar(
    account: CurrentAccount,
    avatars: Annotated[AvatarService, Depends(get_avatar_service)],
) -> None:
    await avatars.remove(account)


@router.post(
    "/email",
    response_model=EmailChangeResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_email_change(
    data: EmailChangeRequest,
    account: CurrentAccount,
    service: Service,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> EmailChangeResponse:
    """Пароль и второй фактор, затем письмо со ссылкой на новый адрес;
    почта сменится после перехода (ТЗ §3). Без приложения второй фактор
    — код на прежний адрес: первый запрос без кода его отправляет."""
    await enforce(limiter, EMAIL_CHANGE_PER_ACCOUNT, str(account.id))
    step = await service.request_email_change(
        account, str(data.new_email), data.password, data.code
    )
    return EmailChangeResponse(status=step.status, email_hint=step.email_hint)


@router.post("/email/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_email_change(
    request: Request,
    data: TokenRequest,
    service: Service,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> None:
    """Ссылка из письма на новый адрес. Без входа: письмо могут открыть
    на другом устройстве."""
    await enforce(limiter, VERIFY_PER_IP, client_ip(request))
    await service.confirm_email_change(data.token)


@router.post("/email/revert", status_code=status.HTTP_204_NO_CONTENT)
async def revert_email_change(
    request: Request,
    data: TokenRequest,
    service: Service,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> None:
    """«Это не я» из письма на прежний адрес. Без входа: у владельца,
    скорее всего, его уже нет."""
    await enforce(limiter, VERIFY_PER_IP, client_ip(request))
    await service.revert_email_change(data.token)


@router.post("/leave", status_code=status.HTTP_204_NO_CONTENT)
async def leave_company(
    data: LeaveCompanyRequest, account: CurrentAccount, service: Service
) -> None:
    """Выйти из компании. Учётка остаётся."""
    await service.leave_company(account, data.tenant_id)


@router.post("/delete", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    response: Response,
    data: PasswordConfirmRequest,
    account: CurrentAccount,
    service: Service,
) -> None:
    """Удалить учётку (152-ФЗ). Пароль — подтверждение, что это владелец."""
    await service.delete_account(account, data.password)
    clear_refresh_cookie(response)


@router.get("/company-requests", response_model=list[CompanyRequestResponse])
async def list_company_requests(
    account: CurrentAccount,
    service: Annotated[CompanyRequestService, Depends(get_company_request_service)],
) -> list[CompanyRequestResponse]:
    return [
        CompanyRequestResponse.model_validate(r)
        for r in await service.list_mine(account)
    ]


@router.post(
    "/company-requests",
    response_model=CompanyRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_company_request(
    data: CompanyRequestCreate,
    account: CurrentAccount,
    service: Annotated[CompanyRequestService, Depends(get_company_request_service)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> CompanyRequestResponse:
    """Заявка «Подключить компанию»: одобряет команда Kronto."""
    await enforce(limiter, COMPANY_REQUEST_PER_ACCOUNT, str(account.id))
    created = await service.create(
        account,
        company_name=data.company_name,
        seats=data.seats,
        comment=data.comment,
    )
    return CompanyRequestResponse.model_validate(created)


@router.post(
    "/company-requests/{request_id}/cancel", response_model=CompanyRequestResponse
)
async def cancel_company_request(
    request_id: UUID,
    account: CurrentAccount,
    service: Annotated[CompanyRequestService, Depends(get_company_request_service)],
) -> CompanyRequestResponse:
    return CompanyRequestResponse.model_validate(
        await service.cancel(account, request_id)
    )


# --- защита: второй фактор и сеансы (ТЗ §3) ---------------------------------

Mfa = Annotated[MfaService, Depends(get_mfa_service)]


@router.get("/security", response_model=SecurityResponse)
async def read_security(account: CurrentAccount, mfa: Mfa) -> SecurityResponse:
    return SecurityResponse(
        totp_enabled=account.totp_enabled_at is not None,
        passkeys=[
            PasskeyResponse.model_validate(k) for k in await mfa.passkeys(account)
        ],
        backup_codes_left=await mfa.backup_codes_left(account),
        strong_required=await mfa.strong_required(account),
    )


@router.post("/totp/setup", response_model=TotpSetupResponse)
async def start_totp_setup(account: CurrentAccount, mfa: Mfa) -> TotpSetupResponse:
    """Секрет для приложения (QR из otpauth_uri). Действует после
    подтверждения кодом — /account/totp/enable."""
    secret, uri, token = await mfa.start_totp_setup(account)
    return TotpSetupResponse(secret=secret, otpauth_uri=uri, setup_token=token)


@router.post("/totp/enable", response_model=BackupCodesResponse)
async def enable_totp(
    request: Request,
    data: TotpEnableRequest,
    account: CurrentAccount,
    mfa: Mfa,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> BackupCodesResponse:
    await enforce(limiter, VERIFY_PER_IP, client_ip(request))
    codes = await mfa.enable_totp(account, data.setup_token, data.code)
    return BackupCodesResponse(backup_codes=codes)


@router.post("/totp/disable", status_code=status.HTTP_204_NO_CONTENT)
async def disable_totp(
    request: Request,
    data: SecondFactorConfirmRequest,
    account: CurrentAccount,
    mfa: Mfa,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> None:
    await enforce(limiter, VERIFY_PER_IP, client_ip(request))
    await mfa.disable_totp(account, data.password, data.code)


@router.post("/passkeys/options", response_model=PasskeySetupResponse)
async def passkey_options(
    account: CurrentAccount,
    mfa: Mfa,
    rp: Annotated[RelyingParty, Depends(get_relying_party)],
) -> PasskeySetupResponse:
    """Параметры для navigator.credentials.create()."""
    options, token = await mfa.passkey_registration_options(account, rp)
    return PasskeySetupResponse(options=json.loads(options), setup_token=token)


@router.post(
    "/passkeys",
    response_model=PasskeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_passkey(
    data: PasskeyRegisterRequest,
    account: CurrentAccount,
    mfa: Mfa,
    rp: Annotated[RelyingParty, Depends(get_relying_party)],
) -> PasskeyCreatedResponse:
    key, codes = await mfa.register_passkey(
        account, data.setup_token, data.credential, data.name, rp
    )
    return PasskeyCreatedResponse(
        passkey=PasskeyResponse.model_validate(key), backup_codes=codes
    )


@router.post("/passkeys/{passkey_id}/delete", status_code=status.HTTP_204_NO_CONTENT)
async def delete_passkey(
    passkey_id: UUID,
    data: PasswordConfirmRequest,
    account: CurrentAccount,
    mfa: Mfa,
) -> None:
    await mfa.delete_passkey(account, passkey_id, data.password)


@router.post("/backup-codes", response_model=BackupCodesResponse)
async def regenerate_backup_codes(
    data: PasswordConfirmRequest, account: CurrentAccount, mfa: Mfa
) -> BackupCodesResponse:
    """Новые 10 резервных кодов; старые перестают действовать."""
    return BackupCodesResponse(
        backup_codes=await mfa.regenerate_backup_codes(account, data.password)
    )


# Сеансы — под /auth: refresh-cookie браузер шлёт только на /api/v1/auth
# (session_cookie.py), а по ней сервер узнаёт «это устройство». Под
# /account cookie не приходила, и текущий сеанс в списке не отмечался.
sessions_router = APIRouter(prefix="/auth/sessions", tags=["account"])


@sessions_router.get("", response_model=list[SessionResponse])
async def list_sessions(
    request: Request, account: CurrentAccount, mfa: Mfa
) -> list[SessionResponse]:
    """Где открыта учётка: браузер, адрес, когда начат сеанс."""
    sessions = await mfa.sessions(account, read_refresh_cookie(request))
    return [
        SessionResponse(
            id=item.family_id,
            device=item.device,
            ip=item.ip,
            started_at=item.started_at,
            last_active_at=item.last_active_at,
            current=item.current,
        )
        for item in sessions
    ]


@sessions_router.post("/{session_id}/end", status_code=status.HTTP_204_NO_CONTENT)
async def end_session(session_id: UUID, account: CurrentAccount, mfa: Mfa) -> None:
    """Выйти на одном устройстве. Его access-токен доживёт до 15 минут —
    для немедленного выхода везде есть /auth/logout-all."""
    await mfa.end_session(account, session_id)
