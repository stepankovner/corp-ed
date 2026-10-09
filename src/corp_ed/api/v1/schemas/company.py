"""Схемы админки (ТЗ §7): настройки компании, тариф, аналитика, папки,
источники глазами сотрудника."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.domain.tariffs import Tariff
from corp_ed.domain.types import NotFoundMode

MfaPolicy = Literal["any", "strong"]
ChatRetentionMonths = Literal[1, 3, 6, 12, 24, 36]
"""Варианты срока хранения диалогов — как CHAT_RETENTION_MONTHS."""


class CompanySettingsResponse(BaseModel):
    """Настройки компании.

    not_found_mode: general — общий ответ с пометкой, strict — честный
    отказ. mfa_policy: any — код на почту, strong — всем приложение или
    ключ доступа (администраторам — всегда). email_domains пусто — по
    приглашению вступает почта любого домена. chat_retention_months —
    сколько месяцев хранятся диалоги без активности; старше удаляются
    целиком, с вложениями и общими ссылками.
    """

    id: UUID
    name: str
    company_code: str
    logo_url: str | None
    not_found_mode: NotFoundMode
    mfa_policy: MfaPolicy
    allow_remember_device: bool
    email_domains: list[str]
    chat_retention_months: int
    tariff: Tariff
    seats: int
    members: int
    """Работающие люди — сколько мест занято."""
    daily_credits_per_member: int | None
    """Личный дневной лимит кредитов на человека (включая администратора);
    null — без лимита."""


class CompanySettingsRequest(RequestModel):
    """Что прислано, то и меняется."""

    name: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"\S")
    not_found_mode: NotFoundMode | None = None
    mfa_policy: MfaPolicy | None = None
    allow_remember_device: bool | None = None
    email_domains: list[str] | None = Field(default=None, max_length=10)
    chat_retention_months: ChatRetentionMonths | None = None

    @field_validator("chat_retention_months", mode="before")
    @classmethod
    def _months_not_bool(cls, value: object) -> object:
        # Literal[1, …] принял бы true из JSON как 1 месяц.
        if isinstance(value, bool):
            raise ValueError("Срок хранения — число месяцев")
        return value

    daily_credits_per_member: int | None = Field(default=None, ge=1, le=10_000)
    """Прислан null — лимит снимается; не прислан — не меняется."""


class LogoResponse(BaseModel):
    logo_url: str


class TariffRequest(RequestModel):
    tariff: Tariff
    seats: int | None = Field(default=None, ge=1, le=10_000)
    comment: str | None = Field(default=None, max_length=1000)


class DayStatsResponse(BaseModel):
    day: date
    questions: int
    answered: int


class FrequentQuestionResponse(BaseModel):
    question: str
    asked: int
    people: int
    answered: int


class FeedbackCommentResponse(BaseModel):
    created_at: datetime
    question: str
    reason: str | None
    comment: str


class AnalyticsResponse(BaseModel):
    """Обезличенная статистика за период (ТЗ §6–7): ни имён, ни почты.

    answered — ответы по документам; general — общий ответ с пометкой;
    refused — честный отказ. frequent — вопросы, которые задали не меньше
    трёх разных людей; comments — последние комментарии к 👎, без автора.
    """

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
    days: list[DayStatsResponse]
    frequent: list[FrequentQuestionResponse]
    comments: list[FeedbackCommentResponse]
    open_gaps: int


class DepartmentBrief(BaseModel):
    id: UUID
    name: str


class FolderResponse(BaseModel):
    id: UUID
    name: str
    restricted: bool
    """True — документы видят только отделы из departments и администраторы."""
    departments: list[DepartmentBrief]
    documents: int
    created_at: datetime


class FolderCreateRequest(RequestModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"\S")
    restricted: bool = False
    department_ids: list[UUID] = Field(default_factory=list, max_length=200)


class FolderUpdateRequest(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"\S")
    restricted: bool | None = None
    department_ids: list[UUID] | None = Field(default=None, max_length=200)


class FileGroupResponse(BaseModel):
    folder_id: UUID | None
    name: str
    restricted: bool
    documents: int


class ConnectorSourceResponse(BaseModel):
    id: UUID
    kind: str
    name: str
    mode: str
    working: bool
    grant_status: str | None
    """Для источников, которые сотрудник подключает сам: active,
    expired, revoked или null — ещё не подключал."""


class MySourcesResponse(BaseModel):
    """Где ищет ассистент для этого сотрудника (ТЗ §5)."""

    files: list[FileGroupResponse]
    connectors: list[ConnectorSourceResponse]
