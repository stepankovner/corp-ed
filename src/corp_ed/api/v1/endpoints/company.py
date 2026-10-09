"""Настройки компании, логотип и заявка на смену тарифа (ТЗ §7)."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile, status

from corp_ed.api.v1.dependencies import get_company_service, require_role
from corp_ed.api.v1.rate_limits import (
    COMPANY_EDIT_PER_TENANT,
    TARIFF_REQUEST_PER_TENANT,
    limit_by_tenant,
)
from corp_ed.api.v1.schemas.company import (
    CompanySettingsRequest,
    CompanySettingsResponse,
    LogoResponse,
    TariffRequest,
)
from corp_ed.domain.models import User, UserRole
from corp_ed.domain.tariffs import Tariff
from corp_ed.domain.types import NotFoundMode
from corp_ed.services.avatar_service import MAX_AVATAR_BYTES
from corp_ed.services.company_service import (
    UNSET,
    CompanyService,
    CompanySettings,
    InvalidLogoError,
)

router = APIRouter(prefix="/company", tags=["company"])

Admin = Annotated[User, Depends(require_role(UserRole.ADMIN))]
Service = Annotated[CompanyService, Depends(get_company_service)]
EDIT_LIMIT = [Depends(limit_by_tenant(COMPANY_EDIT_PER_TENANT))]


def _response(settings: CompanySettings) -> CompanySettingsResponse:
    tenant = settings.tenant
    return CompanySettingsResponse(
        id=tenant.id,
        name=tenant.name,
        company_code=tenant.company_code,
        logo_url=settings.logo_url,
        not_found_mode=NotFoundMode(tenant.not_found_mode),
        mfa_policy="strong" if tenant.mfa_policy == "strong" else "any",
        allow_remember_device=tenant.allow_remember_device,
        email_domains=list(tenant.email_domains or []),
        tariff=Tariff(tenant.tariff),
        seats=tenant.seats,
        members=settings.members,
        daily_credits_per_member=tenant.daily_credits_per_member,
    )


@router.get("", response_model=CompanySettingsResponse)
async def read_settings(service: Service, admin: Admin) -> CompanySettingsResponse:
    return _response(await service.settings())


@router.patch("", response_model=CompanySettingsResponse, dependencies=EDIT_LIMIT)
async def update_settings(
    data: CompanySettingsRequest, service: Service, admin: Admin
) -> CompanySettingsResponse:
    """Название, режим «ответа нет», второй фактор, «запомнить устройство»,
    домены почты, личный дневной лимит кредитов. Каждое изменение — в
    журнал действий."""
    fields = data.model_dump(exclude_unset=True)

    def value(name: str) -> object:
        return fields[name] if fields.get(name) is not None else UNSET

    settings = await service.update(
        admin,
        name=value("name"),  # type: ignore[arg-type]
        not_found_mode=value("not_found_mode"),  # type: ignore[arg-type]
        mfa_policy=value("mfa_policy"),  # type: ignore[arg-type]
        allow_remember_device=value("allow_remember_device"),  # type: ignore[arg-type]
        email_domains=value("email_domains"),  # type: ignore[arg-type]
        # null здесь значит «снять лимит», а не «не менять».
        daily_credits_per_member=(
            data.daily_credits_per_member
            if "daily_credits_per_member" in fields
            else UNSET
        ),
    )
    return _response(settings)


@router.put("/logo", response_model=LogoResponse, dependencies=EDIT_LIMIT)
async def upload_logo(
    file: Annotated[UploadFile, File(description="PNG, JPEG или WebP до 5 МБ")],
    service: Service,
    admin: Admin,
) -> LogoResponse:
    """Логотип: вписывается в квадрат 256×256 без обрезки, метаданные
    файла не сохраняются."""
    raw = await file.read(MAX_AVATAR_BYTES + 1)
    if len(raw) > MAX_AVATAR_BYTES:
        raise InvalidLogoError()
    return LogoResponse(logo_url=await service.save_logo(admin, raw))


@router.delete("/logo", status_code=status.HTTP_204_NO_CONTENT, dependencies=EDIT_LIMIT)
async def delete_logo(service: Service, admin: Admin) -> None:
    await service.remove_logo(admin)


@router.post(
    "/tariff-request",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(limit_by_tenant(TARIFF_REQUEST_PER_TENANT))],
)
async def request_tariff(data: TariffRequest, service: Service, admin: Admin) -> None:
    """«Сменить тариф»: заявка команде kronto; тариф меняет команда,
    оплата — после оформления ИП (ТЗ §10)."""
    await service.request_tariff(
        admin, tariff=data.tariff, seats=data.seats, comment=data.comment
    )
