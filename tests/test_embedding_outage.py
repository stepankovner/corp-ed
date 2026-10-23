"""Эмбеддинги вопроса недоступны (BH-51): быстрая ошибка, а не ожидание.

Ответ по словам ML не рекомендует (замер 23.10): вопрос получает ошибку
сразу, пока предохранитель эмбеддингов открыт, — поставщика не ждём.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Tenant, User
from corp_ed.llm.embedding_breaker import BreakerEmbeddings
from corp_ed.llm.errors import LLMError
from corp_ed.llm.fallback import CircuitBreaker
from corp_ed.repositories.chunk_repository import ChunkRepository
from tests.llm.test_embedding_breaker import Script
from tests.test_search_step import QUESTION, ScriptedStream, _service


async def test_open_breaker_fails_the_question_without_calling_yandex(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    breaker = CircuitBreaker(kind="embedding")
    breaker.record_failure("error_config")
    inner = Script()
    llm = ScriptedStream()
    service = _service(chunk_repo, llm)
    service.embedding_gateway = BreakerEmbeddings(inner, breaker)

    with pytest.raises(LLMError):
        await service.answer_turn(QUESTION, employee, history=[])

    assert inner.calls == 0
    assert llm.calls == []
    breaker.record_success()
