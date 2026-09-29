from uuid import UUID

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import ConflictError, DomainError, NotFoundError
from corp_ed.domain.models import GlossaryTerm, User
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.glossary_repository import GlossaryRepository

logger = structlog.get_logger()

MAX_TERMS = 500
"""Потолок на компанию: expand_query проверяет каждый термин на каждом
вопросе. 500 — с запасом для словаря сокращений одной компании."""


class GlossaryLimitError(DomainError):
    def __init__(self) -> None:
        super().__init__(f"В словаре не больше {MAX_TERMS} терминов")


class GlossaryService:
    """Словарь сокращений компании (M5, BH-14): ведёт админ компании."""

    def __init__(
        self,
        repository: GlossaryRepository,
        audit: AuditRepository,
        session: AsyncSession,
    ) -> None:
        self.repository = repository
        self.audit = audit
        self.session = session

    async def list_all(self) -> list[GlossaryTerm]:
        return await self.repository.list_all()

    async def create(self, actor: User, *, term: str, expansion: str) -> GlossaryTerm:
        if await self.repository.count() >= MAX_TERMS:
            raise GlossaryLimitError()
        await self._ensure_unique(term)
        try:
            entry = await self.repository.create(
                GlossaryTerm(term=term, expansion=expansion)
            )
            self._record(AuditAction.GLOSSARY_TERM_CREATED, actor, entry)
            await self.session.commit()
        except IntegrityError as exc:
            # Два одинаковых термина одновременно: проверка выше их не
            # разведёт, индекс — разведёт. Клиенту 409, а не 500.
            await self.session.rollback()
            raise _duplicate(term) from exc
        return entry

    async def update(
        self,
        actor: User,
        term_id: UUID,
        *,
        term: str | None,
        expansion: str | None,
    ) -> GlossaryTerm:
        entry = await self._get(term_id)
        if term is not None and term.lower() != entry.term.lower():
            await self._ensure_unique(term)
        previous = {"term": entry.term, "expansion": entry.expansion}
        if term is not None:
            entry.term = term
        if expansion is not None:
            entry.expansion = expansion
        self._record(AuditAction.GLOSSARY_TERM_UPDATED, actor, entry, previous)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise _duplicate(entry.term) from exc
        await self.session.refresh(entry)
        return entry

    async def delete(self, actor: User, term_id: UUID) -> None:
        entry = await self._get(term_id)
        self._record(AuditAction.GLOSSARY_TERM_DELETED, actor, entry)
        await self.repository.delete(entry)
        await self.session.commit()

    async def _get(self, term_id: UUID) -> GlossaryTerm:
        entry = await self.repository.get_by_id(term_id)
        if entry is None:
            raise NotFoundError("Термин не найден")
        return entry

    async def _ensure_unique(self, term: str) -> None:
        # Уникальный индекс (tenant_id, lower(term)) — страховка на гонку;
        # здесь — понятное сообщение вместо ошибки базы.
        if await self.repository.find_by_term(term) is not None:
            raise _duplicate(term)

    def _record(
        self,
        action: AuditAction,
        actor: User,
        entry: GlossaryTerm,
        previous: dict[str, str] | None = None,
    ) -> None:
        details: dict[str, object] = {
            "term": entry.term,
            "expansion": entry.expansion,
        }
        if previous is not None:
            details["previous"] = previous
        self.audit.record(
            action,
            tenant_id=entry.tenant_id,
            actor_id=actor.id,
            target_type="glossary_term",
            target_id=entry.id,
            details=details,
        )


def _duplicate(term: str) -> ConflictError:
    return ConflictError(f"Термин «{term}» уже есть в словаре")
