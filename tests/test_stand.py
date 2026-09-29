"""Сквозной сценарий стенда против приложения целиком, с подменённой
моделью: вход, загрузка через песочницу, воркер, ответ со ссылкой,
общий ответ вне документов, оценка, кредиты, удаление."""

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.domain.models import Tenant
from corp_ed.llm.fake import FakeAdapter
from corp_ed.stand import main as stand_main
from corp_ed.stand import run_check, smoke_document, upload_directory
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
    await make_admin(session, tenant_ctx)
    embeddings = WordEmbeddings()
    llm = FakeAdapter(content="Кодовое слово не указано [1].")

    async with stand_client(session_maker, embeddings, llm, rag) as client:
        report = await run_check(
            client,
            company="test",
            email="stand-admin@test.com",
            password=PASSWORD,
            nonce=NONCE,
            before_poll=ingest_hook(session_maker, embeddings, rag),
            sleep=_no_sleep,
        )
        listed = await client.get(
            "/api/v1/materials",
            headers={"Authorization": f"Bearer {await _token(client)}"},
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
    admin.must_change_password = True
    await session.commit()

    async with stand_client(
        session_maker, WordEmbeddings(), FakeAdapter(), rag
    ) as client:
        blocked = await run_check(
            client, company="test", email=admin.email, password=PASSWORD, nonce=NONCE
        )
        changed = await run_check(
            client,
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


async def _token(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "company_code": "test",
            "email": "stand-admin@test.com",
            "password": PASSWORD,
        },
    )
    token: str = response.json()["access_token"]
    return token


async def test_upload_directory_loads_supported_files_once(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    rag,  # type: ignore[no-untyped-def]
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    await make_admin(session, tenant_ctx)
    (tmp_path / "Правила_отпусков.md").write_text("# Отпуск\n\nОтпуск — 28 дней.\n")
    (tmp_path / "faq.txt").write_text("Пропуск выдаёт охрана на первом этаже.\n")
    (tmp_path / "setup.exe").write_bytes(b"MZ\x90\x00")

    async with stand_client(
        session_maker, WordEmbeddings(), FakeAdapter(), rag
    ) as client:
        first = await upload_directory(
            client,
            tmp_path,
            company="test",
            email="stand-admin@test.com",
            password=PASSWORD,
            titles={"faq.txt": "Частые вопросы"},
        )
        again = await upload_directory(
            client,
            tmp_path,
            company="test",
            email="stand-admin@test.com",
            password=PASSWORD,
        )
        token = await _token(client)
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
