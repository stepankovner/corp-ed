import asyncio
import re
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from typing import Protocol
from uuid import UUID, uuid4

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.dialogue_store import (
    DialogueKey,
    DialogueStore,
    DialogueStoreUnavailableError,
)
from corp_ed.core.exceptions import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from corp_ed.core.metrics import FAQ_ANSWERS, FAQ_DEGRADED
from corp_ed.domain.context import select_context
from corp_ed.domain.fulltext import to_fulltext_query
from corp_ed.domain.fusion import DEFAULT_RRF_K, rrf_merge
from corp_ed.domain.gaps import mask_pii
from corp_ed.domain.models import QaLog, User
from corp_ed.domain.query import expand_query
from corp_ed.domain.rerank import RERANK_MAX_WORDS, rerank_allowed
from corp_ed.domain.rerank import rerank as reorder
from corp_ed.domain.threshold import relevance_limit
from corp_ed.domain.tokens import count_tokens
from corp_ed.domain.types import (
    DEFAULT_NOT_FOUND_MODE,
    AnswerDiagnostics,
    AnswerOrigin,
    ChunkMatch,
    FaqAnswer,
    NotFoundMode,
    Retriever,
)
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.errors import LLMError
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.reranker import Reranker, RerankerError, rerank_passage
from corp_ed.llm.types import Completion, FinishReason, Message, Usage
from corp_ed.prompts.dialogue import (
    CONDENSE_PROMPT_VERSION,
    Turn,
    build_condense_messages,
    parse_condensed,
    recent_turns,
)
from corp_ed.prompts.faq import (
    GENERAL_ANSWER_PREFIX,
    NOT_FOUND_ANSWER,
    PROMPT_VERSION,
    SEARCH_PROMPT_VERSION,
    SEARCH_REQUEST_PREFIX,
    SMALL_TALK_PREFIX,
    SMALL_TALK_PROMPT_VERSION,
    SearchStep,
    build_faq_messages,
    build_general_messages,
    ensure_general_prefix,
    is_not_found,
    normalize_citations,
    search_request,
    split_general_answer,
    strip_search_request,
)
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.glossary_repository import GlossaryRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.credit_service import CreditService
from corp_ed.services.general_answer import (
    REFUSAL_ANSWER,
    GeneralAnswerSource,
    ModelKnowledgeSource,
    StreamingGeneralSource,
    finalize_general_answer,
)

logger = structlog.get_logger()

FUSION_CANDIDATES = 50
"""Глубина каждой ветки перед слиянием RRF — как в замерах ML
(eval/bench.py). Слияние топ-5 с топ-5 теряет чанки, которые ни одна
ветка не ставит в пятёрку, но обе держат высоко."""

CONDENSE_MAX_TOKENS = 100
"""Переписанный вопрос — одна строка (контракт BH-28)."""

SEARCH_MORE_LIMIT = 3
"""Второй шаг поиска (BH-43): сколько новых фрагментов добавить к прежним
— как в замере ML (eval/offline_e2e.py)."""

MAX_EXCERPTS = 8
"""Больше выдержек в модель не уходит и со вторым шагом (контракт BH-43)."""

SEARCH_MORE_STAGE = "searching_more"
"""Этап потока: модель попросила дополнительный поиск, ищем (BH-43)."""


@dataclass(frozen=True)
class _Reranked:
    matches: list[ChunkMatch]
    """Тот же пул кандидатов в новом порядке; у оценённых — rerank_score."""
    model: str | None
    """Модель, если порядок дал реранкер; None — порядок вектора."""
    ms: int | None
    failed: bool = False
    """Реранкер не ответил: порядок вектора."""


@dataclass(frozen=True)
class _Condensed:
    question: str
    """Самостоятельный вопрос: по нему поиск, порог и журнал пробелов."""
    completion: Completion | None
    """Вызов переписывания — его токены оплачиваются как обычные."""


@dataclass(frozen=True)
class _Retrieval:
    candidates: list[ChunkMatch]
    """top-limit в итоговом порядке, без порога."""
    relevant: list[ChunkMatch]
    """Прошедшие порог — только они могут попасть в промпт."""
    limit: float | None
    """До какого расстояния выдержки идут в модель (BH-37); None —
    ответа по документам нет."""
    nearest: float | None
    """Расстояние лучшего ВЕКТОРНОГО кандидата (qa_log, классы пробелов)."""
    best_fulltext: float | None
    """ts_rank_cd лучшего полнотекстового совпадения; None — пусто."""


@dataclass
class _SecondSearch:
    """Второй шаг поиска (BH-43): с чем искать и что попросила модель."""

    viewer: UUID
    attached: list[ChunkMatch]
    """Выдержки вложений: идут первыми, как и в первом вызове."""
    documents: list[ChunkMatch]
    """Выдержки документов первого вызова: новые встают после них."""
    query: str | None = None
    """Запрос модели — в журнал (search_query)."""


@dataclass
class _Outcome:
    content: str
    origin: AnswerOrigin
    sources: list[ChunkMatch]
    completions: list[Completion] = field(default_factory=list)
    stopped: bool = False
    """Сотрудник остановил ответ: content — то, что успело прийти."""


@dataclass(frozen=True)
class _Streamed:
    completion: Completion
    stopped: bool


class AnswerSink(Protocol):
    """Куда идёт ход ответа, пока он пишется (чат, ТЗ §6)."""

    async def stage(self, stage: str) -> None:
        """«searching» — ищем в документах, «writing» — модель пишет,
        «searching_more» — ищем в других документах по просьбе модели."""
        ...

    async def origin(self, origin: AnswerOrigin) -> None:
        """Ответ будет не по документам: общий или отказ (плашка сразу)."""
        ...

    async def delta(self, text: str) -> None:
        """Следующий кусок текста ответа."""
        ...

    async def reset(self) -> None:
        """Показанный текст не годится (фильтр, отказ модели) — убрать."""
        ...

    async def should_stop(self) -> bool:
        """Сотрудник нажал «Остановить»."""
        ...


