import os
from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from corp_ed.core.database import Base
from corp_ed.core.tenant_context import current_tenant
from corp_ed.domain.models import Brief, Material, Tenant, Track, User, UserRole
from corp_ed.llm.fake import FakeAdapter
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.material_repository import MaterialRepository
from corp_ed.services.faq_service import FaqService
from corp_ed.services.material_service import MaterialService

load_dotenv()
TEST_DATABASE_URL = os.environ["TEST_DATABASE_URL"]


@pytest.fixture(scope="session")
async def engine() -> AsyncGenerator[AsyncEngine]:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
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
async def manager(session: AsyncSession, tenant_ctx: Tenant) -> User:
    manager = User(
        id=uuid4(),
        tenant_id=tenant_ctx.id,
        email="manager@test.com",
        role=UserRole.MANAGER,
        hashed_password="hashed",
    )
    session.add(manager)
    await session.commit()

    return manager


@pytest.fixture
async def brief(session: AsyncSession, manager: User) -> Brief:
    brief = Brief(
        id=uuid4(),
        tenant_id=manager.tenant_id,
        author_id=manager.id,
        track=Track.MARKETING,
        role_title="Marketing Intern",
        goals="Learn marketing",
        tasks="Assist with campaigns",
        intern_level="junior",
    )
    session.add(brief)
    await session.commit()

    return brief


@pytest.fixture
async def material(session: AsyncSession, tenant_ctx: Tenant) -> Material:
    material = Material(
        id=uuid4(),
        tenant_id=tenant_ctx.id,
        track=Track.MARKETING,
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
def material_service(
    session: AsyncSession,
    fake_embeddings: FakeEmbeddingAdapter,
) -> MaterialService:
    return MaterialService(
        material_repo=MaterialRepository(session),
        chunk_repo=ChunkRepository(session),
        embedding_gateway=fake_embeddings,
        session=session,
        chunk_size=20,
        overlap=0,
    )


@pytest.fixture
def fake_llm() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def faq_service(
    session: AsyncSession,
    fake_embeddings: FakeEmbeddingAdapter,
    fake_llm: FakeAdapter,
) -> FaqService:
    return FaqService(
        chunk_repo=ChunkRepository(session),
        embedding_gateway=fake_embeddings,
        llm_gateway=fake_llm,
        limit=5,
        max_distance=0.6,
    )


@pytest.fixture
async def intern(session: AsyncSession, tenant_ctx: Tenant) -> User:
    intern = User(
        id=uuid4(),
        tenant_id=tenant_ctx.id,
        email="intern@test.com",
        role=UserRole.INTERN,
        hashed_password="hashed",
    )
    session.add(intern)
    await session.commit()
    return intern
