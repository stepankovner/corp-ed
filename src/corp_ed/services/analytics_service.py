"""Аналитика для главной админки (ТЗ §7): вопросы по дням, доля без
ответа, оценки, активные сотрудники, частые вопросы.

Только обезличенно (ТЗ §6): из журнала qa_log, где вопрос уже после
mask_pii; ни одного имени, почты или id человека наружу не уходит.
Частый вопрос показывается, только если его задали не меньше
FREQUENT_MIN_USERS разных людей; комментарии к 👎 — без автора.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.models import GapCluster, MemberStatus, QaLog, User
from corp_ed.services.suggestion_service import FREQUENT_MAX_LENGTH, FREQUENT_MIN_USERS

FREQUENT_LIMIT = 10
COMMENTS_LIMIT = 10


@dataclass(frozen=True)
class DayStats:
    day: date
    questions: int
    answered: int


@dataclass(frozen=True)
class FrequentQuestion:
    question: str
    asked: int
    people: int
    answered: int


@dataclass(frozen=True)
class Comment:
    created_at: datetime
    question: str
    reason: str | None
    comment: str


@dataclass(frozen=True)
class Overview:
    since: date
    until: date
    questions: int
    answered: int
    general: int
    refused: int
    likes: int
    dislikes: int
    reasons: dict[str, int]
    active_people: int
    members: int
    credits: int
    days: list[DayStats]
    frequent: list[FrequentQuestion]
    comments: list[Comment]
    open_gaps: int


class AnalyticsService:
    def __init__(self, session: AsyncSession, *, zone: str) -> None:
        self.session = session
        self.zone = ZoneInfo(zone)

    async def overview(self, days: int, now: datetime | None = None) -> Overview:
        """Последние days дней по времени компании, сегодня включительно."""
        tenant_id = require_tenant()
        local_now = (now or datetime.now(UTC)).astimezone(self.zone)
        first = local_now.date() - timedelta(days=days - 1)
        since = datetime(first.year, first.month, first.day, tzinfo=self.zone)
        period = (QaLog.tenant_id == tenant_id, QaLog.created_at >= since)

        totals = (
            await self.session.execute(
                select(
                    func.count(),
                    func.count().filter(QaLog.answer_given.is_(True)),
                    func.count().filter(QaLog.origin == "general_knowledge"),
                    func.count().filter(QaLog.origin == "none"),
                    func.count().filter(QaLog.feedback == 1),
                    func.count().filter(QaLog.feedback == -1),
                    func.count(func.distinct(QaLog.user_id)),
                    func.coalesce(func.sum(QaLog.credits), 0),
                ).where(*period)
            )
        ).one()

        local_day = cast(func.timezone(self.zone.key, QaLog.created_at), Date)
        per_day = {
            row.day: row
            for row in await self.session.execute(
                select(
                    local_day.label("day"),
                    func.count().label("questions"),
                    func.count().filter(QaLog.answer_given.is_(True)).label("answered"),
                )
                .where(*period)
                .group_by(local_day)
            )
        }
        series: list[DayStats] = []
        for offset in range(days):
            day = first + timedelta(days=offset)
            row = per_day.get(day)
            series.append(
                DayStats(
                    day=day,
                    questions=int(row.questions) if row else 0,
                    answered=int(row.answered) if row else 0,
                )
            )

        reasons = {
            str(reason): int(count)
            for reason, count in await self.session.execute(
                select(QaLog.feedback_reason, func.count())
                .where(
                    *period, QaLog.feedback == -1, QaLog.feedback_reason.is_not(None)
                )
                .group_by(QaLog.feedback_reason)
            )
        }

        members = await self.session.scalar(
            select(func.count()).where(
                User.tenant_id == tenant_id, User.status == MemberStatus.ACTIVE
            )
        )
        open_gaps = await self.session.scalar(
            select(func.count()).where(
                GapCluster.tenant_id == tenant_id,
                GapCluster.status.in_(("new", "in_progress")),
            )
        )

        return Overview(
            since=first,
            until=local_now.date(),
            questions=int(totals[0]),
            answered=int(totals[1]),
            general=int(totals[2]),
            refused=int(totals[3]),
            likes=int(totals[4]),
            dislikes=int(totals[5]),
            reasons=reasons,
            active_people=int(totals[6]),
            members=int(members or 0),
            credits=int(totals[7]),
            days=series,
            frequent=await self._frequent(since),
            comments=await self._comments(since),
            open_gaps=int(open_gaps or 0),
        )

    async def _frequent(self, since: datetime) -> list[FrequentQuestion]:
        """Частые вопросы: тот же текст после маски у FREQUENT_MIN_USERS и
        больше разных людей. Уточнения в диалоге и вопросы к вложениям —
        мимо: вне своего диалога они непонятны."""
        normalized = func.lower(
            func.regexp_replace(
                func.regexp_replace(func.btrim(QaLog.question), r"\s+", " ", "g"),
                r"[?!.…\s]+$",
                "",
            )
        )
        people = func.count(func.distinct(QaLog.user_id))
        rows = await self.session.execute(
            select(
                func.mode().within_group(QaLog.question).label("sample"),
                func.count().label("asked"),
                people.label("people"),
                func.count().filter(QaLog.answer_given.is_(True)).label("answered"),
            )
            .where(
                QaLog.tenant_id == require_tenant(),
                QaLog.created_at >= since,
                QaLog.history_turns == 0,
                QaLog.attachment_chunks == 0,
                func.length(QaLog.question) <= FREQUENT_MAX_LENGTH,
            )
            .group_by(normalized)
            .having(people >= FREQUENT_MIN_USERS)
            .order_by(func.count().desc(), people.desc())
            .limit(FREQUENT_LIMIT)
        )
        return [
            FrequentQuestion(
                question=str(row.sample).strip(),
                asked=int(row.asked),
                people=int(row.people),
                answered=int(row.answered),
            )
            for row in rows
        ]

    async def _comments(self, since: datetime) -> list[Comment]:
        """Последние комментарии к 👎 — что поправить в документах. Без
        автора; текст и вопрос — после mask_pii."""
        rows = await self.session.execute(
            select(
                QaLog.created_at,
                func.coalesce(QaLog.standalone_question, QaLog.question),
                QaLog.feedback_reason,
                QaLog.feedback_comment,
            )
            .where(
                QaLog.tenant_id == require_tenant(),
                QaLog.created_at >= since,
                QaLog.feedback == -1,
                QaLog.feedback_comment.is_not(None),
            )
            .order_by(QaLog.created_at.desc())
            .limit(COMMENTS_LIMIT)
        )
        return [
            Comment(
                created_at=at, question=str(question), reason=reason, comment=str(text)
            )
            for at, question, reason, text in rows
        ]
