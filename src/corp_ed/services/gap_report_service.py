"""Ночной отчёт о пробелах (BH-21): python -m corp_ed.cli gaps --all.

По каждой компании отдельно, в её tenant_scope:
1. classify_miss (ML) по всем вопросам окна → qa_log.miss_kind;
2. вопросы классов gap и model_refusal одной модели эмбеддингов →
   cluster_questions (ML) по уже сохранённым векторам;
3. новые кластеры сопоставляются со вчерашними по общим вопросам: id и
   статус, который поставил админ, переживают пересборку;
4. новые кластеры (и кластеры со старой версией промпта) подписывает
   модель — промпт gaps-v1, вопросы после mask_pii;
5. запись одной транзакцией: кластеры, их состав, удаление исчезнувших.

Модель вызывается вне транзакции: база не ждёт секунды ответа LLM.
Векторы разных компаний не попадают в одну матрицу — кластеризация
идёт внутри цикла по компаниям.
"""

import asyncio
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.config import GapsSettings
from corp_ed.core.exceptions import ConflictError
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.gaps import (
    ClusterQuestion,
    GapThresholds,
    MissKind,
    MissSignals,
    classify_miss,
    cluster_priority,
    cluster_questions,
)
from corp_ed.domain.models import GapCluster, Tenant
from corp_ed.llm.errors import LLMError
from corp_ed.llm.gateway import LLMGateway
from corp_ed.prompts.gaps import (
    GAP_LABEL_SCHEMA,
    MAX_QUESTIONS,
    PROMPT_VERSION,
    build_gap_messages,
    parse_gap_label,
)
from corp_ed.repositories.gap_repository import GapRepository
from corp_ed.repositories.qa_log_repository import CandidateRow, QaLogRepository
from corp_ed.repositories.tenant_repository import TenantRepository

logger = structlog.get_logger()

REPORTED_KINDS = (MissKind.GAP, MissKind.MODEL_REFUSAL)
"""Клиенту — только «в базе нет» и «модель отказала при найденных
выдержках» (BH-23). retrieval_miss, unclear, off_topic — внутренний
мониторинг качества, в кластеры не идут."""

MAX_TITLE_LENGTH = 200
MAX_MISSING_LENGTH = 1000
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# Ключ блокировки «отчёт уже строится»: два запуска cron параллельно
# потратили бы LLM дважды и писали бы одни кластеры наперегонки.
_LOCK_KEY = 0x6A7053  # "gap" + S


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Existing:
    """Вчерашний кластер — данные, без привязки к сессии."""

    id: UUID
    members: frozenset[UUID]
    title: str
    missing: str
    prompt_version: str
    embedding_model: str


@dataclass
class _Draft:
    """Кластер этой ночи до записи в базу."""

    members: list[CandidateRow]
    """Вопросы кластера, новые первыми."""
    priority: float
    match: _Existing | None = None
    title: str = ""
    missing: str = ""
    prompt_version: str = ""
    ids: frozenset[UUID] = field(init=False)

    def __post_init__(self) -> None:
        self.ids = frozenset(row.id for row in self.members)


@dataclass(frozen=True)
class GapRunReport:
    company_code: str
    window: int
    """Вопросов в окне (все классы)."""
    candidates: int
    """Вопросов gap + model_refusal, пошедших в кластеризацию."""
    clusters: int
    labeled: int
    """Подписано моделью в этот запуск."""
    failed: bool = False


