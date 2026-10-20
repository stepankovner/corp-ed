"""«Не вопрос» в общем ответе (BH-45, промпт faq-v2.10).

На «привет», «спасибо», «что ты умеешь?» — дружелюбный ответ без пометки
«В документах компании ответа нет» и без совета уточнить; в отчёт о
пробелах и в счёт вопросов такие строки не идут. Вопрос на общую тему —
как раньше, с пометкой.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.domain.models import QaLog, Tenant, User
from corp_ed.domain.types import AnswerOrigin, NotFoundMode
from corp_ed.llm.types import Message
from corp_ed.prompts.faq import (
    GENERAL_ANSWER_PREFIX,
    NOT_FOUND_ANSWER,
    SMALL_TALK_PREFIX,
    SMALL_TALK_PROMPT_VERSION,
    build_general_messages,
)
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.services.faq_service import FaqService, _SmallTalkGate
from corp_ed.services.general_answer import CLARIFY_ADVICE, REFUSAL_ANSWER
from tests.test_gap_report import ask, clusters_of, service, topic
from tests.test_search_step import (
    QUESTION,
    ScriptedStream,
    _document,
    _service,
)
from tests.test_search_step import QUESTION_AXIS as DOCS_AXIS

HELLO = "Привет!"
REPLY = "Привет! Спросите меня о правилах и документах компании."
GENERAL_TAIL = "Столица Франции — Париж. " * 3


class OriginSink:
    """Ход ответа с событиями origin."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    async def stage(self, stage: str) -> None:
        return None

    async def origin(self, origin: AnswerOrigin) -> None:
        self.events.append(("origin", origin))

    async def delta(self, text: str) -> None:
        self.events.append(("delta", text))

    async def reset(self) -> None:
        self.events.append(("reset", None))

    async def should_stop(self) -> bool:
        return False

    @property
    def origins(self) -> list[object]:
        return [value for kind, value in self.events if kind == "origin"]

    @property
    def shown(self) -> str:
        return "".join(str(value) for kind, value in self.events if kind == "delta")


def _small_talk_service(
    chunk_repo: ChunkRepository, llm: ScriptedStream, *, on: bool = True
) -> FaqService:
    service = _service(chunk_repo, llm)
    return FaqService(
        chunk_repo=service.chunk_repo,
        qa_log_repo=service.qa_log_repo,
        tenant_repo=service.tenant_repo,
        glossary_repo=service.glossary_repo,
        credits=service.credits,
        embedding_gateway=service.embedding_gateway,
        llm_gateway=llm,
        session=service.session,
        limit=5,
        max_distance=0.6,
        context_max_tokens=3000,
        temperature=0.0,
        retriever=service.retriever,
        fulltext_weight=0.5,
        search_step=True,
        small_talk=on,
    )


def _system(messages: list[Message]) -> str:
    return messages[0].content


async def _log(session: AsyncSession) -> QaLog:
    return (await session.scalars(select(QaLog))).one()


