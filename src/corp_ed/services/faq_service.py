import structlog

from corp_ed.domain.types import ChunkMatch, FaqAnswer
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.gateway import LLMGateway
from corp_ed.prompts.faq import build_faq_messages
from corp_ed.repositories.chunk_repository import ChunkRepository

logger = structlog.get_logger()

NO_ANSWER_TEXT = "В материалах нет точного ответа, уточните у руководителя."


class FaqService:
    """Ответы на вопросы стажёра по утверждённым материалам компании."""

    def __init__(
        self,
        chunk_repo: ChunkRepository,
        embedding_gateway: EmbeddingGateway,
        llm_gateway: LLMGateway,
        limit: int,
        max_distance: float,
    ) -> None:
        self.chunk_repo = chunk_repo
        self.embedding_gateway = embedding_gateway
        self.llm_gateway = llm_gateway
        self.limit = limit
        self.max_distance = max_distance

    async def answer(self, question: str) -> FaqAnswer:
        """Найти релевантные чанки и ответить строго по ним.

        Если ни один чанк не ближе max_distance, модель не вызывается:
        отдавать ей нерелевантный контекст дороже и рискованнее, чем
        честно отказать.
        """
        embedded = await self.embedding_gateway.embed_query(question)

        matches = await self.chunk_repo.search(
            embedding=embedded.embedding,
            limit=self.limit,
        )
        relevant: list[ChunkMatch] = [
            match for match in matches if match.distance <= self.max_distance
        ]

        if not relevant:
            logger.info(
                "faq_no_answer",
                question=question,
                found=len(matches),
                nearest=matches[0].distance if matches else None,
            )
            return FaqAnswer(
                content=NO_ANSWER_TEXT,
                answer_given=False,
                sources=[],
            )

        messages = build_faq_messages(question=question, matches=relevant)
        completion = await self.llm_gateway.generate(messages=messages)

        logger.info(
            "faq_answered",
            question=question,
            used=len(relevant),
            nearest=relevant[0].distance,
        )
        return FaqAnswer(
            content=completion.content,
            answer_given=True,
            sources=relevant,
        )
