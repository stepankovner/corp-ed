"""Обвязка тестов «чёрного ящика» (tests/blackbox/HARNESS.md).

Тесты этого каталога написаны по ТЗ (docs/TZ.md) и схеме API
(frontend/openapi.json), без чтения кода продукта. Им доступны только
HTTP API и то, что делает команда kronto с сервера (CLI): завести
компанию, добавить в команду, включить приложение служебной учётке.
Всё остальное — здесь, и описано в HARNESS.md: авторы тестов видят
описание, а не этот файл.

Приложение — целиком, как в бою: настоящие вход, токены, лимиты, RLS
(роль без BYPASSRLS), своя сессия базы на каждый запрос, разбор файлов в
песочнице, воркеры индексации и писем. Подменены только модель (детерми-
нированная, ответ — начало найденной выдержки), эмбеддинги («мешок
слов») и часы приложения-аутентификатора (каждый код — следующий шаг).
"""

import contextlib
import time as real_time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from corp_ed.api.v1.dependencies import (
    get_embedding_gateway,
    get_llm_gateway,
    get_rag_settings,
    get_secret_box,
    get_session_factory,
)
from corp_ed.core import totp as totp_module
from corp_ed.core.config import DemoSettings
from corp_ed.core.database import get_session
from corp_ed.core.dialogue_store import InMemoryDialogueStore
from corp_ed.core.mail import MemorySender
from corp_ed.core.rate_limit import InMemoryRateLimiter
from corp_ed.core.secrets import SecretBox
from corp_ed.domain.models import Account, StaffMember
from corp_ed.domain.tariffs import Tariff
from corp_ed.domain.types import NotFoundMode
from corp_ed.llm.errors import LLMError
from corp_ed.llm.fake import DevAdapter
from corp_ed.llm.fake_embedding import WordEmbeddingAdapter
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.types import Completion, Message
from corp_ed.main import app
from corp_ed.repositories.account_repository import AccountRepository
from corp_ed.repositories.audit_repository import AuditRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.demo_service import DemoService
from corp_ed.services.digest_service import DigestService
from corp_ed.services.mail_worker import MailWorker
from corp_ed.services.tenant_service import TenantService
from corp_ed.worker import IngestWorker
from tests.api.conftest import TEST_SECRETS_KEY, CleanContextTransport
from tests.soft_authenticator import SoftAuthenticator
from tests.stand_harness import production_rag

API = "/api/v1"


@dataclass(frozen=True)
class Letter:
    to: str
    subject: str
    text: str
    html: str


