from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from corp_ed.api.v1.dependencies import (
    get_auth_service,
    get_current_user,
    get_current_user_allow_password_change,
    get_tenant_repository,
)
from corp_ed.api.v1.rate_limits import (
    LOGIN_FAILURES_PER_ACCOUNT,
    LOGIN_PER_IP,
    PASSWORD_CHANGE_PER_USER,
    REFRESH_PER_IP,
    client_ip,
    enforce,
    ensure_not_locked,
    forget,
    get_rate_limiter,
    record,
)
from corp_ed.api.v1.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    MeResponse,
    TokenResponse,
)
from corp_ed.api.v1.session_cookie import (
    clear_refresh_cookie,
    ensure_same_origin,
    read_refresh_cookie,
    session_response,
)
from corp_ed.core.exceptions import InvalidCredentialsError, NotAuthenticatedError
from corp_ed.core.password_policy import validate_password
from corp_ed.core.rate_limit import RateLimiter
from corp_ed.domain.models import User
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=MeResponse)
async def read_me(
    current_user: Annotated[User, Depends(get_current_user_allow_password_change)],
    tenant_repo: Annotated[TenantRepository, Depends(get_tenant_repository)],
) -> MeResponse:
    """Кто вошёл. Доступна и до смены временного пароля: фронту нужно
    знать must_change_password, чтобы показать форму смены."""
    tenant = await tenant_repo.get_by_id(current_user.tenant_id)
    return MeResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
        tenant_id=current_user.tenant_id,
        company_name=tenant.name if tenant else "",
        company_code=tenant.company_code if tenant else "",
        must_change_password=current_user.must_change_password,
        last_login_at=current_user.last_login_at,
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    request: Request,
    response: Response,
    data: LoginRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> TokenResponse:
    """Вход. Два лимита против перебора (ASVS 6.3.1):

    - на IP — все попытки: один адрес не перебирает много учёток;
    - на учётку — только неудачные: распределённый перебор одной учётки
      с многих адресов. Успешный вход счётчик обнуляет.
    """
    await enforce(limiter, LOGIN_PER_IP, client_ip(request))
    account = f"{data.company_code.casefold()}:{data.email.casefold()}"
    await ensure_not_locked(limiter, LOGIN_FAILURES_PER_ACCOUNT, account)

    try:
        pair = await auth_service.login(data.company_code, data.email, data.password)
    except InvalidCredentialsError:
        await record(limiter, LOGIN_FAILURES_PER_ACCOUNT, account)
        raise

    await forget(limiter, LOGIN_FAILURES_PER_ACCOUNT, account)
    return session_response(response, pair)


@router.post(
    "/refresh",
    response_model=TokenResponse,
)
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


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    current_user: Annotated[User, Depends(get_current_user_allow_password_change)],
) -> None:
    """Отозвать цепочку текущего входа и стереть cookie."""
    ensure_same_origin(request)
    await auth_service.logout(current_user, read_refresh_cookie(request))
    clear_refresh_cookie(response)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_everywhere(
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    await auth_service.logout_everywhere(current_user)
    clear_refresh_cookie(response)


@router.post("/change-password", response_model=TokenResponse)
async def change_password(
    response: Response,
    data: ChangePasswordRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    current_user: Annotated[User, Depends(get_current_user_allow_password_change)],
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
    validate_password(data.new_password, email=current_user.email)
    await enforce(limiter, PASSWORD_CHANGE_PER_USER, str(current_user.id))
    pair = await auth_service.change_password(
        current_user, data.current_password, data.new_password
    )
    return session_response(response, pair)
