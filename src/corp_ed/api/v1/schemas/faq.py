from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class FaqQuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


class FaqSourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    material_id: UUID
    position: int
    content: str


class FaqAnswerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    content: str
    answer_given: bool
    sources: list[FaqSourceResponse]
