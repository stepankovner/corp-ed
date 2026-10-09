"""Оплата подписки и пакетов (решения владельца 09.10): страница тарифа
администратора, реквизиты компании, PDF счетов и актов, вебхук банка.

С PAYMENTS_PROVIDER=none ручки отвечают, что оплата через банк не
подключена (enabled=false, 409 payments_disabled), вебхука нет (404)."""

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
)

from corp_ed.api.v1.dependencies import (
    get_billing,
    get_billing_service,
    get_payment_service,
    get_requisites_service,
    require_role,
)
from corp_ed.api.v1.rate_limits import (
    BILLING_CHOICE_PER_TENANT,
    COMPANY_EDIT_PER_TENANT,
    PAYMENT_WEBHOOK_PER_IP,
    limit_by_ip,
    limit_by_tenant,
)
from corp_ed.api.v1.schemas.billing import (
    ActResponse,
    BillingResponse,
    InvoiceResponse,
    QuoteResponse,
    RequisitesRequest,
    RequisitesResponse,
    SubscriptionChoiceRequest,
    SubscriptionChoiceResponse,
    SubscriptionResponse,
)
from corp_ed.core.exceptions import NotFoundError
from corp_ed.domain.billing import BillingPeriod, PaymentMethod, invoice_label
from corp_ed.domain.models import (
    Act,
    CompanyRequisites,
    Invoice,
    Subscription,
    User,
    UserRole,
)
from corp_ed.domain.tariffs import Tariff
from corp_ed.services.billing_pdf import PdfUnavailableError
from corp_ed.services.billing_service import (
    Billing,
    BillingService,
    RequisitesInput,
    RequisitesService,
)
from corp_ed.services.payment_service import PaymentService
from corp_ed.services.payments.provider import InvalidWebhookError

logger = structlog.get_logger()

router = APIRouter(prefix="/billing", tags=["billing"])
requisites_router = APIRouter(prefix="/company/requisites", tags=["company"])
payments_router = APIRouter(prefix="/payments", tags=["payments"])

Admin = Annotated[User, Depends(require_role(UserRole.ADMIN))]
Service = Annotated[BillingService, Depends(get_billing_service)]
Requisites = Annotated[RequisitesService, Depends(get_requisites_service)]


def requisites_response(item: CompanyRequisites) -> RequisitesResponse:
    return RequisitesResponse(
        legal_name=item.legal_name,
        inn=item.inn,
        kpp=item.kpp,
        payer_type="ip" if item.payer_type == "ip" else "company",
        address=item.address,
        documents_email=item.documents_email,
        updated_at=item.updated_at,
    )


def subscription_response(sub: Subscription) -> SubscriptionResponse:
    return SubscriptionResponse(
        tariff=Tariff(sub.tariff),
        seats=sub.seats,
        next_seats=sub.next_seats,
        period=sub.period,  # type: ignore[arg-type]
        payment_method=sub.payment_method,  # type: ignore[arg-type]
        status=sub.status,  # type: ignore[arg-type]
        current_start=sub.current_start,
        current_end=sub.current_end,
        card_amount_kopecks=sub.card_amount_kopecks
        if sub.payment_method == PaymentMethod.CARD.value
        else None,
    )


def invoice_response(item: Invoice) -> InvoiceResponse:
    return InvoiceResponse(
        id=item.id,
        number=invoice_label(item.number),
        kind=item.kind,  # type: ignore[arg-type]
        status=item.status,  # type: ignore[arg-type]
        payment_method=item.payment_method,  # type: ignore[arg-type]
        amount_kopecks=item.amount_kopecks,
        title=item.title,
        purpose=item.purpose,
        period_start=item.period_start,
        period_end=item.period_end,
        due_date=item.due_date,
        payment_url=item.payment_url if item.status == "awaiting_payment" else None,
        has_pdf=item.payment_method == PaymentMethod.INVOICE.value,
        created_at=item.created_at,
        paid_at=item.paid_at,
    )


def act_response(item: Act) -> ActResponse:
    return ActResponse(
        id=item.id,
        number=item.number,
        month=item.month,
        amount_kopecks=item.amount_kopecks,
        created_at=item.created_at,
    )


