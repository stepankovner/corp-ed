"""Логотип компании по подписанной ссылке (ТЗ §7). Без входа: <img> не
шлёт Authorization; ссылку получают только участники компании."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from corp_ed.api.v1.dependencies import get_company_service
from corp_ed.api.v1.rate_limits import (
    AVATAR_FETCH_PER_IP,
    client_ip,
    enforce,
    get_rate_limiter,
)
from corp_ed.core.exceptions import NotFoundError
from corp_ed.core.rate_limit import RateLimiter
from corp_ed.services.company_service import (
    URL_LIFETIME_S,
    CompanyService,
    check_logo_signature,
)

router = APIRouter(prefix="/logos", tags=["company"])


@router.get(
    "/{tenant_id}",
    response_class=Response,
    responses={200: {"content": {"image/webp": {}}}},
)
async def read_logo(
    request: Request,
    tenant_id: UUID,
    v: Annotated[str, Query(min_length=1, max_length=16)],
    exp: Annotated[int, Query(gt=0)],
    sig: Annotated[str, Query(min_length=32, max_length=32)],
    service: Annotated[CompanyService, Depends(get_company_service)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> Response:
    await enforce(limiter, AVATAR_FETCH_PER_IP, client_ip(request))
    # Подпись, срок и наличие — один ответ: по нему не узнать, есть ли логотип.
    if not check_logo_signature(tenant_id, v, exp, sig):
        raise NotFoundError("Логотип не найден")
    content = await service.load_logo(tenant_id, v)
    if content is None:
        raise NotFoundError("Логотип не найден")
    return Response(
        content=content,
        media_type="image/webp",
        headers={
            "Cache-Control": f"private, max-age={URL_LIFETIME_S}",
            "Content-Disposition": "inline",
        },
    )
