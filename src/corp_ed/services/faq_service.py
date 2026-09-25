import structlog

from corp_ed.domain.context import select_context
from corp_ed.domain.types import AnswerOrigin, ChunkMatch, FaqAnswer
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.gateway import LLMGateway
from corp_ed.prompts.faq import (
    PROMPT_VERSION,
    build_faq_messages,
    build_general_messages,
    ensure_general_prefix,
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
        """Ответить по документам, а если в них ответа нет — из общих знаний.

        Решение продукта (25.09, режим Р1 «общий ответ с пометкой»):
        сотрудник не упирается в «не знаю», но ответ не из документов
        всегда помечен — текстом в первой строке и полем origin.

        Выдержки, не прошедшие порог max_distance, в модель не уходят:
        нерелевантный контекст дороже и толкает модель выдать чужой
        пункт за ответ.
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
            return await self._general_answer(
                question, reason="no_relevant_excerpts", nearest=nearest
            )

        messages = build_faq_messages(question=question, matches=context)
        completion = await self.llm_gateway.generate(
            messages=messages,
            temperature=self.temperature,
        )
        # Модели иногда ставят в скобки номер пункта документа [4.2]
        # вместо номера выдержки: фронт такую ссылку не свяжет.
        content = normalize_citations(completion.content, context)

        # Выдержки нашлись, но модель по ним отказала: в документах
        # ответа нет — это тот же случай, что и пустой поиск.
        if is_not_found(content):
            return await self._general_answer(
                question, reason="model_refusal", nearest=nearest
            )

        logger.info(
            "faq_answered",
            origin=AnswerOrigin.DOCUMENTS.value,
            used=len(context),
            nearest=nearest,
            prompt_version=PROMPT_VERSION,
            model=completion.model,
            usage=completion.usage,
            latency_ms=completion.latency_ms,
        )
        return FaqAnswer(
            content=content,
            answer_given=True,
            origin=AnswerOrigin.DOCUMENTS,
            sources=context,
        )

    async def _general_answer(
        self, question: str, *, reason: str, nearest: float | None
    ) -> FaqAnswer:
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
        logger.info(
            "faq_answered",
            origin=AnswerOrigin.GENERAL_KNOWLEDGE.value,
            reason=reason,
            nearest=nearest,
            prompt_version=PROMPT_VERSION,
            model=completion.model,
            usage=completion.usage,
            latency_ms=completion.latency_ms,
        )
        return FaqAnswer(
            content=ensure_general_prefix(completion.content),
            answer_given=False,
            origin=AnswerOrigin.GENERAL_KNOWLEDGE,
            sources=[],
        )