def _pdf(content: bytes, filename: str) -> Response:
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("", response_model=BillingResponse)
async def billing_overview(service: Service, admin: Admin) -> BillingResponse:
    """Подписка, цены периодов со скидкой, реквизиты, счета и акты."""
    data = await service.overview()
    return BillingResponse(
        enabled=data.enabled,
        tariff=Tariff(data.tariff),
        seats=data.seats,
        seat_price_kopecks=data.seat_price_kopecks,
        quotes=[
            QuoteResponse(
                period=quote.period.value,
                discount_percent=quote.discount_percent,
                amount_kopecks=quote.amount_kopecks,
            )
            for quote in data.quotes
        ],
        grace_days=data.grace_days,
        subscription=subscription_response(data.subscription)
        if data.subscription
        else None,
        requisites=requisites_response(data.requisites) if data.requisites else None,
        invoices=[invoice_response(item) for item in data.invoices],
        acts=[act_response(item) for item in data.acts],
    )


@router.post(
    "/subscription",
    response_model=SubscriptionChoiceResponse,
    dependencies=[Depends(limit_by_tenant(BILLING_CHOICE_PER_TENANT))],
)
async def choose_subscription(
    body: SubscriptionChoiceRequest, service: Service, admin: Admin
) -> SubscriptionChoiceResponse:
    """Период и способ оплаты. Первый раз — счёт или ссылка сразу; у
    оплаченной подписки выбор действует со следующего счёта."""
    invoice = await service.choose(
        admin, BillingPeriod(body.period), PaymentMethod(body.payment_method)
    )
    return SubscriptionChoiceResponse(
        invoice=invoice_response(invoice) if invoice is not None else None
    )


@router.get(
    "/invoices/{invoice_id}/pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def invoice_pdf(invoice_id: UUID, service: Service, admin: Admin) -> Response:
    """Счёт в PDF: от банка, если он отдаёт, иначе — своим шаблоном."""
    try:
        invoice, content = await service.invoice_pdf(invoice_id)
    except PdfUnavailableError as exc:
        logger.error("billing_pdf_font_missing")
        raise NotFoundError("Документ временно недоступен") from exc
    return _pdf(content, f"{invoice_label(invoice.number)}.pdf")


@router.get(
    "/acts/{act_id}/pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def act_pdf(act_id: UUID, service: Service, admin: Admin) -> Response:
    try:
        act, content = await service.act_pdf(act_id)
    except PdfUnavailableError as exc:
        logger.error("billing_pdf_font_missing")
        raise NotFoundError("Документ временно недоступен") from exc
    return _pdf(content, f"act-{act.month:%Y-%m}.pdf")


# --- реквизиты ------------------------------------------------------------------------


@requisites_router.get("", response_model=RequisitesResponse | None)
async def read_requisites(
    service: Requisites, admin: Admin
) -> RequisitesResponse | None:
    """Реквизиты компании для счёта; null — ещё не заполнены."""
    item = await service.get()
    return requisites_response(item) if item is not None else None


@requisites_router.put(
    "",
    response_model=RequisitesResponse,
    dependencies=[Depends(limit_by_tenant(COMPANY_EDIT_PER_TENANT))],
)
async def save_requisites(
    body: RequisitesRequest, service: Requisites, admin: Admin
) -> RequisitesResponse:
    """422 invalid_requisites с полем field — ИНН с ошибкой в контрольных
    цифрах, КПП не того вида, КПП у ИП."""
    item = await service.save(
        admin,
        RequisitesInput(
            legal_name=body.legal_name,
            inn=body.inn,
            kpp=body.kpp,
            address=body.address,
            documents_email=str(body.documents_email) if body.documents_email else None,
        ),
    )
    return requisites_response(item)


# --- вебхук банка ---------------------------------------------------------------------


@payments_router.post(
    "/tochka/webhook",
    include_in_schema=False,
    dependencies=[Depends(limit_by_ip(PAYMENT_WEBHOOK_PER_IP))],
)
async def tochka_webhook(
    request: Request,
    background: BackgroundTasks,
    billing: Annotated[Billing, Depends(get_billing)],
    payments: Annotated[PaymentService, Depends(get_payment_service)],
) -> dict[str, bool]:
    """Вебхук Точки: без входа пользователя, тело — строка JWT (RS256).

    Подпись не сошлась — 400 и ни одной записи в базе. Сошлась —
    событие записывается (повтор доставки — та же строка) и банк сразу
    получает 200: разбор с запросом в банк идёт после ответа, а не
    успел — его повторит планировщик."""
    provider = billing.provider
    if not billing.enabled or provider is None or provider.name != "tochka":
        raise NotFoundError("Not Found")
    body = await request.body()
    try:
        event = provider.parse_webhook(body)
    except InvalidWebhookError:
        logger.warning("payment_webhook_bad_signature", size=len(body))
        raise HTTPException(status_code=400, detail="invalid signature") from None
    event_id = await payments.receive(event, body)
    if event_id is not None:
        background.add_task(payments.process, event_id)
    return {"ok": True}
