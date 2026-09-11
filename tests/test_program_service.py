from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import NotFoundError
from corp_ed.domain.models import Brief, Program, ProgramStatus, Tenant, User
from corp_ed.llm.fake import FakeAdapter
from corp_ed.llm.types import FinishReason
from corp_ed.repositories.brief_repository import BriefRepository
from corp_ed.repositories.program_repository import ProgramRepository
from corp_ed.services.program_service import ProgramService


def _build_service(session: AsyncSession, fake: FakeAdapter) -> ProgramService:
    """Собирает сервис с подменённым гейтвеем."""
    return ProgramService(
        ProgramRepository(session),
        BriefRepository(session),
        fake,
        session,
    )


async def test_generate_creates_program(
    brief: Brief,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    fake = FakeAdapter(content="программа на 30 дней")
    service = _build_service(session, fake)

    program = await service.generate(brief.id)

    assert program.content == "программа на 30 дней"
    assert program.brief_id == brief.id
    assert program.status == ProgramStatus.DRAFT
    assert program.tenant_id == tenant_ctx.id

    result = await session.execute(select(Program))
    saved = result.scalars().all()
    assert len(saved) == 1
    assert saved[0].id == program.id


async def test_generate_passes_brief_data_to_model(
    brief: Brief,
    session: AsyncSession,
) -> None:
    fake = FakeAdapter()
    service = _build_service(session, fake)

    await service.generate(brief.id)

    assert len(fake.calls) == 1

    sent_text = " ".join(message.content for message in fake.calls[0])
    assert brief.role_title in sent_text
    assert brief.goals in sent_text
    assert brief.tasks in sent_text
    assert brief.intern_level in sent_text


async def test_generate_raises_when_brief_missing(
    tenant_ctx: Tenant, session: AsyncSession
) -> None:
    fake = FakeAdapter()
    service = _build_service(session, fake)

    with pytest.raises(NotFoundError):
        await service.generate(uuid4())

    assert len(fake.calls) == 0


async def test_finish_reason_truncated(
    brief: Brief,
    session: AsyncSession,
) -> None:
    fake = FakeAdapter(content="обрубок", finish_reason=FinishReason.TRUNCATED)
    service = _build_service(session, fake)

    program = await service.generate(brief.id)

    assert program.content == "обрубок"
    assert program.status == ProgramStatus.DRAFT


async def test_generate_uses_default_model_params(
    brief: Brief,
    session: AsyncSession,
) -> None:
    fake = FakeAdapter()
    service = _build_service(session, fake)

    await service.generate(brief.id)

    assert fake.call_kwargs[0]["temperature"] == 0.3
    assert fake.call_kwargs[0]["max_tokens"] == 1000


async def test_manager_can_read_program(
    brief: Brief,
    manager: User,
    session: AsyncSession,
) -> None:
    service = _build_service(session, FakeAdapter())

    created = await service.generate(brief.id)
    fetched = await service.get(created.id, manager)

    assert fetched.id == created.id
    assert fetched.content == created.content


async def test_get_raises_when_program_missing(
    manager: User,
    session: AsyncSession,
) -> None:
    service = _build_service(session, FakeAdapter())

    with pytest.raises(NotFoundError):
        await service.get(uuid4(), manager)


async def test_intern_cannot_read_draft(
    brief: Brief,
    intern: User,
    session: AsyncSession,
) -> None:
    service = _build_service(session, FakeAdapter())

    created = await service.generate(brief.id)

    with pytest.raises(NotFoundError):
        await service.get(created.id, intern)
