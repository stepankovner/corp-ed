"""«Написать в поддержку» (ТЗ §8).

Обращение хранится в базе и видно команде в нашей панели («Обращения»).
В Telegram команды уходит только номер, тема и код компании: текст и
почта человека — персональные данные, им не место в зарубежном
мессенджере (DEPLOY.md: бот без персональных данных). Отвечает команда
письмом на почту из обращения.
"""

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import structlog
from sqlalchemy import Row, Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import NotFoundError
from corp_ed.domain.models import Account, SupportRequest, Tenant
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.services.team_notify import (
    NULL_NOTIFIER,
    TeamNotifier,
    support_message,
)

logger = structlog.get_logger()

Topic = Literal["login", "documents", "answers", "billing", "other"]
Status = Literal["new", "answered", "closed"]

TOPIC_TITLES: dict[str, str] = {
    "login": "вход и учётная запись",
    "documents": "документы и подключения",
    "answers": "ответы ассистента",
    "billing": "тариф и оплата",
    "other": "другое",
}
LIST_LIMIT = 200


@dataclass(frozen=True)
class SupportItem:
    request: SupportRequest
    email: str | None
    name: str | None
    company: str | None


class SupportService:
    def __init__(
        self,
        session: AsyncSession,
        audit: AuditRepository,
        notifier: TeamNotifier = NULL_NOTIFIER,
    ) -> None:
        self.session = session
        self.audit = audit
        self.notifier = notifier

    async def create(
        self, account: Account, tenant_id: UUID | None, topic: Topic, message: str
    ) -> SupportRequest:
        request = SupportRequest(
            account_id=account.id,
            tenant_id=tenant_id,
            topic=topic,
            message=message.strip(),
        )
        self.session.add(request)
        await self.session.flush()
        self.audit.record(
            AuditAction.SUPPORT_REQUESTED,
            tenant_id=tenant_id,
            target_type="support_request",
            target_id=request.id,
            details={"account_id": str(account.id), "topic": topic},
        )
        await self.session.commit()
        self.notifier.notify(
            support_message(
                request_id=request.id, topic=TOPIC_TITLES[topic], tenant_id=tenant_id
            )
        )
        logger.info("support_requested", request_id=str(request.id), topic=topic)
        return request

    async def mine(self, account: Account) -> list[SupportRequest]:
        return list(
            (
                await self.session.scalars(
                    select(SupportRequest)
                    .where(SupportRequest.account_id == account.id)
                    .order_by(SupportRequest.created_at.desc())
                    .limit(50)
                )
            ).all()
        )

    # --- команда kronto (наша панель) -----------------------------------------

    async def list(self, status: Status | None) -> list[SupportItem]:
        statement = self._items().limit(LIST_LIMIT)
        if status is not None:
            statement = statement.where(SupportRequest.status == status)
        return [_item(row) for row in await self.session.execute(statement)]

    async def set_status(self, request_id: UUID, status: Status) -> SupportItem:
        request = await self.session.get(SupportRequest, request_id)
        if request is None:
            raise NotFoundError("Обращение не найдено")
        request.status = status
        await self.session.commit()
        row = (
            await self.session.execute(
                self._items().where(SupportRequest.id == request_id)
            )
        ).one()
        return _item(row)

    @staticmethod
    def _items() -> Select[Any]:
        return (
            select(
                SupportRequest,
                Account.email,
                Account.first_name,
                Account.last_name,
                Tenant.name,
            )
            .join(Account, Account.id == SupportRequest.account_id)
            .outerjoin(Tenant, Tenant.id == SupportRequest.tenant_id)
            .order_by(SupportRequest.created_at.desc())
        )


def _item(row: Row[Any]) -> SupportItem:
    request, email, first, last, company = row
    name = " ".join(part for part in (first, last) if part) or None
    return SupportItem(request=request, email=email, name=name, company=company)
