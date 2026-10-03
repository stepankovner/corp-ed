"""Приложение целиком для сквозного сценария `corp_ed.stand`.

Настоящие вход, лимиты, RLS-контекст, загрузка через песочницу, воркер
индексации и сервис ответов; подменяются только шлюзы модели и
эмбеддингов (в живом тесте — нет). Каждый запрос получает свою сессию,
как в бою: иначе API видел бы закешированный статус материала, который
воркер уже поменял в другой сессии.
"""

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.api.v1.dependencies import (
    get_embedding_gateway,
    get_llm_gateway,
    get_rag_settings,
)
from corp_ed.core.config import RagSettings
from corp_ed.core.database import get_session
from corp_ed.core.dialogue_store import InMemoryDialogueStore
from corp_ed.core.rate_limit import InMemoryRateLimiter
from corp_ed.core.security import hash_password
from corp_ed.domain.models import Tenant, User, UserRole
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.fake_embedding import WordEmbeddingAdapter
from corp_ed.llm.gateway import LLMGateway
from corp_ed.main import app
from corp_ed.worker import IngestWorker
from tests.factories import make_user

PASSWORD = "stand-check-password-42"


ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def production_rag() -> RagSettings:
    """Значения ML из .env.example (backend-handoff, раздел 1).

    Читаются из файла, а не переписываются сюда: ML меняет число в
    .env.example (порог 0,59 — BH-31), и проверка стенда сразу идёт с тем
    же значением, что продукт.
    """
    values = dotenv_values(ENV_EXAMPLE)
    fields = {
        name: values[key]
        for name in RagSettings.model_fields
        if (key := f"RAG_{name.upper()}") in values
    }
    return RagSettings.model_validate(fields)


WordEmbeddings = WordEmbeddingAdapter


async def make_admin(session: AsyncSession, tenant: Tenant) -> User:
    admin = make_user(
        id=uuid4(),
        tenant_id=tenant.id,
        email="stand-admin@test.com",
        role=UserRole.ADMIN,
        hashed_password=hash_password(PASSWORD),
    )
    session.add(admin)
    await session.commit()
    return admin


def ingest_hook(
    session_maker: async_sessionmaker[AsyncSession],
    embeddings: EmbeddingGateway,
    rag: RagSettings,
) -> Callable[[], "object"]:
    worker = IngestWorker(session_maker, embeddings, rag)

    async def drain() -> None:
        while await worker.run_once():
            pass

    return drain


@asynccontextmanager
async def stand_client(
    session_maker: async_sessionmaker[AsyncSession],
    embeddings: EmbeddingGateway,
    llm: LLMGateway,
    rag: RagSettings,
    *,
    dialogue: bool = False,
) -> AsyncGenerator[httpx.AsyncClient]:
    """dialogue — хранилище реплик (BH-28), как у lifespan в бою; память
    работает, только если и в rag history_turns > 0."""

    async def per_request() -> AsyncGenerator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = per_request
    app.dependency_overrides[get_embedding_gateway] = lambda: embeddings
    app.dependency_overrides[get_llm_gateway] = lambda: llm
    app.dependency_overrides[get_rag_settings] = lambda: rag
    app.state.rate_limiter = InMemoryRateLimiter()
    if dialogue:
        app.state.dialogue_store = InMemoryDialogueStore()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        if dialogue:
            del app.state.dialogue_store
