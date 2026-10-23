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
LLM_FALLBACK = Counter(
    "corp_ed_llm_fallback_total",
    "Основная модель не ответила (BH-50): этап answer/general — ответ "
    "запасной, condense/gaps_label — шаг пропущен; reason — класс отказа",
    ["stage", "reason"],
)
FALLBACK_STAGES = ("answer", "general", "condense", "gaps_label")
FALLBACK_REASONS = (
    "timeout",
    "error_fast",
    "error_config",
    "error_payment",
    "breaker_open",
    "stream_broken",
)

# Главный цикл (BH-52): вызовы Flash, резерва и эмбеддингов, сроки,
# предохранители и пробы. Метки — только перечни ниже.
LLM_PROVIDERS = ("yandex", "cloudru")
LLM_KINDS = ("chat", "embedding")
LLM_CALL_STAGES = {
    "chat": FALLBACK_STAGES,
    "embedding": ("query", "document"),
}
LLM_OUTCOMES = (
    "ok",
    "timeout",
    "error_fast",
    "error_config",
    "error_payment",
    "busy",
    "filtered",
)
LLM_CALLS = Counter(
    "corp_ed_llm_calls_total",
    "Вызовы модели ответа и эмбеддингов по исходу (BH-52)",
    ["provider", "kind", "stage", "outcome"],
)
LLM_FIRST_CHUNK = Histogram(
    "corp_ed_llm_first_chunk_seconds",
    "Поток ответа: время до первого куска; при таймауте — срок",
    ["provider"],
    buckets=(0.25, 0.5, 1, 1.5, 2, 3, 4, 6, 8, 10),
)
LLM_CALL_DURATION = Histogram(
    "corp_ed_llm_call_duration_seconds",
    "Весь вызов модели или эмбеддинга",
    ["provider", "kind"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 15, 30),
)
LLM_BREAKER_STATE = Gauge(
    "corp_ed_llm_breaker_state",
    "Предохранитель Яндекса: 0 — закрыт, 1 — проба, 2 — открыт (chat — "
    "ответы резервом, embedding — вопросы ошибкой, загрузка ждёт)",
    ["provider", "kind"],
)
LLM_BREAKER_OPENED = Counter(
    "corp_ed_llm_breaker_opened_total",
    "Переходы предохранителя в «открыт»",
    ["provider", "kind"],
)
LLM_PROBE = Counter(
    "corp_ed_llm_probe_total",
    "Пробные вызовы раз в 5 минут (при открытом предохранителе — раз в 2)",
    ["provider", "kind", "outcome"],
)
LLM_PROBE_FAILURES = Gauge(
    "corp_ed_llm_probe_consecutive_failures",
    "Неудачные пробы подряд; успех — 0",
    ["provider", "kind"],
)
# Все сочетания — с нуля при старте: тревога видит ряд и без отказов.
for _stage in FALLBACK_STAGES:
    for _reason in FALLBACK_REASONS:
        LLM_FALLBACK.labels(_stage, _reason)
for _provider in LLM_PROVIDERS:
    LLM_FIRST_CHUNK.labels(_provider)
    for _kind in LLM_KINDS:
        LLM_CALL_DURATION.labels(_provider, _kind)
        LLM_BREAKER_OPENED.labels(_provider, _kind)
        LLM_PROBE_FAILURES.labels(_provider, _kind).set(0)
        for _outcome in LLM_OUTCOMES:
            LLM_PROBE.labels(_provider, _kind, _outcome)
            for _call_stage in LLM_CALL_STAGES[_kind]:
                LLM_CALLS.labels(_provider, _kind, _call_stage, _outcome)
for _kind in LLM_KINDS:
    LLM_BREAKER_STATE.labels("yandex", _kind).set(0)

WORKER_QUEUE = Gauge(
    "corp_ed_worker_queue_jobs",
    "Задачи в очередях воркера по статусу",
    ["queue", "status"],
)
WORKER_OLDEST_QUEUED = Gauge(
    "corp_ed_worker_oldest_queued_seconds",
    "Возраст самой старой задачи в очереди, с; пустая очередь — 0 (BH-52)",
    ["queue"],
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
    """Шаблон маршрута целиком: «/api/v1/materials/{material_id}».

    С FastAPI 0.142 в scope["route"] — маршрут без префикса
    include_router («/materials/{material_id}»), а префикс подключённого
    роутера — во внутренних структурах FastAPI. Префиксы подключения у нас
    статичные (/api/v1, main.py), поэтому недостающее начало берётся из
    пути запроса: столько первых сегментов, сколько не покрывает шаблон.
    Значения параметров в метку не попадают — они внутри шаблона.
    """
    route = scope.get("route")
    template = getattr(route, "path", None)
    if not isinstance(template, str):
        return UNMATCHED
    path = str(scope.get("path", ""))
    missing = path.rstrip("/").count("/") - template.rstrip("/").count("/")
    if missing <= 0:
        return template
    return "/".join(path.split("/")[: missing + 1]) + template


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
