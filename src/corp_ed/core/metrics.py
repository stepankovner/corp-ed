"""Метрики для Prometheus (П-9, вариант «б»; MONITORING-RESEARCH.md).

Что собираем — только то, по чему есть тревога или вопрос «работает ли»:
запросы API по маршруту и статусу (SLO, 5xx при отказе Яндекса — 502 на
/faq/ask), ответы по origin, деградации (ответ без переписывания, без
реранкера, без истории), очереди воркера и его пульс. Метки — шаблоны
маршрутов и фиксированные значения: ни текста вопросов, ни id компаний,
ни адресов (152-ФЗ, кардинальность).

API — один процесс uvicorn: хватает реестра по умолчанию. Воркер отдаёт
свои метрики на отдельном порту (worker.py). Наружу /metrics не выходит:
nginx его не проксирует, а сам API отвечает только частным адресам.
"""

import ipaddress
import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HTTP_REQUESTS = Counter(
    "corp_ed_http_requests_total",
    "Запросы API по маршруту (шаблону пути) и коду ответа",
    ["method", "route", "status"],
)
HTTP_LATENCY = Histogram(
    "corp_ed_http_request_duration_seconds",
    "Время ответа API по маршруту",
    ["route"],
    # Ответ на вопрос — секунды: эмбеддинг, модель, иногда второй вызов.
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64),
)
FAQ_ANSWERS = Counter(
    "corp_ed_faq_answers_total",
    "Ответы сотрудникам по источнику: documents, general_knowledge, none",
    ["origin"],
)
FAQ_DEGRADED = Counter(
    "corp_ed_faq_degraded_total",
    "Ответ дан, но без части пайплайна: condense, rerank, dialogue_store",
    ["reason"],
)
WORKER_QUEUE = Gauge(
    "corp_ed_worker_queue_jobs",
    "Задачи в очередях воркера по статусу",
    ["queue", "status"],
)
WORKER_JOBS = Counter(
    "corp_ed_worker_jobs_total",
    "Обработанные задачи воркера по итогу: done, retry, failed",
    ["queue", "result"],
)
WORKER_HEARTBEAT = Gauge(
    "corp_ed_worker_heartbeat_timestamp_seconds",
    "Последний пульс воркера (unix time)",
)

UNMATCHED = "unmatched"
"""Метка для путей без маршрута: иначе каждый случайный URL — новый ряд."""


def _route(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else UNMATCHED


class MetricsMiddleware:
    """Счётчик и время ответа по шаблону маршрута («/api/v1/faq/ask»)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = _route(scope)
            HTTP_REQUESTS.labels(scope.get("method", ""), route, str(status)).inc()
            HTTP_LATENCY.labels(route).observe(time.perf_counter() - started)


def _is_internal(host: str | None) -> bool:
    if host is None:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


async def metrics_endpoint(request: Request) -> Response:
    """Prometheus забирает метрики из сети Docker; остальным — 404."""
    if not _is_internal(request.client.host if request.client else None):
        return Response(status_code=404)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
