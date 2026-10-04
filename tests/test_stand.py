"""Сквозной сценарий стенда против приложения целиком, с подменённой
моделью: вход, загрузка через песочницу, воркер, ответ со ссылкой,
общий ответ вне документов, оценка, кредиты, удаление."""

from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed import stand_scenarios
from corp_ed.core.config import DemoSettings
from corp_ed.core.security import hash_password
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    Department,
    Folder,
    Material,
    Tenant,
    User,
    UserRole,
)
from corp_ed.llm.fake import FakeAdapter
from corp_ed.llm.types import Completion, Message
from corp_ed.services.demo_service import DemoService
from corp_ed.stand import main as stand_main
from corp_ed.stand import run_check, smoke_document, upload_directory
from corp_ed.stand_scenarios import Credentials
from tests.api.conftest import TEST_TOTP_SECRET, bearer, enable_test_totp
from tests.factories import make_user
from tests.stand_harness import (
    PASSWORD,
    WordEmbeddings,
    ingest_hook,
    make_admin,
    production_rag,
    stand_client,
)

NONCE = "c0dec0de"


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def rag():  # type: ignore[no-untyped-def]
    # Мешок слов даёт расстояния крупнее настоящей модели: порог шире,
    # остальное — как в продукте.
    return production_rag().model_copy(update={"faq_max_distance": 0.9})


async def test_stand_check_passes_end_to_end(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    rag,  # type: ignore[no-untyped-def]
) -> None:
    await make_admin(session, tenant_ctx)
    embeddings = WordEmbeddings()
    llm = FakeAdapter(content=f"Кодовое слово проверки стенда — {NONCE} [1].")

    async with stand_client(session_maker, embeddings, llm, rag) as client:
        report = await run_check(
            client,
            totp_secret=TEST_TOTP_SECRET,
            company="test",
            email="stand-admin@test.com",
            password=PASSWORD,
            nonce=NONCE,
            before_poll=ingest_hook(session_maker, embeddings, rag),
            sleep=_no_sleep,
        )

    assert report.ok, "\n".join(report.lines())
    names = [step.name for step in report.steps]
    assert names == [
        "health",
        "вход администратора",
        "загрузка документа",
        "индексация воркером",
        "ответ по документу",
        "оценка ответа",
        "уточняющий вопрос",
        "вопрос вне документов",
        "расход кредитов",
        "удаление документа",
    ]


async def test_wrong_answer_fails_the_check_and_still_cleans_up(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    rag,  # type: ignore[no-untyped-def]
) -> None:
    """Модель не назвала кодовое слово — шаг красный, документ всё равно удалён."""
    admin = await make_admin(session, tenant_ctx)
    embeddings = WordEmbeddings()
    llm = FakeAdapter(content="Кодовое слово не указано [1].")

    async with stand_client(session_maker, embeddings, llm, rag) as client:
        report = await run_check(
            client,
            totp_secret=TEST_TOTP_SECRET,
            company="test",
            email="stand-admin@test.com",
            password=PASSWORD,
            nonce=NONCE,
            before_poll=ingest_hook(session_maker, embeddings, rag),
            sleep=_no_sleep,
        )
        listed = await client.get(
            "/api/v1/materials",
            headers={"Authorization": f"Bearer {_token(admin)}"},
        )

    assert not report.ok
    failed = [step.name for step in report.steps if not step.ok]
    assert failed == ["ответ по документу"]
    assert listed.json() == []


async def test_worker_not_running_times_out_with_a_hint(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    rag,  # type: ignore[no-untyped-def]
) -> None:
    await make_admin(session, tenant_ctx)
    async with stand_client(
        session_maker, WordEmbeddings(), FakeAdapter(), rag
    ) as client:
        report = await run_check(
            client,
            totp_secret=TEST_TOTP_SECRET,
            company="test",
            email="stand-admin@test.com",
            password=PASSWORD,
            nonce=NONCE,
            timeout=0.0,
            sleep=_no_sleep,
        )

    assert not report.ok
    interrupted = next(s for s in report.steps if s.name == "сценарий прерван")
    assert "воркер запущен?" in interrupted.detail
    assert report.steps[-1].name == "удаление документа"
    assert report.steps[-1].ok


