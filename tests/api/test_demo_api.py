"""Песочница на сайте (ТЗ §1): компания заводится командой выкатки,
вопрос без входа — ответ по её документам, лимиты закрыты без Redis."""

import json
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.config import EMBEDDING_DIM, DemoSettings
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.demo import COMPANY_NAME, SUGGESTED_QUESTIONS, documents
from corp_ed.domain.models import (
    Account,
    Chunk,
    IngestJob,
    Material,
    NotificationSetting,
    QaLog,
    Tenant,
    User,
)
from corp_ed.domain.types import NotFoundMode
from corp_ed.llm.fake import FakeAdapter
from corp_ed.main import app
from corp_ed.services.demo_service import NO_LOGIN_HASH, DemoService
from tests.api.conftest import login
from tests.security.test_rate_limits import DownLimiter
from tests.test_credits import spend

SETTINGS = DemoSettings()


async def _setup(session_maker: async_sessionmaker[AsyncSession]) -> Tenant:
    await DemoService(session_maker, SETTINGS).setup()
    async with session_maker() as session:
        tenant = (
            await session.scalars(
                select(Tenant).where(Tenant.company_code == SETTINGS.company_code)
            )
        ).one()
    return tenant


async def _member(
    session_maker: async_sessionmaker[AsyncSession], tenant: Tenant
) -> User:
    with tenant_scope(tenant.id):
        async with session_maker() as session:
            return (await session.scalars(select(User))).one()


async def _index_first_document(
    session_maker: async_sessionmaker[AsyncSession], tenant: Tenant
) -> Material:
    """Как будто воркер проиндексировал первый документ."""
    with tenant_scope(tenant.id):
        async with session_maker() as session:
            material = (
                await session.scalars(
                    select(Material).where(Material.title == documents()[0].title)
                )
            ).one()
            session.add(
                Chunk(
                    material_id=material.id,
                    position=0,
                    content="4.3. Суточные — 700 рублей за каждый день командировки.",
                    embedding=[0.1] * EMBEDDING_DIM,
                    model="fake",
                    model_version="fake",
                )
            )
            await session.commit()
    return material


async def _ask(api: httpx.AsyncClient, question: str, **extra: str) -> httpx.Response:
    return await api.post("/api/v1/demo/ask", json={"question": question, **extra})


