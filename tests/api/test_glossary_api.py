"""Словарь сокращений компании (M5, BH-14): API, изоляция, применение."""

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import AuditEvent, GlossaryTerm, Material, Tenant, User
from corp_ed.llm.fake import FakeAdapter
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.llm.types import Role
from corp_ed.services import glossary_service
from tests.api.conftest import bearer
from tests.test_hybrid_search import FAR, make_chunk

URL = "/api/v1/glossary"
DMS = {"term": "ДМС", "expansion": "добровольное медицинское страхование"}


async def _create(
    api: httpx.AsyncClient, user: User, body: dict[str, object] = DMS
) -> httpx.Response:
    return await api.post(URL, json=body, headers=bearer(user))


async def _foreign_term(session: AsyncSession) -> GlossaryTerm:
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        entry = GlossaryTerm(tenant_id=other.id, term="СЭД", expansion="документы")
        session.add(entry)
        await session.commit()
    return entry


# --- CRUD и роли -----------------------------------------------------------------


async def test_admin_manages_glossary(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    created = await _create(api, admin_account)
    assert created.status_code == 201
    term_id = created.json()["id"]

    updated = await api.patch(
        f"{URL}/{term_id}",
        json={"expansion": "добровольное медстрахование"},
        headers=bearer(admin_account),
    )
    assert updated.status_code == 200
    assert updated.json()["term"] == "ДМС"
    assert updated.json()["expansion"] == "добровольное медстрахование"

    listed = await api.get(URL, headers=bearer(admin_account))
    assert [t["term"] for t in listed.json()] == ["ДМС"]

    deleted = await api.delete(f"{URL}/{term_id}", headers=bearer(admin_account))
    assert deleted.status_code == 204
    assert (await api.get(URL, headers=bearer(admin_account))).json() == []


async def test_employee_cannot_touch_glossary(
    api: httpx.AsyncClient, account: User
) -> None:
    assert (await api.get(URL, headers=bearer(account))).status_code == 403
    assert (await _create(api, account)).status_code == 403


async def test_anonymous_cannot_touch_glossary(api: httpx.AsyncClient) -> None:
    assert (await api.get(URL)).status_code == 401
    assert (await api.post(URL, json=DMS)).status_code == 401


async def test_duplicate_term_is_conflict_case_insensitive(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    await _create(api, admin_account)
    response = await _create(api, admin_account, {"term": "дмс", "expansion": "другое"})
    assert response.status_code == 409


async def test_rename_onto_existing_term_is_conflict(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    await _create(api, admin_account)
    other = await _create(api, admin_account, {"term": "СЭД", "expansion": "x" * 5})

    response = await api.patch(
        f"{URL}/{other.json()['id']}",
        json={"term": "Дмс"},
        headers=bearer(admin_account),
    )

    assert response.status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"term": "Д", "expansion": "короткий термин"},
        {"term": "ДМС", "expansion": "x" * 257},
        {"term": "Д" * 65, "expansion": "длинный термин"},
        {"term": "ДМС", "expansion": "две\nстроки"},
        {"term": "ДМ\x00С", "expansion": "NUL"},
        {"term": "   ", "expansion": "пробелы"},
        {"term": "ДМС"},
        {**DMS, "tenant_id": str(uuid4())},
    ],
)
async def test_invalid_terms_are_rejected(
    api: httpx.AsyncClient, admin_account: User, body: dict[str, object]
) -> None:
    assert (await _create(api, admin_account, body)).status_code == 422


async def test_term_count_is_capped(
    api: httpx.AsyncClient,
    admin_account: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(glossary_service, "MAX_TERMS", 1)
    await _create(api, admin_account)
    response = await _create(api, admin_account, {"term": "СЭД", "expansion": "xx"})
    assert response.status_code == 400


async def test_glossary_changes_are_audited(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    term_id = (await _create(api, admin_account)).json()["id"]
    await api.patch(
        f"{URL}/{term_id}",
        json={"expansion": "ДМС-полис"},
        headers=bearer(admin_account),
    )
    await api.delete(f"{URL}/{term_id}", headers=bearer(admin_account))

    events = (
        (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.action.like("glossary.%"))
                .order_by(AuditEvent.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert [e.action for e in events] == [
        "glossary.created",
        "glossary.updated",
        "glossary.deleted",
    ]
    assert events[1].details["previous"]["expansion"] == DMS["expansion"]
    assert all(e.actor_user_id == admin_account.id for e in events)


# --- изоляция ---------------------------------------------------------------------


async def test_other_company_terms_are_invisible(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    foreign = await _foreign_term(session)

    listed = await api.get(URL, headers=bearer(admin_account))
    patched = await api.patch(
        f"{URL}/{foreign.id}",
        json={"expansion": "взлом"},
        headers=bearer(admin_account),
    )
    deleted = await api.delete(f"{URL}/{foreign.id}", headers=bearer(admin_account))

    assert listed.json() == []
    assert patched.status_code == 404
    assert deleted.status_code == 404
    with tenant_scope(foreign.tenant_id):
        entry = (
            await session.execute(
                select(GlossaryTerm).execution_options(populate_existing=True)
            )
        ).scalar_one()
        await session.commit()
    assert entry.expansion == "документы"


async def test_same_term_allowed_in_different_companies(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    await _foreign_term(session)
    response = await _create(api, admin_account, {"term": "сэд", "expansion": "xx"})
    assert response.status_code == 201


# --- применение к вопросу ------------------------------------------------------------


async def test_expansion_goes_to_search_not_to_model(
    api: httpx.AsyncClient,
    admin_account: User,
    account: User,
    fake_embeddings: FakeEmbeddingAdapter,
    fake_llm: FakeAdapter,
) -> None:
    """Расшифровка — только в поиск. В промпт уходит вопрос сотрудника:
    текст, который пишет админ, не становится инструкцией модели."""
    await _create(api, admin_account)

    await api.post(
        "/api/v1/faq/ask",
        json={"question": "Как оформить ДМС?"},
        headers=bearer(account),
    )

    assert fake_embeddings.query_calls == [
        "Как оформить ДМС? (ДМС — добровольное медицинское страхование)"
    ]
    user_message = next(m for m in fake_llm.calls[0] if m.role is Role.USER)
    assert "Как оформить ДМС?" in user_message.content
    assert "добровольное" not in user_message.content


async def test_expansion_lets_fulltext_find_the_long_form(
    api: httpx.AsyncClient,
    admin_account: User,
    material: Material,
    session: AsyncSession,
) -> None:
    session.add(
        make_chunk(material, 0, "Полис медицинского страхования выдаёт HR.", FAR)
    )
    await session.commit()

    async def search() -> list[dict[str, object]]:
        response = await api.post(
            "/api/v1/faq/search",
            json={"question": "Где взять ДМС?", "retriever": "hybrid"},
            headers=bearer(admin_account),
        )
        matches: list[dict[str, object]] = response.json()["matches"]
        return matches

    before = await search()
    await _create(api, admin_account)
    after = await search()

    assert all(m["fulltext_rank"] is None for m in before)
    assert after[0]["fulltext_rank"] is not None


async def test_other_company_glossary_is_not_applied(
    api: httpx.AsyncClient,
    account: User,
    session: AsyncSession,
    fake_embeddings: FakeEmbeddingAdapter,
) -> None:
    await _foreign_term(session)

    await api.post(
        "/api/v1/faq/ask", json={"question": "Где СЭД?"}, headers=bearer(account)
    )

    assert fake_embeddings.query_calls == ["Где СЭД?"]
