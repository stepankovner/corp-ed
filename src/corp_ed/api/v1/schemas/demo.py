from pydantic import BaseModel, Field, field_validator

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.domain.types import AnswerOrigin

DEMO_QUESTION_MAX = 300


class DemoInfoResponse(BaseModel):
    company: str
    documents: list[str]
    questions: list[str]


class DemoQuestionRequest(RequestModel):
    question: str = Field(min_length=3, max_length=DEMO_QUESTION_MAX)
    website: str = Field(default="", max_length=200)
    """Ловушка для ботов, как в заявке на созвон: заполненное поле —
    ответ без вызова модели."""

    @field_validator("question")
    @classmethod
    def strip_question(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 3:
            raise ValueError("Вопрос слишком короткий")
        return value


class DemoSourceResponse(BaseModel):
    title: str
    heading_path: list[str]
    content: str


class DemoAnswerResponse(BaseModel):
    content: str
    origin: AnswerOrigin
    sources: list[DemoSourceResponse]
