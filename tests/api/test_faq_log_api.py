"""Журнал ответов (BH-20), оценки, отладка поиска (BH-5), сроки хранения."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.config import EMBEDDING_DIM
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import AuditEvent, Chunk, Material, QaLog, Tenant, User
from corp_ed.prompts.faq import PROMPT_VERSION
from corp_ed.services.retention_service import RetentionService
from tests.api.conftest import bearer


async def _chunk(
    session: AsyncSession, material: Material, *, far: bool = False
) -> Chunk:
    vector = [0.1] * EMBEDDING_DIM
    if far:
        vector = [-0.1] * (EMBEDDING_DIM // 2) + [0.1] * (EMBEDDING_DIM // 2)
    chunk = Chunk(
        material_id=material.id,
        position=1 if far else 0,
        content="Далёкий." if far else "Отпуск — 28 дней.",
        embedding=vector,
        model="m",
        model_version="v",
    )
    session.add(chunk)
    await session.commit()
    return chunk


async def _log(session: AsyncSession, tenant_id: UUID) -> list[QaLog]:
    with tenant_scope(tenant_id):
        rows = (
            await session.execute(
                select(QaLog).execution_options(populate_existing=True)
            )
        ).scalars()
        result = list(rows)
        await session.commit()
    return result


async def _ask(api: httpx.AsyncClient, user: User, question: str) -> httpx.Response:
    return await api.post(
        "/api/v1/faq/ask", json={"question": question}, headers=bearer(user)
    )


# --- журнал ------------------------------------------------------------------


async def test_answer_is_logged_with_masked_question(
    api: httpx.AsyncClient, account: User, material: Material, session: AsyncSession
) -> None:
    await _chunk(session, material)

    response = await _ask(
        api, account, "Иван Петров, ivan@corp.ru, +7 915 123-45-67 — сколько отпуска?"
    )

    assert response.status_code == 200
    [entry] = await _log(session, account.tenant_id)
    assert str(entry.id) == response.json()["answer_id"]
    assert entry.user_id == account.id
    assert entry.prompt_version == PROMPT_VERSION
    assert entry.origin == "documents"
    assert entry.answer_given is True
    assert entry.credits >= 1
    assert len(entry.source_chunk_ids) == 1
    # Персональные данные в журнал не попадают (152-ФЗ).
    assert "ivan@corp.ru" not in entry.question
    assert "915" not in entry.question
    assert "Петров" not in entry.question


async def test_general_answer_is_logged_as_not_given(
    api: httpx.AsyncClient, account: User, tenant_ctx: Tenant, session: AsyncSession
) -> None:
    await _ask(api, account, "Как настроить VPN?")

    [entry] = await _log(session, account.tenant_id)
    assert entry.origin == "general_knowledge"
    assert entry.answer_given is False
    assert entry.best_vector_distance is None
    assert entry.source_chunk_ids == []


async def test_diagnostics_only_for_admin(
    api: httpx.AsyncClient, account: User, admin_account: User, material: Material
) -> None:
    employee_answer = await _ask(api, account, "Вопрос?")
    admin_answer = await _ask(api, admin_account, "Вопрос?")

    assert employee_answer.json()["diagnostics"] is None
    diagnostics = admin_answer.json()["diagnostics"]
    assert diagnostics["prompt_version"] == PROMPT_VERSION
    assert diagnostics["model"] == "fake"


# --- оценки ------------------------------------------------------------------


async def test_user_rates_own_answer(
    api: httpx.AsyncClient, account: User, tenant_ctx: Tenant, session: AsyncSession
) -> None:
    answer_id = (await _ask(api, account, "Вопрос?")).json()["answer_id"]

    response = await api.patch(
        f"/api/v1/faq/answers/{answer_id}", json={"value": -1}, headers=bearer(account)
    )

    assert response.status_code == 204
    [entry] = await _log(session, account.tenant_id)
    assert entry.feedback == -1


async def test_cannot_rate_someone_elses_answer(
    api: httpx.AsyncClient, account: User, admin_account: User
) -> None:
    answer_id = (await _ask(api, admin_account, "Вопрос?")).json()["answer_id"]

    response = await api.patch(
        f"/api/v1/faq/answers/{answer_id}", json={"value": 1}, headers=bearer(account)
    )

    assert response.status_code == 404


async def test_rating_value_is_validated(
    api: httpx.AsyncClient, account: User, tenant_ctx: Tenant
) -> None:
    answer_id = (await _ask(api, account, "Вопрос?")).json()["answer_id"]

    for value in (0, 5, "yes"):
        response = await api.patch(
            f"/api/v1/faq/answers/{answer_id}",
            json={"value": value},
            headers=bearer(account),
        )
        assert response.status_code == 422


async def test_rating_unknown_answer_is_404(
    api: httpx.AsyncClient, account: User
) -> None:
    response = await api.patch(
        f"/api/v1/faq/answers/{uuid4()}", json={"value": 1}, headers=bearer(account)
    )
    assert response.status_code == 404


# --- отладка поиска (BH-5) ----------------------------------------------------


async def test_search_returns_matches_without_threshold(
    api: httpx.AsyncClient,
    admin_account: User,
    material: Material,
    session: AsyncSession,
) -> None:
    near = await _chunk(session, material)
    far = await _chunk(session, material, far=True)

    response = await api.post(
        "/api/v1/faq/search",
        json={"question": "Сколько отпуска?", "limit": 10},
        headers=bearer(admin_account),
    )

    assert response.status_code == 200
    matches = response.json()["matches"]
    assert [m["chunk_id"] for m in matches] == [str(near.id), str(far.id)]
    # Далёкий чанк (дальше порога 0.6) тоже здесь — для подбора порога.
    assert matches[1]["distance"] > 0.6
    assert set(matches[0]) == {
        "chunk_id",
        "material_id",
        "material_title",
        "position",
        "heading_path",
        "content",
        "distance",
    }


async def test_search_is_admin_only(api: httpx.AsyncClient, account: User) -> None:
    response = await api.post(
        "/api/v1/faq/search", json={"question": "x"}, headers=bearer(account)
    )
    assert response.status_code == 403


async def test_search_limit_is_bounded(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    response = await api.post(
        "/api/v1/faq/search",
        json={"question": "x", "limit": 500},
        headers=bearer(admin_account),
    )
    assert response.status_code == 422


async def test_search_does_not_see_other_company(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        foreign = Material(tenant_id=other.id, title="Чужой", content="x")
        session.add(foreign)
        await session.commit()
        await _chunk(session, foreign)

    response = await api.post(
        "/api/v1/faq/search", json={"question": "x"}, headers=bearer(admin_account)
    )

    assert response.json()["matches"] == []


# --- сроки хранения -----------------------------------------------------------


async def test_purge_removes_only_expired_records(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    account: User,
) -> None:
    now = datetime.now(UTC)
    with tenant_scope(account.tenant_id):
        for age_days in (1, 100):
            session.add(
                QaLog(
                    tenant_id=account.tenant_id,
                    user_id=account.id,
                    question="q",
                    question_embedding=[0.1] * EMBEDDING_DIM,
                    embedding_model="m",
                    prompt_version="p",
                    llm_model="m",
                    answer_given=True,
                    origin="documents",
                    created_at=now - timedelta(days=age_days),
                )
            )
        await session.commit()
    session.add_all(
        [
            AuditEvent(
                tenant_id=account.tenant_id,
                action="old",
                created_at=now - timedelta(days=400),
            ),
            AuditEvent(tenant_id=account.tenant_id, action="new"),
        ]
    )
    await session.commit()

    report = await RetentionService(session_maker, qa_log_days=90).purge()

    assert (report.qa_log, report.audit_events) == (1, 1)
    assert len(await _log(session, account.tenant_id)) == 1
    actions = (await session.execute(select(AuditEvent.action))).scalars().all()
    assert "new" in actions and "old" not in actions
