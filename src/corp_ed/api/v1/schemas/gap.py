from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.domain.types import GapStatus


class GapClusterResponse(BaseModel):
    """Пробел: тема, чего не хватает, насколько это важно.

    sample_questions — последние вопросы группы после mask_pii. Это текст
    сотрудников: фронт выводит его как текст, не как разметку.
    """

    id: UUID
    title: str
    missing: str
    priority: float
    question_count: int
    user_count: int
    first_seen: datetime
    last_seen: datetime
    status: GapStatus
    sample_questions: list[str]


class GapReportResponse(BaseModel):
    clusters: list[GapClusterResponse]


class GapStatusRequest(RequestModel):
    status: GapStatus
