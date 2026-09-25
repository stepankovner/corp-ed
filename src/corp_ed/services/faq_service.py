from dataclasses import dataclass, field
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import NotFoundError
from corp_ed.domain.context import select_context
from corp_ed.domain.gaps import mask_pii
from corp_ed.domain.models import QaLog, User
from corp_ed.domain.types import AnswerDiagnostics, AnswerOrigin, ChunkMatch, FaqAnswer
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.types import Completion
from corp_ed.prompts.faq import (
    PROMPT_VERSION,
    build_faq_messages,
    build_general_messages,
    ensure_general_prefix,
    is_not_found,
    normalize_citations,
)
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository
from corp_ed.services.credit_service import CreditService

logger = structlog.get_logger()


@dataclass
class _Outcome:
    content: str
    origin: AnswerOrigin
    sources: list[ChunkMatch]
    completions: list[Completion] = field(default_factory=list)


class FaqService:
    """Ответы сотрудникам по документам их компании."""

    def __init__(
        self,
        chunk_repo: ChunkRepository,
        qa_log_repo: QaLogRepository,
        credits: CreditService,
        embedding_gateway: EmbeddingGateway,
        llm_gateway: LLMGateway,
        session: AsyncSession,
        limit: int,
        max_distance: float,
        context_max_tokens: int,
        temperature: float,
    ) -> None:
        self.chunk_repo = chunk_repo
        self.qa_log_repo = qa_log_repo
        self.credits = credits
        self.embedding_gateway = embedding_gateway
        self.llm_gateway = llm_gateway
        self.session = session
        self.limit = limit
        self.max_distance = max_distance
        self.context_max_tokens = context_max_tokens
        self.temperature = temperature

    async def answer(self, question: str, user: User) -> FaqAnswer:
        """Ответить по документам, а если в них ответа нет — из общих знаний.

        Решение продукта (25.09, режим Р1 «общий ответ с пометкой»):
        сотрудник не упирается в «не знаю», но ответ не из документов
        всегда помечен — текстом в первой строке и полем origin.

        Выдержки, не прошедшие порог max_distance, в модель не уходят:
        нерелевантный контекст дороже и толкает модель выдать чужой
        пункт за ответ.

        Каждый ответ пишется в qa_log (BH-20) в той же транзакции:
        версия промпта, модель, лучшее расстояние, токены и кредиты.

        Пул кредитов проверяется первым: исчерпанный пул не должен
        стоить ни эмбеддинга, ни вызова модели (досье 10.2).
        """
        usage = await self.credits.ensure_available()
        embedded = await self.embedding_gateway.embed_query(question)

        matches = await self.chunk_repo.search(
            embedding=embedded.embedding,
            limit=self.limit,
        )
        relevant: list[ChunkMatch] = [
            match for match in matches if match.distance <= self.max_distance
        ]
        # Порядок сохраняется: номер [n] в ответе модели — позиция выдержки
        # в context, и в том же порядке источники уходят клиенту.
        context = select_context(relevant, max_tokens=self.context_max_tokens)
        nearest = matches[0].distance if matches else None

        outcome = await self._answer(question, context)

        input_tokens = sum(c.usage.input_tokens for c in outcome.completions)
        output_tokens = sum(c.usage.output_tokens for c in outcome.completions)
        credits = self.credits.cost(input_tokens + output_tokens)
        model = outcome.completions[-1].model
        answer_given = outcome.origin is AnswerOrigin.DOCUMENTS

        entry = await self.qa_log_repo.add(
            QaLog(
                user_id=user.id,
                # Для отчёта о пробелах нужен текст, но не персональные
                # данные в нём (152-ФЗ): почта, телефоны, ФИО — маской.
                question=mask_pii(question),
                question_embedding=embedded.embedding,
                embedding_model=embedded.model,
                prompt_version=PROMPT_VERSION,
                llm_model=model,
                best_vector_distance=nearest,
                answer_given=answer_given,
                origin=outcome.origin.value,
                source_chunk_ids=[source.id for source in outcome.sources],
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                credits=credits,
            )
        )
        await self.credits.note_spend(usage, credits)
        await self.session.commit()

        logger.info(
            "faq_answered",
            qa_log_id=str(entry.id),
            origin=outcome.origin.value,
            used=len(outcome.sources),
            nearest=nearest,
            prompt_version=PROMPT_VERSION,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=sum(c.latency_ms for c in outcome.completions),
        )
        return FaqAnswer(
            content=outcome.content,
            answer_given=answer_given,
            origin=outcome.origin,
            sources=outcome.sources,
            log_id=entry.id,
            diagnostics=AnswerDiagnostics(
                model=model,
                prompt_version=PROMPT_VERSION,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                credits=credits,
                nearest_distance=nearest,
            ),
        )

    async def search(self, question: str, limit: int) -> list[ChunkMatch]:
        """Отладка поиска для eval (BH-5): top-K без порога и без LLM.

        Порог здесь не применяется: для подбора порога (A8) нужны
        расстояния и у тех вопросов, которые его не прошли.
        """
        embedded = await self.embedding_gateway.embed_query(question)
        return await self.chunk_repo.search(embedding=embedded.embedding, limit=limit)

    async def rate(self, user: User, log_id: UUID, feedback: int) -> None:
        """👍/👎 к своему ответу. Чужой ответ — 404, как несуществующий."""
        entry = await self.qa_log_repo.get_by_id(log_id)
        if entry is None or entry.user_id != user.id:
            raise NotFoundError("Ответ не найден")
        entry.feedback = feedback
        await self.session.commit()

    async def _answer(self, question: str, context: list[ChunkMatch]) -> _Outcome:
        if not context:
            return await self._general_answer(question, reason="no_relevant_excerpts")

        completion = await self.llm_gateway.generate(
            messages=build_faq_messages(question=question, matches=context),
            temperature=self.temperature,
        )
        # Модели иногда ставят в скобки номер пункта документа [4.2]
        # вместо номера выдержки: фронт такую ссылку не свяжет.
        content = normalize_citations(completion.content, context)

        # Выдержки нашлись, но модель по ним отказала: в документах
        # ответа нет — это тот же случай, что и пустой поиск.
        if is_not_found(content):
            general = await self._general_answer(question, reason="model_refusal")
            general.completions.insert(0, completion)
            return general

        return _Outcome(
            content=content,
            origin=AnswerOrigin.DOCUMENTS,
            sources=context,
            completions=[completion],
        )

    async def _general_answer(self, question: str, *, reason: str) -> _Outcome:
        """Ответ из общих знаний со строгой пометкой.

        В этот промпт не уходит ни одной выдержки: смешать общие сведения
        с документами компании модель здесь не может. ensure_general_prefix
        ставит пометку, даже если модель её потеряла или переписала, —
        ответ без пометки клиенту уйти не может. Источников нет.
        """
        completion = await self.llm_gateway.generate(
            messages=build_general_messages(question),
            temperature=self.temperature,
        )
        logger.info("faq_general_answer", reason=reason)
        return _Outcome(
            content=ensure_general_prefix(completion.content),
            origin=AnswerOrigin.GENERAL_KNOWLEDGE,
            sources=[],
            completions=[completion],
        )
