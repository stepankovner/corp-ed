from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Brief, Program, ProgramStatus, Tenant
from corp_ed.llm.fake import FakeAdapter
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
