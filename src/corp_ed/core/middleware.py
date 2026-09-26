import uuid
from collections.abc import Awaitable, Callable, Iterable

import structlog
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from corp_ed.core.request_context import current_client_ip, current_request_id

logger = structlog.get_logger()

INTERNAL_ERROR_DETAIL = "Внутренняя ошибка сервера"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Присваивает каждому запросу уникальный request_id и кладёт в лог-контекст.

    Идентификатор генерируется всегда, входящий X-Request-ID не
    принимается: чужая строка в логах — это подделка записей (log
    injection) и путаница при разборе инцидента.

    Здесь же — последний рубеж для непойманных исключений. Обработчик
    Exception у FastAPI вызывается снаружи всех middleware, и ответ 500
    ушёл бы без заголовков безопасности и без request_id. Поймав
    исключение здесь, мы отдаём тот же JSON, что и для остальных ошибок,
    а трассировка остаётся в логе и никогда не уходит клиенту.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = str(uuid.uuid4())

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        current_request_id.set(request_id)
        current_client_ip.set(request.client.host if request.client else None)

        try:
            response = await call_next(request)
        except Exception:
            logger.exception("unhandled_error", path=request.url.path)
            response = JSONResponse(
                status_code=500,
                content={"detail": INTERNAL_ERROR_DETAIL, "request_id": request_id},
            )
        response.headers["X-Request-ID"] = request_id
        return response


# Заголовки для JSON-API (OWASP Secure Headers Project, ASVS 3.4).
# Страниц и скриптов API не отдаёт, поэтому CSP максимально строгий:
# если ответ всё же откроют в браузере, ничего не выполнится и не
# встроится во фрейм.
_BASE_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    # Ответы содержат документы компании и персональные данные: ни
    # браузер, ни промежуточный прокси не должны их кешировать.
    "Cache-Control": "no-store",
}
_HSTS = "max-age=63072000; includeSubDomains"
# Swagger UI грузит скрипты с CDN — строгий CSP его ломает. Документация
# доступна только вне production (см. main.py).
_DOCS_PREFIXES = ("/docs", "/redoc", "/openapi.json")


class SecurityHeadersMiddleware:
    """Добавляет заголовки безопасности к каждому ответу.

    Чистый ASGI, а не BaseHTTPMiddleware: заголовки дописываются в
    момент отправки, без буферизации тела и лишней задачи на запрос.
    HSTS — только в production: локально нет TLS, а браузер запомнит
    политику для localhost на два года.
    """

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_docs = str(scope.get("path", "")).startswith(_DOCS_PREFIXES)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _BASE_HEADERS.items():
                    if is_docs and name == "Content-Security-Policy":
                        continue
                    headers.setdefault(name, value)
                if self.hsts:
                    headers["Strict-Transport-Security"] = _HSTS
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodySizeLimitMiddleware:
    """Отклоняет слишком большие тела запросов (413) до разбора.

    Проверяется и Content-Length (дёшево, до чтения), и реально
    прочитанные байты: клиент может прислать chunked-тело без длины или
    соврать в заголовке. Без лимита один запрос на гигабайт занимает
    память воркера — отказ в обслуживании (OWASP API4:2023).

    Для путей загрузки файлов — отдельный, больший лимит.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        max_upload_bytes: int,
        upload_paths: Iterable[str],
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_upload_bytes = max_upload_bytes
        self.upload_paths = tuple(upload_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        limit = (
            self.max_upload_bytes
            if path.endswith(self.upload_paths)
            else self.max_body_bytes
        )

        declared = _content_length(scope)
        if declared is not None and declared > limit:
            await _too_large(send)
            return

        received = 0
        rejected = False

        async def limited_receive() -> Message:
            # Исключение отсюда бросать нельзя: FastAPI превращает любую
            # ошибку при чтении тела в 400 «error parsing the body».
            # Вместо этого сами отвечаем 413 и сообщаем приложению, что
            # клиент отключился, — дальше оно тело не читает.
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    rejected = True
                    await _too_large(send)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            # После нашего 413 ответ приложения (обычно 400) отбрасывается:
            # второй http.response.start сломал бы протокол.
            if not rejected:
                await send(message)

        await self.app(scope, limited_receive, guarded_send)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _too_large(send: Send) -> None:
    response = JSONResponse(
        status_code=413, content={"detail": "Слишком большое тело запроса"}
    )
    await send(
        {
            "type": "http.response.start",
            "status": response.status_code,
            "headers": response.raw_headers,
        }
    )
    await send({"type": "http.response.body", "body": response.body})