class AttachmentSource(Protocol):
    """Выдержки вложений сотрудника к вопросу (ТЗ §6)."""

    async def select(self, embedding: list[float]) -> list[ChunkMatch]:
        """Что из вложений положить в промпт: эмбеддинг — вопроса."""
        ...


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
        general_source: GeneralAnswerSource | None = None,
        dialogue_store: DialogueStore | None = None,
        history_turns: int = 0,
        history_ttl_minutes: int = 720,
        condense_timeout: float = 5.0,
        reranker: Reranker | None = None,
        rerank_depth: int = 30,
        rerank_timeout: float = 3.0,
        rerank_max_words: int | None = RERANK_MAX_WORDS,
        gate_distance: float | None = None,
        near_margin: float = 0.0,
        search_step: bool = False,
        small_talk: bool = False,
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
        self.gate_distance = gate_distance
        self.near_margin = near_margin
        self.context_max_tokens = context_max_tokens
        self.temperature = temperature
        self.retriever = retriever
        self.fulltext_weight = fulltext_weight
        self.general_source = general_source or ModelKnowledgeSource(
            llm_gateway, temperature=temperature, small_talk=small_talk
        )
        self.dialogue_store = dialogue_store
        # Без хранилища истории нет — функция выключена целиком.
        self.history_turns = history_turns if dialogue_store is not None else 0
        self.history_ttl_seconds = history_ttl_minutes * 60
        self.condense_timeout = condense_timeout
        self.reranker = reranker
        self.rerank_depth = rerank_depth
        self.rerank_timeout = rerank_timeout
        self.rerank_max_words = rerank_max_words
        # Второй шаг поиска (BH-43): выключен — промпт faq-v2.7 байт в байт.
        self.search_step = search_step
        # «Не вопрос» в общем ответе (BH-45): выключен — промпт общего
        # ответа прежний байт в байт.
        self.small_talk = small_talk
        if small_talk:
            self.prompt_version = SMALL_TALK_PROMPT_VERSION
        elif search_step:
            self.prompt_version = SEARCH_PROMPT_VERSION
        else:
            self.prompt_version = PROMPT_VERSION

    async def answer(
        self, question: str, user: User, conversation_id: UUID | None = None
    ) -> FaqAnswer:
        """Ответ целиком, с памятью диалога в Redis (/faq/ask, BH-28).

        conversation_id — диалог, который клиент продолжает; без него
        начинается новый, id возвращается в ответе. Реплики живут в Redis
        (core/dialogue_store.py); чат приложения хранит диалоги в базе и
        передаёт историю сам (answer_turn, ChatService). Сбой хранилища
        реплик ответ не ломает — он идёт без истории.
        """
        dialogue = DialogueKey(
            tenant_id=user.tenant_id,
            user_id=user.id,
            conversation_id=conversation_id or uuid4(),
        )
        history = await self._history(dialogue, user) if conversation_id else []
        result = await self.answer_turn(
            question, user, history=history, conversation_id=dialogue.conversation_id
        )
        # После commit: реплика, которой нет в журнале, в историю не идёт.
        # Вопрос — как в журнале (после mask_pii), ответ — что видел
        # сотрудник; отказы — тоже реплики (контракт BH-28).
        await self._remember(
            dialogue,
            Turn(mask_pii(question), result.content),
            materials={match.material_id for match in result.sources},
        )
        return result

    async def answer_turn(
        self,
        question: str,
        user: User,
        *,
        history: list[Turn],
        conversation_id: UUID | None = None,
        sink: AnswerSink | None = None,
        attachments: AttachmentSource | None = None,
        commit: bool = True,
    ) -> FaqAnswer:
        """Ответить по документам; если в них ответа нет — по режиму компании.

        Что отвечать, когда в документах ответа нет, решает режим
        компании (NotFoundMode): по умолчанию общий ответ, всегда
        помеченный текстом в первой строке и полем origin (решение Артёма
        29.09, BH-29; services/general_answer.py), в режиме STRICT —
        честный отказ.

        history — прошлые реплики диалога (BH-28). Если они есть, вопрос
        сначала переписывается в самостоятельный (prompts/dialogue.py): по
        нему идут словарь, поиск, порог и общий ответ, а модель ответа
        видит и историю. Сбой переписывания ответ не ломает.

        Выдержки, не прошедшие порог max_distance, в модель не уходят:
        нерелевантный контекст дороже и толкает модель выдать чужой
        пункт за ответ. Как именно применяется порог — зависит от
        способа поиска (_retrieve).

        attachments — вложения сотрудника к диалогу (ТЗ §6): их выдержки
        идут первыми и вне порога — о файле спросили явно. В базу
        компании они не попадают и в source_chunk_ids журнала не пишутся.

        sink — ход ответа для потока в чате (ТЗ §6): этап, текст по мере
        генерации, остановка по просьбе сотрудника. Без него — ответ
        целиком (/faq/ask). Остановленный ответ — тоже ответ: текст до
        остановки, токены и кредиты пишутся как обычно.

        Каждый ответ пишется в qa_log (BH-20): версия промпта, модель,
        лучшее расстояние, токены и кредиты. До модели база только
        читается, короткими транзакциями: перед каждым внешним вызовом
        соединение возвращается в пул (_release). Запись журнала —
        одна транзакция после ответа; commit=False — её завершает
        вызывающий (ChatGenerator пишет сообщение диалога вместе с
        журналом).

        Пул кредитов проверяется первым: исчерпанный пул не должен
        стоить ни эмбеддинга, ни вызова модели (досье 10.2).
        """
        sink = sink or _SILENT
        usage = await self.credits.ensure_available(user)
        tenant = await self.tenant_repo.get_by_id(user.tenant_id)
        mode = NotFoundMode(tenant.not_found_mode) if tenant else DEFAULT_NOT_FOUND_MODE
        await sink.stage("searching")
        condensed = await self._condense(history, question)
        standalone = condensed.question
        search_text = await self._search_text(standalone)
        await self._release()
        embedded = await self.embedding_gateway.embed_query(search_text)

        found = await self._retrieve(
            search_text,
            embedded.embedding,
            limit=self._fetch_limit(self.limit),
            retriever=self.retriever,
            viewer=user.id,
        )
        reranked, chosen = await self._choose(search_text, found)
        # Порядок сохраняется: номер [n] в ответе модели — позиция выдержки
        # в context, и в том же порядке источники уходят клиенту.
        attached = await attachments.select(embedded.embedding) if attachments else []
        documents = select_context(chosen, max_tokens=self.context_max_tokens)
        context = attached + documents
        nearest = found.nearest
        second = (
            _SecondSearch(viewer=user.id, attached=attached, documents=documents)
            if self.search_step
            else None
        )

        await self._release()
        outcome = await self._answer(
            question,
            context,
            mode,
            history=history,
            standalone=standalone,
            sink=sink,
            second=second,
        )
        search_query = second.query if second else None

        # Переписывание — такой же вызов модели, его токены оплачиваются.
        completions = outcome.completions + (
            [condensed.completion] if condensed.completion else []
        )
        input_tokens = sum(c.usage.input_tokens for c in completions)
        output_tokens = sum(c.usage.output_tokens for c in completions)
        # Строгий отказ без выдержек не вызывал модель — и не стоит кредита.
        credits = self.credits.cost(input_tokens + output_tokens) if completions else 0
        # Модель ответа, а не переписывания: по ней метрики журнала.
        last = outcome.completions[-1] if outcome.completions else None
        model = last.model if last else None
        answer_given = outcome.origin is AnswerOrigin.DOCUMENTS
        attached_ids = {match.id for match in attached}

        entry = await self.qa_log_repo.add(
            QaLog(
                user_id=user.id,
                # Для отчёта о пробелах нужен текст, но не персональные
                # данные в нём (152-ФЗ): почта, телефоны, ФИО — маской.
                question=mask_pii(question),
                question_embedding=embedded.embedding,
                embedding_model=embedded.model,
                prompt_version=self.prompt_version,
                llm_model=model,
                llm_model_version=last.model_version if last else None,
                best_vector_distance=nearest,
                best_fulltext_score=found.best_fulltext,
                answer_given=answer_given,
                origin=outcome.origin.value,
                source_chunk_ids=[
                    source.id
                    for source in outcome.sources
                    if source.id not in attached_ids
                ],
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                credits=credits,
                conversation_id=conversation_id,
                standalone_question=mask_pii(standalone) if history else None,
                condense_prompt_version=CONDENSE_PROMPT_VERSION if history else None,
                history_turns=len(history),
                rerank_model=reranked.model,
                rerank_ms=reranked.ms,
                attachment_chunks=len(attached),
                search_query=mask_pii(search_query) if search_query else None,
            )
        )
        await self.credits.note_spend(usage, credits)
        if commit:
            await self.session.commit()
        else:
            await self.session.flush()
        FAQ_ANSWERS.labels(outcome.origin.value).inc()

        logger.info(
            "faq_answered",
            qa_log_id=str(entry.id),
            origin=outcome.origin.value,
            used=len(outcome.sources),
            nearest=nearest,
            prompt_version=self.prompt_version,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=sum(c.latency_ms for c in completions),
            history_turns=len(history),
            condensed=standalone != question,
            reranked=reranked.model is not None,
            rerank_ms=reranked.ms,
            attachment_chunks=len(attached),
            searched_more=search_query is not None,
            stopped=outcome.stopped,
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
                prompt_version=self.prompt_version,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                credits=credits,
                nearest_distance=nearest,
                standalone_question=standalone if history else None,
                history_turns=len(history),
                rerank_model=reranked.model,
                rerank_ms=reranked.ms,
            ),
            conversation_id=conversation_id,
            stopped=outcome.stopped,
        )

    async def search(
        self,
        question: str,
        limit: int,
        retriever: Retriever | None = None,
        *,
        viewer: User,
        rerank: bool = False,
    ) -> list[ChunkMatch]:
        """Отладка поиска для eval (BH-5): top-K без порога и без LLM.

        Порог здесь не применяется: для подбора порога (A8) нужны
        расстояния и у тех вопросов, которые его не прошли. retriever —
        сравнить способы поиска на живой базе, не меняя настройку.
        Права источников действуют и здесь: админ видит то, что видит
        сам, а не всё подряд.

        rerank — порядок, который дал бы ответ с реранкером (BH-32):
        прошедшие порог — по баллу rerank_score, за ними остальные в
        порядке вектора; первые limit. Сами кандидаты порогом не
        отсекаются, как и во всём /faq/search. Только с векторным поиском —
        как в замерах ML.
        """
        retriever = retriever or self.retriever
        if rerank and self.reranker is None:
            raise ConflictError("Реранкер выключен (RAG_RERANK_MODEL не задан)")
        if rerank and retriever is not Retriever.VECTOR:
            raise ConflictError("Реранкер работает только с векторным поиском")
        search_text = await self._search_text(question)
        await self._release()
        embedded = await self.embedding_gateway.embed_query(search_text)
        found = await self._retrieve(
            search_text,
            embedded.embedding,
            limit=self._fetch_limit(limit) if rerank else limit,
            retriever=retriever,
            viewer=viewer.id,
        )
        if not rerank or found.limit is None:
            return found.candidates[:limit]
        reranked = await self._rerank(
            search_text, found.candidates, max_distance=found.limit
        )
        if reranked.failed:
            raise ServiceUnavailableError()
        return reranked.matches[:limit]

    async def _choose(
        self, search_text: str, found: _Retrieval
    ) -> tuple[_Reranked, list[ChunkMatch]]:
        """Какие выдержки идут в модель: до limit прошедших порог, в порядке
        реранкера (M3, BH-32) или вектора.

        Реранкер переставляет прошедших порог; отвечать или нет —
        по-прежнему по вектору, и в модель идут только прошедшие. Выключен —
        первые limit по вектору. Пара для модели — текст, который ушёл в
        поиск (после переписывания и словаря).
        """
        if found.limit is None:
            # Ответа по документам нет — переставлять нечего, модель
            # реранкера не зовём.
            reranked = _Reranked(matches=found.candidates, model=None, ms=None)
        elif self.retriever is Retriever.VECTOR:
            # Порог — этого вопроса (BH-37): в зоне (max; gate] пул не пуст.
            reranked = await self._rerank(
                search_text, found.candidates, max_distance=found.limit
            )
        else:
            # HYBRID: порог решён целиком по лучшему вектору (_retrieve).
            reranked = await self._rerank(
                search_text, found.relevant, max_distance=None
            )
        relevant = {match.id for match in found.relevant}
        chosen = [m for m in reranked.matches if m.id in relevant][: self.limit]
        return reranked, chosen

    async def _search_more(
        self, second: _SecondSearch, query: str
    ) -> list[ChunkMatch] | None:
        """Второй шаг поиска по запросу модели (BH-43, faq-v2.9).

        Тот же путь, что у вопроса: словарь, эмбеддинг, поиск с правами
        сотрудника, порог BH-37 по ближайшему для запроса, реранкер по
        правилам BH-32 / BH-40. Новые фрагменты (которых не было в первом
        вызове) — до SEARCH_MORE_LIMIT, после прежних: номера [1]…[k]
        прежних не меняются. Бюджет токенов — тот же, всего выдержек — не
        больше MAX_EXCERPTS.

        Возвращает выдержки второго вызова целиком; None — нового не нашлось
        (или не влезло): ответа по документам нет.
        """
        search_text = await self._search_text(query)
        await self._release()
        embedded = await self.embedding_gateway.embed_query(search_text)
        found = await self._retrieve(
            search_text,
            embedded.embedding,
            limit=self._fetch_limit(self.limit),
            retriever=self.retriever,
            viewer=second.viewer,
        )
        _, chosen = await self._choose(search_text, found)
        await self._release()
        seen = {match.id for match in second.attached + second.documents}
        room = MAX_EXCERPTS - len(seen)
        fresh = [m for m in chosen if m.id not in seen][
            : max(0, min(SEARCH_MORE_LIMIT, room))
        ]
        # Прежние уже уложились в бюджет и остаются на своих местах; из
        # новых — сколько влезет.
        documents = select_context(
            second.documents + fresh, max_tokens=self.context_max_tokens
        )
        if len(documents) == len(second.documents):
            return None
        return second.attached + documents

    async def _release(self) -> None:
        """Вернуть соединение в пул перед внешним вызовом: модель,
        эмбеддинги, реранкер.

        Ответ ждёт квоту модели и пишется секунды; всё это время
        открытая транзакция чтения держала соединение. Сотня вопросов
        разом — и пул (5 + 10 на процесс) кончался: остальные запросы
        API ждали соединение 30 с и падали (docs/LOAD-TEST.md). Здесь
        только чтение, закрыть его — commit; следующий запрос начнёт
        новую транзакцию с тем же тенантом (core/database.py), объекты
        не устаревают (expire_on_commit=False). Держать транзакцию через
        вызов модели не хотели и в кредитах (CreditService.ensure_available).
        """
        if self.session.in_transaction():
            await self.session.commit()

    def _fetch_limit(self, limit: int) -> int:
        """Сколько кандидатов брать у поиска: с реранкером — глубину для
        пересортировки, без него — ровно limit."""
        return max(limit, self.rerank_depth) if self.reranker else limit

    async def _rerank(
        self, query: str, pool: list[ChunkMatch], *, max_distance: float | None
    ) -> _Reranked:
        """Пул кандидатов в порядке реранкера (BH-32); без него — как есть.

        Правило — domain.rerank.rerank, одно на стенд ML и продукт: модель
        оценивает первых rerank_depth кандидатов, прошедших max_distance;
        они идут первыми по убыванию балла, остальные — следом в прежнем
        порядке. Пары — «вопрос — embed_text» (крошки и текст, как мерил
        ML). Оценивать нечего (меньше двух прошедших) — модель не зовём.

        Сбой, таймаут (RAG_RERANK_TIMEOUT_MS) или ответ не по контракту —
        не сбой ответа: порядок вектора, событие в лог и метрику, в
        журнале rerank_model пуст.

        Вопрос длиннее rerank_max_words слов (BH-40) — модель не зовём,
        порядок вектора, как с выключенным реранкером: на длинных
        «разговорных» вопросах она выталкивает нужный фрагмент. query —
        search_text, тот же текст, что ушёл бы в пару.
        """
        if self.reranker is None or not rerank_allowed(query, self.rerank_max_words):
            return _Reranked(matches=pool, model=None, ms=None)
        by_id = {match.id: match for match in pool}
        ranking = [match.id for match in pool]
        distance_of = {match.id: match.distance for match in pool}

        # reorder (domain.rerank.rerank) ждёт синхронную функцию баллов, а
        # модель — HTTP-вызов. Первый проход только узнаёт, кого правило
        # отдаёт модели, второй переставляет по полученным баллам: отбор
        # целиком в правиле ML.
        asked: list[UUID] = []

        def remember(ids: list[UUID]) -> list[float]:
            asked.extend(ids)
            return [0.0] * len(ids)

        reorder(
            ranking,
            distance_of,
            remember,
            depth=self.rerank_depth,
            max_distance=max_distance,
        )
        if len(asked) <= 1:
            return _Reranked(matches=pool, model=None, ms=None)

        await self._release()
        started = time.perf_counter()
        try:
            scores = await asyncio.wait_for(
                self.reranker.score(query, [rerank_passage(by_id[i]) for i in asked]),
                timeout=self.rerank_timeout,
            )
        except (RerankerError, TimeoutError) as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "faq_rerank_failed", error=type(exc).__name__, rerank_ms=elapsed
            )
            FAQ_DEGRADED.labels("rerank").inc()
            return _Reranked(matches=pool, model=None, ms=elapsed, failed=True)
        elapsed = int((time.perf_counter() - started) * 1000)
        score_of = dict(zip(asked, scores, strict=True))
        ordered = reorder(
            ranking,
            distance_of,
            lambda ids: [score_of[i] for i in ids],
            depth=self.rerank_depth,
            max_distance=max_distance,
        )
        return _Reranked(
            matches=[replace(by_id[i], rerank_score=score_of.get(i)) for i in ordered],
            model=self.reranker.model,
            ms=elapsed,
        )

    async def _history(self, dialogue: DialogueKey, user: User) -> list[Turn]:
        """Последние history_turns реплик диалога; сбой — без истории.

        Реплики по документам, к которым у сотрудника больше нет доступа
        (ушёл из отдела, папку закрыли), отбрасываются: снятый доступ не
        возвращается в модель через историю."""
        if self.history_turns <= 0 or self.dialogue_store is None:
            return []
        try:
            remembered = await self.dialogue_store.load_remembered(dialogue)
        except DialogueStoreUnavailableError:
            logger.warning("faq_dialogue_store_unavailable", stage="load")
            FAQ_DEGRADED.labels("dialogue_store").inc()
            return []
        closed = await self.chunk_repo.closed_material_ids(
            {m for item in remembered for m in item.materials}, viewer=user.id
        )
        turns = [item.turn for item in remembered if not item.materials & closed]
        return recent_turns(turns, self.history_turns)

    async def _remember(
        self, dialogue: DialogueKey, turn: Turn, *, materials: set[UUID]
    ) -> None:
        if self.history_turns <= 0 or self.dialogue_store is None:
            return
        try:
            await self.dialogue_store.append(
                dialogue,
                turn,
                keep=self.history_turns,
                ttl_seconds=self.history_ttl_seconds,
                materials=materials,
            )
        except DialogueStoreUnavailableError:
            logger.warning("faq_dialogue_store_unavailable", stage="append")
            FAQ_DEGRADED.labels("dialogue_store").inc()

    async def _condense(self, history: list[Turn], question: str) -> _Condensed:
        """Уточняющий вопрос → самостоятельный (BH-28, prompts/dialogue.py).

        Без истории вызова нет. Температура 0, короткий ответ, свой
        таймаут: переписывание не должно стоить сотруднику ответа. Сбой,
        таймаут или фильтр содержимого — ищем по исходному вопросу.
        """
        if not history:
            return _Condensed(question=question, completion=None)
        await self._release()
        try:
            completion = await asyncio.wait_for(
                self.llm_gateway.generate(
                    messages=build_condense_messages(
                        history, question, max_turns=self.history_turns
                    ),
                    temperature=0.0,
                    max_tokens=CONDENSE_MAX_TOKENS,
                ),
                timeout=self.condense_timeout,
            )
        except (LLMError, TimeoutError) as exc:
            logger.warning("faq_condense_failed", error=type(exc).__name__)
            FAQ_DEGRADED.labels("condense").inc()
            return _Condensed(question=question, completion=None)
        if completion.finish_reason is FinishReason.FILTERED:
            logger.info("faq_condense_filtered")
            return _Condensed(question=question, completion=completion)
        return _Condensed(
            question=parse_condensed(completion.content, question),
            completion=completion,
        )

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
        viewer: UUID,
    ) -> _Retrieval:
        """Найти выдержки и решить, какие из них проходят порог.

        viewer — сотрудник, чьи права на документы применяются в поиске
        (materials.visibility и material_access): чужие документы не
        попадают ни в промпт, ни в источники, ни в отладку.

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
            vector = await self.chunk_repo.search(
                embedding=embedding, limit=limit, viewer=viewer
            )
            fulltext = (
                await self.chunk_repo.search_fulltext(
                    query, embedding, limit=1, viewer=viewer
                )
                if query
                else []
            )
            nearest = vector[0].distance if vector else None
            cutoff = self._relevance_limit(nearest)
            return _Retrieval(
                candidates=vector,
                relevant=[
                    m for m in vector if cutoff is not None and m.distance <= cutoff
                ],
                limit=cutoff,
                nearest=nearest,
                best_fulltext=fulltext[0].fulltext_rank if fulltext else None,
            )

        depth = max(limit, FUSION_CANDIDATES)
        vector = await self.chunk_repo.search(
            embedding=embedding, limit=depth, viewer=viewer
        )
        # Пустая строка (вопрос из одних знаков) — ветку не вызывать.
        fulltext = (
            await self.chunk_repo.search_fulltext(
                query, embedding, limit=depth, viewer=viewer
            )
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
        cutoff = self._relevance_limit(nearest)
        return _Retrieval(
            candidates=candidates,
            relevant=candidates if cutoff is not None else [],
            limit=cutoff,
            nearest=nearest,
            best_fulltext=fulltext[0].fulltext_rank if fulltext else None,
        )

    def _relevance_limit(self, nearest: float | None) -> float | None:
        """Порог выдержек этого вопроса — правило ML (BH-37): без
        gate_distance — max_distance, если ближайший его прошёл."""
        return relevance_limit(
            nearest, self.max_distance, self.gate_distance, self.near_margin
        )

    async def rate(self, user: User, log_id: UUID, feedback: int) -> None:
        """👍/👎 к своему ответу. Чужой ответ — 404, как несуществующий."""
        entry = await self.qa_log_repo.get_by_id(log_id)
        if entry is None or entry.user_id != user.id:
            raise NotFoundError("Ответ не найден")
        entry.feedback = feedback
        await self.session.commit()

    async def _answer(
        self,
        question: str,
        context: list[ChunkMatch],
        mode: NotFoundMode,
        *,
        history: list[Turn],
        standalone: str,
        sink: AnswerSink,
        second: _SecondSearch | None = None,
    ) -> _Outcome:
        """Ответ по выдержкам или, если в них ответа нет, по режиму.

        Модель ответа видит вопрос сотрудника как есть, историю и
        переписанный вопрос (build_faq_messages); без истории промпт байт
        в байт прежний. Прошлые ответы «в документах ответа нет» в историю
        для модели ответа не идут: фактов в них нет, а модель, видя свой
        отказ, повторяла его, хотя выдержка уже нашлась — отдел подтвердили
        или файл загрузили (владелец 06.10). Остались одни отказы — модель
        получает переписанный вопрос. Общий ответ строится по переписанному
        вопросу: «А для УМНИК?» без контекста общему источнику непонятен.

        В поток отказ модели не попадает: _RefusalGate придерживает
        начало ответа, пока оно может оказаться фразой NOT_FOUND_ANSWER,
        — дальше по режиму пойдёт общий ответ или отказ.

        second — второй шаг поиска (BH-43, faq-v2.9); None — выключен, промпт
        faq-v2.7. Модель может ответить строкой «Дополнительный поиск: …»:
        её клиент не видит (_SearchGate), сервис ищет по запросу
        (_search_more) и спрашивает снова с новыми выдержками после прежних.
        Один раз на вопрос: нового не нашлось или модель снова просит поиск
        — ответа по документам нет, дальше как при отказе модели.
        """
        if not context:
            return await self._not_found(
                standalone, mode, reason="no_relevant_excerpts", sink=sink
            )

        answered = [turn for turn in history if not is_not_found(turn.answer)]

        def messages_for(matches: list[ChunkMatch], step: SearchStep) -> list[Message]:
            return build_faq_messages(
                question=question if answered or not history else standalone,
                matches=matches,
                history=answered,
                standalone_question=standalone,
                search=step,
            )

        step: SearchStep = "allowed" if second else "off"
        await sink.stage("writing")
        streamed = await self._ask(messages_for(context, step), sink=sink, step=step)
        completions = [streamed.completion]

        if second and _answered_with_search(streamed):
            query = search_request(streamed.completion.content)
            second.query = query
            await sink.stage(SEARCH_MORE_STAGE)
            extended = await self._search_more(second, query) if query else None
            if extended is None:
                logger.info("faq_search_more_empty", asked=query is not None)
                return await self._fallback(
                    standalone,
                    mode,
                    completions,
                    reason="search_nothing_new",
                    sink=sink,
                )
            context = extended
            await sink.stage("writing")
            streamed = await self._ask(
                messages_for(context, "done"), sink=sink, step="done"
            )
            completions.append(streamed.completion)
            if _answered_with_search(streamed):
                # Второй раз не ищем: ответа по документам нет.
                return await self._fallback(
                    standalone, mode, completions, reason="search_repeated", sink=sink
                )

        completion = streamed.completion
        if streamed.stopped:
            # Строку запроса (и её начало) сотрудник не видел — и в ответ
            # она не идёт.
            content = completion.content
            if second and _held_back(content):
                content = ""
            elif second:
                content = strip_search_request(content)
            return _Outcome(
                content=normalize_citations(content, context),
                origin=AnswerOrigin.DOCUMENTS,
                sources=context,
                completions=completions,
                stopped=True,
            )
        if completion.finish_reason is FinishReason.FILTERED:
            await sink.reset()
            outcome = self._filtered(completion, reason="documents")
            outcome.completions = completions
            return outcome
        # Ответила и всё равно дописала запрос (BH-44): строку срезаем, второй
        # поиск по ней не делаем — ответ сотрудник уже видит.
        raw = strip_search_request(completion.content) if second else completion.content
        # Модели иногда ставят в скобки номер пункта документа [4.2]
        # вместо номера выдержки: фронт такую ссылку не свяжет.
        content = normalize_citations(raw, context)

        # Выдержки нашлись, но модель по ним отказала: в документах
        # ответа нет — это тот же случай, что и пустой поиск.
        if is_not_found(content):
            return await self._fallback(
                standalone, mode, completions, reason="model_refusal", sink=sink
            )

        return _Outcome(
            content=content,
            origin=AnswerOrigin.DOCUMENTS,
            sources=context,
            completions=completions,
        )

    async def _ask(
        self, messages: list[Message], *, sink: AnswerSink, step: SearchStep
    ) -> _Streamed:
        """Вызов модели ответа по выдержкам с потоком в sink."""
        gate: _Gate = _RefusalGate()
        if step != "off":
            # faq-v2.9: строка «Дополнительный поиск: …» клиенту не видна.
            gate = _SearchGate(gate)
        return await self._consume(
            self.llm_gateway.stream(messages, temperature=self.temperature),
            messages,
            sink=sink,
            gate=gate,
        )

    async def _fallback(
        self,
        question: str,
        mode: NotFoundMode,
        completions: list[Completion],
        *,
        reason: str,
        sink: AnswerSink,
    ) -> _Outcome:
        """По выдержкам ответа нет, хотя модель их видела: показанное
        убрать, дальше — по режиму; вызовы модели по выдержкам оплачиваются."""
        await sink.reset()
        fallback = await self._not_found(question, mode, reason=reason, sink=sink)
        fallback.completions[:0] = completions
        return fallback

    async def _not_found(
        self, question: str, mode: NotFoundMode, *, reason: str, sink: AnswerSink
    ) -> _Outcome:
        """В документах ответа нет: общий ответ или честный отказ."""
        if mode is NotFoundMode.STRICT:
            logger.info("faq_not_found_strict", reason=reason)
            await sink.origin(AnswerOrigin.NONE)
            return _Outcome(
                content=REFUSAL_ANSWER, origin=AnswerOrigin.NONE, sources=[]
            )
        return await self._general_answer(question, reason=reason, sink=sink)

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
            content=REFUSAL_ANSWER,
            origin=AnswerOrigin.NONE,
            sources=[],
            completions=[completion],
        )

    async def _general_answer(
        self, question: str, *, reason: str, sink: AnswerSink
    ) -> _Outcome:
        """Общий ответ со строгой пометкой и советом уточнить.

        Источнику не уходит ни одной выдержки: смешать общие сведения с
        документами компании он не может. finalize_general_answer ставит
        пометку и совет, даже если модель их потеряла или переписала, —
        ответ без пометки клиенту уйти не может. Источников нет.

        В поток пометка не идёт: фронт узнаёт об общем ответе из события
        origin и показывает плашку; итоговый текст — с пометкой.

        С правилом «не вопрос» (BH-45) — _general_or_small_talk.
        """
        if self.small_talk:
            return await self._general_or_small_talk(question, reason=reason, sink=sink)
        await sink.origin(AnswerOrigin.GENERAL_KNOWLEDGE)
        if isinstance(self.general_source, StreamingGeneralSource):
            source_stream = self.general_source.stream(question)
        else:
            source_stream = _single(await self.general_source.generate(question))
        streamed = await self._consume(
            source_stream,
            build_general_messages(question),
            sink=sink,
            gate=_GeneralPrefixGate(),
        )
        completion = streamed.completion
        if streamed.stopped:
            return _Outcome(
                content=ensure_general_prefix(completion.content),
                origin=AnswerOrigin.GENERAL_KNOWLEDGE,
                sources=[],
                completions=[completion],
                stopped=True,
            )
        if completion.finish_reason is FinishReason.FILTERED:
            await sink.reset()
            return self._filtered(completion, reason="general")
        logger.info(
            "faq_general_answer", reason=reason, source=self.general_source.name
        )
        return _Outcome(
            content=finalize_general_answer(completion.content),
            origin=AnswerOrigin.GENERAL_KNOWLEDGE,
            sources=[],
            completions=[completion],
        )

    async def _general_or_small_talk(
        self, question: str, *, reason: str, sink: AnswerSink
    ) -> _Outcome:
        """Общий ответ с правилом «не вопрос» (BH-45, промпт faq-v2.10).

        На приветствие, благодарность или вопрос об ассистенте модель пишет
        первой строкой SMALL_TALK_PREFIX и короткий дружелюбный ответ —
        его показываем без пометки «ответа нет» и без совета уточнить,
        origin SMALL_TALK, в отчёт о пробелах он не идёт. Иначе — общий
        ответ, как раньше.

        В потоке плашку нельзя показать сразу: что это — решает первая
        строка (_SmallTalkGate); origin уходит перед первым показанным
        куском. Итог — по split_general_answer (код ML): если модель
        написала обе части, это вопрос, и показанное заменяет общий ответ.
        """
        if isinstance(self.general_source, StreamingGeneralSource):
            source_stream = self.general_source.stream(question)
        else:
            source_stream = _single(await self.general_source.generate(question))
        gate = _SmallTalkGate()
        deferred = _OriginOnDecision(sink, gate)
        streamed = await self._consume(
            source_stream,
            build_general_messages(question, small_talk=True),
            sink=deferred,
            gate=gate,
        )
        completion = streamed.completion
        if completion.finish_reason is FinishReason.FILTERED and not streamed.stopped:
            await sink.reset()
            return self._filtered(completion, reason="general")
        small_talk, text = split_general_answer(completion.content)
        origin = (
            AnswerOrigin.SMALL_TALK if small_talk else AnswerOrigin.GENERAL_KNOWLEDGE
        )
        await deferred.settle(origin)
        if streamed.stopped:
            return _Outcome(
                content=text,
                origin=origin,
                sources=[],
                completions=[completion],
                stopped=True,
            )
        logger.info(
            "faq_small_talk" if small_talk else "faq_general_answer",
            reason=reason,
            source=self.general_source.name,
        )
        return _Outcome(
            content=text if small_talk else finalize_general_answer(text),
            origin=origin,
            sources=[],
            completions=[completion],
        )

    async def _consume(
        self,
        stream: AsyncGenerator[str | Completion, None],
        messages: list[Message],
        *,
        sink: AnswerSink,
        gate: "_Gate",
    ) -> _Streamed:
        """Прочитать поток модели: текст — в sink через gate, остановка —
        по просьбе сотрудника (sink.should_stop) перед каждым куском.

        Остановленный поток закрывается (адаптер рвёт соединение), а
        ответ собирается из того, что успело прийти: токены — по оценке
        count_tokens, провайдер до конца не дошёл и расхода не назвал.
        """
        started = time.perf_counter()
        text = ""
        async with aclosing(stream) as pieces:
            async for piece in pieces:
                if isinstance(piece, Completion):
                    return _Streamed(completion=piece, stopped=False)
                # Проверка до показа: что пришло после «Остановить», сотрудник
                # уже не видит — и в ответ это не идёт.
                if await sink.should_stop():
                    break
                text += piece
                visible = gate.feed(piece)
                if visible:
                    await sink.delta(visible)
            else:
                raise LLMError("stream ended without completion", retryable=False)
        return _Streamed(
            completion=Completion(
                content=text.strip(),
                finish_reason=FinishReason.TRUNCATED,
                usage=Usage(
                    input_tokens=sum(count_tokens(m.content) for m in messages),
                    output_tokens=count_tokens(text),
                ),
                model_version=self.llm_gateway.model_name,
                model=self.llm_gateway.model_name,
                latency_ms=int((time.perf_counter() - started) * 1000),
            ),
            stopped=True,
        )


class _Gate(Protocol):
    def feed(self, piece: str) -> str:
        """Что из куска показать сотруднику сейчас."""
        ...


_REFUSAL_WORDS = " ".join(NOT_FOUND_ANSWER.lower().rstrip(".").split())


class _RefusalGate:
    """Не показывать отказ модели кусками.

    Пока начало ответа может оказаться фразой «В документах компании
    ответа нет» — текст придерживается: дальше будет общий ответ или
    плашка отказа, а не мелькнувшая фраза. Ответ пошёл другой — всё
    придержанное уходит разом, дальше куски идут как есть.
    """

    def __init__(self) -> None:
        self._held = ""
        self._open = False

    def feed(self, piece: str) -> str:
        if self._open:
            return piece
        self._held += piece
        probe = " ".join(self._held.lstrip(" \t\n«»\"'*").lower().split())
        if _REFUSAL_WORDS.startswith(probe) or probe.startswith(_REFUSAL_WORDS):
            return ""
        self._open = True
        held, self._held = self._held, ""
        return held


# Начало строки запроса второго шага — как в prompts.faq.search_request:
# регистр, кавычки и пробелы не важны.
_SEARCH_REQUEST_START = re.compile(
    r"^[\s«\"*]*дополнительный\s+поиск\s*:", re.IGNORECASE
)
_SEARCH_WORDS = " ".join(SEARCH_REQUEST_PREFIX.lower().split())


def _asks_search(answer: str) -> bool:
    """Ответ — просьба о втором шаге поиска (faq-v2.9), даже с пустым
    запросом: search_request тогда None, а ответа по документам нет."""
    return _SEARCH_REQUEST_START.match(answer) is not None


# Строка запроса в любом месте ответа (BH-44) — как в
# prompts.faq.strip_search_request: поток режется там же, где итоговый текст.
_SEARCH_REQUEST_ANYWHERE = re.compile(
    r"[ \t]*\n?[ \t«\"*]*дополнительный\s+поиск\s*:", re.IGNORECASE
)
_SEARCH_HOLD_CHARS = len(SEARCH_REQUEST_PREFIX) + 20
"""Хвост ответа, который может оказаться началом строки запроса, держим
не дольше длины фразы (с запасом на пробелы, кавычки и `**`)."""


def _held_back(text: str) -> bool:
    """Начало ответа — строка запроса или может ещё ею оказаться."""
    probe = " ".join(text.lstrip(' \t\n«"*').lower().split())
    return _asks_search(text) or _SEARCH_WORDS.startswith(probe)


def _answered_with_search(streamed: _Streamed) -> bool:
    return (
        not streamed.stopped
        and streamed.completion.finish_reason is not FinishReason.FILTERED
        and _asks_search(streamed.completion.content)
    )


class _SearchGate:
    """Не показывать строку «Дополнительный поиск: …».

    Модель пишет её первой строкой вместо ответа (BH-43). Пока начало
    ответа может оказаться ею — текст придерживается (это не дольше первых
    ~25 символов, контракт BH-43); оказалось — не показывается ничего:
    сервис ищет и спрашивает модель снова. Нет — придержанное и всё дальше
    идёт в следующий фильтр (then, отказ модели).

    Бывает, что модель отвечает и в конце всё равно дописывает запрос
    (BH-44). Поэтому и дальше хвост, который может оказаться началом
    строки запроса, придерживается; дописалась строка — до конца ответа
    ничего не показываем. Итоговый текст режет strip_search_request.
    """

    def __init__(self, then: "_Gate") -> None:
        self._then = then
        self._held = ""
        self._open = False
        self._swallow = False

    def feed(self, piece: str) -> str:
        if self._swallow:
            return ""
        self._held += piece
        if not self._open:
            if _asks_search(self._held):
                self._swallow = True
                return ""
            if _held_back(self._held):
                return ""
            self._open = True
        return self._then.feed(self._release())

    def _release(self) -> str:
        """Что из придержанного можно показать: всё до строки запроса или
        до хвоста, который ещё может ею оказаться."""
        text = self._held
        found = _SEARCH_REQUEST_ANYWHERE.search(text)
        if found:
            self._swallow = True
            self._held = ""
            return text[: found.start()]
        cut = len(text)
        for start in range(max(0, len(text) - _SEARCH_HOLD_CHARS), len(text)):
            if not text[start].isspace() and _may_start_request(text[start:]):
                cut = start
                break
        self._held = text[cut:]
        return text[:cut]


def _may_start_request(tail: str) -> bool:
    """Хвост ответа может оказаться началом «Дополнительный поиск:»."""
    probe = " ".join(tail.lstrip(' \t\n«"*').lower().split())
    return _SEARCH_WORDS.startswith(probe)


_GENERAL_HOLD_CHARS = len(GENERAL_ANSWER_PREFIX) + 40


class _GeneralPrefixGate:
    """Общий ответ — без служебной пометки в начале.

    Модель пишет пометку сама (промпт ML), в одну строку или в две.
    Начало придерживается, пока пометка точно не дописана, затем
    ensure_general_prefix отделяет её, и наружу идёт только ответ.
    """

    def __init__(self) -> None:
        self._held = ""
        self._open = False

    def feed(self, piece: str) -> str:
        if self._open:
            return piece
        self._held += piece
        if len(self._held) < _GENERAL_HOLD_CHARS:
            return ""
        self._open = True
        trailing = self._held[len(self._held.rstrip()) :]
        marked = ensure_general_prefix(self._held)
        return marked[len(GENERAL_ANSWER_PREFIX) :].lstrip("\n") + trailing


# Служебная первая строка «не вопроса» — как в prompts.faq.small_talk_reply:
# кавычки, звёздочки, регистр и знак в конце не важны.
_SMALL_TALK_LINE = re.compile(
    r"^[\s«»\"'*]*реплика\s+без\s+вопроса[\s.!:»\"'*]*", re.IGNORECASE
)
_SMALL_TALK_WORDS = " ".join(SMALL_TALK_PREFIX.lower().rstrip(".").split())


class _SmallTalkGate:
    """Общий ответ с правилом «не вопрос» (BH-45): решить по первой строке.

    Пока начало может оказаться служебной строкой SMALL_TALK_PREFIX — текст
    придерживается (не дольше её длины). Оказалось — строку не показываем,
    origin SMALL_TALK, дальше поток как есть. Нет — общий ответ: дальше
    _GeneralPrefixGate (его на «не вопрос» звать нельзя — он срезает
    пометку по длине). Решение — в origin, его отправит _OriginOnDecision.
    """

    def __init__(self) -> None:
        self._held = ""
        self.origin: AnswerOrigin | None = None
        self._general = _GeneralPrefixGate()

    def feed(self, piece: str) -> str:
        if self.origin is AnswerOrigin.SMALL_TALK:
            return piece
        if self.origin is AnswerOrigin.GENERAL_KNOWLEDGE:
            return self._general.feed(piece)
        self._held += piece
        line = _SMALL_TALK_LINE.match(self._held)
        if line:
            if line.end() == len(self._held):
                # Строка дописана, но за ней могут быть ещё знаки и пробелы.
                return ""
            self.origin = AnswerOrigin.SMALL_TALK
            return self._held[line.end() :]
        probe = " ".join(self._held.lstrip(" \t\n«»\"'*").lower().split())
        if _SMALL_TALK_WORDS.startswith(probe):
            return ""
        self.origin = AnswerOrigin.GENERAL_KNOWLEDGE
        return self._general.feed(self._held)


class _OriginOnDecision:
    """Поток общего ответа с правилом «не вопрос» (BH-45): событие origin —
    когда _SmallTalkGate решил, перед первым показанным куском."""

    def __init__(self, sink: AnswerSink, gate: _SmallTalkGate) -> None:
        self._sink = sink
        self._gate = gate
        self.sent: AnswerOrigin | None = None

    async def stage(self, stage: str) -> None:
        await self._sink.stage(stage)

    async def origin(self, origin: AnswerOrigin) -> None:
        await self._sink.origin(origin)
        self.sent = origin

    async def delta(self, text: str) -> None:
        if self.sent is None and self._gate.origin is not None:
            await self.origin(self._gate.origin)
        await self._sink.delta(text)

    async def reset(self) -> None:
        await self._sink.reset()

    async def should_stop(self) -> bool:
        return await self._sink.should_stop()

    async def settle(self, origin: AnswerOrigin) -> None:
        """Итоговый origin: поток мог закончиться до решения, а итог —
        разойтись с первой строкой (модель написала обе части)."""
        if self.sent is origin:
            return
        if self.sent is not None:
            await self._sink.reset()
        await self.origin(origin)


async def _single(completion: Completion) -> AsyncGenerator[str | Completion, None]:
    if completion.content:
        yield completion.content
    yield completion


class _SilentSink:
    """Ответ без потока (/faq/ask): события никуда не идут."""

    async def stage(self, stage: str) -> None:
        return None

    async def origin(self, origin: AnswerOrigin) -> None:
        return None

    async def delta(self, text: str) -> None:
        return None

    async def reset(self) -> None:
        return None

    async def should_stop(self) -> bool:
        return False


_SILENT = _SilentSink()