async def _stream(
    api: httpx.AsyncClient, question: str, **extra: str
) -> list[dict[str, Any]]:
    response = await api.post(
        "/api/v1/demo/ask/stream", json={"question": question, **extra}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    events = [
        json.loads(line[len("data: ") :])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events[-1]["type"] in ("done", "error")
    return events


# --- cli demo setup ---------------------------------------------------------------


async def test_setup_creates_company_once_and_updates_changed_documents(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    service = DemoService(session_maker, SETTINGS)
    count = len(documents())

    first = await service.setup()
    assert first.tenant_created is True
    assert (first.created, first.updated, first.unchanged) == (count, 0, 0)

    again = await service.setup()
    assert again.tenant_created is False
    assert (again.created, again.updated, again.unchanged) == (0, 0, count)

    tenant = await _setup(session_maker)
    # Ответ только по документам: песочница — не бесплатный чат с моделью.
    assert tenant.not_found_mode == NotFoundMode.STRICT.value
    assert tenant.seats == SETTINGS.seats

    with tenant_scope(tenant.id):
        async with session_maker() as session:
            jobs = await session.scalar(select(func.count()).select_from(IngestJob))
            assert jobs == count
            member = (await session.scalars(select(User))).one()
            prefs = await session.get(NotificationSetting, member.id)
            assert prefs is not None
            assert not any(
                (
                    prefs.email_connectors,
                    prefs.email_credits,
                    prefs.email_join_requests,
                    prefs.email_weekly_digest,
                )
            )
            # Документ поправили в репозитории — переиндексируется только он.
            material = (
                await session.scalars(
                    select(Material).where(Material.title == documents()[1].title)
                )
            ).one()
            material.content = "старая редакция"
            await session.execute(IngestJob.__table__.delete())
            await session.commit()

    changed = await service.setup()
    assert (changed.created, changed.updated) == (0, 1)
    with tenant_scope(tenant.id):
        async with session_maker() as session:
            assert (
                await session.scalar(select(func.count()).select_from(IngestJob))
            ) == 1

    async with session_maker() as session:
        account = (
            await session.scalars(
                select(Account).where(Account.email == SETTINGS.account_email)
            )
        ).one()
        assert account.hashed_password == NO_LOGIN_HASH


async def test_demo_account_cannot_sign_in(
    api: httpx.AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(session_maker)
    for password in ("", NO_LOGIN_HASH, "correct-horse-battery-staple"):
        response = await login(api, SETTINGS.account_email, password or "x" * 12)
        assert response.status_code == 401


# --- сайт -------------------------------------------------------------------------


async def test_off_until_setup(api: httpx.AsyncClient) -> None:
    info = await api.get("/api/v1/demo")
    assert info.status_code == 503
    assert info.json()["code"] == "demo_off"

    ask = await _ask(api, "Как оформить командировку?")
    assert ask.status_code == 503
    assert ask.json()["code"] == "demo_off"


async def test_info_lists_company_documents_and_questions(
    api: httpx.AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(session_maker)

    response = await api.get("/api/v1/demo")

    assert response.status_code == 200
    assert response.json() == {
        "company": COMPANY_NAME,
        "documents": [document.title for document in documents()],
        "questions": list(SUGGESTED_QUESTIONS),
    }


async def test_answer_from_documents_with_sources(
    api: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
) -> None:
    tenant = await _setup(session_maker)
    material = await _index_first_document(session_maker, tenant)

    response = await _ask(api, "  Какие   суточные в командировке?  ")

    assert response.status_code == 200
    body = response.json()
    assert body["origin"] == "documents"
    assert body["content"] == fake_llm.content
    assert body["sources"] == [
        {
            "title": material.title,
            "heading_path": [],
            "content": "4.3. Суточные — 700 рублей за каждый день командировки.",
        }
    ]
    # Вопрос — в журнале компании песочницы, как у любой компании.
    with tenant_scope(tenant.id):
        async with session_maker() as session:
            log = (await session.scalars(select(QaLog))).one()
            assert log.question == "Какие суточные в командировке?"


async def test_outside_documents_is_honest_refusal_without_model(
    api: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
) -> None:
    await _setup(session_maker)

    response = await _ask(api, "Сколько дней удалёнки положено проектировщикам?")

    assert response.status_code == 200
    assert response.json()["origin"] == "none"
    assert response.json()["sources"] == []
    assert fake_llm.calls == []


async def test_honeypot_answers_without_model_or_log(
    api: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
) -> None:
    tenant = await _setup(session_maker)
    await _index_first_document(session_maker, tenant)

    response = await _ask(api, "Какие суточные?", website="https://spam.example")

    assert response.status_code == 200
    assert response.json()["origin"] == "none"
    assert fake_llm.calls == []
    with tenant_scope(tenant.id):
        async with session_maker() as session:
            assert await session.scalar(select(func.count()).select_from(QaLog)) == 0


async def test_question_is_short_and_body_strict(
    api: httpx.AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(session_maker)

    assert (await _ask(api, "д" * 301)).status_code == 422
    assert (await _ask(api, "  а  ")).status_code == 422
    extra = await api.post(
        "/api/v1/demo/ask", json={"question": "Какие суточные?", "tenant_id": "x"}
    )
    assert extra.status_code == 422


async def test_limit_per_ip(
    api: httpx.AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(session_maker)

    for _ in range(10):
        assert (await _ask(api, "Сколько дней удалёнки?")).status_code == 200
    response = await _ask(api, "Сколько дней удалёнки?")

    assert response.status_code == 429
    assert "Retry-After" in response.headers


async def test_fails_closed_without_redis(
    api: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
) -> None:
    await _setup(session_maker)
    app.state.rate_limiter = DownLimiter()

    response = await _ask(api, "Какие суточные?")

    assert response.status_code == 503
    assert fake_llm.calls == []


async def test_exhausted_pool_is_demo_busy(
    api: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
) -> None:
    tenant = await _setup(session_maker)
    member = await _member(session_maker, tenant)
    with tenant_scope(tenant.id):
        async with session_maker() as session:
            spend(session, member, 420 * SETTINGS.seats)
            await session.commit()

    response = await _ask(api, "Какие суточные?")

    assert response.status_code == 503
    assert response.json()["code"] == "demo_busy"
    assert "созвон" in response.json()["detail"]
    assert fake_llm.calls == []


# --- ответ потоком ----------------------------------------------------------------


async def test_stream_prints_answer_then_gives_sources(
    api: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
) -> None:
    tenant = await _setup(session_maker)
    material = await _index_first_document(session_maker, tenant)

    events = await _stream(api, "Какие суточные в командировке?")

    assert events[0] == {"type": "stage", "stage": "searching"}
    deltas = [event["text"] for event in events if event["type"] == "delta"]
    assert deltas
    done = events[-1]
    assert done["type"] == "done"
    assert done["answer"]["origin"] == "documents"
    assert done["answer"]["content"] == fake_llm.content
    assert [source["title"] for source in done["answer"]["sources"]] == [material.title]
    with tenant_scope(tenant.id):
        async with session_maker() as session:
            assert await session.scalar(select(func.count()).select_from(QaLog)) == 1


async def test_stream_outside_documents_is_refusal_without_model(
    api: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
) -> None:
    await _setup(session_maker)

    events = await _stream(api, "Сколько дней удалёнки положено проектировщикам?")

    assert events[-1]["answer"]["origin"] == "none"
    assert not [event for event in events if event["type"] == "delta"]
    assert fake_llm.calls == []


async def test_stream_honeypot_answers_without_model(
    api: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
) -> None:
    tenant = await _setup(session_maker)
    await _index_first_document(session_maker, tenant)

    events = await _stream(api, "Какие суточные?", website="https://spam.example")

    assert [event["type"] for event in events] == ["done"]
    assert events[0]["answer"]["origin"] == "none"
    assert fake_llm.calls == []


async def test_stream_checks_limits_and_company_before_streaming(
    api: httpx.AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """Песочница не заведена — 503 до потока; лимит по IP общий с /ask."""
    off = await api.post(
        "/api/v1/demo/ask/stream", json={"question": "Какие суточные?"}
    )
    assert off.status_code == 503
    assert off.json()["code"] == "demo_off"

    await _setup(session_maker)
    # Лимит считает и вопрос к незаведённой песочнице: он идёт первым.
    for _ in range(9):
        assert (await _ask(api, "Сколько дней удалёнки?")).status_code == 200
    limited = await api.post(
        "/api/v1/demo/ask/stream", json={"question": "Сколько дней удалёнки?"}
    )
    assert limited.status_code == 429


async def test_stream_exhausted_pool_is_error_event(
    api: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    fake_llm: FakeAdapter,
) -> None:
    tenant = await _setup(session_maker)
    member = await _member(session_maker, tenant)
    with tenant_scope(tenant.id):
        async with session_maker() as session:
            spend(session, member, 420 * SETTINGS.seats)
            await session.commit()

    events = await _stream(api, "Какие суточные?")

    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "demo_busy"
    assert "созвон" in events[-1]["message"]
    assert fake_llm.calls == []