async def test_temporary_password_needs_a_new_one(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    rag,  # type: ignore[no-untyped-def]
) -> None:
    admin = await make_admin(session, tenant_ctx)
    assert admin.account is not None
    admin.account.must_change_password = True
    await session.commit()

    async with stand_client(
        session_maker, WordEmbeddings(), FakeAdapter(), rag
    ) as client:
        blocked = await run_check(
            client,
            totp_secret=TEST_TOTP_SECRET,
            company="test",
            email=admin.email,
            password=PASSWORD,
            nonce=NONCE,
        )
        changed = await run_check(
            client,
            totp_secret=TEST_TOTP_SECRET,
            company="test",
            email=admin.email,
            password=PASSWORD,
            new_password="a-brand-new-password-77",
            nonce=NONCE,
            timeout=0.0,
            sleep=_no_sleep,
        )

    assert "CORP_ED_NEW_PASSWORD" in blocked.steps[-1].detail
    assert any(s.name == "вход администратора" and s.ok for s in changed.steps)


def test_smoke_document_is_unique_per_nonce() -> None:
    first = smoke_document("aaaa1111")
    second = smoke_document("bbbb2222")
    assert first[1] != second[1]
    assert "aaaa1111" in first[1].decode()
    assert first[0].endswith(".md")


def _token(admin: User) -> str:
    """Токен админа без входа: вход занял бы ещё один код приложения, а
    один и тот же код сервер дважды не принимает."""
    return bearer(admin)["Authorization"].removeprefix("Bearer ")