class GapReportService:
    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        llm_gateway: LLMGateway,
        settings: GapsSettings,
        *,
        max_distance: float,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.session_maker = session_maker
        self.llm = llm_gateway
        self.settings = settings
        self.now = now
        # Пороги полнотекста не заданы — полнотекст в классификации не
        # участвует: и «сильного», и «пустого» совпадения не бывает.
        self.thresholds = GapThresholds(
            max_distance=max_distance,
            strong_fulltext=_or_inf(settings.strong_fulltext),
            empty_fulltext=_or_inf(settings.empty_fulltext),
            off_topic_distance=settings.off_topic_distance,
        )

    async def run(self, company_code: str | None = None) -> list[GapRunReport]:
        """Построить отчёт для одной компании или для всех активных.

        Ошибка в одной компании не останавливает остальные: она в логе и
        в отчёте (failed), а CLI вернёт ненулевой код.
        """
        async with self.session_maker() as lock_session:
            # Транзакционная блокировка: держится, пока открыта эта
            # транзакция, и снимается откатом в конце — даже при падении.
            acquired = await lock_session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _LOCK_KEY}
            )
            if not acquired:
                raise ConflictError("Отчёт о пробелах уже строится")
            try:
                tenants = await self._tenants(company_code)
                reports = []
                for tenant in tenants:
                    reports.append(await self._run_safely(tenant))
                return reports
            finally:
                await lock_session.rollback()

    async def _tenants(self, company_code: str | None) -> list[Tenant]:
        async with self.session_maker() as session:
            repo = TenantRepository(session)
            if company_code is not None:
                tenant = await repo.get_by_company_code(company_code)
                if tenant is None:
                    raise ConflictError(f"Компании с кодом '{company_code}' нет")
                return [tenant]
            # Приостановленным компаниям отчёт не строим: LLM стоит денег,
            # а смотреть его некому — вход закрыт.
            return [t for t in await repo.list_all() if t.is_active]

    async def _run_safely(self, tenant: Tenant) -> GapRunReport:
        try:
            return await self._run_tenant(tenant)
        except Exception:  # noqa: BLE001 — одна компания не роняет ночь
            logger.exception("gaps_tenant_failed", tenant_id=str(tenant.id))
            return GapRunReport(
                company_code=tenant.company_code,
                window=0,
                candidates=0,
                clusters=0,
                labeled=0,
                failed=True,
            )

    async def _run_tenant(self, tenant: Tenant) -> GapRunReport:
        since = self.now() - timedelta(days=self.settings.window_days)
        with tenant_scope(tenant.id):
            window, candidates, model, existing = await self._classify(since)
            drafts = await asyncio.to_thread(self._cluster, candidates)
            self._match(drafts, existing, model)
            labeled = await self._label(drafts)
            await self._save(drafts, existing, model)

        logger.info(
            "gaps_built",
            tenant_id=str(tenant.id),
            window=window,
            candidates=len(candidates),
            clusters=len(drafts),
            labeled=labeled,
        )
        return GapRunReport(
            company_code=tenant.company_code,
            window=window,
            candidates=len(candidates),
            clusters=len(drafts),
            labeled=labeled,
        )

    async def _classify(
        self, since: datetime
    ) -> tuple[int, Sequence[CandidateRow], str | None, list[_Existing]]:
        """Шаг 1: классы вопросов окна и кандидаты в кластеры.

        Классифицируется всё окно каждую ночь: 👎 ставят и после ответа,
        и класс вопроса меняется задним числом.
        """
        async with self.session_maker() as session:
            qa_log = QaLogRepository(session)
            signals = await qa_log.signals_since(since)
            changed: dict[str, list[UUID]] = defaultdict(list)
            for row in signals:
                kind = classify_miss(
                    MissSignals(
                        best_vector_distance=row.best_vector_distance,
                        best_fulltext_score=row.best_fulltext_score,
                        answer_given=row.answer_given,
                        feedback=row.feedback,
                    ),
                    self.thresholds,
                )
                if kind.value != row.miss_kind:
                    changed[kind.value].append(row.id)
            for kind_value, ids in changed.items():
                await qa_log.set_miss_kind(kind_value, ids)

            # Векторы разных моделей эмбеддингов несравнимы: после смены
            # модели окно начинается заново с её вопросов (BH-20).
            model = await qa_log.newest_embedding_model(since)
            candidates: Sequence[CandidateRow] = []
            if model is not None:
                candidates = await qa_log.gap_candidates(
                    since=since,
                    kinds=[kind.value for kind in REPORTED_KINDS],
                    embedding_model=model,
                    limit=self.settings.max_questions,
                )
            existing = [
                _Existing(
                    id=cluster.id,
                    members=frozenset(members),
                    title=cluster.title,
                    missing=cluster.missing,
                    prompt_version=cluster.prompt_version,
                    embedding_model=cluster.embedding_model,
                )
                for cluster, members in await GapRepository(session).list_with_members()
            ]
            await session.commit()
        return len(signals), candidates, model, existing

    def _cluster(self, candidates: Sequence[CandidateRow]) -> list[_Draft]:
        """Шаг 2 (CPU, в отдельном потоке): кластеры и их приоритет."""
        if not candidates:
            return []
        labels = cluster_questions(
            [row.question_embedding for row in candidates],
            max_distance=self.settings.cluster_distance,
        )
        groups: dict[int, list[CandidateRow]] = defaultdict(list)
        for label, row in zip(labels, candidates, strict=True):
            groups[label].append(row)

        now = self.now()
        drafts = [
            _Draft(
                members=rows,
                priority=cluster_priority(
                    [
                        # Вопрос удалённого пользователя (user_id пуст)
                        # считается отдельным человеком.
                        ClusterQuestion(
                            user_id=str(row.user_id or row.id),
                            asked_at=row.created_at,
                        )
                        for row in rows
                    ],
                    now=now,
                    half_life_days=self.settings.half_life_days,
                ),
            )
            for rows in groups.values()
            if len(rows) >= self.settings.min_cluster_size
        ]
        drafts.sort(key=lambda draft: draft.priority, reverse=True)
        return drafts

    @staticmethod
    def _match(
        drafts: list[_Draft], existing: list[_Existing], model: str | None
    ) -> None:
        """Шаг 3: узнать вчерашние кластеры по общим вопросам.

        Жадно, от самых больших пересечений: каждому новому — не больше
        одного старого и наоборот. Узнанный кластер сохраняет id, статус
        и подпись; остальные старые удалятся при записи.
        """
        owner = {
            qa_log_id: old
            for old in existing
            if old.embedding_model == model
            for qa_log_id in old.members
        }
        pairs: list[tuple[int, int, _Existing]] = []
        for index, draft in enumerate(drafts):
            overlap = Counter(owner[q] for q in draft.ids if q in owner)
            pairs.extend((count, index, old) for old, count in overlap.items())
        pairs.sort(key=lambda pair: (-pair[0], pair[1]))

        taken: set[UUID] = set()
        for _, index, old in pairs:
            draft = drafts[index]
            if draft.match is not None or old.id in taken:
                continue
            draft.match = old
            taken.add(old.id)
            draft.title, draft.missing = old.title, old.missing
            draft.prompt_version = old.prompt_version

    async def _label(self, drafts: list[_Draft]) -> int:
        """Шаг 4: подписать новые кластеры и кластеры со старым промптом.

        Узнанный кластер с актуальной подписью не переподписывается:
        название в отчёте не прыгает каждую ночь, и LLM не платим зря.
        Сбой модели — не сбой отчёта: кластер получает временное название
        из вопроса и будет подписан в следующий запуск.
        """
        labeled = 0
        for draft in drafts:
            if draft.prompt_version == PROMPT_VERSION:
                continue
            if labeled >= self.settings.max_labels_per_run:
                self._placeholder(draft)
                continue
            questions = [row.question for row in draft.members[:MAX_QUESTIONS]]
            try:
                completion = await self.llm.generate(
                    build_gap_messages(questions),
                    temperature=0,
                    max_tokens=400,
                    response_format=GAP_LABEL_SCHEMA,
                )
                label = parse_gap_label(completion.content)
            except LLMError as exc:
                logger.warning("gaps_label_failed", error=str(exc))
                self._placeholder(draft)
                continue
            title = _clean(label.title, MAX_TITLE_LENGTH)
            if not title:
                self._placeholder(draft)
                continue
            draft.title = title
            draft.missing = _clean(label.missing, MAX_MISSING_LENGTH)
            draft.prompt_version = PROMPT_VERSION
            labeled += 1
        return labeled

    @staticmethod
    def _placeholder(draft: _Draft) -> None:
        if not draft.title:
            # Вопросы в qa_log уже после mask_pii.
            draft.title = _clean(draft.members[0].question, MAX_TITLE_LENGTH)

    async def _save(
        self, drafts: list[_Draft], existing: list[_Existing], model: str | None
    ) -> None:
        """Шаг 5: одна транзакция на компанию.

        Статус узнанного кластера не трогается: админ мог поменять его,
        пока модель подписывала кластеры, — его решение важнее.
        """
        async with self.session_maker() as session:
            repo = GapRepository(session)
            kept = {draft.match.id for draft in drafts if draft.match is not None}
            for old in existing:
                if old.id not in kept:
                    stale = await repo.get_by_id(old.id)
                    if stale is not None:
                        await repo.delete(stale)

            for draft in drafts:
                cluster = (
                    await repo.get_by_id(draft.match.id)
                    if draft.match is not None
                    else None
                )
                if cluster is None:
                    cluster = GapCluster(status="new")
                cluster.title = draft.title
                cluster.missing = draft.missing
                cluster.prompt_version = draft.prompt_version
                cluster.priority = draft.priority
                cluster.question_count = len(draft.members)
                cluster.user_count = len(
                    {row.user_id or row.id for row in draft.members}
                )
                cluster.first_seen = min(row.created_at for row in draft.members)
                cluster.last_seen = max(row.created_at for row in draft.members)
                cluster.embedding_model = model or ""
                if draft.match is None:
                    await repo.add(cluster)
                await repo.replace_members(cluster.id, draft.ids)
            await session.commit()


def _or_inf(value: float | None) -> float:
    return math.inf if value is None else value


def _clean(value: str, limit: int) -> str:
    """Текст модели или сотрудника для отчёта: одна строка, без
    управляющих символов, не длиннее поля."""
    return " ".join(_CONTROL.sub(" ", value).split())[:limit]
