import os
from collections.abc import AsyncGenerator
from typing import Any
from uuid import uuid4

import pytest
from dotenv import load_dotenv
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from corp_ed.core.config import BillingSettings
from corp_ed.core.database import Base
from corp_ed.core.db_policies import apply_all
from corp_ed.core.tenant_context import current_tenant
from corp_ed.domain.models import Material, Tenant, User, UserRole
from corp_ed.llm.fake import FakeAdapter
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.repositories.audit_repository import AuditRepository
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.ingest_job_repository import IngestJobRepository
from corp_ed.repositories.material_repository import MaterialRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.credit_service import CreditService
from corp_ed.services.faq_service import FaqService
from corp_ed.services.ingest_service import IngestService
from corp_ed.services.material_service import MaterialService

load_dotenv()
TEST_DATABASE_URL = os.environ["TEST_DATABASE_URL"]
# Настройки читаются лениво (lru_cache), поэтому окружение можно
# дополнить до первого обращения. В CI нет ни SECRET_KEY, ни
# DATABASE_URL — тестам с настоящими токенами они нужны. Ключ —
# только для тестов, длина проходит проверку Settings.
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-" + "x" * 32)
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)


# Роль, под которой работают тесты: без SUPERUSER и BYPASSRLS, как
# роль приложения в production. Под суперпользователем RLS не действует,
# и тесты изоляции ничего бы не доказывали.
APP_ROLE = "corp_ed_app_test"


@pytest.fixture(scope="session")
async def engine() -> AsyncGenerator[AsyncEngine]:
    admin = create_async_engine(TEST_DATABASE_URL)
    async with admin.begin() as conn:
        # create_all не меняет существующие таблицы: новая колонка в модели
        # при старой локальной базе даёт UndefinedColumnError на первом же
        # запросе. Схема пересоздаётся целиком на каждый прогон — таблицы,
        # удалённые из моделей, тоже уходят.
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        # Триггеры и политики RLS, которых нет в моделях (см. db_policies).
        await conn.run_sync(apply_all)
        await conn.execute(
            text(
                f"""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}')
                    THEN CREATE ROLE {APP_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS;
                    END IF;
                END $$
                """
            )
        )
        await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
        await conn.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE "
                f"ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
            )
        )
    await admin.dispose()

    engine = create_async_engine(TEST_DATABASE_URL)

    @event.listens_for(engine.sync_engine, "connect")
    def _drop_privileges(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET ROLE {APP_ROLE}")
        cursor.close()

    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession]:
    tables = ", ".join(Base.metadata.tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))

    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s


@pytest.fixture
async def tenant_ctx(session: AsyncSession) -> AsyncGenerator[Tenant]:
    tenant = Tenant(id=uuid4(), company_code="test", name="Test Co")
    session.add(tenant)
    await session.commit()

    token = current_tenant.set(tenant.id)
    try:
        yield tenant
    finally:
        current_tenant.reset(token)


@pytest.fixture
async def admin(session: AsyncSession, tenant_ctx: Tenant) -> User:
    admin = User(
        id=uuid4(),
        tenant_id=tenant_ctx.id,
        email="admin@test.com",
        role=UserRole.ADMIN,
        hashed_password="hashed",
    )
    session.add(admin)
    await session.commit()

    return admin


@pytest.fixture
async def material(session: AsyncSession, tenant_ctx: Tenant) -> Material:
    material = Material(
        id=uuid4(),
        tenant_id=tenant_ctx.id,
        title="Регламент отпусков",
        content="Первый абзац.\n\nВторой абзац.\n\nТретий абзац.",
    )
    session.add(material)
    await session.commit()
    return material


@pytest.fixture
def fake_embeddings() -> FakeEmbeddingAdapter:
    return FakeEmbeddingAdapter()


@pytest.fixture
def chunk_repo(session: AsyncSession) -> ChunkRepository:
    return ChunkRepository(session)


@pytest.fixture
def ingest_service(
    session: AsyncSession,
    fake_embeddings: FakeEmbeddingAdapter,
) -> IngestService:
    return IngestService(
        material_repo=MaterialRepository(session),
        chunk_repo=ChunkRepository(session),
        embedding_gateway=fake_embeddings,
        session=session,
        # Крошечный бюджет: каждый абзац фикстуры ложится в свой чанк.
        chunk_tokens=5,
        overlap_tokens=0,
    )


@pytest.fixture
def material_service(session: AsyncSession) -> MaterialService:
    return MaterialService(
        material_repo=MaterialRepository(session),
        job_repo=IngestJobRepository(session),
        audit=AuditRepository(session),
        session=session,
    )


@pytest.fixture
def session_maker(
    engine: AsyncEngine, session: AsyncSession
) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий для кода, который открывает их сам (воркер, CLI).

    Зависит от session, чтобы таблицы уже были очищены.
    """
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def fake_llm() -> FakeAdapter:
    return FakeAdapter()


def make_credit_service(session: AsyncSession) -> CreditService:
    """Пул с дефолтами досье: 420 кредитов на место, 2 000 токенов."""
    settings = BillingSettings()
    return CreditService(
        TenantRepository(session),
        QaLogRepository(session),
        AuditRepository(session),
        credits_per_seat=settings.credits_per_seat,
        tokens_per_credit=settings.tokens_per_credit,
        zone=settings.zone,
        warn_at_percent=settings.warn_at_percent,
    )


@pytest.fixture
def faq_service(
    session: AsyncSession,
    fake_embeddings: FakeEmbeddingAdapter,
    fake_llm: FakeAdapter,
) -> FaqService:
    return FaqService(
        chunk_repo=ChunkRepository(session),
        qa_log_repo=QaLogRepository(session),
        credits=make_credit_service(session),
        embedding_gateway=fake_embeddings,
        llm_gateway=fake_llm,
        session=session,
        limit=5,
        max_distance=0.6,
        context_max_tokens=3000,
        temperature=0.0,
    )


@pytest.fixture
async def employee(session: AsyncSession, tenant_ctx: Tenant) -> User:
    employee = User(
        id=uuid4(),
        tenant_id=tenant_ctx.id,
        email="employee@test.com",
        role=UserRole.EMPLOYEE,
        hashed_password="hashed",
    )
    session.add(employee)
    await session.commit()
    return employee
