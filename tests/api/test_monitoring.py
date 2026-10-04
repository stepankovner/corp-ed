"""Мониторинг со стороны приложения (П-9): /metrics, /health/ready, пульс.

Сам стек (Prometheus, Alertmanager, Grafana, Loki) — deploy/monitoring;
здесь — что приложение ему отдаёт.
"""

import asyncio
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from prometheus_client import REGISTRY
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from corp_ed.core import readiness
from corp_ed.core.metrics import _is_internal, metrics_endpoint
from corp_ed.core.readiness import WORKER_HEARTBEAT_KEY, readiness_failures
from corp_ed.domain.models import IngestJob, Material, Tenant, User
from corp_ed.main import app
from corp_ed.services.faq_service import FaqService
from corp_ed.worker import heartbeat

REDIS_URL = os.environ.get("TEST_REDIS_URL")
needs_redis = pytest.mark.skipif(REDIS_URL is None, reason="TEST_REDIS_URL не задан")


def _sample(name: str, labels: dict[str, str]) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


# --- /metrics --------------------------------------------------------------------


async def test_metrics_count_requests_by_route_template(
    api: httpx.AsyncClient,
) -> None:
    labels = {"method": "GET", "route": "/health", "status": "200"}
    before = _sample("corp_ed_http_requests_total", labels)

    await api.get("/health")
    await api.get("/health")
    response = await api.get("/metrics")

    assert response.status_code == 200
    assert "corp_ed_http_requests_total" in response.text
    assert _sample("corp_ed_http_requests_total", labels) == before + 2


async def test_router_routes_are_labelled_with_the_full_template(
    api: httpx.AsyncClient,
) -> None:
    """Префикс include_router («/api/v1») — в метке, значения параметров —
    нет: иначе тревоги и панели по маршрутам молча теряют ряды, а каждый
    id стал бы новым рядом (FastAPI 0.142 отдаёт маршрут без префикса)."""
    me = {"method": "GET", "route": "/api/v1/auth/me", "status": "401"}
    chat = {
        "method": "GET",
        "route": "/api/v1/conversations/{conversation_id}",
        "status": "401",
    }
    before = (
        _sample("corp_ed_http_requests_total", me),
        _sample("corp_ed_http_requests_total", chat),
    )

    await api.get("/api/v1/auth/me")
    await api.get(f"/api/v1/conversations/{uuid4()}")

    assert _sample("corp_ed_http_requests_total", me) == before[0] + 1
    assert _sample("corp_ed_http_requests_total", chat) == before[1] + 1


async def test_unknown_paths_share_one_label(api: httpx.AsyncClient) -> None:
    """Случайные URL не плодят ряды: иначе сканер съест память Prometheus."""
    labels = {"method": "GET", "route": "unmatched", "status": "404"}
    before = _sample("corp_ed_http_requests_total", labels)

    for path in ("/wp-login.php", f"/{uuid4()}"):
        await api.get(path)

    assert _sample("corp_ed_http_requests_total", labels) == before + 2


@pytest.mark.parametrize(
    ("host", "internal"),
    [
        ("127.0.0.1", True),
        ("172.30.61.5", True),
        ("10.0.0.3", True),
        ("8.8.8.8", False),
        ("2a00:1450::1", False),
        (None, False),
        ("not-an-ip", False),
    ],
)
def test_metrics_are_for_internal_addresses_only(
    host: str | None, internal: bool
) -> None:
    assert _is_internal(host) is internal


async def test_metrics_endpoint_hides_from_public_address() -> None:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/metrics",
        "headers": [],
        "client": ("8.8.8.8", 443),
    }
    response = await metrics_endpoint(Request(scope))
    assert response.status_code == 404


async def test_answers_are_counted_by_origin(
    faq_service: FaqService, employee: User
) -> None:
    labels = {"origin": "general_knowledge"}
    before = _sample("corp_ed_faq_answers_total", labels)

    await faq_service.answer("Где найти документацию?", employee)

    assert _sample("corp_ed_faq_answers_total", labels) == before + 1


# --- /health/ready ----------------------------------------------------------------


async def test_ready_without_redis_checks_the_database(api: httpx.AsyncClient) -> None:
    response = await api.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_reports_a_dead_database(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken:
        def connect(self) -> None:
            raise OSError("db down")

    monkeypatch.setattr(readiness, "get_engine", lambda: Broken())

    response = await api.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "fail", "failed": ["database"]}


class State:
    def __init__(self, redis: Redis | None) -> None:
        self.redis = redis


@pytest.fixture
async def redis() -> AsyncGenerator[Redis]:
    assert REDIS_URL is not None
    client = Redis.from_url(REDIS_URL)
    await client.delete(WORKER_HEARTBEAT_KEY)
    yield client
    await client.delete(WORKER_HEARTBEAT_KEY)
    await client.aclose()


@needs_redis
async def test_ready_needs_the_worker_heartbeat(redis: Redis) -> None:
    assert await readiness_failures(State(redis)) == ["worker"]

    await redis.set(WORKER_HEARTBEAT_KEY, "1", ex=60)

    assert await readiness_failures(State(redis)) == []


async def test_ready_reports_a_dead_redis() -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    try:
        assert await readiness_failures(State(dead)) == ["redis"]
    finally:
        await dead.aclose()


async def test_ready_is_public_through_the_app_without_details(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(state: object) -> list[str]:
        return ["worker"]

    monkeypatch.setattr("corp_ed.main.readiness_failures", failing)
    app.state.redis = None

    response = await api.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "fail", "failed": ["worker"]}


# --- пульс воркера -----------------------------------------------------------------


@needs_redis
async def test_heartbeat_marks_worker_alive_and_counts_queues(
    tmp_path: Path,
    redis: Redis,
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    material: Material,
) -> None:
    session.add(IngestJob(tenant_id=tenant_ctx.id, material_id=material.id))
    await session.commit()
    stop = asyncio.Event()

    task = asyncio.create_task(
        heartbeat(
            stop,
            tmp_path / "alive",
            every=0.01,
            redis=redis,
            session_maker=session_maker,
        )
    )
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=1)

    assert 0 < await redis.ttl(WORKER_HEARTBEAT_KEY) <= 120
    assert (
        _sample("corp_ed_worker_queue_jobs", {"queue": "ingest", "status": "queued"})
        >= 1
    )
    assert _sample("corp_ed_worker_heartbeat_timestamp_seconds", {}) > 0
