"""Приложение целиком для сквозного сценария `corp_ed.stand`.

Настоящие вход, лимиты, RLS-контекст, загрузка через песочницу, воркер
индексации и сервис ответов; подменяются только шлюзы модели и
эмбеддингов (в живом тесте — нет). Каждый запрос получает свою сессию,
как в бою: иначе API видел бы закешированный статус материала, который
воркер уже поменял в другой сессии.
"""

import math
import re
import zlib
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.api.v1.dependencies import (
    get_embedding_gateway,
    get_llm_gateway,
    get_rag_settings,
)
from corp_ed.core.config import EMBEDDING_DIM, RagSettings
from corp_ed.core.database import get_session
from corp_ed.core.rate_limit import InMemoryRateLimiter
from corp_ed.core.security import hash_password
from corp_ed.domain.models import Tenant, User, UserRole
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.types import EmbeddingResult
from corp_ed.main import app
from corp_ed.worker import IngestWorker

PASSWORD = "stand-check-password-42"


def production_rag() -> RagSettings:
    """Значения ML из .env.example (backend-handoff v2, раздел 1)."""
    return RagSettings(
        chunk_tokens=400,
        overlap_tokens=50,
        faq_limit=5,
        faq_max_distance=0.51,
        context_max_tokens=3000,
        faq_temperature=0.0,
        retriever="vector",
        fulltext_weight=0.5,
    )


class WordEmbeddings(EmbeddingGateway):
    """«Мешок слов» вместо модели: общие слова сближают тексты, без общих
    слов расстояние близко к 1. Достаточно, чтобы вопрос по документу
    находил его, а вопрос вне документов — нет."""

    async def embed_document(self, text: str) -> EmbeddingResult:
        return self._result(text)

    async def embed_query(self, text: str) -> EmbeddingResult:
        return self._result(text)

    def _result(self, text: str) -> EmbeddingResult:
        vector = [0.0] * EMBEDDING_DIM
        for word in re.findall(r"\w+", text.lower()):
            vector[zlib.crc32(word.encode()) % EMBEDDING_DIM] += 1.0
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        return EmbeddingResult(
            embedding=[x / norm for x in vector],
            input_tokens=len(text) // 3,
            model_version="words",
            model=f"words@{EMBEDDING_DIM}",
            latency_ms=0,
        )


async def make_admin(session: AsyncSession, tenant: Tenant) -> User:
    admin = User(
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
) -> AsyncGenerator[httpx.AsyncClient]:
    async def per_request() -> AsyncGenerator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = per_request
    app.dependency_overrides[get_embedding_gateway] = lambda: embeddings
    app.dependency_overrides[get_llm_gateway] = lambda: llm
    app.dependency_overrides[get_rag_settings] = lambda: rag
    app.state.rate_limiter = InMemoryRateLimiter()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
