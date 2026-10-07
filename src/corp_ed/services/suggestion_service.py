"""Подсказки вопросов на пустом экране чата (ТЗ §6).

Два источника: заданные администратором (первыми, в его порядке) и
частые вопросы компании — только обезличенно: текст из журнала после
маски персональных данных и только если вопрос задали несколько разных
людей (SuggestionRepository.frequent_questions). Частые вопросы у
каждого свои: в счёт идут только ответы из документов, которые
сотруднику видны.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import CodedConflictError, NotFoundError
from corp_ed.domain.models import ChatSuggestion, User
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.chat_repository import SuggestionRepository

MAX_SUGGESTIONS = 12
MAX_TEXT = 200
FREQUENT_LIMIT = 6
FREQUENT_MIN_USERS = 3
"""Сколько разных людей должны задать вопрос, чтобы он стал подсказкой."""
FREQUENT_WINDOW = timedelta(days=90)
FREQUENT_MAX_LENGTH = 160


@dataclass(frozen=True)
class Suggestions:
    company: list[ChatSuggestion]
    frequent: list[str]


def _clean(text: str) -> str:
    return " ".join(text.split())[:MAX_TEXT]


class SuggestionService:
    def __init__(self, session: AsyncSession, audit: AuditRepository) -> None:
        self.session = session
        self.repo = SuggestionRepository(session)
        self.audit = audit

    async def for_chat(self, viewer: User) -> Suggestions:
        """Подсказки для пустого экрана чата сотрудника viewer.

        Частые вопросы — только те, на которые ответили документы, видимые
        viewer (SuggestionRepository.frequent_questions): вопрос, ответ на
        который есть лишь в закрытых для него документах, выдал бы их тему.
        """
        company = await self.repo.list_all()
        taken = {item.text.casefold() for item in company}
        frequent = [
            text
            for text in await self.repo.frequent_questions(
                viewer=viewer.id,
                since=datetime.now(UTC) - FREQUENT_WINDOW,
                min_users=FREQUENT_MIN_USERS,
                limit=FREQUENT_LIMIT,
                max_length=FREQUENT_MAX_LENGTH,
            )
            if text.casefold() not in taken
        ]
        return Suggestions(company=company, frequent=frequent)

    async def create(self, actor: User, text: str) -> ChatSuggestion:
        items = await self.repo.list_all()
        if len(items) >= MAX_SUGGESTIONS:
            raise CodedConflictError(
                f"Подсказок — не больше {MAX_SUGGESTIONS}", "suggestions_limit"
            )
        suggestion = ChatSuggestion(
            text=_clean(text),
            position=max((item.position for item in items), default=-1) + 1,
        )
        self.repo.add(suggestion)
        await self.session.flush()
        self._audit(AuditAction.SUGGESTION_CREATED, actor, suggestion)
        await self.session.commit()
        return suggestion

    async def update(
        self, actor: User, suggestion_id: UUID, text: str
    ) -> ChatSuggestion:
        suggestion = await self._get(suggestion_id)
        suggestion.text = _clean(text)
        self._audit(AuditAction.SUGGESTION_UPDATED, actor, suggestion)
        await self.session.commit()
        return suggestion

    async def delete(self, actor: User, suggestion_id: UUID) -> None:
        suggestion = await self._get(suggestion_id)
        self._audit(AuditAction.SUGGESTION_DELETED, actor, suggestion)
        await self.repo.delete(suggestion)
        await self.session.commit()

    async def reorder(self, actor: User, ids: list[UUID]) -> list[ChatSuggestion]:
        """Новый порядок: все подсказки компании, каждая один раз."""
        items = await self.repo.list_all()
        by_id = {item.id: item for item in items}
        if len(ids) != len(by_id) or set(ids) != set(by_id):
            raise CodedConflictError(
                "Список подсказок изменился — обновите страницу", "suggestions_changed"
            )
        for position, suggestion_id in enumerate(ids):
            by_id[suggestion_id].position = position
        await self.session.commit()
        return [by_id[i] for i in ids]

    async def _get(self, suggestion_id: UUID) -> ChatSuggestion:
        suggestion = await self.repo.get(suggestion_id)
        if suggestion is None:
            raise NotFoundError("Подсказка не найдена")
        return suggestion

    def _audit(self, action: AuditAction, actor: User, item: ChatSuggestion) -> None:
        self.audit.record(
            action,
            tenant_id=actor.tenant_id,
            actor_id=actor.id,
            target_type="suggestion",
            target_id=item.id,
        )
