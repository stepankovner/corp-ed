import structlog

from corp_ed.domain.context import select_context
from corp_ed.domain.types import ChunkMatch, FaqAnswer
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.gateway import LLMGateway
from corp_ed.prompts.faq import (
    NOT_FOUND_ANSWER,
    PROMPT_VERSION,
    build_faq_messages,
    is_not_found,
    normalize_citations,
)
from corp_ed.repositories.chunk_repository import ChunkRepository

logger = structlog.get_logger()


class FaqService:
    """Ответы сотрудникам по документам их компании."""

    def __init__(
        self,
        chunk_repo: ChunkRepository,
        embedding_gateway: EmbeddingGateway,
        llm_gateway: LLMGateway,
        limit: int,
        max_distance: float,
        context_max_tokens: int,
        temperature: float,
    ) -> None:
        self.chunk_repo = chunk_repo
        self.embedding_gateway = embedding_gateway
        self.llm_gateway = llm_gateway
        self.limit = limit
        self.max_distance = max_distance
        self.context_max_tokens = context_max_tokens
        self.temperature = temperature

    async def answer(self, question: str) -> FaqAnswer:
        """Найти релевантные чанки и ответить строго по ним.

        Если ни один чанк не ближе max_distance, модель не вызывается:
        отдавать ей нерелевантный контекст дороже и рискованнее, чем
        честно отказать. Режим «строгий отказ» — позиция досье (3.1):
        ассистент отказывается, а не выдумывает.
        """
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

        if not context:
            logger.info(
                "faq_no_answer",
                found=len(matches),
                nearest=nearest,
                prompt_version=PROMPT_VERSION,
            )
            return FaqAnswer(content=NOT_FOUND_ANSWER, answer_given=False, sources=[])

        messages = build_faq_messages(question=question, matches=context)
        completion = await self.llm_gateway.generate(
            messages=messages,
            temperature=self.temperature,
        )
        # Модели иногда ставят в скобки номер пункта документа [4.2]
        # вместо номера выдержки: фронт такую ссылку не свяжет.
        content = normalize_citations(completion.content, context)
        # Модель может отказать и при найденных выдержках.
        answer_given = not is_not_found(content)

        logger.info(
            "faq_answered",
            used=len(context),
            nearest=nearest,
            answer_given=answer_given,
            prompt_version=PROMPT_VERSION,
            model=completion.model,
            usage=completion.usage,
            latency_ms=completion.latency_ms,
        )
        return FaqAnswer(
            content=content,
            answer_given=answer_given,
            sources=context if answer_given else [],
        )
