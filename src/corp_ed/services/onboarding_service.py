"""Первые шаги (ТЗ §8).

Администратору новой компании — чек-лист: загрузить документы или
подключить источник, пригласить людей, задать первый вопрос; галочки
ставятся сами по данным компании. Сотруднику — 3–4 подсказки при первом
входе. Что показано и что скрыто — в членстве (users.tips_seen_at,
checklist_hidden_at), а не в браузере: на новом устройстве не всплывёт.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import (
    Connector,
    Invite,
    Material,
    MemberStatus,
    QaLog,
    User,
)

Step = Literal["tips", "checklist"]


@dataclass(frozen=True)
class Onboarding:
    documents: bool
    """Есть загруженный документ или подключение."""
    people: bool
    """Создано приглашение или в компании кто-то кроме администратора."""
    question: bool
    """В компании задан хотя бы один вопрос."""
    tips_seen: bool
    checklist_hidden: bool


class OnboardingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def state(self, member: User) -> Onboarding:
        tenant_id = member.tenant_id

        async def exists(statement: Select[Any]) -> bool:
            return bool(await self.session.scalar(statement))

        documents = await exists(
            select(select(Material.id).where(Material.tenant_id == tenant_id).exists())
        ) or await exists(
            select(
                select(Connector.id).where(Connector.tenant_id == tenant_id).exists()
            )
        )
        others = await self.session.scalar(
            select(func.count()).where(
                User.tenant_id == tenant_id,
                User.status == MemberStatus.ACTIVE,
                User.id != member.id,
            )
        )
        people = bool(others) or await exists(
            select(select(Invite.id).where(Invite.tenant_id == tenant_id).exists())
        )
        question = await exists(
            select(select(QaLog.id).where(QaLog.tenant_id == tenant_id).exists())
        )
        return Onboarding(
            documents=documents,
            people=people,
            question=question,
            tips_seen=member.tips_seen_at is not None,
            checklist_hidden=member.checklist_hidden_at is not None,
        )

    async def done(self, member: User, step: Step) -> Onboarding:
        now = datetime.now(UTC)
        if step == "tips" and member.tips_seen_at is None:
            member.tips_seen_at = now
        if step == "checklist" and member.checklist_hidden_at is None:
            member.checklist_hidden_at = now
        await self.session.commit()
        return await self.state(member)
