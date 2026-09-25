"""Права источников в поиске: restricted виден только по material_access."""

from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Material, MaterialAccess, Tenant, User
from corp_ed.domain.types import Retriever
from corp_ed.llm.fake import FakeAdapter
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.services.faq_service import FaqService
from tests.test_hybrid_search import NEAR, make_chunk


@pytest.fixture
async def documents(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    chunk_repo: ChunkRepository,
) -> dict[str, Material]:
    public = Material(title="Для всех", content="x", visibility="tenant")
    secret = Material(title="Только отделу", content="x", visibility="restricted")
    orphan = Material(title="Никому", content="x", visibility="restricted")
    session.add_all([public, secret, orphan])
    await session.flush()
    session.add(MaterialAccess(material_id=secret.id, user_id=employee.id))
    await chunk_repo.bulk_create(
        [
            make_chunk(public, 0, "Отпуск для всех.", NEAR),
            make_chunk(secret, 0, "Отпуск отдела.", NEAR),
            make_chunk(orphan, 0, "Отпуск ничей.", NEAR),
        ]
    )
    await session.commit()
    return {"public": public, "secret": secret, "orphan": orphan}


async def _titles(chunk_repo: ChunkRepository, viewer: UUID) -> set[str]:
    return {m.title for m in await chunk_repo.search(NEAR, limit=10, viewer=viewer)}


async def test_vector_search_respects_material_access(
    chunk_repo: ChunkRepository,
    documents: dict[str, Material],
    admin: User,
    employee: User,
) -> None:
    assert await _titles(chunk_repo, employee.id) == {"Для всех", "Только отделу"}
    assert await _titles(chunk_repo, admin.id) == {"Для всех"}


async def test_fulltext_search_respects_material_access(
    chunk_repo: ChunkRepository,
    documents: dict[str, Material],
    admin: User,
    employee: User,
) -> None:
    async def titles(viewer: UUID) -> set[str]:
        found = await chunk_repo.search_fulltext(
            "отпуск", NEAR, limit=10, viewer=viewer
        )
        return {m.title for m in found}

    assert await titles(employee.id) == {"Для всех", "Только отделу"}
    assert await titles(admin.id) == {"Для всех"}


async def test_answer_and_debug_search_use_the_asking_user(
    faq_service: FaqService,
    fake_llm: FakeAdapter,
    fake_embeddings: FakeEmbeddingAdapter,
    documents: dict[str, Material],
    admin: User,
    employee: User,
) -> None:
    fake_llm.content = "Ответ [1]."
    as_employee = await faq_service.answer("Сколько дней отпуска?", employee)
    assert {s.title for s in as_employee.sources} == {"Для всех", "Только отделу"}
    as_admin = await faq_service.answer("Сколько дней отпуска?", admin)
    assert {s.title for s in as_admin.sources} == {"Для всех"}

    debug = await faq_service.search("отпуск", 10, Retriever.HYBRID, viewer=admin)
    assert {m.title for m in debug} == {"Для всех"}
