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
from corp_ed.domain.models import Tenant

load_dotenv()
TEST_DATABASE_URL = os.environ["TEST_DATABASE_URL"]


@pytest.fixture(scope="session")
async def engine() -> AsyncGenerator[AsyncEngine]:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
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
