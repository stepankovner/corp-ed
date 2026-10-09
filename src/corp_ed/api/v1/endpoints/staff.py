"""Наша панель (ТЗ §9): заявки на компании, компании, расход на модели,
помощь со входом, заявки на созвон. Только команда kronto с приложением
или ключом доступа (get_staff_account); остальным — 404."""

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.api.v1.dependencies import (
    get_account_service,
    get_company_request_service,
    get_lead_service,
    get_staff_account,
    get_staff_billing_service,
    get_staff_credit_service,
    get_staff_service,
    get_support_service,
    get_tenant_service,
)
from corp_ed.api.v1.endpoints.billing import invoice_response, subscription_response
from corp_ed.api.v1.endpoints.credits import order_response
from corp_ed.api.v1.rate_limits import (
    STAFF_EDIT_PER_ACCOUNT,
    STAFF_RESET_PER_ACCOUNT,
    enforce,
    get_rate_limiter,
)
from corp_ed.api.v1.schemas.billing import (
    StaffBillingResponse,
    StaffInvoiceResponse,
    StaffPaymentResolveRequest,
    StaffPaymentResponse,
    StaffSubscriptionResponse,
)
from corp_ed.api.v1.schemas.notification import (
    StaffSupportResponse,
    StaffSupportUpdate,
    SupportStatus,
)
from corp_ed.api.v1.schemas.staff import (
    SpendCompanyResponse,
    SpendDayResponse,
    SpendModelResponse,
    SpendResponse,
    StaffApproveRequest,
    StaffCompanyResponse,
    StaffCompanyUpdate,
    StaffLeadResponse,
    StaffLeadUpdate,
    StaffOverviewResponse,
    StaffPersonCompanyResponse,
    StaffPersonResponse,
    StaffRequestResponse,
)
from corp_ed.api.v1.schemas.usage import (
    StaffCreditGrantRequest,
    StaffCreditGrantResponse,
    StaffCreditOrderResponse,
)
from corp_ed.core.database import get_session
from corp_ed.core.exceptions import (
    CodedConflictError,
    InvalidBillingInputError,
    PaymentUnavailableError,
)
from corp_ed.core.rate_limit import RateLimiter
from corp_ed.core.tenant_context import current_tenant
from corp_ed.domain.leads import LeadStatus
from corp_ed.domain.models import Account, CompanyRequest
from corp_ed.domain.tariffs import Tariff
from corp_ed.services.account_service import AccountService
from corp_ed.services.company_request_service import CompanyRequestService
from corp_ed.services.credit_order_service import StaffCreditService, StaffOrder
from corp_ed.services.lead_service import LeadService
from corp_ed.services.payment_service import (
    SeatsPlan,
    StaffBillingService,
    StaffInvoice,
)
from corp_ed.services.seats import seats_check
from corp_ed.services.staff_service import CompanyRow, Person, StaffService
from corp_ed.services.support_service import SupportItem, SupportService
from corp_ed.services.team_notify import seats_topup_failed_message
from corp_ed.services.tenant_service import TenantService

logger = structlog.get_logger()

router = APIRouter(prefix="/staff", tags=["staff"])

Staff = Annotated[Account, Depends(get_staff_account)]
Service = Annotated[StaffService, Depends(get_staff_service)]
Credits = Annotated[StaffCreditService, Depends(get_staff_credit_service)]
Payments = Annotated[StaffBillingService, Depends(get_staff_billing_service)]
Limiter = Annotated[RateLimiter, Depends(get_rate_limiter)]


def _company(row: CompanyRow) -> StaffCompanyResponse:
    tenant = row.tenant
    return StaffCompanyResponse(
        id=tenant.id,
        name=tenant.name,
        company_code=tenant.company_code,
        is_active=tenant.is_active,
        tariff=Tariff(tenant.tariff),
        seats=tenant.seats,
        pilot_until=tenant.pilot_until,
        members=row.members,
        pending=row.pending,
        admins=row.admins,
        credits_used=row.credits_used,
        pool=row.pool,
        purchased_credits=row.purchased,
        questions_month=row.questions_month,
        last_question_at=row.last_question_at,
        documents=row.documents,
        connectors=row.connectors,
    )


