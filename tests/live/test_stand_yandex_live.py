"""Сквозной сценарий этапа 5 с настоящим Yandex Cloud: эмбеддинги
text-embeddings-v2 и Alice AI LLM Flash, как в продукте.

Запускается только с ключами (в CI их нет — пропуск):

    YC_API_KEY=… YC_FOLDER_ID=… uv run pytest tests/live -q

Второй тест дополнительно нужен тестовый портал Битрикс24
(BITRIX24_TEST_PORTAL, BITRIX24_TEST_WEBHOOK): документ с портала
проходит синхронизацию, индексацию настоящими эмбеддингами и попадает в
ответ сотруднику со ссылкой на портал — критерий «документ в ответе со
ссылкой» этапа 2 (WORKLOG).
"""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.config import LLMSettings
from corp_ed.core.security import hash_password
from corp_ed.domain.models import Tenant, UserRole
from corp_ed.domain.types import SyncRunStatus, SyncTrigger
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.factory import build_llm_gateway
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.throttle import InMemoryThrottle
from corp_ed.llm.yandex_embedding import YandexEmbeddingAdapter
from corp_ed.stand import run_check
from tests.factories import make_user
from tests.live.bitrix24_stand import HAVE_PORTAL, Bitrix24Stand
from tests.stand_harness import (
    PASSWORD,
    ingest_hook,
    make_admin,
    production_rag,
    stand_client,
)

HAVE_YC = bool(os.environ.get("YC_API_KEY") and os.environ.get("YC_FOLDER_ID"))

pytestmark = pytest.mark.skipif(not HAVE_YC, reason="нужны YC_API_KEY и YC_FOLDER_ID")


@asynccontextmanager
async def yandex_cloud() -> AsyncGenerator[tuple[EmbeddingGateway, LLMGateway]]:
    settings = LLMSettings()  # type: ignore[call-arg]
    async with httpx.AsyncClient() as raw:
        embeddings = YandexEmbeddingAdapter(
            client=raw,
            folder_id=settings.yc_folder_id,
            api_key=settings.yc_api_key.get_secret_value(),
            family=settings.embedding_model,
            dim=settings.embedding_dim,
            document_throttle=InMemoryThrottle(
                settings.embedding_ingest_rps, max_wait=300.0
            ),
            query_throttle=InMemoryThrottle(
                settings.embedding_query_rps, max_wait=60.0
            ),
        )
        yield embeddings, build_llm_gateway(raw, settings)


async def test_stand_check_with_real_yandex_cloud(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await make_admin(session, tenant_ctx)
    rag = production_rag()
    async with (
        yandex_cloud() as (embeddings, llm),
        stand_client(session_maker, embeddings, llm, rag) as client,
    ):
        report = await run_check(
            client,
            company="test",
            email="stand-admin@test.com",
            password=PASSWORD,
            before_poll=ingest_hook(session_maker, embeddings, rag),
            poll_interval=0.5,
        )
    assert report.ok, "\n".join(report.lines())


async def test_stand_check_with_dialogue_memory(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Как на стенде: память диалога включена (3 пары, BH-28). Уточнение
    «А кто его называет?» настоящая модель переписывает в вопрос про
    кодовое слово, и ответ — по документу, про дежурного инженера."""
    await make_admin(session, tenant_ctx)
    rag = production_rag().model_copy(update={"history_turns": 3})
    async with (
        yandex_cloud() as (embeddings, llm),
        stand_client(session_maker, embeddings, llm, rag, dialogue=True) as client,
    ):
        report = await run_check(
            client,
            company="test",
            email="stand-admin@test.com",
            password=PASSWORD,
            before_poll=ingest_hook(session_maker, embeddings, rag),
            poll_interval=0.5,
        )
    follow_up = next(step for step in report.steps if step.name == "уточняющий вопрос")
    assert "учтено реплик 1" in follow_up.detail, follow_up.detail
    assert report.ok, "\n".join(report.lines())


@pytest.mark.skipif(
    not HAVE_PORTAL, reason="нужны BITRIX24_TEST_PORTAL и BITRIX24_TEST_WEBHOOK"
)
async def test_portal_document_answers_with_a_link_to_the_portal(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    employee = make_user(
        id=uuid4(),
        tenant_id=tenant_ctx.id,
        email="portal-employee@test.com",
        role=UserRole.EMPLOYEE,
        hashed_password=hash_password(PASSWORD),
    )
    session.add(employee)
    await session.commit()
    stand = Bitrix24Stand()
    connector = await stand.connect(session, employee)
    rag = production_rag()

    async with (
        yandex_cloud() as (embeddings, llm),
        httpx.AsyncClient() as raw,
        stand_client(session_maker, embeddings, llm, rag) as client,
    ):
        outcome = await stand.sync_service(raw, session_maker).run(
            tenant_ctx.id, connector.id, trigger=SyncTrigger.MANUAL
        )
        assert outcome is not None
        assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
        await ingest_hook(session_maker, embeddings, rag)()

        login = await client.post(
            "/api/v1/auth/login",
            json={
                "company_code": "test",
                "email": employee.email,
                "password": PASSWORD,
            },
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        # Положение о программе «УМНИК» на личном диске тестового портала:
        # «Общий срок выполнения Работ по Договору – 12 месяцев» (п. 3.2).
        response = await client.post(
            "/api/v1/faq/ask",
            json={"question": "Какой общий срок выполнения работ по договору УМНИК?"},
            headers=headers,
        )

    answer = response.json()
    assert response.status_code == 200, answer
    assert answer["origin"] == "documents", answer["content"]
    links = [s["source_url"] for s in answer["sources"] if s.get("source_url")]
    assert links, answer["sources"]
    assert all(link.startswith(stand.portal) for link in links)
    assert "12" in answer["content"], answer["content"]
