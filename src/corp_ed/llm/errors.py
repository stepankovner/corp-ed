import httpx

from corp_ed.core.exceptions import DomainError

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class LLMError(DomainError):
    """Ошибка вызова языковой модели."""

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def classify(response: httpx.Response) -> LLMError | None:
    """Превращает ответ провайдера в доменную ошибку или None, если всё хорошо."""
    if response.status_code == 200:
        return None

    retryable = response.status_code in RETRYABLE_STATUSES

    body = response.json().get("error", {})
    msg = (
        f"{response.status_code} {body.get('httpStatus', '')}: "
        f"{body.get('message', '')}"
    )
    return LLMError(msg, retryable=retryable)
