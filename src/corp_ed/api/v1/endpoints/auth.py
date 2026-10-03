import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from corp_ed.api.v1.dependencies import (
    Principal,
    get_account_service,
    get_auth_service,
    get_invite_service,
    get_mfa_service,
    get_principal,
    get_principal_allow_password_change,
    get_relying_party,
    get_tenant_repository,
    get_user_repository,
)
from corp_ed.api.v1.rate_limits import (
    LOGIN_FAILURES_PER_ACCOUNT,
    LOGIN_PER_IP,
    MAIL_PER_ADDRESS,
    MAIL_PER_IP,
    PASSWORD_CHANGE_PER_USER,
    REFRESH_PER_IP,
    REGISTER_PER_IP,
    VERIFY_PER_IP,
    client_ip,
    enforce,
    ensure_not_locked,
    forget,
    get_rate_limiter,
    record,
)
from corp_ed.api.v1.schemas.auth import (
    ChangePasswordRequest,
    CurrentCompany,
    EmailRequest,
    EmailSentResponse,
    LoginRequest,
    LoginResponse,
    MembershipItem,
    MeResponse,
    MfaChallenge,
    MfaState,
    MfaTokenRequest,
    MfaVerifyRequest,
    PasskeyOptionsResponse,
    RegisterRequest,
    ResetPasswordRequest,
    SwitchCompanyRequest,
    TokenRequest,
    TokenResponse,
    VerifyCodeRequest,
)
from corp_ed.api.v1.session_cookie import (
    clear_refresh_cookie,
    ensure_same_origin,
    read_device_cookie,
    read_refresh_cookie,
    session_response,
    set_device_cookie,
)
from corp_ed.core.exceptions import InvalidCredentialsError, NotAuthenticatedError
from corp_ed.core.password_policy import validate_password
from corp_ed.core.rate_limit import RateLimiter
from corp_ed.core.tenant_context import account_scope
from corp_ed.repositories.account_repository import normalize_email
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.account_service import AccountService
from corp_ed.services.auth_service import AuthService
from corp_ed.services.invite_service import InviteService
from corp_ed.services.mfa_service import MfaService, RelyingParty

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=MeResponse)
async def read_me(
    principal: Annotated[Principal, Depends(get_principal_allow_password_change)],
    tenant_repo: Annotated[TenantRepository, Depends(get_tenant_repository)],
    user_repo: Annotated[UserRepository, Depends(get_user_repository)],
    mfa: Annotated[MfaService, Depends(get_mfa_service)],
) -> MeResponse:
    """Кто вошёл: учётка, выбранная компания и все компании человека.
    Доступна и до смены временного пароля: фронту нужно знать
    must_change_password, чтобы показать форму смены."""
    account, member = principal.account, principal.member
    with account_scope(account.id):
        memberships = await user_repo.memberships_of_account(account.id)
    companies: list[MembershipItem] = []
    for membership in memberships:
        tenant = await tenant_repo.get_by_id(membership.tenant_id)
        if tenant is None or not tenant.is_active:
            continue
        companies.append(
            MembershipItem(
                tenant_id=tenant.id,
                company_name=tenant.name,
                role=membership.role,
                status=membership.status,
            )
        )
    company = None
    if member is not None:
        tenant = await tenant_repo.get_by_id(member.tenant_id)
        company = CurrentCompany(
            tenant_id=member.tenant_id,
            member_id=member.id,
            name=tenant.name if tenant else "",
            role=member.role,
        )
    return MeResponse(
        id=account.id,
        email=account.email,
        first_name=account.first_name,
        last_name=account.last_name,
        full_name=account.full_name,
        must_change_password=account.must_change_password,
        last_login_at=account.last_login_at,
        company=company,
        companies=companies,
        mfa=MfaState(
            strong=await mfa.has_strong(account),
            strong_required=await mfa.strong_required(account),
        ),
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    request: Request,
    response: Response,
    data: LoginRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    mfa: Annotated[MfaService, Depends(get_mfa_service)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> LoginResponse:
    """Вход по почте и паролю (ТЗ §2–3). Верный пароль даёт сессию сразу,
    только если браузер — доверенное устройство учётки; иначе — шаг
    второго фактора (/auth/mfa/verify).

    Два лимита против перебора (ASVS 6.3.1):
    - на IP — все попытки: один адрес не перебирает много учёток;
    - на почту — только неудачные: распределённый перебор одной учётки
      с многих адресов. Успешный вход счётчик обнуляет.
    """
    await enforce(limiter, LOGIN_PER_IP, client_ip(request))
    account_key = normalize_email(str(data.email))
    await ensure_not_locked(limiter, LOGIN_FAILURES_PER_ACCOUNT, account_key)

    try:
        account = await auth_service.check_password(str(data.email), data.password)
    except InvalidCredentialsError:
        await record(limiter, LOGIN_FAILURES_PER_ACCOUNT, account_key)
        raise
    await forget(limiter, LOGIN_FAILURES_PER_ACCOUNT, account_key)

    if await mfa.is_trusted(account, read_device_cookie(request)):
        remember = data.remember and await mfa.remember_allowed(account)
        pair = await auth_service.login_session(account, remember=remember)
        token = session_response(response, pair)
        return LoginResponse(
            status="ok", access_token=token.access_token, expires_in=token.expires_in
        )

    step = await mfa.start_login(account, remember=data.remember)
    return LoginResponse(
        status="mfa_required",
        mfa=MfaChallenge(
            token=step.token, methods=step.methods, email_hint=step.email_hint
        ),
    )


@router.post("/mfa/verify", response_model=TokenResponse)
async def verify_second_factor(
    request: Request,
    response: Response,
    data: MfaVerifyRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    mfa: Annotated[MfaService, Depends(get_mfa_service)],
    rp: Annotated[RelyingParty, Depends(get_relying_party)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> TokenResponse:
    """Второй шаг входа: код из письма, приложения, резервный или ключ
    доступа. «Запомнить» — доверенное устройство на 30 дней, если ни одна
    компания человека это не запретила."""
    await enforce(limiter, VERIFY_PER_IP, client_ip(request))
    done = await mfa.complete_login(
        data.token, data.method, code=data.code, credential=data.credential, rp=rp
    )
    remember = done.remember and await mfa.remember_allowed(done.account)
    device = mfa.trust_device(done.account) if remember else None
    pair = await auth_service.login_session(done.account, remember=remember)
    if device is not None:
        set_device_cookie(response, device)
    return session_response(response, pair)


@router.post("/mfa/resend", status_code=status.HTTP_202_ACCEPTED)
async def resend_login_code(
    request: Request,
    data: MfaTokenRequest,
    mfa: Annotated[MfaService, Depends(get_mfa_service)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> None:
    """Новый код на почту для того же шага входа."""
    await enforce(limiter, MAIL_PER_IP, client_ip(request))
    await enforce(limiter, MAIL_PER_ADDRESS, f"mfa:{data.token[:16]}")
    await mfa.resend_login_code(data.token)


@router.post("/mfa/passkey-options", response_model=PasskeyOptionsResponse)
async def passkey_login_options(
    request: Request,
    data: MfaTokenRequest,
    mfa: Annotated[MfaService, Depends(get_mfa_service)],
    rp: Annotated[RelyingParty, Depends(get_relying_party)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> PasskeyOptionsResponse:
    """Параметры для navigator.credentials.get() на шаге входа."""
    await enforce(limiter, VERIFY_PER_IP, client_ip(request))
    options = await mfa.passkey_login_options(data.token, rp)
    return PasskeyOptionsResponse(options=json.loads(options))


@router.post(
    "/register",
    response_model=EmailSentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def register(
    request: Request,
    data: RegisterRequest,
    service: Annotated[AccountService, Depends(get_account_service)],
    invites: Annotated[InviteService, Depends(get_invite_service)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> EmailSentResponse:
    """Регистрация (ТЗ §2): учётка без компании и письмо с кодом. Ответ
    одинаковый, есть ли уже учётка с этой почтой."""
    email = normalize_email(str(data.email))
    await enforce(limiter, REGISTER_PER_IP, client_ip(request))
    validate_password(data.password, email=email)
    await enforce(limiter, MAIL_PER_ADDRESS, email)
    invited = data.invite is not None and await invites.is_valid(data.invite)
    await service.register(
        first_name=data.first_name,
        last_name=data.last_name,
        email=email,
        password=data.password,
        invited=invited,
    )
    return EmailSentResponse(email=email)


@router.post(
    "/verify-email/resend",
    response_model=EmailSentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resend_verification(
    request: Request,
    data: EmailRequest,
    service: Annotated[AccountService, Depends(get_account_service)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> EmailSentResponse:
    email = normalize_email(str(data.email))
    await enforce(limiter, MAIL_PER_IP, client_ip(request))
    await enforce(limiter, MAIL_PER_ADDRESS, email)
    await service.resend_verification(email)
    return EmailSentResponse(email=email)


@router.post("/verify-email", response_model=TokenResponse)
async def verify_email_code(
    request: Request,
    response: Response,
    data: VerifyCodeRequest,
    service: Annotated[AccountService, Depends(get_account_service)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> TokenResponse:
    """Подтвердить почту кодом из письма и сразу войти."""
    await enforce(limiter, VERIFY_PER_IP, client_ip(request))
    pair = await service.verify_by_code(str(data.email), data.code)
    return session_response(response, pair)


@router.post("/verify-email/link", response_model=TokenResponse)
async def verify_email_link(
    request: Request,
    response: Response,
    data: TokenRequest,
    service: Annotated[AccountService, Depends(get_account_service)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> TokenResponse:
    """Подтвердить почту по ссылке из письма и сразу войти."""
    await enforce(limiter, VERIFY_PER_IP, client_ip(request))
    pair = await service.verify_by_link(data.token)
    return session_response(response, pair)


@router.post(
    "/forgot-password",
    response_model=EmailSentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def forgot_password(
    request: Request,
    data: EmailRequest,
    service: Annotated[AccountService, Depends(get_account_service)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> EmailSentResponse:
    """Ссылка для нового пароля. Ответ одинаковый, есть учётка или нет."""
    email = normalize_email(str(data.email))
    await enforce(limiter, MAIL_PER_IP, client_ip(request))
    await enforce(limiter, MAIL_PER_ADDRESS, email)
    await service.forgot_password(email)
    return EmailSentResponse(email=email)


@router.post("/reset-password", response_model=TokenResponse)
async def reset_password(
    request: Request,
    response: Response,
    data: ResetPasswordRequest,
    service: Annotated[AccountService, Depends(get_account_service)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> TokenResponse:
    """Новый пароль по ссылке из письма: прежние сессии закрываются,
    открывается новая."""
    await enforce(limiter, VERIFY_PER_IP, client_ip(request))
    pair = await service.reset_password(
        data.token, data.new_password, data.second_factor
    )
    return session_response(response, pair)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> TokenResponse:
    """Новая пара по refresh-токену из cookie (ротация). Так же фронт
    восстанавливает сессию после перезагрузки страницы: access-токен
    живёт только в памяти вкладки."""
    ensure_same_origin(request)
    await enforce(limiter, REFRESH_PER_IP, client_ip(request))
    raw = read_refresh_cookie(request)
    if raw is None:
        raise NotAuthenticatedError("Нет refresh-токена")
    return session_response(response, await auth_service.refresh(raw))


@router.post("/switch-company", response_model=TokenResponse)
async def switch_company(
    request: Request,
    response: Response,
    data: SwitchCompanyRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    principal: Annotated[Principal, Depends(get_principal)],
) -> TokenResponse:
    """Перейти в другую свою компанию — новая пара токенов."""
    ensure_same_origin(request)
    pair = await auth_service.switch_company(
        principal.account, data.tenant_id, read_refresh_cookie(request)
    )
    return session_response(response, pair)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    principal: Annotated[Principal, Depends(get_principal_allow_password_change)],
) -> None:
    """Отозвать цепочку текущего входа и стереть cookie."""
    ensure_same_origin(request)
    await auth_service.logout(
        principal.account, principal.member, read_refresh_cookie(request)
    )
    clear_refresh_cookie(response)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_everywhere(
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    principal: Annotated[Principal, Depends(get_principal)],
) -> None:
    await auth_service.logout_everywhere(principal.account, principal.member)
    clear_refresh_cookie(response)


@router.post("/change-password", response_model=TokenResponse)
async def change_password(
    response: Response,
    data: ChangePasswordRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    principal: Annotated[Principal, Depends(get_principal_allow_password_change)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> TokenResponse:
    """Сменить пароль. Все прежние сессии закрываются, возвращается
    новая пара токенов для текущего устройства.

    Лимит — против подбора текущего пароля украденным access-токеном.
    Новый пароль проверяется по политике до лимита: человек, который
    подбирает пароль под правила, не должен упереться в «слишком много
    запросов» (стенд 02.10). Политика не трогает текущий пароль, так что
    перебору это ничего не даёт.
    """
    validate_password(data.new_password, email=principal.account.email)
    await enforce(limiter, PASSWORD_CHANGE_PER_USER, str(principal.account.id))
    pair = await auth_service.change_password(
        principal.account, principal.member, data.current_password, data.new_password
    )
    return session_response(response, pair)
