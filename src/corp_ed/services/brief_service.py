from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Brief, Track
from corp_ed.repositories.brief_repository import BriefRepository

logger = structlog.get_logger()


class BriefService:
    """Бизнес-логика работы с брифами.

    Отдельный сервис, а не метод в ProgramService: у того своя причина
    меняться — генерация, — и смешивать её с созданием анкеты значит
    получить один файл, который правят из-за двух разных задач.
    """

    def __init__(self, brief_repo: BriefRepository, session: AsyncSession) -> None:
        self.brief_repo = brief_repo
        self.session = session

    async def create(
        self,
        *,
        author_id: UUID,
        track: Track,
        role_title: str,
        goals: str,
        tasks: str,
        intern_level: str,
    ) -> Brief:
        """Создать бриф от имени переданного автора.

        tenant_id не передаётся: его проставит хук записи из контекста.
        """
        brief = Brief(
            author_id=author_id,
            track=track,
            role_title=role_title,
            goals=goals,
            tasks=tasks,
            intern_level=intern_level,
        )

        await self.brief_repo.create(brief)
        await self.session.commit()

        logger.info(
            "brief_created",
            brief_id=str(brief.id),
            author_id=str(author_id),
            track=track.value,
        )

        return brief
