"""Реплики правилом в сервисе ответов (BH-48).

«привет» не доходит до эмбеддинга, поиска и модели; в журнале — вид
реплики без вектора; в отчёт о пробелах не идёт. Реплика с вопросом —
обычный путь. Строгий режим — тоже шаблон.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.domain.models import QaLog, Tenant, User
from corp_ed.domain.small_talk import reply
from corp_ed.domain.types import AnswerOrigin, NotFoundMode
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.services.faq_service import SMALL_TALK_RULE_VERSION, FaqService
from tests.llm.test_embedding_breaker import Script
from tests.test_gap_report import ask, service, topic
from tests.test_search_step import ScriptedStream, _service
from tests.test_small_talk import OriginSink


def _rule_service(
    chunk_repo: ChunkRepository, llm: ScriptedStream, *, on: bool = True
) -> FaqService:
    faq = _service(chunk_repo, llm)
    faq.small_talk_rule = on
    return faq


async def _log(session: AsyncSession) -> QaLog:
    return (await session.scalars(select(QaLog))).one()


async def test_greeting_is_answered_before_search_and_model(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    llm = ScriptedStream()
    faq = _rule_service(chunk_repo, llm)
    embeddings = Script()
    faq.embedding_gateway = embeddings
    sink = OriginSink()

    result = await faq.answer_turn(
        "Привет, ассистент!", employee, history=[], sink=sink
    )

    assert result.content == reply("greeting")
    assert result.origin is AnswerOrigin.SMALL_TALK
    assert result.sources == []
    assert (llm.calls, embeddings.calls) == ([], 0)
    assert sink.origins == [AnswerOrigin.SMALL_TALK]
    assert sink.shown == reply("greeting")

    entry = await _log(session)
    assert entry.small_talk_rule == "greeting"
    assert entry.origin == "small_talk"
    assert entry.question_embedding is None
    assert entry.prompt_version == SMALL_TALK_RULE_VERSION
    assert (entry.input_tokens, entry.credits) == (0, 0)


async def test_greeting_with_a_question_goes_the_usual_way(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    llm = ScriptedStream("В документах компании ответа нет.\nНе знаю.")
    faq = _rule_service(chunk_repo, llm)

    await faq.answer_turn("Привет! Сколько дней отпуска?", employee, history=[])

    assert len(llm.calls) == 1
    assert (await _log(session)).small_talk_rule is None


async def test_strict_mode_thanks_gets_the_template(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    tenant_ctx.not_found_mode = NotFoundMode.STRICT.value
    await session.commit()

    result = await _rule_service(chunk_repo, ScriptedStream()).answer_turn(
        "спасибо большое)))", employee, history=[]
    )

    assert result.content == reply("thanks")
    assert (await _log(session)).small_talk_rule == "thanks"


async def test_switch_off_keeps_the_old_path(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    llm = ScriptedStream("В документах компании ответа нет.\nЗдравствуйте!")

    await _rule_service(chunk_repo, llm, on=False).answer_turn(
        "привет", employee, history=[]
    )

    assert len(llm.calls) == 1
    assert (await _log(session)).small_talk_rule is None


async def test_rule_replies_stay_out_of_the_gap_report(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    ask(session, employee, "Как подключить VPN?", topic(1))
    ask(session, employee, "VPN не работает", topic(1))
    for _ in range(3):
        session.add(
            QaLog(
                tenant_id=tenant_ctx.id,
                user_id=employee.id,
                question="привет",
                question_embedding=None,
                embedding_model=None,
                prompt_version=SMALL_TALK_RULE_VERSION,
                answer_given=False,
                origin="small_talk",
                source_chunk_ids=[],
                small_talk_rule="greeting",
            )
        )
    await session.commit()

    [report] = await service(session_maker).run("test")

    assert (report.window, report.clusters) == (2, 1)
