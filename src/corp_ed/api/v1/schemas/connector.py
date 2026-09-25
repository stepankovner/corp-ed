from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.domain.types import ConnectorMode, ConnectorStatus, GrantStatus

MAX_NAME_LENGTH = 100
MAX_FIELD_VALUE_LENGTH = 2048
MAX_FIELDS = 32
MIN_SYNC_INTERVAL = 15
MAX_SYNC_INTERVAL = 1440

# Значения полей формы: строки ограниченной длины. Не dict[str, Any]:
# вложенные структуры в config — путь к неожиданным типам в JSONB и
# в аргументах адаптера.
FieldValues = dict[str, str]


class FieldSpecResponse(BaseModel):
    name: str
    title: str
    required: bool
    secret: bool


class ModuleSpecResponse(BaseModel):
    name: str
    title: str


class ConnectorKindResponse(BaseModel):
    """Вид коннектора из каталога — для формы подключения на фронте."""

    kind: str
    title: str
    mode: ConnectorMode
    modules: list[ModuleSpecResponse]
    config_fields: list[FieldSpecResponse]
    credential_fields: list[FieldSpecResponse]


class ConnectorCreateRequest(RequestModel):
    kind: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    modules: list[str] = Field(min_length=1, max_length=16)
    config: FieldValues = Field(default_factory=dict)
    sync_interval_minutes: int | None = Field(
        default=None, ge=MIN_SYNC_INTERVAL, le=MAX_SYNC_INTERVAL
    )


class ConnectorUpdateRequest(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    modules: list[str] | None = Field(default=None, min_length=1, max_length=16)
    config: FieldValues | None = None
    sync_interval_minutes: int | None = Field(
        default=None, ge=MIN_SYNC_INTERVAL, le=MAX_SYNC_INTERVAL
    )
    # Админ может приостановить и возобновить; error снимается только
    # новыми учётными данными.
    status: ConnectorStatus | None = None


class CredentialsRequest(RequestModel):
    """Учётные данные — только на запись: в ответах их нет."""

    credentials: FieldValues = Field(min_length=1, max_length=MAX_FIELDS)


class ConnectorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: str
    name: str
    mode: ConnectorMode
    modules: list[str]
    config: dict[str, Any]
    status: ConnectorStatus
    sync_interval_minutes: int
    last_sync_at: datetime | None
    last_error_code: str | None
    credentials_set_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ConnectorTestResponse(BaseModel):
    ok: bool
    error_code: str | None = None


class SyncRequestedResponse(BaseModel):
    connector_id: UUID
    # False — задача уже стояла в очереди или выполняется.
    queued: bool


class SyncRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    stats: dict[str, Any]
    error_code: str | None


class MyConnectorResponse(BaseModel):
    """Коннектор режима per_user глазами сотрудника: подключён ли он сам."""

    id: UUID
    kind: str
    name: str
    grant_status: GrantStatus | None
    grant_error_code: str | None
