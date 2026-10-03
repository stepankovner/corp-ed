"""Фото профиля по подписанной ссылке (ТЗ §4).

Без входа: <img> не шлёт Authorization. Доступ — сама ссылка: её выдают
только тем, кому показан профиль, подпись проверяется, срок короткий
(services/avatar_service.py).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from corp_ed.api.v1.dependencies import get_avatar_service
from corp_ed.api.v1.rate_limits import (
    AVATAR_FETCH_PER_IP,
    client_ip,
    enforce,
    get_rate_limiter,
)
from corp_ed.core.exceptions import NotFoundError
from corp_ed.core.rate_limit import RateLimiter
from corp_ed.services.avatar_service import (
    URL_LIFETIME_S,
    AvatarService,
    check_signature,
)

router = APIRouter(prefix="/avatars", tags=["avatars"])


@router.get(
    "/{account_id}",
    response_class=Response,
    responses={200: {"content": {"image/webp": {}}}},
)
async def read_avatar(
    request: Request,
    account_id: UUID,
    v: Annotated[str, Query(min_length=1, max_length=16)],
    exp: Annotated[int, Query(gt=0)],
    sig: Annotated[str, Query(min_length=32, max_length=32)],
    avatars: Annotated[AvatarService, Depends(get_avatar_service)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> Response:
    await enforce(limiter, AVATAR_FETCH_PER_IP, client_ip(request))
    # Неверная подпись, истёкшая ссылка и нет фото — один ответ: по нему
    # не узнать, есть ли у учётки фото.
    if not check_signature(account_id, v, exp, sig):
        raise NotFoundError("Фото не найдено")
    avatar = await avatars.load(account_id, v)
    if avatar is None:
        raise NotFoundError("Фото не найдено")
    return Response(
        content=avatar.content,
        media_type="image/webp",
        headers={
            # Версия в адресе: новое фото — новый адрес, кэш не мешает.
            "Cache-Control": f"private, max-age={URL_LIFETIME_S}",
            "Content-Disposition": "inline",
        },
    )