def _person(person: Person) -> StaffPersonResponse:
    account = person.account
    return StaffPersonResponse(
        id=account.id,
        email=account.email,
        full_name=account.full_name,
        email_verified=account.email_verified_at is not None,
        created_at=account.created_at,
        last_login_at=account.last_login_at,
        must_change_password=account.must_change_password,
        totp=person.totp,
        passkeys=person.passkeys,
        backup_codes=person.backup_codes,
        sessions=person.sessions,
        staff=person.staff,
        companies=[
            StaffPersonCompanyResponse(
                tenant_id=item.tenant_id,
                company_name=item.company_name,
                role=item.role,
                status=item.status,
                last_login_at=item.last_login_at,
            )
            for item in person.companies
        ],
    )


@router.get("/overview", response_model=StaffOverviewResponse)
async def overview(service: Service, staff: Staff) -> StaffOverviewResponse:
    data = await service.overview()
    return StaffOverviewResponse(
        companies=data.companies,
        active_companies=data.active_companies,
        pilots_ending=data.pilots_ending,
        requests_new=data.requests_new,
        accounts=data.accounts,
    )


# --- заявки «Подключить компанию» ---------------------------------------------


@router.get("/requests", response_model=list[StaffRequestResponse])
async def list_requests(
    requests: Annotated[CompanyRequestService, Depends(get_company_request_service)],
    session: Annotated[AsyncSession, Depends(get_session)],
    staff: Staff,
    state: Annotated[str, Query(alias="status", pattern="^(new|all)$")] = "new",
) -> list[StaffRequestResponse]:
    items: list[CompanyRequest] = await requests.list("new" if state == "new" else None)
    accounts = {
        account.id: account
        for account in (
            await session.scalars(
                select(Account).where(Account.id.in_({i.account_id for i in items}))
            )
        ).all()
    }
    result = []
    for item in items:
        applicant = accounts.get(item.account_id)
        result.append(
            StaffRequestResponse(
                id=item.id,
                company_name=item.company_name,
                seats=item.seats,
                comment=item.comment,
                status=item.status,  # type: ignore[arg-type]
                created_at=item.created_at,
                decided_at=item.decided_at,
                tenant_id=item.tenant_id,
                applicant_email=applicant.email if applicant else None,
                applicant_name=applicant.full_name if applicant else None,
            )
        )
    return result


@router.post("/requests/{request_id}/approve", response_model=StaffCompanyResponse)
async def approve_request(
    request_id: UUID,
    body: StaffApproveRequest,
    requests: Annotated[CompanyRequestService, Depends(get_company_request_service)],
    service: Service,
    staff: Staff,
    limiter: Limiter,
) -> StaffCompanyResponse:
    """Создать компанию: заявитель — администратор, ему уходит письмо."""
    await enforce(limiter, STAFF_EDIT_PER_ACCOUNT, str(staff.id))
    tenant = await requests.approve(request_id, seats=body.seats, tariff=body.tariff)
    if body.pilot_until is not None:
        await service.set_pilot(tenant.id, body.pilot_until)
    return _company(await service.company(tenant.id))


