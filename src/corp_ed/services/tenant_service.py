import re
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import ConflictError, DomainError
from corp_ed.core.security import generate_temporary_password, hash_password
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Tenant, User, UserRole
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository

logger = structlog.get_logger()

# Код компании вводится при входе и попадает в логи: только латиница в
# нижнем регистре, цифры и дефис — без пробелов, юникода и спецсимволов.
_COMPANY_CODE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class InvalidCompanyCodeError(DomainError):
    def __init__(self) -> None:
        super().__init__(
            "Код компании: 2–63 символа, латиница в нижнем регистре, цифры, дефис"
        )


@dataclass(frozen=True)
class ProvisionedTenant:
    tenant: Tenant
    admin: User
    temporary_password: str


class TenantService:
    """Заведение компаний. Только для команды Kronto, только из CLI.

    По досье (10.1) компанию подключает команда после созвона, а не
    клиент сам. Поэтому HTTP-ручки для этого нет вовсе: нельзя
    атаковать то, чего нет в сети. См. corp_ed.cli.
    """

    def __init__(
        self,
        tenant_repo: TenantRepository,
        user_repo: UserRepository,
        audit: AuditRepository,
        session: AsyncSession,
    ) -> None:
        self.tenant_repo = tenant_repo
        self.user_repo = user_repo
        self.audit = audit
        self.session = session

    async def provision(
        self,
        *,
        company_code: str,
        name: str,
        admin_email: str,
        admin_full_name: str | None,
    ) -> ProvisionedTenant:
        """Создать компанию и её первого администратора одной транзакцией."""
        code = company_code.strip().casefold()
        if not _COMPANY_CODE.fullmatch(code):
            raise InvalidCompanyCodeError()
        if await self.tenant_repo.get_by_company_code(code) is not None:
            raise ConflictError(f"Компания с кодом '{code}' уже существует")

        tenant = await self.tenant_repo.create(Tenant(company_code=code, name=name))
        temporary = generate_temporary_password()

        with tenant_scope(tenant.id):
            admin = await self.user_repo.create(
                User(
                    email=admin_email.casefold(),
                    full_name=admin_full_name,
                    role=UserRole.ADMIN,
                    hashed_password=hash_password(temporary),
                    must_change_password=True,
                )
            )
            # actor_id пуст: действие выполнено из CLI на сервере, а не
            # пользователем системы. Это и есть отметка «сделала команда».
            self.audit.record(
                AuditAction.TENANT_CREATED,
                tenant_id=tenant.id,
                target_type="tenant",
                target_id=tenant.id,
                details={"company_code": code, "admin_user_id": str(admin.id)},
            )
            await self.session.commit()

        logger.info("tenant_provisioned", tenant_id=str(tenant.id), company_code=code)
        return ProvisionedTenant(
            tenant=tenant, admin=admin, temporary_password=temporary
        )

    async def set_active(self, company_code: str, *, active: bool) -> Tenant:
        """Приостановить или вернуть компанию.

        Токены пользователей приостановленной компании перестают
        приниматься сразу: get_current_user проверяет статус тенанта
        на каждый запрос.
        """
        tenant = await self.tenant_repo.get_by_company_code(company_code)
        if tenant is None:
            raise ConflictError(f"Компании с кодом '{company_code}' нет")
        tenant.is_active = active
        self.audit.record(
            AuditAction.TENANT_RESUMED if active else AuditAction.TENANT_SUSPENDED,
            tenant_id=tenant.id,
            target_type="tenant",
            target_id=tenant.id,
        )
        await self.session.commit()
        logger.info("tenant_status_changed", tenant_id=str(tenant.id), active=active)
        return tenant
