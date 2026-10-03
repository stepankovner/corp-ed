import asyncio
import contextvars
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
from corp_ed.api.v1.session_cookie import REFRESH_COOKIE
from corp_ed.core.config import RagSettings
from corp_ed.core.database import get_session
from corp_ed.core.rate_limit import InMemoryRateLimiter
from corp_ed.core.security import create_access_token, hash_password
from corp_ed.core.tenant_context import current_account, current_tenant
from corp_ed.domain.models import Account, Tenant, User, UserRole
from corp_ed.llm.fake import FakeAdapter
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.main import app
from tests.factories import make_user


class CleanContextTransport(httpx.ASGITransport):
    """Каждый запрос — в чистом контексте, как на сервере.

    ASGITransport выполняет приложение в задаче теста, и контекст теста
    (компания из фикстуры tenant_ctx) протекал в обработчик: код, который
    пишет в базу вне своей компании, в тестах проходил, а на сервере RLS
    его отвергал (вход 03.10). Здесь запрос идёт в копии контекста без
    компании и учётки — их ставит только сам обработчик.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        context = contextvars.copy_context()
        context.run(current_tenant.set, None)
        context.run(current_account.set, None)
        task = asyncio.create_task(
            super().handle_async_request(request), context=context
        )
        return await task


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
        retriever="vector",
        fulltext_weight=0.5,
    )

    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[get_embedding_gateway] = lambda: fake_embeddings
    app.dependency_overrides[get_llm_gateway] = lambda: fake_llm
    app.dependency_overrides[get_rag_settings] = lambda: settings
    # lifespan в тестах не запускается (ASGITransport его не вызывает),
    # поэтому лимитер ставится здесь — свежий на каждый тест, чтобы
    # счётчики одного теста не влияли на другой.
    app.state.rate_limiter = InMemoryRateLimiter()

    transport = CleanContextTransport(app=app)

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


PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
async def account(session: AsyncSession, tenant_ctx: Tenant) -> User:
    """Сотрудник с настоящим паролем — для тестов входа без подмены."""
    user = make_user(
        tenant_id=tenant_ctx.id,
        email="worker@test.com",
        role=UserRole.EMPLOYEE,
        hashed_password=hash_password(PASSWORD),
    )
    session.add(user)
    await session.commit()
    return user


@pytest.fixture
async def admin_account(session: AsyncSession, tenant_ctx: Tenant) -> User:
    user = make_user(
        tenant_id=tenant_ctx.id,
        email="boss@test.com",
        role=UserRole.ADMIN,
        hashed_password=hash_password(PASSWORD),
    )
    session.add(user)
    await session.commit()
    return user


def bearer(user: User) -> dict[str, str]:
    """Заголовок с настоящим подписанным токеном: учётка и её членство."""
    assert user.account is not None
    token = create_access_token(
        user.account.id,
        user.account.token_version,
        tenant_id=user.tenant_id,
        member_id=user.id,
        role=user.role.value,
        member_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


def account_bearer(account: Account) -> dict[str, str]:
    """Токен учётки без выбранной компании."""
    token = create_access_token(account.id, account.token_version)
    return {"Authorization": f"Bearer {token}"}


async def login(
    api: httpx.AsyncClient, email: str, password: str = PASSWORD
) -> httpx.Response:
    return await api.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )


def refresh_token_of(response: httpx.Response) -> str:
    """Refresh-токен из Set-Cookie ответа: в теле его нет (RISKS №44)."""
    return response.cookies[REFRESH_COOKIE]


async def refresh_with(
    api: httpx.AsyncClient, raw: str, headers: dict[str, str] | None = None
) -> httpx.Response:
    """Обновить пару, предъявив refresh-токен так, как это делает браузер.

    Явный заголовок Cookie, а не банка клиента: банка подставила бы
    последний выданный токен, а тестам нужен конкретный (украденный,
    чужой, истёкший).
    """
    return await api.post(
        "/api/v1/auth/refresh",
        headers={"Cookie": f"{REFRESH_COOKIE}={raw}", **(headers or {})},
    )
