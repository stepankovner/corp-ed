from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import NotFoundError
from corp_ed.domain.models import Program, ProgramStatus, User, UserRole
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.types import FinishReason
from corp_ed.prompts.program import build_program_messages
from corp_ed.repositories.brief_repository import BriefRepository
from corp_ed.repositories.program_repository import ProgramRepository

logger = structlog.get_logger()


class ProgramService:
    """Бизнес-логика работы с программами."""

    def __init__(
        self,
        program_repo: ProgramRepository,
        brief_repo: BriefRepository,
        gateway: LLMGateway,
        session: AsyncSession,
    ) -> None:
        self.program_repo = program_repo
        self.brief_repo = brief_repo
        self.gateway = gateway
        self.session = session

    async def generate(self, brief_id: UUID) -> Program:
        brief = await self.brief_repo.get_by_id(brief_id)
        if brief is None:
            raise NotFoundError("Бриф с таким id не найден")

        messages = build_program_messages(brief)

        completion = await self.gateway.generate(messages)

        if completion.finish_reason is not FinishReason.COMPLETED:
            logger.warning(
                "program_generation_incomplete",
                brief_id=brief.id,
                finish_reason=completion.finish_reason.value,
                output_tokens=completion.usage.output_tokens,
            )

        program = Program(
            brief_id=brief.id,
            content=completion.content,
        )

        await self.program_repo.create(program)
        await self.session.commit()

        logger.info(
            "program_generated",
            program_id=program.id,
            brief_id=brief.id,
            usage=completion.usage,
            model_version=completion.model_version,
            model=completion.model,
            latency_ms=completion.latency_ms,
        )

        return program

    async def get(self, program_id: UUID, current_user: User) -> Program:
        program = await self.program_repo.get_by_id(program_id)
        if program is None:
            raise NotFoundError("Программа с таким id не найдена")

        is_manager = current_user.role is UserRole.MANAGER
        is_own_published = (
            program.intern_id == current_user.id
            and program.status is ProgramStatus.APPROVED
        )

        if not (is_manager or is_own_published):
            logger.warning(
                "program_access_denied",
                program_id=program.id,
                user_id=current_user.id,
                user_role=current_user.role.value,
                program_status=program.status.value,
            )
            raise NotFoundError("Программа с таким id не найдена")

        return program