async def test_greeting_gets_a_friendly_reply_without_the_mark(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    llm = ScriptedStream(["Реплика без", " вопроса.", "\nПривет! ", REPLY[8:]])
    sink = OriginSink()

    result = await _small_talk_service(chunk_repo, llm).answer_turn(
        HELLO, employee, history=[], sink=sink
    )

    assert result.content == REPLY
    assert result.origin is AnswerOrigin.SMALL_TALK
    assert result.answer_given is False
    assert result.sources == []
    assert CLARIFY_ADVICE not in result.content
    # Плашки «ответа нет» нет: origin — «не вопрос», и он приходит раньше
    # первого показанного куска; служебная строка не видна.
    assert sink.origins == [AnswerOrigin.SMALL_TALK]
    assert sink.events[0] == ("origin", AnswerOrigin.SMALL_TALK)
    assert sink.shown == REPLY
    assert SMALL_TALK_PREFIX in _system(llm.calls[0])

    entry = await _log(session)
    assert entry.origin == "small_talk"
    assert entry.prompt_version == SMALL_TALK_PROMPT_VERSION
    assert entry.answer_given is False


async def test_general_question_keeps_the_mark_and_advice(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    llm = ScriptedStream([f"{GENERAL_ANSWER_PREFIX}\n", GENERAL_TAIL])
    sink = OriginSink()

    result = await _small_talk_service(chunk_repo, llm).answer_turn(
        "Какая столица Франции?", employee, history=[], sink=sink
    )

    assert result.origin is AnswerOrigin.GENERAL_KNOWLEDGE
    assert result.content == (
        f"{GENERAL_ANSWER_PREFIX}\n{GENERAL_TAIL.strip()}\n\n{CLARIFY_ADVICE}"
    )
    assert sink.origins == [AnswerOrigin.GENERAL_KNOWLEDGE]
    # Пометку показывает плашка, а не текст.
    assert GENERAL_ANSWER_PREFIX not in sink.shown
    assert sink.shown.strip() == GENERAL_TAIL.strip()
    assert (await _log(session)).origin == "general_knowledge"


async def test_both_parts_mean_a_question(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    # Модель написала и служебную строку, и пометку: это вопрос
    # (split_general_answer), показанное приветствие сбрасывается.
    llm = ScriptedStream(
        [
            f"{SMALL_TALK_PREFIX}\nПривет! ",
            f"{GENERAL_ANSWER_PREFIX}\nПариж.",
        ]
    )
    sink = OriginSink()

    result = await _small_talk_service(chunk_repo, llm).answer_turn(
        "Привет! Какая столица Франции?", employee, history=[], sink=sink
    )

    assert result.origin is AnswerOrigin.GENERAL_KNOWLEDGE
    assert result.content == f"{GENERAL_ANSWER_PREFIX}\nПариж.\n\n{CLARIFY_ADVICE}"
    assert sink.origins == [AnswerOrigin.SMALL_TALK, AnswerOrigin.GENERAL_KNOWLEDGE]
    assert ("reset", None) in sink.events


async def test_refusal_over_excerpts_then_greeting(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    # Выдержки прошли порог, модель по ним отказала, общий вызов узнал
    # «не вопрос»: два вызова в токенах, ответ без пометки.
    await _document(session, "Регламент", ["Закупки согласует директор."], DOCS_AXIS)
    llm = ScriptedStream(NOT_FOUND_ANSWER, f"{SMALL_TALK_PREFIX}\n{REPLY}")

    result = await _small_talk_service(chunk_repo, llm).answer_turn(
        QUESTION, employee, history=[]
    )

    assert result.origin is AnswerOrigin.SMALL_TALK
    assert result.content == REPLY
    assert len(llm.calls) == 2
    assert (await _log(session)).input_tokens == 200


async def test_switch_off_keeps_the_old_general_prompt(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    llm = ScriptedStream(f"{GENERAL_ANSWER_PREFIX}\nЗдравствуйте!")

    result = await _small_talk_service(chunk_repo, llm, on=False).answer_turn(
        HELLO, employee, history=[]
    )

    assert llm.calls[0] == build_general_messages(HELLO)
    assert result.origin is AnswerOrigin.GENERAL_KNOWLEDGE
    assert result.content.endswith(CLARIFY_ADVICE)
    assert (await _log(session)).prompt_version != SMALL_TALK_PROMPT_VERSION


async def test_strict_mode_still_refuses_without_a_model_call(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    # Строгий режим отказывает до общего вызова (решение — Артём, BH-45).
    tenant_ctx.not_found_mode = NotFoundMode.STRICT.value
    await session.commit()
    llm = ScriptedStream()

    result = await _small_talk_service(chunk_repo, llm).answer_turn(
        HELLO, employee, history=[]
    )

    assert result.content == REFUSAL_ANSWER
    assert result.origin is AnswerOrigin.NONE
    assert llm.calls == []


def _feed(pieces: list[str]) -> tuple[list[str], AnswerOrigin | None]:
    gate = _SmallTalkGate()
    return [gate.feed(piece) for piece in pieces], gate.origin


def test_gate_hides_the_service_line_split_over_pieces() -> None:
    assert _feed(
        ["**Репл", "ика без воп", "роса.**", "\n", "Привет!", " Чем помочь?"]
    ) == (
        ["", "", "", "", "Привет!", " Чем помочь?"],
        AnswerOrigin.SMALL_TALK,
    )


def test_gate_gives_a_question_to_the_general_gate() -> None:
    # «Реплика…» без «без вопроса» — не служебная строка; общий ответ
    # придерживается своим фильтром, пока пометка не дописана.
    shown, origin = _feed(["Реплики", " бывают разные."])
    assert origin is AnswerOrigin.GENERAL_KNOWLEDGE
    assert shown == ["", ""]


async def test_small_talk_is_not_a_gap(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    gaps = [
        ask(session, employee, "Как подключить VPN?", topic(1)),
        ask(session, employee, "VPN не работает", topic(1)),
    ]
    hellos = [ask(session, employee, "Привет", topic(2)) for _ in range(3)]
    for row in hellos:
        row.origin = "small_talk"
    await session.commit()

    [report] = await service(session_maker).run("test")

    assert (report.window, report.clusters) == (2, 1)
    [cluster] = await clusters_of(session, tenant_ctx.id)
    assert cluster.question_count == len(gaps)
    for row in hellos:
        await session.refresh(row)
        assert row.miss_kind is None
