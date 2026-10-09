"""Пакеты кредитов сверх месячного пула (решение владельца 09.10):
список пакетов и заказы администратора компании (оплату отмечает команда
kronto в нашей панели, endpoints/staff.py); «Попросить администратора
пополнить» — для всех сотрудников."""

from typing import Annotated

from fastapi import APIRouter, Depends, status

from corp_ed.api.v1.dependencies import (
    get_credit_order_service,
    get_credit_service,
    get_current_user,
    require_role,
)
from corp_ed.api.v1.rate_limits import (
    CREDIT_ORDER_PER_TENANT,
    TOPUP_REQUEST_PER_USER,
    limit_by_tenant,
    limit_by_user,
)
from corp_ed.api.v1.schemas.usage import (
    CreditOrderRequest,
    CreditOrderResponse,
    CreditPackResponse,
    TopupRequestResponse,
    TopupStatusResponse,
)
from corp_ed.domain.credit_packs import PACKS
from corp_ed.domain.models import CreditOrder, User, UserRole
from corp_ed.services.credit_order_service import CreditOrderService
from corp_ed.services.credit_service import CreditService

router = APIRouter(prefix="/credits", tags=["credits"])

Admin = Annotated[User, Depends(require_role(UserRole.ADMIN))]
Member = Annotated[User, Depends(get_current_user)]
Orders = Annotated[CreditOrderService, Depends(get_credit_order_service)]
Credits = Annotated[CreditService, Depends(get_credit_service)]


def order_response(order: CreditOrder) -> CreditOrderResponse:
    return CreditOrderResponse(
        id=order.id,
        number=order.number,
        pack=order.pack,
        credits=order.credits,
        amount_kopecks=order.amount_kopecks,
        status=order.status,  # type: ignore[arg-type]
        payment_method=order.payment_method,  # type: ignore[arg-type]
        created_at=order.created_at,
        paid_at=order.paid_at,
        cancelled_at=order.cancelled_at,
    )


@router.get("/packs", response_model=list[CreditPackResponse])
async def list_packs(admin: Admin) -> list[CreditPackResponse]:
    """Пакеты и цены — из одного места бэкенда (domain/credit_packs.py):
    фронт их не хардкодит."""
    return [
        CreditPackResponse(
            code=pack.code, credits=pack.credits, price_kopecks=pack.price_kopecks
        )
        for pack in PACKS
    ]


@router.get("/orders", response_model=list[CreditOrderResponse])
async def list_orders(orders: Orders, admin: Admin) -> list[CreditOrderResponse]:
    """Заказы своей компании, новые первыми."""
    return [order_response(order) for order in await orders.orders()]


@router.post(
    "/orders",
    response_model=CreditOrderResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_by_tenant(CREDIT_ORDER_PER_TENANT))],
)
async def create_order(
    body: CreditOrderRequest, orders: Orders, admin: Admin
) -> CreditOrderResponse:
    """Заказать пакет: заказ ждёт оплаты по счёту, команде kronto уходит
    уведомление. Кредиты зачисляются, когда команда отметит оплату."""
    return order_response(await orders.create(admin, body.pack))


@router.get("/topup-request", response_model=TopupStatusResponse)
async def topup_status(credits: Credits, member: Member) -> TopupStatusResponse:
    """Кончились ли кредиты и просил ли уже кто-то пополнить: тогда вместо
    кнопки — «Администратор уже уведомлён»."""
    stopped, requested = await credits.topup_status()
    return TopupStatusResponse(stopped=stopped, requested=requested)


@router.post(
    "/topup-request",
    response_model=TopupRequestResponse,
    dependencies=[Depends(limit_by_user(TOPUP_REQUEST_PER_USER))],
)
async def request_topup(credits: Credits, member: Member) -> TopupRequestResponse:
    """«Попросить администратора пополнить»: администраторам — одно
    уведомление на эпизод исчерпания (до пополнения или нового месяца),
    сколько бы сотрудников ни нажали. 409 credits_available — кредиты есть."""
    return TopupRequestResponse(sent=await credits.request_topup(member))
