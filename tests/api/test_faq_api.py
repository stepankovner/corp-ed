import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.api.v1.dependencies import get_llm_gateway
from corp_ed.domain.models import Chunk, Material
from corp_ed.llm.errors import LLMError
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.types import Completion, Message
from corp_ed.main import app


class FailingLLM(LLMGateway):
    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> Completion:
        raise LLMError("провайдер недоступен", retryable=True)


async def test_faq_empty_database_returns_no_answer(
    employee_client: httpx.AsyncClient,
    fake_llm,
) -> None:
    response = await employee_client.post(
        "/api/v1/faq/ask",
        json={"question": "Что написано в материалах?"},
    )

    assert response.status_code == 200

    body = response.json()

    assert body["answer_given"] is False
    assert body["sources"] == []
    assert fake_llm.calls == []


async def test_faq_returns_answer_with_source(
    employee_client: httpx.AsyncClient,
    material: Material,
    chunk_repo,
    session: AsyncSession,
    fake_llm,
) -> None:
    chunk = Chunk(
        material_id=material.id,
        position=0,
        content="Первый чанк.",
        embedding=[0.1] * 256,
        model="fake",
        model_version="fake",
    )

    await chunk_repo.bulk_create(chunks=[chunk])
    await session.commit()

    response = await employee_client.post(
        "/api/v1/faq/ask",
        json={"question": "Что написано?"},
    )

    assert response.status_code == 200

    body = response.json()

    assert body["answer_given"] is True
    assert body["content"] == fake_llm.content
    assert len(body["sources"]) == 1
    assert body["sources"][0]["material_id"] == str(material.id)
    assert body["sources"][0]["title"] == material.title
    assert body["sources"][0]["heading_path"] == []
    assert "distance" not in body["sources"][0]


async def test_faq_empty_question_returns_422(
    employee_client: httpx.AsyncClient,
    fake_embeddings,
) -> None:
    response = await employee_client.post(
        "/api/v1/faq/ask",
        json={"question": ""},
    )

    assert response.status_code == 422
    assert fake_embeddings.query_calls == []


async def test_faq_llm_error_returns_502(
    employee_client: httpx.AsyncClient,
    material: Material,
    chunk_repo,
    session: AsyncSession,
) -> None:
    chunk = Chunk(
        material_id=material.id,
        position=0,
        content="Первый чанк.",
        embedding=[0.1] * 256,
        model="fake",
        model_version="fake",
    )

    await chunk_repo.bulk_create(chunks=[chunk])
    await session.commit()

    app.dependency_overrides[get_llm_gateway] = lambda: FailingLLM()

    response = await employee_client.post(
        "/api/v1/faq/ask",
        json={"question": "Что написано?"},
    )

    assert response.status_code == 502

    body = response.json()

    assert "провайдер недоступен" not in body["detail"]
