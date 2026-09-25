from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from corp_ed.api.v1.schemas.base import RequestModel


class FaqQuestionRequest(RequestModel):
    question: str = Field(min_length=1, max_length=1000)


class FaqSourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    material_id: UUID
    title: str
    heading_path: list[str]
    position: int
    content: str


class FaqAnswerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    content: str
    answer_given: bool
    sources: list[FaqSourceResponse]
