from collections.abc import AsyncGenerator, Awaitable, Callable

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.api.v1.dependencies import (
    get_current_user,
    get_embedding_gateway,
    get_llm_gateway,
    get_rag_settings,
)
from corp_ed.core.config import RagSettings
from corp_ed.core.database import get_session
from corp_ed.core.tenant_context import current_tenant
from corp_ed.domain.models import User
from corp_ed.llm.fake import FakeAdapter
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.main import app


def _as(user: User) -> Callable[[], Awaitable[User]]:
    async def current_user() -> User:
        current_tenant.set(user.tenant_id)
        return user

    return current_user


@pytest.fixture
async def api(
    session: AsyncSession,
    fake_embeddings: FakeEmbeddingAdapter,
    fake_llm: FakeAdapter,
) -> AsyncGenerator[httpx.AsyncClient]:
    async def test_session() -> AsyncGenerator[AsyncSession]:
        yield session

    settings = RagSettings(
        chunk_tokens=5,
        overlap_tokens=0,
        faq_limit=5,
        faq_max_distance=0.6,
        context_max_tokens=3000,
        faq_temperature=0.0,
    )

    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[get_embedding_gateway] = lambda: fake_embeddings
    app.dependency_overrides[get_llm_gateway] = lambda: fake_llm
    app.dependency_overrides[get_rag_settings] = lambda: settings

    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def admin_client(
    api: httpx.AsyncClient,
    admin: User,
) -> httpx.AsyncClient:
    app.dependency_overrides[get_current_user] = _as(admin)
    return api


@pytest.fixture
def employee_client(
    api: httpx.AsyncClient,
    employee: User,
) -> httpx.AsyncClient:
    app.dependency_overrides[get_current_user] = _as(employee)
    return api
