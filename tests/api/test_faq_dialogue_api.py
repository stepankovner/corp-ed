"""POST /faq/ask и память диалога (BH-28): conversation_id туда и обратно."""

from collections.abc import AsyncGenerator

import httpx
import pytest

from corp_ed.api.v1.dependencies import get_rag_settings
from corp_ed.core.config import RagSettings
from corp_ed.core.dialogue_store import InMemoryDialogueStore
from corp_ed.main import app


@pytest.fixture
async def memory_on(api: httpx.AsyncClient) -> AsyncGenerator[None]:
    """Хранилище реплик и RAG_HISTORY_TURNS=3, как на стенде."""
    base = app.dependency_overrides[get_rag_settings]()
    settings = RagSettings.model_validate(base.model_dump() | {"history_turns": 3})
    app.dependency_overrides[get_rag_settings] = lambda: settings
    app.state.dialogue_store = InMemoryDialogueStore()
    try:
        yield
    finally:
        del app.state.dialogue_store


async def test_new_question_gets_a_conversation_id(
    employee_client: httpx.AsyncClient,
) -> None:
    response = await employee_client.post(
        "/api/v1/faq/ask", json={"question": "Сколько дней отпуска?"}
    )

    assert response.status_code == 200
    assert response.json()["conversation_id"]


@pytest.mark.usefixtures("memory_on")
async def test_follow_up_continues_the_dialogue(
    admin_client: httpx.AsyncClient,
) -> None:
    first = await admin_client.post(
        "/api/v1/faq/ask", json={"question": "Сколько дней отпуска?"}
    )
    conversation = first.json()["conversation_id"]
    assert first.json()["diagnostics"]["history_turns"] == 0

    second = await admin_client.post(
        "/api/v1/faq/ask",
        json={"question": "А летом?", "conversation_id": conversation},
    )

    body = second.json()
    assert second.status_code == 200
    assert body["conversation_id"] == conversation
    # Админу видно, сколько реплик учтено и как понят вопрос (замер ML).
    assert body["diagnostics"]["history_turns"] == 1
    assert body["diagnostics"]["standalone_question"]


@pytest.mark.usefixtures("memory_on")
async def test_employee_does_not_see_diagnostics_in_a_dialogue(
    employee_client: httpx.AsyncClient,
) -> None:
    first = await employee_client.post(
        "/api/v1/faq/ask", json={"question": "Сколько дней отпуска?"}
    )
    conversation = first.json()["conversation_id"]
    second = await employee_client.post(
        "/api/v1/faq/ask",
        json={"question": "А летом?", "conversation_id": conversation},
    )

    assert second.status_code == 200
    assert second.json()["diagnostics"] is None


async def test_malformed_conversation_id_is_rejected(
    employee_client: httpx.AsyncClient,
) -> None:
    response = await employee_client.post(
        "/api/v1/faq/ask",
        json={"question": "Отпуск?", "conversation_id": "не-uuid"},
    )

    assert response.status_code == 422
