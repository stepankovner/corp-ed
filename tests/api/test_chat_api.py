"""Чат на сервере (ТЗ §6): диалоги, поток ответа, «Остановить», версии,
оценки с комментарием, «поделиться», вложения, подсказки, анонимность.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.api.v1.dependencies import get_llm_gateway
from corp_ed.core.config import EMBEDDING_DIM
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    ChatAttachment,
    ChatMessage,
    Chunk,
    Department,
    Folder,
    FolderDepartment,
    Material,
    MaterialAccess,
    QaLog,
    Tenant,
    User,
    UserRole,
)
from corp_ed.domain.types import NotFoundMode
from corp_ed.llm.fake import FakeAdapter
from corp_ed.main import app
from corp_ed.prompts.faq import GENERAL_ANSWER_PREFIX, NOT_FOUND_ANSWER
from corp_ed.services.chat_service import HIDDEN_ANSWER
from corp_ed.services.retention_service import RetentionService
from tests.api.conftest import bearer
from tests.api.test_faq_api import BusyLLM, FailingLLM
from tests.factories import make_user

BASE = "/api/v1/conversations"


def events_of(response: httpx.Response) -> list[dict[str, Any]]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    return [
        json.loads(line[len("data: ") :])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def final(events: list[dict[str, Any]]) -> dict[str, Any]:
    assert events[0]["type"] == "start"
    assert events[-1]["type"] in ("done", "error")
    return events[-1]


async def _document(session: AsyncSession, tenant: Tenant, **fields: Any) -> Material:
    material = Material(
        id=uuid4(),
        tenant_id=tenant.id,
        title=fields.pop("title", "Положение об отпусках"),
        content="Отпуск — 28 дней.",
        **fields,
    )
    session.add(material)
    await session.flush()
    session.add(
        Chunk(
            material_id=material.id,
            position=0,
            content="Ежегодный отпуск — 28 календарных дней.",
            embedding=[0.1] * EMBEDDING_DIM,
            model="fake",
            model_version="fake",
        )
    )
    await session.commit()
    return material


@pytest.fixture
async def colleague(session: AsyncSession, tenant_ctx: Tenant) -> User:
    user = make_user(
        tenant_id=tenant_ctx.id,
        email="colleague@test.com",
        full_name="Пётр Коллегин",
        role=UserRole.EMPLOYEE,
    )
    session.add(user)
    await session.commit()
    return user


async def _ask(
    api: httpx.AsyncClient, user: User, question: str, **body: Any
) -> list[dict[str, Any]]:
    conversation_id = body.pop("conversation_id", None)
    path = f"{BASE}/{conversation_id}/messages" if conversation_id else BASE
    if conversation_id is not None:
        body.setdefault("parent_id", None)
    response = await api.post(
        path, json={"question": question, **body}, headers=bearer(user)
    )
    return events_of(response)


# --- поток и сохранение ---------------------------------------------------------


async def test_new_conversation_streams_answer_and_keeps_it(
    api: httpx.AsyncClient,
    employee: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
    fake_llm: FakeAdapter,
) -> None:
    await _document(session, tenant_ctx)
    fake_llm.content = "Отпуск — 28 дней [1]."
    fake_llm.pieces = ["Отпуск ", "— 28 дней", " [1]."]

    events = await _ask(api, employee, "Сколько дней отпуска у сотрудника?")

    start = events[0]
    assert start["conversation"]["title"] == "Сколько дней отпуска у сотрудника?"
    assert start["question"]["role"] == "user"
    assert start["answer"]["status"] == "generating"
    assert [e["stage"] for e in events if e["type"] == "stage"] == [
        "searching",
        "writing",
    ]
    deltas = "".join(e["text"] for e in events if e["type"] == "delta")
    assert deltas == "Отпуск — 28 дней [1]."
    done = final(events)
    assert done["type"] == "done"
    assert done["answer"]["status"] == "complete"
    assert done["answer"]["origin"] == "documents"
    assert done["answer"]["content"] == "Отпуск — 28 дней [1]."
    assert done["answer"]["sources"][0]["kind"] == "document"
    # Модель и токены — только администратору.
    assert done["diagnostics"] is None

    conversation_id = start["conversation"]["id"]
    listed = await api.get(BASE, headers=bearer(employee))
    assert [c["id"] for c in listed.json()["items"]] == [conversation_id]

    opened = (
        await api.get(f"{BASE}/{conversation_id}", headers=bearer(employee))
    ).json()
    assert [m["role"] for m in opened["messages"]] == ["user", "assistant"]
    answer = opened["messages"][1]
    assert answer["content"] == "Отпуск — 28 дней [1]."
    assert answer["sources"][0]["content"].startswith("Ежегодный отпуск")
    assert opened["current_message_id"] == answer["id"]

    # Журнал — обезличенная статистика, как у /faq/ask.
    log = (await session.scalars(select(QaLog))).one()
    assert log.conversation_id == UUID(conversation_id)
    assert log.answer_given is True


async def test_admin_gets_diagnostics_in_done(
    api: httpx.AsyncClient, admin: User, tenant_ctx: Tenant, session: AsyncSession
) -> None:
    await _document(session, tenant_ctx)
    done = final(await _ask(api, admin, "Сколько дней отпуска?"))
    assert done["diagnostics"]["prompt_version"]


async def test_follow_up_question_gets_history_from_the_conversation(
    api: httpx.AsyncClient,
    employee: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
) -> None:
    await _document(session, tenant_ctx)
    first = await _ask(api, employee, "Сколько дней отпуска?")
    conversation_id = first[0]["conversation"]["id"]
    second = await _ask(
        api,
        employee,
        "А для совместителей?",
        conversation_id=conversation_id,
        parent_id=final(first)["answer"]["id"],
    )
    assert final(second)["type"] == "done"

    logs = (await session.scalars(select(QaLog).order_by(QaLog.created_at))).all()
    assert [log.history_turns for log in logs] == [0, 1]
    assert logs[1].standalone_question is not None


async def test_answer_from_a_closed_folder_is_hidden_after_access_is_lost(
    api: httpx.AsyncClient,
    employee: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
) -> None:
    """Сотрудника убрали из отдела: в его прежнем диалоге ответ по закрытой
    папке отдела больше не показывается и в модель с историей не уходит.
    Ответ по удалённому документу так не скрывается — это не снятие прав."""
    department = Department(tenant_id=tenant_ctx.id, name="Бухгалтерия")
    folder = Folder(tenant_id=tenant_ctx.id, name="Закрытая", restricted=True)
    session.add_all([department, folder])
    await session.flush()
    session.add(
        FolderDepartment(
            tenant_id=tenant_ctx.id, folder_id=folder.id, department_id=department.id
        )
    )
    employee.department_id = department.id
    employee.department_confirmed = True
    await session.commit()
    employee_id = employee.id
    await _document(session, tenant_ctx, folder_id=folder.id)

    first = await _ask(api, employee, "Сколько дней отпуска?")
    conversation_id = first[0]["conversation"]["id"]
    answer = final(first)["answer"]
    assert answer["sources"][0]["content"]
    assert answer["content"] != HIDDEN_ANSWER

    with tenant_scope(tenant_ctx.id):
        member = await session.get(User, employee_id)
        assert member is not None
        member.department_id = None
        member.department_confirmed = False
        await session.commit()

    view = await api.get(f"{BASE}/{conversation_id}", headers=bearer(employee))
    assert view.status_code == 200, view.text
    shown = view.json()["messages"][-1]
    assert shown["content"] == HIDDEN_ANSWER
    assert shown["sources"][0]["content"] is None

    follow = await _ask(
        api,
        employee,
        "А для совместителей?",
        conversation_id=conversation_id,
        parent_id=answer["id"],
    )
    assert final(follow)["type"] == "done"
    logs = (await session.scalars(select(QaLog).order_by(QaLog.created_at))).all()
    assert [log.history_turns for log in logs] == [0, 0]


async def test_general_answer_streams_without_service_prefix(
    api: httpx.AsyncClient, employee: User, fake_llm: FakeAdapter
) -> None:
    fake_llm.content = (
        f"{GENERAL_ANSWER_PREFIX}\nОбычно ежегодный отпуск составляет "
        "28 календарных дней, но правила компании могут отличаться. "
        "Проверьте внутренние документы."
    )
    events = await _ask(api, employee, "Сколько дней отпуска?")

    assert {"type": "origin", "origin": "general_knowledge"} in events
    streamed = "".join(e["text"] for e in events if e["type"] == "delta")
    assert streamed.startswith("Обычно ежегодный отпуск")
    assert NOT_FOUND_ANSWER not in streamed
    done = final(events)
    assert done["answer"]["origin"] == "general_knowledge"
    assert done["answer"]["content"].startswith(GENERAL_ANSWER_PREFIX)


async def test_model_refusal_is_not_shown_while_streaming(
    api: httpx.AsyncClient,
    employee: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
    fake_llm: FakeAdapter,
) -> None:
    await _document(session, tenant_ctx)
    fake_llm.content = NOT_FOUND_ANSWER
    fake_llm.pieces = ["В документах ", "компании ", "ответа нет."]

    events = await _ask(api, employee, "Какая погода завтра?")

    assert not [e for e in events if e["type"] == "delta" and "ответа нет" in e["text"]]
    assert final(events)["answer"]["origin"] == "general_knowledge"


async def test_strict_mode_refusal(
    api: httpx.AsyncClient,
    employee: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
) -> None:
    tenant_ctx.not_found_mode = NotFoundMode.STRICT.value
    await session.commit()
    events = await _ask(api, employee, "Какая погода завтра?")
    assert {"type": "origin", "origin": "none"} in events
    done = final(events)
    assert done["answer"]["origin"] == "none"
    assert done["answer"]["content"].startswith(NOT_FOUND_ANSWER)


async def test_model_failure_keeps_failed_answer_and_regenerate_works(
    api: httpx.AsyncClient,
    employee: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
    fake_llm: FakeAdapter,
) -> None:
    await _document(session, tenant_ctx)
    app.dependency_overrides[get_llm_gateway] = lambda: FailingLLM()
    events = await _ask(api, employee, "Сколько дней отпуска?")
    error = final(events)
    assert error["type"] == "error"
    assert error["code"] == "llm_unavailable"
    assert error["answer"]["status"] == "failed"

    app.dependency_overrides[get_llm_gateway] = lambda: fake_llm
    conversation_id = events[0]["conversation"]["id"]
    question_id = events[0]["question"]["id"]
    response = await api.post(
        f"{BASE}/{conversation_id}/messages/{question_id}/regenerate",
        headers=bearer(employee),
    )
    again = events_of(response)
    assert final(again)["answer"]["status"] == "complete"
    assert again[0]["answer"]["siblings"] == [
        error["answer"]["id"],
        again[0]["answer"]["id"],
    ]


async def test_overloaded_quota_is_busy_not_model_failure(
    api: httpx.AsyncClient,
    employee: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
) -> None:
    await _document(session, tenant_ctx)
    app.dependency_overrides[get_llm_gateway] = lambda: BusyLLM()
    error = final(await _ask(api, employee, "Сколько дней отпуска?"))
    assert error["type"] == "error"
    assert error["code"] == "busy"
    assert "много вопросов" in error["message"]
    assert error["answer"]["error_code"] == "busy"


async def test_question_limit_is_4000_characters(
    api: httpx.AsyncClient, employee: User
) -> None:
    too_long = await api.post(
        BASE, json={"question": "я" * 4001}, headers=bearer(employee)
    )
    assert too_long.status_code == 422
    assert final(await _ask(api, employee, "я" * 4000))["type"] == "done"


# --- остановка ------------------------------------------------------------------


async def test_stop_keeps_what_was_written(
    api: httpx.AsyncClient,
    employee: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
    fake_llm: FakeAdapter,
) -> None:
    await _document(session, tenant_ctx)
    fake_llm.content = "Первая часть. Вторая часть."
    fake_llm.pieces = ["Первая часть. ", "Вторая часть."]
    second_piece = asyncio.Event()
    release = asyncio.Event()

    async def before_piece(index: int) -> None:
        if index == 1:
            second_piece.set()
            await release.wait()

    fake_llm.before_piece = before_piece
    request = asyncio.create_task(
        api.post(
            BASE, json={"question": "Расскажите про отпуск"}, headers=bearer(employee)
        )
    )
    await asyncio.wait_for(second_piece.wait(), timeout=10)

    reader = async_sessionmaker(session.bind, expire_on_commit=False)
    with tenant_scope(tenant_ctx.id):
        async with reader() as fresh:
            answer = (
                await fresh.scalars(
                    select(ChatMessage).where(ChatMessage.status == "generating")
                )
            ).one()
    stopped = await api.post(
        f"{BASE}/{answer.conversation_id}/messages/{answer.id}/stop",
        headers=bearer(employee),
    )
    assert stopped.status_code == 204
    release.set()

    done = final(events_of(await request))
    assert done["answer"]["status"] == "stopped"
    assert done["answer"]["content"] == "Первая часть."
    log = (await session.scalars(select(QaLog))).one()
    assert log.output_tokens > 0  # остановленный ответ тоже стоит токенов

    # Уже готовый ответ остановить нечего — это не ошибка.
    again = await api.post(
        f"{BASE}/{answer.conversation_id}/messages/{answer.id}/stop",
        headers=bearer(employee),
    )
    assert again.status_code == 204


async def test_answer_in_progress_blocks_next_question_until_stale(
    api: httpx.AsyncClient, employee: User, session: AsyncSession
) -> None:
    events = await _ask(api, employee, "Первый вопрос")
    conversation_id = UUID(events[0]["conversation"]["id"])
    answer_id = UUID(final(events)["answer"]["id"])
    answer = await session.get(ChatMessage, answer_id)
    assert answer is not None
    answer.status = "generating"
    await session.commit()

    blocked = await api.post(
        f"{BASE}/{conversation_id}/messages",
        json={"question": "Второй", "parent_id": str(answer_id)},
        headers=bearer(employee),
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "answer_in_progress"

    # Задача умерла с перезапуском API: через 10 минут ответ — «прерван».
    answer.created_at = datetime.now(UTC) - timedelta(minutes=11)
    await session.commit()
    opened = await api.get(f"{BASE}/{conversation_id}", headers=bearer(employee))
    assert opened.json()["messages"][1]["status"] == "failed"
    assert opened.json()["messages"][1]["error_code"] == "interrupted"


# --- правка вопроса и версии ---------------------------------------------------------


async def test_edit_question_and_regenerate_create_versions(
    api: httpx.AsyncClient, employee: User
) -> None:
    first = await _ask(api, employee, "Вопрос один")
    conversation_id = first[0]["conversation"]["id"]
    answer_1 = final(first)["answer"]["id"]
    second = await _ask(
        api, employee, "Вопрос два", conversation_id=conversation_id, parent_id=answer_1
    )
    question_2 = second[0]["question"]["id"]

    edited = await _ask(
        api,
        employee,
        "Вопрос два, исправленный",
        conversation_id=conversation_id,
        parent_id=answer_1,
    )
    assert edited[0]["question"]["siblings"] == [
        question_2,
        edited[0]["question"]["id"],
    ]

    headers = bearer(employee)
    opened = (await api.get(f"{BASE}/{conversation_id}", headers=headers)).json()
    assert [m["content"] for m in opened["messages"] if m["role"] == "user"] == [
        "Вопрос один",
        "Вопрос два, исправленный",
    ]

    switched = await api.put(
        f"{BASE}/{conversation_id}/current",
        json={"message_id": question_2},
        headers=headers,
    )
    assert [
        m["content"] for m in switched.json()["messages"] if m["role"] == "user"
    ] == [
        "Вопрос один",
        "Вопрос два",
    ]
    assert switched.json()["current_message_id"] == final(second)["answer"]["id"]

    # Правка первого вопроса — новая ветка от корня.
    root = await _ask(
        api, employee, "Вопрос один, иначе", conversation_id=conversation_id
    )
    assert len(root[0]["question"]["siblings"]) == 2


async def test_regenerate_needs_own_question(
    api: httpx.AsyncClient, employee: User
) -> None:
    events = await _ask(api, employee, "Вопрос")
    conversation_id = events[0]["conversation"]["id"]
    answer_id = final(events)["answer"]["id"]
    response = await api.post(
        f"{BASE}/{conversation_id}/messages/{answer_id}/regenerate",
        headers=bearer(employee),
    )
    assert response.status_code == 404


# --- список: название, закрепление, поиск, удаление -----------------------------------


async def test_rename_pin_search_and_delete(
    api: httpx.AsyncClient, employee: User
) -> None:
    headers = bearer(employee)
    vacation = (await _ask(api, employee, "Сколько дней отпуска?"))[0]["conversation"]
    sick = (await _ask(api, employee, "Как оформить больничный?"))[0]["conversation"]

    listed = (await api.get(BASE, headers=headers)).json()["items"]
    assert [c["id"] for c in listed] == [sick["id"], vacation["id"]]

    renamed = await api.patch(
        f"{BASE}/{vacation['id']}",
        json={"title": "  Отпуск   2026 ", "pinned": True},
        headers=headers,
    )
    assert renamed.json()["title"] == "Отпуск 2026"
    assert renamed.json()["pinned"] is True
    listed = (await api.get(BASE, headers=headers)).json()["items"]
    assert [c["id"] for c in listed] == [vacation["id"], sick["id"]]

    found = (await api.get(BASE, params={"q": "больнич"}, headers=headers)).json()
    assert [c["id"] for c in found["items"]] == [sick["id"]]
    # Поиск — и по тексту ответов; % в запросе — буква, а не шаблон.
    assert (await api.get(BASE, params={"q": "%"}, headers=headers)).json()[
        "items"
    ] == []

    assert (
        await api.delete(f"{BASE}/{sick['id']}", headers=headers)
    ).status_code == 204
    assert (await api.get(f"{BASE}/{sick['id']}", headers=headers)).status_code == 404


async def test_list_pagination(api: httpx.AsyncClient, employee: User) -> None:
    for n in range(3):
        await _ask(api, employee, f"Вопрос {n}")
    headers = bearer(employee)
    first = (await api.get(BASE, params={"limit": 2}, headers=headers)).json()
    assert len(first["items"]) == 2 and first["next_before"]
    rest = (
        await api.get(
            BASE, params={"limit": 2, "before": first["next_before"]}, headers=headers
        )
    ).json()
    assert [c["title"] for c in rest["items"]] == ["Вопрос 0"]
    assert rest["next_before"] is None


# --- анонимность и чужие диалоги ------------------------------------------------------


async def test_nobody_else_sees_a_conversation_not_even_admin(
    api: httpx.AsyncClient, employee: User, colleague: User, admin: User
) -> None:
    events = await _ask(api, employee, "Личный вопрос про отпуск")
    conversation_id = events[0]["conversation"]["id"]
    answer_id = final(events)["answer"]["id"]

    for stranger in (colleague, admin):
        headers = bearer(stranger)
        assert (await api.get(BASE, headers=headers)).json()["items"] == []
        for method, path, body in (
            ("GET", f"{BASE}/{conversation_id}", None),
            ("PATCH", f"{BASE}/{conversation_id}", {"title": "x"}),
            ("DELETE", f"{BASE}/{conversation_id}", None),
            ("POST", f"{BASE}/{conversation_id}/share", None),
            ("POST", f"{BASE}/{conversation_id}/messages/{answer_id}/stop", None),
            (
                "PUT",
                f"{BASE}/{conversation_id}/messages/{answer_id}/feedback",
                {"value": 1},
            ),
            ("PUT", f"{BASE}/{conversation_id}/current", {"message_id": answer_id}),
            (
                "POST",
                f"{BASE}/{conversation_id}/messages",
                {"question": "Чужой?", "parent_id": answer_id},
            ),
        ):
            response = await api.request(method, path, json=body, headers=headers)
            assert response.status_code == 404, (stranger.email, method, path)


# --- оценка с комментарием ------------------------------------------------------------


async def test_feedback_with_reason_and_comment_is_masked_in_log(
    api: httpx.AsyncClient, employee: User, session: AsyncSession
) -> None:
    events = await _ask(api, employee, "Сколько дней отпуска?")
    conversation_id = events[0]["conversation"]["id"]
    answer_id = final(events)["answer"]["id"]
    headers = bearer(employee)
    path = f"{BASE}/{conversation_id}/messages/{answer_id}/feedback"

    response = await api.put(
        path,
        json={
            "value": -1,
            "reason": "outdated",
            "comment": "Устарело, звоните 8 999 123-45-67",
        },
        headers=headers,
    )
    assert response.status_code == 204
    answer = (await api.get(f"{BASE}/{conversation_id}", headers=headers)).json()[
        "messages"
    ][1]
    assert (answer["feedback"], answer["feedback_reason"]) == (-1, "outdated")
    assert answer["feedback_comment"] == "Устарело, звоните 8 999 123-45-67"

    log = (await session.scalars(select(QaLog))).one()
    await session.refresh(log)
    assert (log.feedback, log.feedback_reason) == (-1, "outdated")
    assert log.feedback_comment is not None
    assert "123-45-67" not in log.feedback_comment

    wrong = await api.put(
        path, json={"value": 1, "reason": "outdated"}, headers=headers
    )
    assert wrong.status_code == 409
    cleared = await api.put(path, json={"value": None}, headers=headers)
    assert cleared.status_code == 204
    await session.refresh(log)
    assert (log.feedback, log.feedback_reason, log.feedback_comment) == (
        None,
        None,
        None,
    )


# --- поделиться -----------------------------------------------------------------------


async def test_share_is_a_snapshot_for_colleagues_only(
    api: httpx.AsyncClient,
    employee: User,
    colleague: User,
    session: AsyncSession,
) -> None:
    events = await _ask(api, employee, "Сколько дней отпуска?")
    conversation_id = events[0]["conversation"]["id"]
    owner = bearer(employee)
    await api.put(
        f"{BASE}/{conversation_id}/messages/{final(events)['answer']['id']}/feedback",
        json={"value": -1, "comment": "личное"},
        headers=owner,
    )
    shared = (await api.post(f"{BASE}/{conversation_id}/share", headers=owner)).json()
    token = shared["token"]
    assert len(token) >= 24

    view = await api.get(f"{BASE}/shared/{token}", headers=bearer(colleague))
    assert view.status_code == 200
    body = view.json()
    assert body["title"] == "Сколько дней отпуска?"
    assert len(body["messages"]) == 2
    # Оценки и комментарии автора — не для коллег.
    assert body["messages"][1]["feedback"] is None
    assert body["messages"][1]["feedback_comment"] is None

    # Новые сообщения в ссылку не попадают, пока автор не обновит её.
    await _ask(
        api,
        employee,
        "А в 2027 году?",
        conversation_id=conversation_id,
        parent_id=final(events)["answer"]["id"],
    )
    assert (
        len(
            (await api.get(f"{BASE}/shared/{token}", headers=bearer(colleague))).json()[
                "messages"
            ]
        )
        == 2
    )
    again = (await api.post(f"{BASE}/{conversation_id}/share", headers=owner)).json()
    assert again["token"] == token
    assert (
        len(
            (await api.get(f"{BASE}/shared/{token}", headers=bearer(colleague))).json()[
                "messages"
            ]
        )
        == 4
    )

    # Другая компания ссылку не откроет.
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        stranger = make_user(email="stranger@other.com")
        session.add(stranger)
        await session.commit()
    assert (
        await api.get(f"{BASE}/shared/{token}", headers=bearer(stranger))
    ).status_code == 404

    assert (
        await api.delete(f"{BASE}/{conversation_id}/share", headers=owner)
    ).status_code == 204
    assert (
        await api.get(f"{BASE}/shared/{token}", headers=bearer(colleague))
    ).status_code == 404


async def test_shared_sources_follow_viewer_access(
    api: httpx.AsyncClient,
    employee: User,
    colleague: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
) -> None:
    material = await _document(
        session, tenant_ctx, title="Зарплаты отдела", visibility="restricted"
    )
    session.add(MaterialAccess(material_id=material.id, user_id=employee.id))
    await session.commit()

    events = await _ask(api, employee, "Сколько дней отпуска?")
    assert final(events)["answer"]["sources"][0]["content"]
    conversation_id = events[0]["conversation"]["id"]
    token = (
        await api.post(f"{BASE}/{conversation_id}/share", headers=bearer(employee))
    ).json()["token"]

    source = (
        await api.get(f"{BASE}/shared/{token}", headers=bearer(colleague))
    ).json()["messages"][1]["sources"][0]
    assert source["title"] == "Зарплаты отдела"
    assert source["content"] is None
    own = (await api.get(f"{BASE}/{conversation_id}", headers=bearer(employee))).json()[
        "messages"
    ][1]["sources"][0]
    assert own["content"]


# --- вложения -------------------------------------------------------------------------


async def _upload(
    api: httpx.AsyncClient, user: User, text: str, name: str = "договор.txt"
) -> httpx.Response:
    return await api.post(
        "/api/v1/attachments",
        files={"file": (name, text.encode(), "text/plain")},
        headers=bearer(user),
    )


async def test_attachment_goes_into_prompt_but_not_into_company_base(
    api: httpx.AsyncClient,
    employee: User,
    colleague: User,
    session: AsyncSession,
    fake_llm: FakeAdapter,
) -> None:
    uploaded = await _upload(api, employee, "Срок аренды — 11 месяцев.")
    assert uploaded.status_code == 201, uploaded.text
    attachment_id = uploaded.json()["id"]
    assert uploaded.json()["filename"] == "договор.txt"

    # Чужое вложение к своему вопросу не приложить.
    stolen = await api.post(
        BASE,
        json={"question": "Что в файле?", "attachment_ids": [attachment_id]},
        headers=bearer(colleague),
    )
    assert stolen.status_code == 404

    events = await _ask(
        api, employee, "Какой срок аренды?", attachment_ids=[attachment_id]
    )
    done = final(events)
    prompt = fake_llm.calls[-1][-1].content
    assert "Срок аренды —" in prompt and "11 месяцев." in prompt
    assert done["answer"]["origin"] == "documents"
    assert done["answer"]["sources"][0]["kind"] == "attachment"
    assert done["answer"]["sources"][0]["title"] == "договор.txt"
    assert events[0]["question"]["attachments"][0]["id"] == attachment_id

    log = (await session.scalars(select(QaLog))).one()
    assert log.attachment_chunks >= 1
    assert log.source_chunk_ids == []
    assert (await session.scalars(select(Material))).all() == []

    # Отправленное вложение живёт с диалогом, отдельно не удаляется.
    gone = await api.delete(
        f"/api/v1/attachments/{attachment_id}", headers=bearer(employee)
    )
    assert gone.status_code == 404
    conversation_id = events[0]["conversation"]["id"]
    await api.delete(f"{BASE}/{conversation_id}", headers=bearer(employee))
    assert (await session.scalars(select(ChatAttachment))).all() == []


async def test_pending_attachment_can_be_removed_and_is_purged(
    api: httpx.AsyncClient,
    employee: User,
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    first = (await _upload(api, employee, "Первый файл")).json()["id"]
    second = (await _upload(api, employee, "Второй файл")).json()["id"]
    removed = await api.delete(f"/api/v1/attachments/{first}", headers=bearer(employee))
    assert removed.status_code == 204

    stale = await session.get(ChatAttachment, UUID(second))
    assert stale is not None
    stale.created_at = datetime.now(UTC) - timedelta(days=2)
    await session.commit()
    report = await RetentionService(session_maker, qa_log_days=365).purge()
    assert report.attachments == 1


async def test_attachment_rejections(api: httpx.AsyncClient, employee: User) -> None:
    empty = await _upload(api, employee, "   ")
    assert empty.status_code == 422
    huge = await _upload(
        api,
        employee,
        "\n\n".join(
            f"Пункт {i}. Арендатор вносит платёж номер {i} до {i % 28 + 1} числа."
            for i in range(200)
        ),
    )
    assert huge.status_code == 422
    assert huge.json()["code"] == "attachment_too_large"
    exe = await _upload(api, employee, "MZ\x90\x00", name="virus.exe")
    assert exe.status_code == 415


# --- подсказки ------------------------------------------------------------------------


async def test_admin_suggestions_and_frequent_questions(
    api: httpx.AsyncClient,
    admin: User,
    employee: User,
    colleague: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
) -> None:
    headers = bearer(admin)
    created = [
        (
            await api.post("/api/v1/suggestions", json={"text": text}, headers=headers)
        ).json()
        for text in ("Как оформить отпуск?", "Где взять справку 2-НДФЛ?")
    ]
    reordered = await api.put(
        "/api/v1/suggestions/order",
        json={"ids": [created[1]["id"], created[0]["id"]]},
        headers=headers,
    )
    assert [item["text"] for item in reordered.json()] == [
        "Где взять справку 2-НДФЛ?",
        "Как оформить отпуск?",
    ]
    denied = await api.post(
        "/api/v1/suggestions", json={"text": "Мой вопрос"}, headers=bearer(employee)
    )
    assert denied.status_code == 403

    third = make_user(tenant_id=tenant_ctx.id, email="third@test.com")
    session.add(third)
    await session.commit()

    def log(user: User, question: str, **fields: Any) -> QaLog:
        return QaLog(
            tenant_id=tenant_ctx.id,
            user_id=user.id,
            question=question,
            question_embedding=[0.1] * EMBEDDING_DIM,
            embedding_model="fake",
            prompt_version="test",
            answer_given=fields.pop("answer_given", True),
            origin="documents",
            **fields,
        )

    session.add_all(
        [
            # Трое разных людей — подсказка (регистр и «?» не важны).
            log(employee, "Как заказать пропуск для гостя?"),
            log(colleague, "как заказать пропуск для гостя"),
            log(third, "Как заказать пропуск для гостя?"),
            # Двое — мало: по вопросу можно узнать человека.
            log(employee, "Когда премия?"),
            log(colleague, "Когда премия?"),
            # С маской персональных данных и с 👎 — нет.
            log(employee, "Где [ФИО]?"),
            log(colleague, "Где [ФИО]?"),
            log(third, "Где [ФИО]?"),
            log(employee, "Где столовая?"),
            log(colleague, "Где столовая?"),
            log(third, "Где столовая?", feedback=-1),
        ]
    )
    await session.commit()

    suggestions = (
        await api.get("/api/v1/suggestions", headers=bearer(employee))
    ).json()
    assert [item["text"] for item in suggestions["company"]] == [
        "Где взять справку 2-НДФЛ?",
        "Как оформить отпуск?",
    ]
    assert suggestions["frequent"] == ["Как заказать пропуск для гостя?"]

    deleted = await api.delete(
        f"/api/v1/suggestions/{created[0]['id']}", headers=headers
    )
    assert deleted.status_code == 204
