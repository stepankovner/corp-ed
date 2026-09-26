from typing import Any

from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.types import Completion, FinishReason, Message, Usage


class FakeAdapter(LLMGateway):
    def __init__(
        self,
        content: str = "фейковый ответ",
        finish_reason: FinishReason = FinishReason.COMPLETED,
    ):
        self.content = content
        self.calls: list[list[Message]] = []
        self.call_kwargs: list[dict[str, float | int]] = []
        self.response_formats: list[dict[str, Any] | None] = []
        self.finish_reason = finish_reason

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        self.calls.append(messages)
        self.call_kwargs.append({"temperature": temperature, "max_tokens": max_tokens})
        self.response_formats.append(response_format)

        return Completion(
            content=self.content,
            finish_reason=self.finish_reason,
            usage=Usage(
                input_tokens=0,
                output_tokens=0,
            ),
            model_version="fake",
            model="fake",
            latency_ms=0,
        )