async def test_upload_directory_loads_supported_files_once(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    rag,  # type: ignore[no-untyped-def]
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    admin = await make_admin(session, tenant_ctx)
    (tmp_path / "Правила_отпусков.md").write_text("# Отпуск\n\nОтпуск — 28 дней.\n")
    (tmp_path / "faq.txt").write_text("Пропуск выдаёт охрана на первом этаже.\n")
    (tmp_path / "setup.exe").write_bytes(b"MZ\x90\x00")

    async with stand_client(
        session_maker, WordEmbeddings(), FakeAdapter(), rag
    ) as client:
        first = await upload_directory(
            client,
            tmp_path,
            totp_secret=TEST_TOTP_SECRET,
            company="test",
            email="stand-admin@test.com",
            password=PASSWORD,
            titles={"faq.txt": "Частые вопросы"},
        )
        again = await upload_directory(
            client,
            tmp_path,
            totp_secret=TEST_TOTP_SECRET,
            company="test",
            email="stand-admin@test.com",
            password=PASSWORD,
        )
        token = _token(admin)
        listed = await client.get(
            "/api/v1/materials", headers={"Authorization": f"Bearer {token}"}
        )

    assert first.ok and again.ok
    assert [s.name for s in first.steps] == ["faq.txt", "Правила_отпусков.md"]
    assert all(s.detail == "уже загружен" for s in again.steps)
    assert sorted(m["title"] for m in listed.json()) == [
        "Правила отпусков",
        "Частые вопросы",
    ]


def test_cli_needs_company_and_email(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CORP_ED_COMPANY", "CORP_ED_EMAIL", "CORP_ED_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert stand_main(["check"]) == 2


class EchoExcerpts(FakeAdapter):
    """Отвечает выдержками из промпта: в ответе есть всё, что нашёл поиск
    (кодовое слово, код склада), и ничего, чего он не нашёл."""

    async def generate(  # type: ignore[override]
        self, messages: list[Message], **kwargs: Any
    ) -> Completion:
        completion = await super().generate(messages, **kwargs)
        prompt = messages[-1].content if messages else ""
        if "[1]" not in prompt:
            return completion
        return replace(completion, content=f"По документам [1]: {prompt[:4000]}")


async def test_stage_scenarios_pass_with_employee(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    rag,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Шаги этапов 1–10: чат, обзор, отдел с закрытой папкой, сотрудник по
    приглашению (папку видит, только когда он в отделе), песочница сайта,
    загрузка Word и PDF. За собой сценарий убирает всё."""
    await make_admin(session, tenant_ctx)
    other = Tenant(company_code="stand-employee", name="Вторая компания")
    session.add(other)
    await session.flush()
    employee = make_user(
        email="stand-employee@test.com",
        role=UserRole.ADMIN,
        tenant_id=other.id,
        hashed_password=hash_password("temporary-password-42"),
        must_change_password=True,
    )
    assert employee.account is not None
    enable_test_totp(employee.account)
    with tenant_scope(other.id):
        session.add(employee)
        await session.commit()
    await DemoService(session_maker, DemoSettings()).setup()
    # «Мешок слов» не связывает «командировку» и «командировки»: вопрос
    # песочницы — словами документа. На стенде — обычный вопрос.
    monkeypatch.setattr(
        stand_scenarios,
        "DEMO_QUESTION",
        "Суточные в размере 700 рублей за каждый день командировки",
    )

    embeddings = WordEmbeddings()
    llm = EchoExcerpts()

    credentials = Credentials(
        email="stand-employee@test.com",
        password="temporary-password-42",
        totp_secret=TEST_TOTP_SECRET,
        new_password="second-check-account-2026",
    )
    reports = []
    # Дважды, как на стенде при каждой выкатке: второй раз сотрудник уже
    # сменил пароль и вступает снова после того, как его убрали.
    for nonce in (NONCE, "beefcafe"):
        async with stand_client(session_maker, embeddings, llm, rag) as client:
            reports.append(
                await run_check(
                    client,
                    totp_secret=TEST_TOTP_SECRET,
                    email="stand-admin@test.com",
                    password=PASSWORD,
                    nonce=nonce,
                    before_poll=ingest_hook(session_maker, embeddings, rag),
                    sleep=_no_sleep,
                    scenarios=True,
                    employee=credentials
                    if nonce == NONCE
                    else replace(credentials, password=credentials.new_password or ""),
                )
            )

    report = reports[1]
    assert reports[0].ok, "\n".join(reports[0].lines())
    assert report.ok, "\n".join(report.lines())
    names = [step.name for step in report.steps]
    # Последний шаг — удаление документа основного сценария.
    assert names[-14:] == [
        "чат: ответ потоком",
        "чат: список, «поделиться», удаление",
        "уведомления и первые шаги",
        "обзор и настройки компании",
        "сеансы входа",
        "отдел и закрытая папка",
        "сотрудник по приглашению",
        "сотрудник: общий документ виден, папка отдела — нет",
        "сотрудник в отделе видит папку",
        "выход гасит токен",
        "песочница сайта",
        "загрузка Word и PDF",
        "уборка сценариев",
        "удаление документа",
    ]
    # Убрано: сотрудника в компании нет, документов, отделов и папок не
    # осталось.
    with tenant_scope(tenant_ctx.id):
        async with session_maker() as check:
            members = (
                await check.scalars(select(User).where(User.tenant_id == tenant_ctx.id))
            ).all()
            # Убранный сотрудник — «ушёл»: учётка жива, доступа к компании нет.
            assert sorted((m.role.value, m.status.value) for m in members) == [
                ("admin", "active"),
                ("employee", "left"),
            ]
            assert (await check.scalar(select(func.count()).select_from(Material))) == 0
            assert (await check.scalar(select(func.count()).select_from(Folder))) == 0
            assert (
                await check.scalar(select(func.count()).select_from(Department))
            ) == 0
