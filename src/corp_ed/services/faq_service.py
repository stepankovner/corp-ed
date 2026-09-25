from dataclasses import dataclass, field, replace
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import NotFoundError
from corp_ed.domain.context import select_context
from corp_ed.domain.fulltext import to_fulltext_query
from corp_ed.domain.fusion import DEFAULT_RRF_K, rrf_merge
from corp_ed.domain.gaps import mask_pii
from corp_ed.domain.models import QaLog, User
from corp_ed.domain.query import expand_query
from corp_ed.domain.types import (
    AnswerDiagnostics,
    AnswerOrigin,
    ChunkMatch,
    FaqAnswer,
    NotFoundMode,
    Retriever,
)
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.types import Completion, FinishReason
from corp_ed.prompts.faq import (
    NOT_FOUND_ANSWER,
    PROMPT_VERSION,
    build_faq_messages,
    build_general_messages,
    ensure_general_prefix,
    is_not_found,
    normalize_citations,
)
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.glossary_repository import GlossaryRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.credit_service import CreditService

logger = structlog.get_logger()

FUSION_CANDIDATES = 50
"""Глубина каждой ветки перед слиянием RRF — как в замерах ML
(eval/bench.py). Слияние топ-5 с топ-5 теряет чанки, которые ни одна
ветка не ставит в пятёрку, но обе держат высоко."""


