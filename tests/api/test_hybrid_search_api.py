"""/faq/search с выбором способа поиска (M1, BH-12)."""

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Material, User
from tests.api.conftest import bearer
from tests.test_hybrid_search import FAR, NEAR, make_chunk


async def test_search_can_compare_retrievers(
    api: httpx.AsyncClient,
    admin_account: User,
    material: Material,
    session: AsyncSession,
) -> None:
    session.add_all(
        [
            make_chunk(material, 0, "Общие положения.", NEAR),
            make_chunk(material, 1, "Отпуск — 28 календарных дней.", FAR),
        ]
    )
    await session.commit()

    async def search(retriever: str | None) -> list[dict[str, object]]:
        body: dict[str, object] = {"question": "Сколько дней отпуска?", "limit": 1}
        if retriever:
            body["retriever"] = retriever
        response = await api.post(
            "/api/v1/faq/search", json=body, headers=bearer(admin_account)
        )
        assert response.status_code == 200
        matches: list[dict[str, object]] = response.json()["matches"]
        return matches

    [vector] = await search(None)  # RAG_RETRIEVER в тестах — vector
    assert vector["content"] == "Общие положения."
    assert vector["fulltext_rank"] is None

    # RRF: у «Отпуск…» второй ранг вектора (1/62) плюс первый ранг
    # полнотекста (0.5/61) — больше, чем первый ранг вектора (1/61).
    [hybrid] = await search("hybrid")
    assert hybrid["content"] == "Отпуск — 28 календарных дней."
    assert hybrid["fulltext_rank"] is not None


async def test_search_rejects_unknown_retriever(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    response = await api.post(
        "/api/v1/faq/search",
        json={"question": "x", "retriever": "bm25"},
        headers=bearer(admin_account),
    )
    assert response.status_code == 422
