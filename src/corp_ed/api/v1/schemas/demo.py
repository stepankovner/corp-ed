from typing import Annotated, Literal

from pydantic import BaseModel, Field, RootModel, field_validator

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.demo import SUGGESTED_QUESTIONS
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
    def known_question(cls, value: str) -> str:
        """Только вопрос из списка (SUGGESTED_QUESTIONS): свой текст
        посетителя в модель не уходит."""
        value = " ".join(value.split())
        if value not in SUGGESTED_QUESTIONS:
            raise ValueError("Выберите вопрос из списка")
        return value


class DemoSourceResponse(BaseModel):
    title: str
    heading_path: list[str]
    content: str


class DemoAnswerResponse(BaseModel):
    content: str
    origin: AnswerOrigin
    sources: list[DemoSourceResponse]


class DemoStreamStage(BaseModel):
    """searching — ищем в документах; writing — модель пишет ответ;
    searching_more — ищем в других документах (BH-43)."""

    type: Literal["stage"] = "stage"
    stage: Literal["searching", "writing", "searching_more"]


class DemoStreamDelta(BaseModel):
    type: Literal["delta"] = "delta"
    text: str


class DemoStreamReset(BaseModel):
    """Показанный текст убрать: дальше пойдёт другой ответ."""

    type: Literal["reset"] = "reset"


class DemoStreamDone(BaseModel):
    """Итог: answer.content заменяет напечатанный текст (ссылки уже
    нормализованы), источники — только здесь."""

    type: Literal["done"] = "done"
    answer: DemoAnswerResponse


class DemoStreamError(BaseModel):
    """Ответа не будет: demo_busy — пул песочницы исчерпан или модель не
    отвечает; internal — сбой."""

    type: Literal["error"] = "error"
    code: str
    message: str


class DemoStreamEvent(
    RootModel[
        Annotated[
            DemoStreamStage
            | DemoStreamDelta
            | DemoStreamReset
            | DemoStreamDone
            | DemoStreamError,
            Field(discriminator="type"),
        ]
    ]
):
    """Событие потока ответа песочницы."""
