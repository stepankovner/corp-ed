"""Уведомления, первые шаги, поддержка (ТЗ §8)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from corp_ed.api.v1.schemas.base import RequestModel

NotificationKindName = Literal[
    "connector_stopped",
    "credits_warning",
    "credits_exhausted",
    "credits_added",
    "credits_topup_requested",
    "join_request",
    "department_request",
    "department_confirmed",
    "department_rejected",
    "weekly_digest",
    "billing_invoice",
    "billing_paid",
    "billing_overdue",
    "billing_act",
]
"""credits_added — администраторам: команда зачислила купленные кредиты или
начислила бонус; credits_topup_requested — сотрудник упёрся в лимит и
просит пополнить (одно на эпизод исчерпания). department_request —
администраторам: сотрудник выбрал отдел, которому
открыта закрытая папка, и ждёт подтверждения; department_confirmed и
department_rejected — сотруднику: решение по его отделу (ТЗ §7)."""


class NotificationResponse(BaseModel):
    id: UUID
    kind: NotificationKindName
    title: str
    body: str
    link: str | None
    created_at: datetime
    read: bool


class NotificationsResponse(BaseModel):
    items: list[NotificationResponse]
    unread: int


class MarkReadRequest(RequestModel):
    """ids нет — прочитать все свои."""

    ids: list[UUID] | None = Field(default=None, max_length=100)


class NotificationSettingsResponse(BaseModel):
    """Письма администратору; колокольчик приходит всегда. Письма о
    безопасности учётки настройками не выключаются."""

    email_connectors: bool
    email_credits: bool
    email_join_requests: bool
    """Заявки на вступление и отдел с закрытой папкой, ждущий
    подтверждения (ТЗ §7)."""
    email_weekly_digest: bool


class NotificationSettingsRequest(RequestModel):
    email_connectors: bool | None = None
    email_credits: bool | None = None
    email_join_requests: bool | None = None
    email_weekly_digest: bool | None = None


class OnboardingResponse(BaseModel):
    documents: bool
    people: bool
    question: bool
    tips_seen: bool
    checklist_hidden: bool


SupportTopic = Literal["login", "documents", "answers", "billing", "other"]
SupportStatus = Literal["new", "answered", "closed"]


class SupportCreateRequest(RequestModel):
    topic: SupportTopic
    message: str = Field(min_length=10, max_length=4000, pattern=r"\S")


class SupportResponse(BaseModel):
    id: UUID
    topic: SupportTopic
    message: str
    status: SupportStatus
    created_at: datetime


class StaffSupportResponse(SupportResponse):
    email: str | None
    name: str | None
    company: str | None
    updated_at: datetime


class StaffSupportUpdate(RequestModel):
    status: SupportStatus
