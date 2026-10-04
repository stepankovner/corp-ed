"""Заявки «Подключить компанию» (ТЗ §2, решение 03.10).

Человек с учёткой без компании подаёт заявку; команда Kronto одобряет
(cli requests, позже — наша панель): создаётся компания, заявитель
становится её администратором и получает письмо. Самостоятельное
создание с оплатой — после первого пилота.
"""

import re
import secrets
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import (
    CompanyRequestExistsError,
    ConflictError,
    NotFoundError,
)
from corp_ed.domain.models import Account, CompanyRequest, Tenant
from corp_ed.domain.tariffs import DEFAULT_TARIFF, Tariff
from corp_ed.repositories.account_repository import AccountRepository
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.company_request_repository import CompanyRequestRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services import email_templates
from corp_ed.services.email_service import EmailService
from corp_ed.services.team_notify import (
    NULL_NOTIFIER,
    TeamNotifier,
    company_request_message,
)
from corp_ed.services.tenant_service import TenantService

logger = structlog.get_logger()

DEFAULT_REQUEST_SEATS = 10

_TRANSLIT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
)  # fmt: skip


def company_code_for(name: str) -> str:
    """Внутренний код компании из названия: латиница и случайный хвост.

    Код больше не вводят при входе (ТЗ §2) — он нужен команде в cli и в
    логах. Хвост делает его уникальным без перебора занятых.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold().translate(_TRANSLIT))
    slug = slug.strip("-")[:40].strip("-") or "company"
    if not slug[0].isalnum():
        slug = f"c{slug}"
    return f"{slug}-{secrets.token_hex(2)}"


class CompanyRequestService:
    def __init__(
        self,
        session: AsyncSession,
        audit: AuditRepository,
        notifier: TeamNotifier = NULL_NOTIFIER,
    ) -> None:
        self.session = session
        self.audit = audit
        self.notifier = notifier
        self.requests = CompanyRequestRepository(session)

    async def create(
        self,
        account: Account,
        *,
        company_name: str,
        seats: int | None,
        comment: str | None,
    ) -> CompanyRequest:
        if await self.requests.open_for_account(account.id) is not None:
            raise CompanyRequestExistsError()
        request = await self.requests.add(
            CompanyRequest(
                account_id=account.id,
                company_name=" ".join(company_name.split()),
                seats=seats,
                comment=(comment or "").strip() or None,
            )
        )
        self.audit.record(
            AuditAction.COMPANY_REQUESTED,
            target_type="company_request",
            target_id=request.id,
            details={"account_id": str(account.id)},
        )
        await self.session.commit()
        # В Telegram — без названия и контактов: только факт и места.
        self.notifier.notify(company_request_message(seats=seats))
        logger.info("company_requested", request_id=str(request.id))
        return request

    async def list_mine(self, account: Account) -> list[CompanyRequest]:
        return await self.requests.list_by_account(account.id)

    async def cancel(self, account: Account, request_id: UUID) -> CompanyRequest:
        request = await self.requests.get_for_update(request_id)
        if request is None or request.account_id != account.id:
            raise NotFoundError("Заявка не найдена")
        if request.status == "new":
            request.status = "cancelled"
            request.decided_at = _now()
            await self.session.commit()
        return request

    # --- команда Kronto (cli) -------------------------------------------------

    async def list(self, status: str | None = "new") -> list[CompanyRequest]:
        return await self.requests.list_by_status(status)

    async def approve(
        self,
        request_id: UUID,
        *,
        seats: int | None = None,
        tariff: Tariff = DEFAULT_TARIFF,
        company_code: str | None = None,
    ) -> Tenant:
        request = await self._open(request_id)
        account = await AccountRepository(self.session).get(request.account_id)
        if account is None:
            raise NotFoundError("Учётка заявителя удалена")
        tenants = TenantService(
            TenantRepository(self.session),
            UserRepository(self.session),
            self.audit,
            self.session,
        )
        provisioned = await tenants.provision(
            company_code=company_code or company_code_for(request.company_name),
            name=request.company_name,
            admin_email=account.email,
            admin_full_name=None,
            admin_password=None,
            seats=seats or request.seats or DEFAULT_REQUEST_SEATS,
            tariff=tariff,
            commit=False,
        )
        request.status = "approved"
        request.tenant_id = provisioned.tenant.id
        request.decided_at = _now()
        account.last_tenant_id = provisioned.tenant.id
        self.audit.record(
            AuditAction.COMPANY_REQUEST_APPROVED,
            tenant_id=provisioned.tenant.id,
            target_type="company_request",
            target_id=request.id,
        )
        mail = EmailService(self.session)
        mail.enqueue(
            account.email,
            email_templates.company_approved(
                name=account.first_name,
                company=request.company_name,
                url=mail.url("/"),
            ),
        )
        await self.session.commit()
        logger.info(
            "company_request_approved",
            request_id=str(request.id),
            tenant_id=str(provisioned.tenant.id),
        )
        return provisioned.tenant

    async def reject(self, request_id: UUID) -> CompanyRequest:
        request = await self._open(request_id)
        request.status = "rejected"
        request.decided_at = _now()
        account = await AccountRepository(self.session).get(request.account_id)
        if account is not None:
            EmailService(self.session).enqueue(
                account.email,
                email_templates.company_rejected(
                    name=account.first_name, company=request.company_name
                ),
            )
        self.audit.record(
            AuditAction.COMPANY_REQUEST_REJECTED,
            target_type="company_request",
            target_id=request.id,
        )
        await self.session.commit()
        return request

    async def _open(self, request_id: UUID) -> CompanyRequest:
        request = await self.requests.get_for_update(request_id)
        if request is None:
            raise NotFoundError("Заявка не найдена")
        if request.status != "new":
            raise ConflictError("Заявка уже рассмотрена")
        return request


def _now() -> datetime:
    return datetime.now(UTC)
