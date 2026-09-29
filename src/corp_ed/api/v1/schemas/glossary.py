from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from corp_ed.api.v1.schemas.base import RequestModel

MAX_TERM_LENGTH = 64
MAX_EXPANSION_LENGTH = 256


def _single_line(value: str) -> str:
    """Одна строка без управляющих символов, края без пробелов.

    Расшифровка дописывается к вопросу перед поиском: перевод строки или
    NUL в ней ломали бы запрос и журнал, а невидимые символы делали бы
    термин, который совпадает не с тем, что видно в интерфейсе.
    """
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Только одна строка, без управляющих символов")
    if not value:
        raise ValueError("Пустое значение")
    return value


Term = Annotated[
    str, Field(min_length=2, max_length=MAX_TERM_LENGTH), AfterValidator(_single_line)
]
Expansion = Annotated[
    str,
    Field(min_length=2, max_length=MAX_EXPANSION_LENGTH),
    AfterValidator(_single_line),
]


class GlossaryTermCreateRequest(RequestModel):
    """«ДМС» → «добровольное медицинское страхование»."""

    term: Term
    expansion: Expansion


class GlossaryTermUpdateRequest(RequestModel):
    term: Term | None = None
    expansion: Expansion | None = None


class GlossaryTermResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    term: str
    expansion: str
    created_at: datetime
    updated_at: datetime