@dataclass(frozen=True)
class _Retrieval:
    candidates: list[ChunkMatch]
    """top-limit в итоговом порядке, без порога."""
    relevant: list[ChunkMatch]
    """Прошедшие порог — только они могут попасть в промпт."""
    nearest: float | None
    """Расстояние лучшего ВЕКТОРНОГО кандидата (qa_log, классы пробелов)."""
    best_fulltext: float | None
    """ts_rank_cd лучшего полнотекстового совпадения; None — пусто."""


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
        tenant_repo: TenantRepository,
        glossary_repo: GlossaryRepository,
        credits: CreditService,
        embedding_gateway: EmbeddingGateway,
        llm_gateway: LLMGateway,
        session: AsyncSession,
        limit: int,
        max_distance: float,
        context_max_tokens: int,
        temperature: float,
        retriever: Retriever,
        fulltext_weight: float,
    ) -> None:
        self.chunk_repo = chunk_repo
        self.qa_log_repo = qa_log_repo
        self.tenant_repo = tenant_repo
        self.glossary_repo = glossary_repo
        self.credits = credits
        self.embedding_gateway = embedding_gateway
        self.llm_gateway = llm_gateway
        self.session = session
        self.limit = limit
        self.max_distance = max_distance
        self.context_max_tokens = context_max_tokens
        self.temperature = temperature
        self.retriever = retriever
        self.fulltext_weight = fulltext_weight

    async def answer(self, question: str, user: User) -> FaqAnswer:
        """Ответить по документам, а если в них ответа нет — из общих знаний.

        Решение продукта (25.09, режим Р1 «общий ответ с пометкой»):
        сотрудник не упирается в «не знаю», но ответ не из документов
        всегда помечен — текстом в первой строке и полем origin.
        Компания в строгом режиме (NotFoundMode.STRICT) вместо этого
        получает честный отказ, как в досье v3.2 (BH-24).

        Выдержки, не прошедшие порог max_distance, в модель не уходят:
        нерелевантный контекст дороже и толкает модель выдать чужой
        пункт за ответ. Как именно применяется порог — зависит от
        способа поиска (_retrieve).

        Каждый ответ пишется в qa_log (BH-20) в той же транзакции:
        версия промпта, модель, лучшее расстояние, токены и кредиты.

        Пул кредитов проверяется первым: исчерпанный пул не должен
        стоить ни эмбеддинга, ни вызова модели (досье 10.2).
        """
        usage = await self.credits.ensure_available()
        tenant = await self.tenant_repo.get_by_id(user.tenant_id)
        mode = NotFoundMode(tenant.not_found_mode) if tenant else NotFoundMode.GENERAL
        search_text = await self._search_text(question)
        embedded = await self.embedding_gateway.embed_query(search_text)

        found = await self._retrieve(
            search_text, embedded.embedding, limit=self.limit, retriever=self.retriever
        )
        # Порядок сохраняется: номер [n] в ответе модели — позиция выдержки
        # в context, и в том же порядке источники уходят клиенту.
        context = select_context(found.relevant, max_tokens=self.context_max_tokens)
        nearest = found.nearest

        outcome = await self._answer(question, context, mode)

        input_tokens = sum(c.usage.input_tokens for c in outcome.completions)
        output_tokens = sum(c.usage.output_tokens for c in outcome.completions)
        # Строгий отказ без выдержек не вызывал модель — и не стоит кредита.
        credits = (
            self.credits.cost(input_tokens + output_tokens)
            if outcome.completions
            else 0
        )
        last = outcome.completions[-1] if outcome.completions else None
        model = last.model if last else None
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
                llm_model_version=last.model_version if last else None,
                best_vector_distance=nearest,
                best_fulltext_score=found.best_fulltext,
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
                model_version=last.model_version if last else None,
                prompt_version=PROMPT_VERSION,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                credits=credits,
                nearest_distance=nearest,
            ),
        )

    async def search(
        self, question: str, limit: int, retriever: Retriever | None = None
    ) -> list[ChunkMatch]:
        """Отладка поиска для eval (BH-5): top-K без порога и без LLM.

        Порог здесь не применяется: для подбора порога (A8) нужны
        расстояния и у тех вопросов, которые его не прошли. retriever —
        сравнить способы поиска на живой базе, не меняя настройку.
        """
        search_text = await self._search_text(question)
        embedded = await self.embedding_gateway.embed_query(search_text)
        found = await self._retrieve(
            search_text,
            embedded.embedding,
            limit=limit,
            retriever=retriever or self.retriever,
        )
        return found.candidates

    async def _search_text(self, question: str) -> str:
        """Вопрос с расшифровками сокращений компании (M5, BH-14).

        Только для поиска: эмбеддинг и полнотекст. В промпт модели уходит
        исходный вопрос — текст из словаря, который пишет админ, не
        становится инструкцией для модели, а ответ не «видит» того, чего
        сотрудник не спрашивал.
        """
        glossary = await self.glossary_repo.as_mapping()
        if not glossary:
            return question
        expanded = expand_query(question, glossary)
        if expanded != question:
            # Только факт: текст вопроса в логах — персональные данные.
            logger.info("faq_question_expanded")
        return expanded

    async def _retrieve(
        self,
        question: str,
        embedding: list[float],
        *,
        limit: int,
        retriever: Retriever,
    ) -> _Retrieval:
        """Найти выдержки и решить, какие из них проходят порог.

        VECTOR: порог — на каждой выдержке. HYBRID (M1, BH-12): вектор и
        полнотекст по FUSION_CANDIDATES, слияние RRF с весами 1.0 и
        fulltext_weight; порог — на лучшем ВЕКТОРНОМ кандидате: прошёл —
        в промпт идёт вся выдача, нет — ответа в документах нет. Скор RRF
        зависит только от рангов, порог по нему ставить нельзя (ML).

        Полнотекстовая ветка считается и в режиме VECTOR (одна строка):
        её лучший ранг — сигнал отчёта о пробелах (classify_miss),
        «точное совпадение было, а вектор промахнулся».
        """
        query = to_fulltext_query(question)
        if retriever is Retriever.VECTOR:
            vector = await self.chunk_repo.search(embedding=embedding, limit=limit)
            fulltext = (
                await self.chunk_repo.search_fulltext(query, embedding, limit=1)
                if query
                else []
            )
            return _Retrieval(
                candidates=vector,
                relevant=[m for m in vector if m.distance <= self.max_distance],
                nearest=vector[0].distance if vector else None,
                best_fulltext=fulltext[0].fulltext_rank if fulltext else None,
            )

        depth = max(limit, FUSION_CANDIDATES)
        vector = await self.chunk_repo.search(embedding=embedding, limit=depth)
        # Пустая строка (вопрос из одних знаков) — ветку не вызывать.
        fulltext = (
            await self.chunk_repo.search_fulltext(query, embedding, limit=depth)
            if query
            else []
        )
        ranks = {m.id: m.fulltext_rank for m in fulltext}
        by_id = {m.id: m for m in fulltext} | {
            m.id: replace(m, fulltext_rank=ranks.get(m.id)) for m in vector
        }
        merged = rrf_merge(
            [[m.id for m in vector], [m.id for m in fulltext]],
            weights=[1.0, self.fulltext_weight],
            k=DEFAULT_RRF_K,
        )
        candidates = [by_id[chunk_id] for chunk_id, _ in merged[:limit]]
        nearest = vector[0].distance if vector else None
        passed = nearest is not None and nearest <= self.max_distance
        return _Retrieval(
            candidates=candidates,
            relevant=candidates if passed else [],
            nearest=nearest,
            best_fulltext=fulltext[0].fulltext_rank if fulltext else None,
        )

    async def rate(self, user: User, log_id: UUID, feedback: int) -> None:
        """👍/👎 к своему ответу. Чужой ответ — 404, как несуществующий."""
        entry = await self.qa_log_repo.get_by_id(log_id)
        if entry is None or entry.user_id != user.id:
            raise NotFoundError("Ответ не найден")
        entry.feedback = feedback
        await self.session.commit()

    async def _answer(
        self, question: str, context: list[ChunkMatch], mode: NotFoundMode
    ) -> _Outcome:
        if not context:
            return await self._not_found(question, mode, reason="no_relevant_excerpts")

        completion = await self.llm_gateway.generate(
            messages=build_faq_messages(question=question, matches=context),
            temperature=self.temperature,
        )
        if completion.finish_reason is FinishReason.FILTERED:
            return self._filtered(completion, reason="documents")
        # Модели иногда ставят в скобки номер пункта документа [4.2]
        # вместо номера выдержки: фронт такую ссылку не свяжет.
        content = normalize_citations(completion.content, context)

        # Выдержки нашлись, но модель по ним отказала: в документах
        # ответа нет — это тот же случай, что и пустой поиск.
        if is_not_found(content):
            fallback = await self._not_found(question, mode, reason="model_refusal")
            fallback.completions.insert(0, completion)
            return fallback

        return _Outcome(
            content=content,
            origin=AnswerOrigin.DOCUMENTS,
            sources=context,
            completions=[completion],
        )

    async def _not_found(
        self, question: str, mode: NotFoundMode, *, reason: str
    ) -> _Outcome:
        """В документах ответа нет: общий ответ или честный отказ."""
        if mode is NotFoundMode.STRICT:
            logger.info("faq_not_found_strict", reason=reason)
            return _Outcome(
                content=NOT_FOUND_ANSWER, origin=AnswerOrigin.NONE, sources=[]
            )
        return await self._general_answer(question, reason=reason)

    @staticmethod
    def _filtered(completion: Completion, *, reason: str) -> _Outcome:
        """Провайдер пометил ответ фильтром содержимого (BH-25).

        Это отказ, а не сбой: клиенту — фиксированный отказ без второго
        вызова (общий промпт на тот же вопрос отфильтруется так же), а не
        текст модели «Я не могу обсуждать…» — он без пометки и без
        источников, фронт показал бы его как ответ по документам. Строка
        в qa_log пишется как обычно: для отчёта о пробелах это промах.
        """
        logger.info("faq_content_filtered", stage=reason)
        return _Outcome(
            content=NOT_FOUND_ANSWER,
            origin=AnswerOrigin.NONE,
            sources=[],
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
        if completion.finish_reason is FinishReason.FILTERED:
            return self._filtered(completion, reason="general")
        logger.info("faq_general_answer", reason=reason)
        return _Outcome(
            content=ensure_general_prefix(completion.content),
            origin=AnswerOrigin.GENERAL_KNOWLEDGE,
            sources=[],
            completions=[completion],
        )
