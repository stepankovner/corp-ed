import asyncio
import contextvars
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.api.v1.dependencies import (
    get_current_user,
    get_embedding_gateway,
    get_llm_gateway,
    get_rag_settings,
    get_secret_box,
    get_session_factory,
)
from corp_ed.api.v1.session_cookie import REFRESH_COOKIE
from corp_ed.core import totp
from corp_ed.core.config import RagSettings
from corp_ed.core.database import get_session
from corp_ed.core.rate_limit import InMemoryRateLimiter
from corp_ed.core.secrets import SecretBox
from corp_ed.core.security import create_access_token, hash_password
from corp_ed.core.tenant_context import current_account, current_tenant
from corp_ed.domain.models import Account, OutboxEmail, Tenant, User, UserRole
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
    # Фоновый ответ чата открывает свою сессию — к той же тестовой базе.
    factory = async_sessionmaker(
        session.bind, class_=AsyncSession, expire_on_commit=False
    )
    app.dependency_overrides[get_session_factory] = lambda: factory
    # Ключ шифрования секретов (TOTP, подключения) — только для тестов.
    app.dependency_overrides[get_secret_box] = lambda: SecretBox([TEST_SECRETS_KEY])
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
            # login() читает код второго фактора из очереди писем.
            client.test_session = session  # type: ignore[attr-defined]
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
    """Админ с приложением-аутентификатором: без надёжного второго фактора
    ручки компании администратору закрыты (ТЗ §3)."""
    user = make_user(
        tenant_id=tenant_ctx.id,
        email="boss@test.com",
        role=UserRole.ADMIN,
        hashed_password=hash_password(PASSWORD),
    )
    assert user.account is not None
    enable_test_totp(user.account)
    session.add(user)
    await session.commit()
    return user


TEST_SECRETS_KEY = Fernet.generate_key().decode()
TEST_TOTP_SECRET = totp.new_secret()


def enable_test_totp(account: Account) -> None:
    """Приложение-аутентификатор с известным тестам секретом."""
    account.totp_secret = SecretBox([TEST_SECRETS_KEY]).encrypt(
        {"secret": TEST_TOTP_SECRET}
    )
    account.totp_enabled_at = datetime.now(UTC)


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
    api: httpx.AsyncClient,
    email: str,
    password: str = PASSWORD,
    *,
    remember: bool = True,
) -> httpx.Response:
    """Вход целиком, как в браузере: пароль, затем второй фактор (код из
    письма или из приложения). Ответ — последнего шага: сессия или ошибка.
    Только первый шаг — login_step()."""
    first = await login_step(api, email, password, remember=remember)
    if first.status_code != 200 or first.json()["status"] == "ok":
        return first
    challenge = first.json()["mfa"]
    if challenge["methods"] == ["email"]:
        method, code = "email", await last_login_code(api, email)
    else:
        method = "totp"
        code = await _fresh_totp_code(api, email)
    return await api.post(
        "/api/v1/auth/mfa/verify",
        json={"token": challenge["token"], "method": method, "code": code},
    )


async def _fresh_totp_code(api: httpx.AsyncClient, email: str) -> str:
    """Код ещё не использованного шага: тот же код дважды не принимается."""
    session: AsyncSession = api.test_session  # type: ignore[attr-defined]
    account = await session.scalar(
        select(Account)
        .where(Account.email == email.strip().casefold())
        .execution_options(populate_existing=True)
    )
    assert account is not None
    step = max(totp.current_step(), (account.totp_last_step or 0) + 1)
    return totp.code_at(TEST_TOTP_SECRET, step)


async def login_step(
    api: httpx.AsyncClient,
    email: str,
    password: str = PASSWORD,
    *,
    remember: bool = True,
) -> httpx.Response:
    return await api.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "remember": remember},
    )


async def last_login_code(api: httpx.AsyncClient, email: str) -> str:
    session: AsyncSession = api.test_session  # type: ignore[attr-defined]
    mail = (
        await session.scalars(
            select(OutboxEmail)
            .where(
                OutboxEmail.to_email == email.strip().casefold(),
                OutboxEmail.kind == "login_code",
            )
            .order_by(OutboxEmail.created_at.desc())
        )
    ).first()
    assert mail is not None, f"нет кода входа для {email}"
    match = re.search(r"Код для входа: (\d{6})", mail.text_body)
    assert match
    return match.group(1)


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
