import json
from typing import Literal

import httpx

from corp_ed.core.exceptions import DomainError

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
INPUT_REJECTED_STATUSES = {400, 413, 422}
"""Поставщик эмбеддингов отверг сам текст: виноват документ, а не
поставщик (BH-51 (в))."""

ErrorKind = Literal[
    "timeout", "error_fast", "error_config", "error_payment", "stream_broken"
]
"""Класс отказа поставщика (BH-50, BH-49): по нему запасная модель и
предохранитель решают, что делать, и с ним считаются метрики.

timeout — не ответил в срок; error_fast — быстрая ошибка (5xx, 429,
обрыв соединения до ответа, ответ не по контракту); error_config —
неповторяемый ответ (400, 401, 403, 404): ключ, каталог, модель;
error_payment — 402: оплата; stream_broken — поток оборвался после
первых кусков."""


class LLMError(DomainError):
    """Ошибка вызова языковой модели."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        kind: ErrorKind | None = None,
        status: int | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.kind: ErrorKind = kind or ("error_fast" if retryable else "error_config")
        # HTTP-код ответа поставщика; None — ответа не было.
        self.status = status

    @property
    def input_rejected(self) -> bool:
        return self.status in INPUT_REJECTED_STATUSES


def classify(response: httpx.Response) -> LLMError | None:
    """Превращает ответ провайдера в доменную ошибку или None, если всё хорошо."""
    if response.status_code == 200:
        return None

    retryable = response.status_code in RETRYABLE_STATUSES
    kind: ErrorKind = (
        "error_payment"
        if response.status_code == 402
        else ("error_fast" if retryable else "error_config")
    )

    try:
        parsed = response.json()
    except json.JSONDecodeError:
        snippet = response.text[:200]
        if len(response.text) > 200:
            snippet += "…"
        return LLMError(
            f"{response.status_code} {snippet}",
            retryable=retryable,
            kind=kind,
            status=response.status_code,
        )

    body = parsed if isinstance(parsed, dict) else {}
    error = body.get("error", {})
    res_body = error if isinstance(error, dict) else {}
    msg = (
        f"{response.status_code} {res_body.get('httpStatus', '')}: "
        f"{res_body.get('message', '')}"
    )
    return LLMError(msg, retryable=retryable, kind=kind, status=response.status_code)
