from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import ConflictError, NotFoundError
from corp_ed.domain.models import Program, ProgramStatus, User, UserRole
from corp_ed.domain.types import ProgramSummary
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

    async def list_all(self) -> list[ProgramSummary]:
        """Программы тенанта для списка: должность берётся из брифа."""
        programs = await self.program_repo.list_all()

        return [
            ProgramSummary(
                id=program.id,
                status=program.status,
                role_title=program.brief.role_title,
                created_at=program.created_at,
            )
            for program in programs
        ]

    async def update(
        self,
        program_id: UUID,
        *,
        content: str | None = None,
        intern_id: UUID | None = None,
    ) -> Program:
        """Правка черновика: текст и назначенный стажёр.

        Утверждённую программу менять нельзя: стажёр уже мог её прочитать,
        и подмена текста задним числом сделала бы утверждение бессмысленным.
        """
        program = await self._get_or_raise(program_id)

        if program.status is ProgramStatus.APPROVED:
            raise ConflictError("Утверждённую программу изменить нельзя")

        if content is not None:
            program.content = content
        if intern_id is not None:
            program.intern_id = intern_id

        await self.session.commit()

        logger.info(
            "program_updated",
            program_id=program.id,
            content_changed=content is not None,
            intern_assigned=intern_id is not None,
        )

        return program

    async def approve(self, program_id: UUID) -> Program:
        """Утвердить программу и тем самым открыть её стажёру.

        Без назначенного стажёра утверждать нечего: такую программу
        не увидит никто, и руководитель решит, что всё получилось.

        Повторное утверждение ничего не меняет и не считается ошибкой:
        два нажатия подряд не должны заканчиваться пятисоткой.
        """
        program = await self._get_or_raise(program_id)

        if program.status is ProgramStatus.APPROVED:
            return program

        if program.intern_id is None:
            raise ConflictError(
                "Назначьте стажёра перед утверждением: иначе программу никто не увидит"
            )

        program.status = ProgramStatus.APPROVED
        await self.session.commit()

        logger.info(
            "program_approved",
            program_id=program.id,
            intern_id=program.intern_id,
        )

        return program

    async def get_for_intern(self, current_user: User) -> Program:
        """Утверждённая программа стажёра. Черновики сюда не попадают."""
        program = await self.program_repo.get_approved_for_intern(current_user.id)
        if program is None:
            raise NotFoundError("Программа адаптации ещё готовится")
        return program

    async def _get_or_raise(self, program_id: UUID) -> Program:
        program = await self.program_repo.get_by_id(program_id)
        if program is None:
            raise NotFoundError("Программа с таким id не найдена")
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