@router.post(
    "/requests/{request_id}/reject",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def reject_request(
    request_id: UUID,
    requests: Annotated[CompanyRequestService, Depends(get_company_request_service)],
    staff: Staff,
    limiter: Limiter,
) -> Response:
    await enforce(limiter, STAFF_EDIT_PER_ACCOUNT, str(staff.id))
    await requests.reject(request_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- компании ------------------------------------------------------------------


@router.get("/companies", response_model=list[StaffCompanyResponse])
async def list_companies(service: Service, staff: Staff) -> list[StaffCompanyResponse]:
    return [_company(row) for row in await service.companies()]


@router.get("/companies/{tenant_id}", response_model=StaffCompanyResponse)
async def read_company(
    tenant_id: UUID, service: Service, staff: Staff
) -> StaffCompanyResponse:
    return _company(await service.company(tenant_id))


@router.patch("/companies/{tenant_id}", response_model=StaffCompanyResponse)
async def update_company(
    tenant_id: UUID,
    body: StaffCompanyUpdate,
    service: Service,
    tenants: Annotated[TenantService, Depends(get_tenant_service)],
    session: Annotated[AsyncSession, Depends(get_session)],
    payments: Payments,
    staff: Staff,
    limiter: Limiter,
) -> StaffCompanyResponse:
    """Тариф, места, срок пилота, приостановка — как cli set-tariff,
    set-seats, suspend-tenant, но с журналом от имени команды.

    С подключённой оплатой и оплаченным периодом места добавляются сразу
    с доплатой пропорционально оставшимся дням, а сокращаются со
    следующего периода (решение владельца 09.10)."""
    await enforce(limiter, STAFF_EDIT_PER_ACCOUNT, str(staff.id))
    row = await service.company(tenant_id)
    code = row.tenant.company_code
    fields = body.model_fields_set
    # Сначала все проверки, потом изменения: каждое изменение коммитится
    # само, и отказ посреди списка оставил бы часть применённой.
    pause = body.is_active is False and row.tenant.is_active
    if pause and current_tenant.get() == tenant_id:
        # Токен этой компании перестал бы приниматься — панель
        # закрылась бы посреди работы.
        raise CodedConflictError(
            "Свою компанию из панели не приостановить — "
            "переключитесь на другую или используйте cli suspend-tenant",
            "own_company",
        )
    current_seats = row.tenant.seats
    new_seats = body.seats if body.seats not in (None, current_seats) else None
    plan = (
        await payments.plan_seats(tenant_id, current_seats, new_seats)
        if new_seats is not None
        else SeatsPlan()
    )
    if new_seats is not None and not plan.defer:
        check = await seats_check(session, code, new_seats)
        if check.stops_pool and not body.confirm:
            raise CodedConflictError(check.message or "", "seats_stop_pool")

    if new_seats is not None and plan.defer:
        await payments.defer_seats(tenant_id, new_seats)
    elif new_seats is not None:
        await tenants.set_seats(code, new_seats)
        if plan.topup:
            try:
                await payments.issue_topup(tenant_id, new_seats - current_seats)
            except (PaymentUnavailableError, InvalidBillingInputError) as exc:
                # Места уже добавлены; доплату команда выставит сама.
                logger.warning("billing_topup_failed", error=type(exc).__name__)
                payments.billing.notifier.notify(
                    seats_topup_failed_message(company_code=code)
                )
    if body.tariff is not None and body.tariff.value != row.tenant.tariff:
        await tenants.set_tariff(code, body.tariff)
    if "pilot_until" in fields:
        await service.set_pilot(tenant_id, body.pilot_until)
    if body.is_active is not None and body.is_active != row.tenant.is_active:
        await tenants.set_active(code, active=body.is_active)
    return _company(await service.company(tenant_id))


# --- кредиты: заказы пакетов и начисления -----------------------------------------


def _order(item: StaffOrder) -> StaffCreditOrderResponse:
    order = item.order
    return StaffCreditOrderResponse(
        **order_response(order).model_dump(),
        tenant_id=item.tenant.id,
        company_name=item.tenant.name,
        company_code=item.tenant.company_code,
    )


@router.get("/credit-orders", response_model=list[StaffCreditOrderResponse])
async def list_credit_orders(
    credits: Credits,
    staff: Staff,
    state: Annotated[
        str, Query(alias="status", pattern="^(awaiting_payment|all)$")
    ] = "awaiting_payment",
) -> list[StaffCreditOrderResponse]:
    """Заказы пакетов всех компаний: по умолчанию — ждут оплаты."""
    items = await credits.orders(None if state == "all" else state)
    return [_order(item) for item in items]


@router.post(
    "/companies/{tenant_id}/credit-orders/{order_id}/paid",
    response_model=StaffCreditOrderResponse,
)
async def mark_credit_order_paid(
    tenant_id: UUID,
    order_id: UUID,
    credits: Credits,
    staff: Staff,
    limiter: Limiter,
) -> StaffCreditOrderResponse:
    """Оплата по счёту пришла: кредиты зачисляются на 12 месяцев,
    администраторам компании — «Кредиты зачислены»."""
    await enforce(limiter, STAFF_EDIT_PER_ACCOUNT, str(staff.id))
    return _order(await credits.mark_paid(tenant_id, order_id))


@router.post(
    "/companies/{tenant_id}/credit-orders/{order_id}/cancel",
    response_model=StaffCreditOrderResponse,
)
async def cancel_credit_order(
    tenant_id: UUID,
    order_id: UUID,
    credits: Credits,
    staff: Staff,
    limiter: Limiter,
) -> StaffCreditOrderResponse:
    """Отменить неоплаченный заказ."""
    await enforce(limiter, STAFF_EDIT_PER_ACCOUNT, str(staff.id))
    return _order(await credits.cancel(tenant_id, order_id))


@router.post(
    "/companies/{tenant_id}/credits",
    response_model=StaffCreditGrantResponse,
    status_code=status.HTTP_201_CREATED,
)
async def grant_credits(
    tenant_id: UUID,
    body: StaffCreditGrantRequest,
    credits: Credits,
    staff: Staff,
    limiter: Limiter,
) -> StaffCreditGrantResponse:
    """Начислить кредиты без заказа (бонус, компенсация): на 12 месяцев,
    комментарий — в журнал действий компании."""
    await enforce(limiter, STAFF_EDIT_PER_ACCOUNT, str(staff.id))
    grant = await credits.grant(tenant_id, body.credits, body.comment.strip())
    return StaffCreditGrantResponse(
        id=grant.id, credits=grant.credits, expires_at=grant.expires_at
    )


# --- оплата: подписки, счета, платежи из банка -------------------------------------


def _staff_invoice(item: StaffInvoice) -> StaffInvoiceResponse:
    return StaffInvoiceResponse(
        **invoice_response(item.invoice).model_dump(),
        tenant_id=item.tenant.id,
        company_name=item.tenant.name,
        company_code=item.tenant.company_code,
        payer_name=item.invoice.payer_name,
        payer_inn=item.invoice.payer_inn,
    )


@router.get("/billing", response_model=StaffBillingResponse)
async def billing_overview(payments: Payments, staff: Staff) -> StaffBillingResponse:
    """Подписки компаний: просроченные первыми. enabled=false — банк не
    подключён (PAYMENTS_PROVIDER=none), оплату отмечают вручную."""
    items = await payments.subscriptions()
    return StaffBillingResponse(
        enabled=payments.billing.enabled,
        provider=payments.billing.settings.provider,
        subscriptions=[
            StaffSubscriptionResponse(
                **subscription_response(item.subscription).model_dump(),
                tenant_id=item.tenant.id,
                company_name=item.tenant.name,
                company_code=item.tenant.company_code,
                is_active=item.tenant.is_active,
            )
            for item in items
        ],
    )


@router.get("/billing/invoices", response_model=list[StaffInvoiceResponse])
async def list_invoices(
    payments: Payments,
    staff: Staff,
    state: Annotated[
        str, Query(alias="status", pattern="^(awaiting_payment|all)$")
    ] = "awaiting_payment",
) -> list[StaffInvoiceResponse]:
    """Счета и ссылки всех компаний; по умолчанию — ждут оплаты."""
    items = await payments.invoices(None if state == "all" else state)
    return [_staff_invoice(item) for item in items]


@router.post(
    "/companies/{tenant_id}/invoices/{invoice_id}/paid",
    response_model=StaffInvoiceResponse,
)
async def mark_invoice_paid(
    tenant_id: UUID,
    invoice_id: UUID,
    payments: Payments,
    staff: Staff,
    limiter: Limiter,
) -> StaffInvoiceResponse:
    """Оплата пришла мимо автоматики: счёт оплачен, услуга зачислена (как
    по вебхуку)."""
    await enforce(limiter, STAFF_EDIT_PER_ACCOUNT, str(staff.id))
    return _staff_invoice(await payments.mark_paid(tenant_id, invoice_id))


@router.post(
    "/companies/{tenant_id}/invoices/{invoice_id}/cancel",
    response_model=StaffInvoiceResponse,
)
async def cancel_invoice(
    tenant_id: UUID,
    invoice_id: UUID,
    payments: Payments,
    staff: Staff,
    limiter: Limiter,
) -> StaffInvoiceResponse:
    await enforce(limiter, STAFF_EDIT_PER_ACCOUNT, str(staff.id))
    return _staff_invoice(await payments.cancel(tenant_id, invoice_id))


@router.get("/billing/payments", response_model=list[StaffPaymentResponse])
async def list_payments(
    payments: Payments,
    staff: Staff,
    state: Annotated[str, Query(alias="status", pattern="^(review|all)$")] = "review",
) -> list[StaffPaymentResponse]:
    """Платежи из банка: по умолчанию — те, что ждут разбора (не сошлись
    сумма, ИНН, номер; банк не подтвердил)."""
    rows = await payments.payments(review_only=state == "review")
    return [
        StaffPaymentResponse(
            id=row.id,
            kind=row.kind,  # type: ignore[arg-type]
            status=row.status,  # type: ignore[arg-type]
            problem=row.problem,
            amount_kopecks=row.amount_kopecks,
            payer_inn=row.payer_inn,
            payer_name=row.payer_name,
            purpose=row.purpose,
            tenant_id=row.tenant_id,
            company_name=tenant.name if tenant else None,
            invoice_id=row.invoice_id,
            note=row.note,
            created_at=row.created_at,
        )
        for row, tenant in rows
    ]


@router.post(
    "/billing/payments/{event_id}/resolve",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def resolve_payment(
    event_id: UUID,
    body: StaffPaymentResolveRequest,
    payments: Payments,
    staff: Staff,
    limiter: Limiter,
) -> Response:
    """Ручной разбор: зачесть платёж в счёт (tenant_id и invoice_id) или
    закрыть без зачёта — с комментарием (возврат, чужой платёж)."""
    await enforce(limiter, STAFF_EDIT_PER_ACCOUNT, str(staff.id))
    if (body.tenant_id is None) != (body.invoice_id is None):
        raise CodedConflictError(
            "Укажите и компанию, и счёт — или ни того, ни другого",
            "invoice_and_company_required",
        )
    if body.invoice_id is None and not (body.note or "").strip():
        raise CodedConflictError(
            "Без зачёта нужен комментарий: почему платёж закрыт", "note_required"
        )
    await payments.resolve(
        event_id,
        staff_account=staff.id,
        tenant_id=body.tenant_id,
        invoice_id=body.invoice_id,
        note=body.note,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- расход --------------------------------------------------------------------


@router.get("/spend", response_model=SpendResponse)
async def spend(
    service: Service,
    staff: Staff,
    days: Annotated[int, Query(ge=1, le=90)] = 30,
) -> SpendResponse:
    data = await service.spend(days)
    return SpendResponse(
        since=data.since,
        until=data.until,
        questions=data.questions,
        input_tokens=data.input_tokens,
        output_tokens=data.output_tokens,
        credits=data.credits,
        rub=data.rub,
        rub_per_1k_tokens=data.rub_per_1k_tokens,
        days=[
            SpendDayResponse(
                day=d.day, questions=d.questions, tokens=d.tokens, credits=d.credits
            )
            for d in data.days
        ],
        models=[
            SpendModelResponse(
                model=m.model,
                questions=m.questions,
                input_tokens=m.input_tokens,
                output_tokens=m.output_tokens,
            )
            for m in data.models
        ],
        companies=[
            SpendCompanyResponse(
                tenant_id=c.tenant_id,
                name=c.name,
                company_code=c.company_code,
                questions=c.questions,
                tokens=c.tokens,
                credits=c.credits,
            )
            for c in data.companies
        ],
    )


# --- люди ----------------------------------------------------------------------


@router.get("/people", response_model=list[StaffPersonResponse])
async def search_people(
    service: Service,
    staff: Staff,
    q: Annotated[str, Query(max_length=254)] = "",
) -> list[StaffPersonResponse]:
    """Почта или имя, от трёх символов."""
    return [_person(person) for person in await service.search_people(q)]


@router.get("/people/{account_id}", response_model=StaffPersonResponse)
async def read_person(
    account_id: UUID, service: Service, staff: Staff
) -> StaffPersonResponse:
    return _person(await service.person(account_id))


@router.post(
    "/people/{account_id}/password-reset",
    status_code=status.HTTP_202_ACCEPTED,
    response_class=Response,
)
async def send_password_reset(
    account_id: UUID,
    service: Service,
    accounts: Annotated[AccountService, Depends(get_account_service)],
    staff: Staff,
    limiter: Limiter,
) -> Response:
    """Письмо со ссылкой на новый пароль — то же, что «Забыли пароль?».
    Пароль команда не видит и не задаёт."""
    await enforce(limiter, STAFF_RESET_PER_ACCOUNT, str(staff.id))
    person = await service.person(account_id)
    await accounts.forgot_password(person.account.email)
    return Response(status_code=status.HTTP_202_ACCEPTED)


# --- заявки на созвон ----------------------------------------------------------


def _lead(lead: object) -> StaffLeadResponse:
    return StaffLeadResponse.model_validate(lead, from_attributes=True)


@router.get("/leads", response_model=list[StaffLeadResponse])
async def list_leads(
    leads: Annotated[LeadService, Depends(get_lead_service)],
    staff: Staff,
    state: Annotated[LeadStatus | None, Query(alias="status")] = None,
) -> list[StaffLeadResponse]:
    return [_lead(lead) for lead in await leads.list_recent(status=state, limit=200)]


@router.patch("/leads/{lead_id}", response_model=StaffLeadResponse)
async def update_lead(
    lead_id: UUID,
    body: StaffLeadUpdate,
    leads: Annotated[LeadService, Depends(get_lead_service)],
    staff: Staff,
    limiter: Limiter,
) -> StaffLeadResponse:
    await enforce(limiter, STAFF_EDIT_PER_ACCOUNT, str(staff.id))
    return _lead(await leads.set_status(lead_id, body.status))


# --- обращения в поддержку (ТЗ §8) -------------------------------------------


def _support(item: SupportItem) -> StaffSupportResponse:
    request = item.request
    return StaffSupportResponse(
        id=request.id,
        topic=request.topic,  # type: ignore[arg-type]
        message=request.message,
        status=request.status,  # type: ignore[arg-type]
        created_at=request.created_at,
        updated_at=request.updated_at,
        email=item.email,
        name=item.name,
        company=item.company,
    )


@router.get("/support", response_model=list[StaffSupportResponse])
async def list_support(
    support: Annotated[SupportService, Depends(get_support_service)],
    staff: Staff,
    state: Annotated[SupportStatus | None, Query(alias="status")] = None,
) -> list[StaffSupportResponse]:
    """Обращения: текст и почта — только здесь, в Telegram их нет."""
    return [_support(item) for item in await support.list(state)]


@router.patch("/support/{request_id}", response_model=StaffSupportResponse)
async def update_support(
    request_id: UUID,
    body: StaffSupportUpdate,
    support: Annotated[SupportService, Depends(get_support_service)],
    staff: Staff,
    limiter: Limiter,
) -> StaffSupportResponse:
    await enforce(limiter, STAFF_EDIT_PER_ACCOUNT, str(staff.id))
    return _support(await support.set_status(request_id, body.status))