class Model(LLMGateway):
    """Детерминированная модель: ответ по документам — «Режим разработки,
    ответ без модели. По документам: <начало первой выдержки> [1]»; без
    выдержек — «Режим разработки: общий ответ без модели.».
    unavailable=True — поставщик модели лежит."""

    model_name = "dev"

    def __init__(self) -> None:
        self._dev = DevAdapter(stream_delay=0)
        self.unavailable = False
        self.calls = 0

    def _check(self) -> None:
        if self.unavailable:
            raise LLMError("503 model unavailable", retryable=True)
        self.calls += 1

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        self._check()
        return await self._dev.generate(
            messages, temperature=temperature, max_tokens=max_tokens
        )

    async def stream(  # type: ignore[override]
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> AsyncGenerator[str | Completion, None]:
        self._check()
        async for piece in self._dev.stream(
            messages, temperature=temperature, max_tokens=max_tokens
        ):
            yield piece


class _TotpClock:
    """Часы модуля TOTP: каждый выданный тестам код — следующий 30-секундный
    шаг, иначе второй вход за полминуты упирался бы в «код уже был»."""

    def __init__(self) -> None:
        self.offset = 0.0

    def time(self) -> float:
        return real_time.time() + self.offset


class Kronto:
    """То, что тесты делают не через API (HARNESS.md)."""

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        model: Model,
        embeddings: WordEmbeddingAdapter,
        clock: _TotpClock,
    ) -> None:
        self.session_maker = session_maker
        self.model = model
        self._clock = clock
        self._browsers: list[httpx.AsyncClient] = []
        self._sender = MemorySender()
        self._mail = MailWorker(session_maker, self._sender)
        rag = _rag()
        self._ingest = IngestWorker(session_maker, embeddings, rag)
        self._box = SecretBox([TEST_SECRETS_KEY])

    # --- браузеры ---------------------------------------------------------

    def browser(self) -> httpx.AsyncClient:
        """Новый браузер: своё хранилище cookie, новое «устройство»."""
        client = httpx.AsyncClient(
            transport=CleanContextTransport(app=app), base_url="http://test"
        )
        self._browsers.append(client)
        return client

    async def close(self) -> None:
        for client in self._browsers:
            await client.aclose()

    # --- команда kronto (CLI на сервере) ----------------------------------

    async def create_company(
        self,
        *,
        code: str,
        name: str,
        admin_email: str,
        admin_password: str | None = None,
        seats: int = 10,
        tariff: str = "base",
        not_found_mode: str = "general",
    ) -> dict[str, Any]:
        """cli create-tenant: компания и её администратор. Нет учётки —
        заводится с временным паролем admin_password (сменить при первом
        входе), почта подтверждена. Есть — становится администратором."""
        async with self.session_maker() as session:
            service = TenantService(
                TenantRepository(session),
                UserRepository(session),
                AuditRepository(session),
                session,
            )
            result = await service.provision(
                company_code=code,
                name=name,
                admin_email=admin_email,
                admin_full_name=None,
                admin_password=admin_password,
                seats=seats,
                not_found_mode=NotFoundMode(not_found_mode),
                tariff=Tariff(tariff),
            )
            return {
                "id": str(result.tenant.id),
                "code": result.tenant.company_code,
                "account_created": result.account_created,
            }

    async def make_staff(self, email: str) -> None:
        """cli staff add: учётка — в команде kronto (панель /staff)."""
        async with self.session_maker() as session:
            account = await self._account(session, email)
            if await session.get(StaffMember, account.id) is None:
                session.add(StaffMember(account_id=account.id))
                await session.commit()

    async def enable_totp(self, email: str) -> str:
        """cli set-totp: включить учётке приложение-аутентификатор.
        Возвращает секрет base32 — коды из него даёт totp()."""
        secret = totp_module.new_secret()
        async with self.session_maker() as session:
            account = await self._account(session, email)
            account.totp_secret = self._box.encrypt({"secret": secret})
            account.totp_enabled_at = datetime.now(UTC)
            account.totp_last_step = None
            await session.commit()
        return secret

    def totp(self, secret: str) -> str:
        """Код приложения-аутентификатора: каждый вызов — новый код
        (следующий шаг часов). Подходит и для секрета, который выдал API
        при подключении приложения."""
        self._clock.offset += totp_module.PERIOD
        return totp_module.code_at(secret, totp_module.current_step(self._clock.time()))

    def passkey(self) -> SoftAuthenticator:
        """Программный ключ доступа (как отпечаток или Face ID в браузере):
        register(options) — ответ на регистрацию, sign(options) — подпись
        входа; options — publicKey-параметры, которые выдал API."""
        return SoftAuthenticator()

    async def setup_demo(self) -> None:
        """cli demo setup: вымышленная компания песочницы сайта и её
        документы (на стенде это делает выкатка)."""
        await DemoService(self.session_maker, DemoSettings()).setup()
        await self.run_background()

    async def send_digest(self, company_code: str | None = None) -> int:
        """cli digest --force: недельная сводка администраторам сейчас."""
        report = await DigestService(self.session_maker, zone="Europe/Moscow").send_due(
            force=True, company_code=company_code
        )
        return report.sent

    # --- фоновые процессы -------------------------------------------------

    async def run_background(self) -> None:
        """Воркер: индексирует загруженные документы и отправляет письма из
        очереди — до пустых очередей."""
        for _ in range(200):
            worked = await self._ingest.run_once()
            worked = await self._mail.run_once() or worked
            if not worked:
                return

    async def inbox(self, email: str) -> list[Letter]:
        """Письма, дошедшие до адреса, по порядку (сначала run_background)."""
        await self.run_background()
        wanted = email.strip().casefold()
        return [
            Letter(to=m.to, subject=m.subject, text=m.text, html=m.html)
            for m in self._sender.sent
            if m.to.strip().casefold() == wanted
        ]

    async def last_letter(self, email: str) -> Letter:
        letters = await self.inbox(email)
        assert letters, f"писем на {email} нет"
        return letters[-1]

    async def _account(self, session: AsyncSession, email: str) -> Account:
        account = await AccountRepository(session).get_by_email(email)
        assert account is not None, f"учётки {email} нет"
        return account


def _rag() -> Any:
    # Значения ML из .env.example; «мешок слов» даёт расстояния крупнее
    # настоящей модели — порог шире, остальное как в продукте.
    return production_rag().model_copy(
        update={"faq_max_distance": 0.9, "faq_gate_distance": 0.9}
    )


@pytest.fixture
async def kronto(
    engine: AsyncEngine,
    session: AsyncSession,  # чистая база: TRUNCATE перед тестом
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[Kronto]:
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    model = Model()
    embeddings = WordEmbeddingAdapter()
    clock = _TotpClock()
    monkeypatch.setattr(totp_module, "time", clock)

    async def per_request() -> AsyncGenerator[AsyncSession]:
        async with maker() as request_session:
            yield request_session

    rag = _rag()
    app.dependency_overrides[get_session] = per_request
    app.dependency_overrides[get_session_factory] = lambda: maker
    app.dependency_overrides[get_secret_box] = lambda: SecretBox([TEST_SECRETS_KEY])
    app.dependency_overrides[get_embedding_gateway] = lambda: embeddings
    app.dependency_overrides[get_llm_gateway] = lambda: model
    app.dependency_overrides[get_rag_settings] = lambda: rag
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.dialogue_store = InMemoryDialogueStore()
    harness = Kronto(maker, model, embeddings, clock)
    try:
        yield harness
    finally:
        await harness.close()
        app.dependency_overrides.clear()
        with contextlib.suppress(AttributeError):
            del app.state.dialogue_store
