"""Заявки на созвон со страницы тарифов (досье 3.3 и 10.1, решение 28.09).

Путь по досье: тариф → удобные дата и время → данные компании и телефон
→ команда перезванивает и подтверждает (так отсекаются случайные
заявки) → созвон. Своей записи в календарь нет: это выбор удобного
окна, точное время подтверждает команда.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import LeadSettings
from corp_ed.core.exceptions import InvalidLeadError, NotFoundError
from corp_ed.domain.leads import (
    CALL_SLOTS,
    LeadStatus,
    LeadTariff,
    call_dates,
    is_workday,
)
from corp_ed.domain.models import Lead
from corp_ed.repositories.lead_repository import LeadRepository

logger = structlog.get_logger()


class LeadsClosedError(NotFoundError):
    def __init__(self) -> None:
        super().__init__("Запись на созвон пока не открыта")


@dataclass(frozen=True)
class LeadDraft:
    company_name: str
    contact_name: str
    phone: str
    email: str | None
    seats: int
    tariff: LeadTariff
    preferred_date: date
    preferred_slot: str
    comment: str | None
    policy_version: str
    consent: bool


class LeadService:
    def __init__(
        self,
        repository: LeadRepository,
        session: AsyncSession,
        settings: LeadSettings,
    ) -> None:
        self.repository = repository
        self.session = session
        self.settings = settings

    def ensure_open(self) -> None:
        if not self.settings.enabled:
            raise LeadsClosedError()

    async def submit(self, draft: LeadDraft, *, today: date) -> Lead:
        """Проверить и сохранить заявку. today — дата по Москве."""
        self.ensure_open()
        if not draft.consent:
            raise InvalidLeadError("Нужно согласие на обработку персональных данных")
        if draft.policy_version != self.settings.policy_version:
            # Политику обновили, пока форма была открыта: согласие дано на
            # другую редакцию.
            raise InvalidLeadError(
                "Политика обработки данных обновилась — обновите страницу"
            )
        first, last = call_dates(today, self.settings.days_ahead)
        if not first <= draft.preferred_date <= last:
            raise InvalidLeadError(f"Дата созвона — с {first:%d.%m} по {last:%d.%m}")
        if not is_workday(draft.preferred_date):
            raise InvalidLeadError("Созвоны — по будним дням")
        if draft.preferred_slot not in CALL_SLOTS:
            raise InvalidLeadError("Выберите время из предложенных")

        lead = await self.repository.add(
            Lead(
                company_name=draft.company_name,
                contact_name=draft.contact_name,
                phone=draft.phone,
                email=draft.email,
                seats=draft.seats,
                tariff=draft.tariff.value,
                preferred_date=draft.preferred_date,
                preferred_slot=draft.preferred_slot,
                comment=draft.comment,
                policy_version=draft.policy_version,
                consented_at=datetime.now(UTC),
            )
        )
        await self.session.commit()
        # В лог — только id: персональные данные читает команда из CLI.
        logger.info("lead_created", lead_id=str(lead.id), tariff=lead.tariff)
        return lead

    async def list_recent(self, *, status: LeadStatus | None, limit: int) -> list[Lead]:
        return await self.repository.list_recent(
            status=status.value if status else None, limit=limit
        )

    async def set_status(self, lead_id: UUID, status: LeadStatus) -> Lead:
        lead = await self.repository.get(lead_id)
        if lead is None:
            raise NotFoundError("Заявка не найдена")
        lead.status = status.value
        await self.session.commit()
        logger.info("lead_status_changed", lead_id=str(lead.id), status=status.value)
        return lead
