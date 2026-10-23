"""Цикл «решение до ответа» (eval.decision_loop) на подделках модели и поиска."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from corp_ed.domain.decision import Route
from corp_ed.domain.directory import Entry
from corp_ed.llm.types import Message
from corp_ed.prompts.faq import (
    NOT_FOUND_ANSWER,
    SMALL_TALK_PREFIX,
    build_faq_messages,
    build_general_messages,
)
from eval.decision_loop import (
    DEFAULT_GATES,
    SPECULATION_GATES,
    run_decided,
    speculation_skip,
)
from eval.providers import CallTimeoutError
from eval.yandex import Completion


@dataclass
class Chunk:
    content: str
    title: str = "Документ"
    heading_path: list[str] = field(default_factory=list)


def completion(text: str) -> Completion:
    return Completion(
        text=text, input_tokens=10, output_tokens=5, latency_ms=1.0, model="m"
    )


def decision(**fields: str) -> str:
    base = {
        "kind": "question",
        "subject": "—",
        "excerpts_subject": "—",
        "match": "n/a",
        "sufficiency": "full",
        "missing": "—",
        "search_query": "—",
    }
    return json.dumps({**base, **fields}, ensure_ascii=False)


class Script:
    """Подделка модели: отдаёт ответы по очереди, запоминает сообщения."""

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.seen: list[list[Message]] = []

    def __call__(self, messages: list[Message]) -> Completion:
        self.seen.append(messages)
        return completion(self.texts.pop(0))


def no_search(query: str) -> Sequence[Chunk]:
    raise AssertionError(f"поиск не ожидался: {query}")


def test_full_answers_with_separate_call() -> None:
    decide = Script(decision())
    answer = Script("Согласует финансовый директор [1].")
    run = run_decided(
        "Кто согласует?", [Chunk("а")], decide=decide, answer=answer, search=no_search
    )
    assert run.route is Route.ANSWER
    assert run.text == "Согласует финансовый директор [1]."
    assert len(run.calls) == 2 and run.invalid == 0


def test_partial_searches_then_answers() -> None:
    decide = Script(
        decision(sufficiency="partial", search_query="финансовый директор ФИО"),
        decision(),
    )
    answer = Script("Финансовый директор — Кукушкина [2].")
    queries: list[str] = []

    def search(query: str) -> Sequence[Chunk]:
        queries.append(query)
        return [Chunk("а"), Chunk("б")]  # «а» уже показана — не повторяем

    run = run_decided(
        "Кто (ФИО)?", [Chunk("а")], decide=decide, answer=answer, search=search
    )
    assert queries == ["финансовый директор ФИО"]
    assert run.added == [1]
    assert [b.content for b in run.shown] == ["а", "б"]
    # Второе решение видит обе выдержки.
    assert "[2]" in decide.seen[1][1].content
    assert run.route is Route.ANSWER and not run.exhausted


def test_empty_search_answers_with_what_is_there() -> None:
    decide = Script(decision(sufficiency="partial", search_query="ФИО директора"))
    answer = Script("Согласует директор [1]; ФИО в документах нет.")
    run = run_decided(
        "Кто?",
        [Chunk("а")],
        decide=decide,
        answer=answer,
        search=lambda q: [Chunk("а")],
    )
    assert run.exhausted and run.added == [0]
    assert run.route is Route.ANSWER
    assert len(decide.seen) == 1


def test_search_limit() -> None:
    decide = Script(
        *(decision(sufficiency="partial", search_query=f"запрос {i}") for i in range(3))
    )
    answer = Script("Ответ [1].")
    counter = iter(range(100))

    def search(query: str) -> Sequence[Chunk]:
        return [Chunk(f"новая {next(counter)}")]

    run = run_decided(
        "Вопрос?",
        [Chunk("а")],
        decide=decide,
        answer=answer,
        search=search,
        max_extra=2,
    )
    assert run.queries == ["запрос 0", "запрос 1"]
    assert run.exhausted and run.route is Route.ANSWER


def test_other_subject_and_not_found_skip_answer() -> None:
    for fields in (
        {"match": "other", "subject": "Старт-ИИ-2"},
        {"sufficiency": "none"},
    ):
        run = run_decided(
            "Вопрос?",
            [Chunk("а")],
            decide=Script(decision(**fields)),
            answer=Script(),
            search=no_search,
        )
        assert run.text == NOT_FOUND_ANSWER and run.needs_general


def test_edition_note_prepended() -> None:
    decide = Script(
        decision(
            match="other_edition",
            subject="ГПБ Тех 2025",
            excerpts_subject="ГПБ Тех 2026",
        )
    )
    answer = Script("Требования такие [1].")
    run = run_decided(
        "Вопрос?", [Chunk("а")], decide=decide, answer=answer, search=no_search
    )
    assert (
        run.text == "В документах — «ГПБ Тех 2026», а не «ГПБ Тех 2025»; в запрошенной "
        "редакции условия могли отличаться. Требования такие [1]."
    )
    assert "Отвечай по той редакции" in answer.seen[0][0].content


def test_other_edition_none_still_answers() -> None:
    # Приёмка 3: «ничего» при другой редакции — ответ по найденной редакции.
    decide = Script(
        decision(
            match="other_edition",
            sufficiency="none",
            subject="П-ОТП-07, редакция 3",
            excerpts_subject="П-ОТП-07, редакция 4",
        )
    )
    answer = Script("В документах — редакция 4, а не третья. Не более 6 [1].")
    run = run_decided(
        "Вопрос?", [Chunk("а")], decide=decide, answer=answer, search=no_search
    )
    assert run.route is Route.ANSWER and not run.needs_general
    assert run.text == (
        "В документах — редакция 4, а не третья; в запрошенной редакции условия "
        "могли отличаться. Не более 6 [1]."
    )


def test_small_talk_separate_and_merged() -> None:
    small = decision(kind="small_talk", sufficiency="none")
    run = run_decided(
        "Привет!",
        [],
        decide=Script(small),
        answer=Script(f"{SMALL_TALK_PREFIX}\nЗдравствуйте! Спросите о документах [1]."),
        search=no_search,
    )
    assert run.small_talk and not run.needs_general
    assert run.text == "Здравствуйте! Спросите о документах."
    merged = json.loads(small)
    merged["answer"] = "Привет! Чем помочь?"
    run = run_decided(
        "Привет!",
        [],
        decide=Script(json.dumps(merged, ensure_ascii=False)),
        answer=Script(),
        search=no_search,
        merged=True,
    )
    assert run.small_talk and run.text == "Привет! Чем помочь?" and len(run.calls) == 1


def test_merged_full_uses_answer_field() -> None:
    merged = json.loads(decision())
    merged["answer"] = "Согласует директор [1]."
    run = run_decided(
        "Кто?",
        [Chunk("а")],
        decide=Script(json.dumps(merged, ensure_ascii=False)),
        answer=Script(),
        search=no_search,
        merged=True,
    )
    assert run.text == "Согласует директор [1]." and len(run.calls) == 1


def test_invalid_decision_falls_back_to_answer() -> None:
    answer = Script("Ответ по выдержкам [1].")
    run = run_decided(
        "Вопрос?",
        [Chunk("а")],
        decide=Script("не JSON"),
        answer=answer,
        search=no_search,
    )
    assert run.invalid == 1 and run.route is Route.ANSWER
    assert run.text == "Ответ по выдержкам [1]."


def test_merged_small_talk_with_not_found_is_a_question() -> None:
    # Как в BH-45: «ответа нет» в ответе перевешивает признак реплики.
    merged = json.loads(decision(kind="small_talk", sufficiency="none"))
    merged["answer"] = NOT_FOUND_ANSWER
    run = run_decided(
        "Кто изобрёл телефон?",
        [],
        decide=Script(json.dumps(merged, ensure_ascii=False)),
        answer=Script(),
        search=no_search,
        merged=True,
    )
    assert not run.small_talk and run.route is Route.NOT_FOUND and run.needs_general


def test_search_history_shown_and_repeat_not_searched() -> None:
    partial = decision(sufficiency="partial", search_query="финансовый директор ФИО")
    decide = Script(partial, partial)
    answer = Script("Согласует финансовый директор [1].")
    calls: list[str] = []

    def search(query: str) -> Sequence[Chunk]:
        calls.append(query)
        return [Chunk("новая")]

    run = run_decided(
        "Кто (ФИО)?", [Chunk("а")], decide=decide, answer=answer, search=search
    )
    # Второе решение видит, что уже искали, и тот же запрос не повторяется.
    assert "Уже искали: «финансовый директор ФИО» — новых выдержек: 1" in (
        decide.seen[1][1].content
    )
    assert calls == ["финансовый директор ФИО"]
    assert run.exhausted and run.route is Route.ANSWER


def test_rule_answers_without_model() -> None:
    run = run_decided(
        "Привет!", [], decide=Script(), answer=Script(), search=no_search, rule=True
    )
    assert run.by_rule and run.small_talk and not run.calls and run.wall_ms == 0
    assert run.text.startswith("Здравствуйте")
    # Вопрос с приветствием — не реплика: идёт к модели.
    run = run_decided(
        "Привет! Кто согласует?",
        [Chunk("а")],
        decide=Script(decision()),
        answer=Script("Директор [1]."),
        search=no_search,
        rule=True,
    )
    assert not run.by_rule and run.text == "Директор [1]."


def test_speculative_answer_used_and_timed() -> None:
    decide = Script(decision())
    answer = Script("Согласует директор [1].")
    run = run_decided(
        "Кто?",
        [Chunk("а")],
        decide=decide,
        answer=answer,
        search=no_search,
        speculative=True,
    )
    assert run.speculated and run.wasted == 0 and run.text == "Согласует директор [1]."
    assert len(run.calls) == 2 and len(answer.seen) == 1
    assert run.wall_ms == 1.0  # max(1, 1), не 2


def test_speculative_answer_discarded_on_search_and_refusal() -> None:
    decide = Script(
        decision(sufficiency="partial", search_query="ФИО директора"), decision()
    )
    answer = Script("черновик [1]", "Директор — Кукушкина [2].")
    run = run_decided(
        "Кто (ФИО)?",
        [Chunk("а")],
        decide=decide,
        answer=answer,
        search=lambda q: [Chunk("б")],
        speculative=True,
    )
    assert run.wasted == 1 and run.text == "Директор — Кукушкина [2]."
    assert run.wall_ms == 3.0  # max(1, 1) + решение + ответ
    run = run_decided(
        "Вопрос?",
        [Chunk("а")],
        decide=Script(decision(sufficiency="none")),
        answer=Script("черновик"),
        search=no_search,
        speculative=True,
    )
    assert run.wasted == 1 and run.text == NOT_FOUND_ANSWER


def test_speculative_other_edition_always_reanswers() -> None:
    # Спекулятивный ответ — по faq-v2.7: при другой редакции выбрасывается,
    # даже если это не отказ, — ответ промптом edition-v1.
    other = decision(
        match="other_edition", subject="ГПБ Тех 2025", excerpts_subject="ГПБ Тех 2026"
    )
    for draft in (NOT_FOUND_ANSWER, "Требования такие [1]."):
        answer = Script(draft, "Требования такие [1].")
        run = run_decided(
            "Вопрос?",
            [Chunk("а")],
            decide=Script(other),
            answer=answer,
            search=no_search,
            speculative=True,
        )
        assert run.wasted == 1 and len(answer.seen) == 2
        assert "Отвечай по той редакции" in answer.seen[1][0].content
        assert run.text.startswith(
            "В документах — «ГПБ Тех 2026», а не «ГПБ Тех 2025»; в запрошенной "
            "редакции условия могли отличаться. Требования"
        )


def test_directory_hit_answers_without_second_decision() -> None:
    partial = decision(
        sufficiency="partial",
        search_query="финансовый директор ФИО",
        lookup_role="финансовый директор",
    )
    decide = Script(partial)
    answer = Script("черновик", "Согласует финансовый директор — Кукушкина [2].")
    calls: list[str] = []

    def directory(role: str, missing: str) -> tuple[Sequence[Chunk], bool]:
        calls.append(role)
        return [
            Chunk(
                "Финансовый директор — Ярослава Игоревна Кукушкина, кабинет 402.",
                "Оргструктура",
            )
        ], True

    run = run_decided(
        "Кто (ФИО)?",
        [Chunk("а")],
        decide=decide,
        answer=answer,
        search=no_search,
        directory=directory,
        speculative=True,
    )
    assert calls == ["финансовый директор"]
    assert run.directory_hits == 1 and run.directory_misses == 0 and run.queries == []
    assert len(decide.seen) == 1 and run.wasted == 1
    assert run.text == "Согласует финансовый директор — Кукушкина [2]."
    assert [b.content for b in run.shown][-1].startswith("Финансовый директор —")


def test_directory_miss_falls_back_to_search() -> None:
    partial = decision(
        sufficiency="partial",
        search_query="ФИО директора",
        lookup_role="директор по качеству",
    )
    run = run_decided(
        "Кто?",
        [Chunk("а")],
        decide=Script(partial, decision()),
        answer=Script("Ответ [2]."),
        search=lambda q: [Chunk("б")],
        directory=lambda role, missing: ([], False),
    )
    assert run.directory_misses == 1 and run.queries == ["ФИО директора"]
    assert run.text == "Ответ [2]."


@pytest.mark.parametrize("asker", [False, True])
def test_directory_gets_question_with_asker_unit(asker: bool) -> None:
    """asker_unit=True: справочник получает текст вопроса третьим
    аргументом, пометка «что нашли» — в run.asker_unit; без флага — вызов
    с двумя аргументами, как было."""
    partial = decision(
        sufficiency="partial", search_query="ФИО", lookup_role="руководитель"
    )
    question = "Я в ОКП. Кто мой руководитель?"
    seen: list[tuple[str, ...]] = []

    def directory(*args: str) -> tuple[Sequence[Chunk], bool, list[Entry], str]:
        seen.append(args)
        note = "ОКП (вопрос) → руководитель Пряжкина" if len(args) > 2 else ""
        return [Chunk("ОКП — руководитель Пряжкина.")], True, [], note

    run = run_decided(
        question,
        [Chunk("а")],
        decide=Script(partial),
        answer=Script("Пряжкина [2]."),
        search=no_search,
        directory=directory,
        asker_unit=asker,
    )
    if asker:
        assert seen == [("руководитель", "—", question)]
        assert run.asker_unit == ["ОКП (вопрос) → руководитель Пряжкина"]
    else:
        assert seen == [("руководитель", "—")]
        assert run.asker_unit == []
    assert run.directory_hits == 1 and run.text == "Пряжкина [2]."


def test_directory_generic_role_miss_goes_on_as_before() -> None:
    """Промах «общая роль без отдела» — как любой промах: добор вектором,
    причина — в run.asker_unit."""
    partial = decision(
        sufficiency="partial",
        search_query="приказ генерального директора кабинет",
        lookup_role="руководитель",
    )
    run = run_decided(
        "Чьим приказом вносятся изменения и в каком кабинете?",
        [Chunk("а")],
        decide=Script(partial, decision()),
        answer=Script("Генеральный директор, кабинет 401 [2]."),
        search=lambda q: [Chunk("Генеральный директор — кабинет 401.")],
        directory=lambda role, missing, question: (
            [],
            False,
            [],
            "промах: общая роль без отдела спрашивающего",
        ),
        asker_unit=True,
    )
    assert run.directory_misses == 1 and run.directory_hits == 0
    assert run.queries == ["приказ генерального директора кабинет"]
    assert run.asker_unit == ["промах: общая роль без отдела спрашивающего"]
    assert run.text == "Генеральный директор, кабинет 401 [2]."


def test_directory_partial_adds_entry_and_searches() -> None:
    """Роль нашлась, но без запрошенной почты — запись в выдержки и добор."""
    partial = decision(
        sufficiency="partial",
        search_query="почта отдела персонала",
        lookup_role="отдел персонала",
    )
    run = run_decided(
        "Куда писать?",
        [Chunk("а")],
        decide=Script(partial, decision()),
        answer=Script("черновик", "Пишите на hr@ [3]."),
        search=lambda q: [Chunk("hr@stk-test.example")],
        directory=lambda role, missing: (
            [Chunk("Отдел персонала — кабинет 210.")],
            False,
        ),
        speculative=True,
    )
    assert run.directory_partial == 1 and run.directory_hits == 0
    assert run.queries == ["почта отдела персонала"] and run.wasted == 1
    assert [b.content for b in run.shown] == [
        "а",
        "Отдел персонала — кабинет 210.",
        "hr@stk-test.example",
    ]
    assert run.text == "Пишите на hr@ [3]."


def test_directory_entry_kept_when_excerpts_full() -> None:
    """Выдержек уже под лимит — запись справочника всё равно в выдержках."""
    partial = decision(
        sufficiency="partial", search_query="ФИО", lookup_role="финансовый директор"
    )
    run = run_decided(
        "Кто?",
        [Chunk("а"), Chunk("б")],
        decide=Script(partial),
        answer=Script("Кукушкина [3]."),
        search=no_search,
        directory=lambda role, missing: (
            [Chunk("Финансовый директор — Кукушкина.")],
            True,
        ),
        max_excerpts=2,
    )
    assert run.directory_hits == 1
    assert run.shown[-1].content == "Финансовый директор — Кукушкина."


def timed(text: str, ms: float) -> Completion:
    return Completion(
        text=text, input_tokens=10, output_tokens=5, latency_ms=ms, model="m"
    )


class Timed(Script):
    """Подделка с заданным временем каждого ответа."""

    def __init__(self, *items: tuple[str, float]) -> None:
        super().__init__(*(text for text, _ in items))
        self.ms = [ms for _, ms in items]

    def __call__(self, messages: list[Message]) -> Completion:
        self.seen.append(messages)
        return timed(self.texts.pop(0), self.ms.pop(0))


def test_recheck_not_called_when_fast_answers() -> None:
    # Быстрое решение «хватает» или «не хватает» — по нему, без перепроверки.
    for fast_text, extra in (
        (decision(), []),
        (decision(sufficiency="partial", search_query="ФИО директора"), [decision()]),
    ):
        fast, slow = Script(fast_text, *extra), Script()
        run = run_decided(
            "Кто?",
            [Chunk("а")],
            decide=slow,
            decide_fast=fast,
            answer=Script("Директор [1]."),
            search=lambda q: [Chunk("б")],
        )
        assert slow.seen == [] and run.rechecked == 0
        assert run.route is Route.ANSWER and run.text == "Директор [1]."
        assert len(run.decision_calls) == len(fast.seen)


def test_recheck_on_refusal_takes_slow_route() -> None:
    fast = Script(decision(sufficiency="none"))
    slow = Script(decision())
    run = run_decided(
        "Кто согласует?",
        [Chunk("а")],
        decide=slow,
        decide_fast=fast,
        answer=Script("Согласует директор [1]."),
        search=no_search,
    )
    # Перепроверка видит те же сообщения; маршрут — по ней.
    assert slow.seen == fast.seen
    assert run.rechecked == 1 and run.route is Route.ANSWER
    assert run.text == "Согласует директор [1]."
    assert len(run.decision_calls) == 2 and run.decision.sufficiency == "full"
    assert run.decisions[0]["fast"] is True and "fast" not in run.decisions[1]


def test_recheck_confirms_other_subject_and_none_edition() -> None:
    other = decision(match="other", subject="Старт-ИИ-2", excerpts_subject="Старт-ИИ-1")
    run = run_decided(
        "Грант по Старт-ИИ-2?",
        [Chunk("а")],
        decide=Script(other),
        decide_fast=Script(other),
        answer=Script(),
        search=no_search,
    )
    assert run.rechecked == 1 and run.route is Route.OTHER_SUBJECT
    assert run.text == NOT_FOUND_ANSWER
    # «ничего» при другой редакции — маршрут «ответ», но перепроверка всё равно.
    edition = decision(sufficiency="none", match="other_edition", subject="2024")
    slow = Script(edition)
    run = run_decided(
        "Сколько в 2024?",
        [Chunk("а")],
        decide=slow,
        decide_fast=Script(edition),
        answer=Script("Сумма — 5 млн [1]."),
        search=no_search,
    )
    assert run.rechecked == 1 and len(slow.seen) == 1


def test_recheck_partial_then_searches() -> None:
    fast = Script(decision(sufficiency="none"), decision())
    slow = Script(decision(sufficiency="partial", search_query="ФИО директора"))
    queries: list[str] = []

    def search(query: str) -> Sequence[Chunk]:
        queries.append(query)
        return [Chunk("б")]

    run = run_decided(
        "Кто (ФИО)?",
        [Chunk("а")],
        decide=slow,
        decide_fast=fast,
        answer=Script("Кукушкина [2]."),
        search=search,
    )
    # Перепроверка попросила добор; второе решение — снова быстрое, без отказа.
    assert queries == ["ФИО директора"] and run.rechecked == 1
    assert len(fast.seen) == 2 and len(slow.seen) == 1
    assert run.route is Route.ANSWER and run.text == "Кукушкина [2]."


def test_recheck_wall_time_with_speculative_answer() -> None:
    # Цепочка быстрый + перепроверка — последовательно, ответ — параллельно.
    for spec_ms, wall in ((1000.0, 3500.0), (5000.0, 5000.0)):
        run = run_decided(
            "Кто?",
            [Chunk("а")],
            decide=Timed((decision(), 2500.0)),
            decide_fast=Timed((decision(sufficiency="none"), 1000.0)),
            answer=Timed(("Директор [1].", spec_ms)),
            search=no_search,
            speculative=True,
        )
        assert run.rechecked == 1 and run.wasted == 0
        assert run.text == "Директор [1]." and run.wall_ms == wall
    # Без перепроверки — max(быстрый, ответ).
    run = run_decided(
        "Кто?",
        [Chunk("а")],
        decide=Script(),
        decide_fast=Timed((decision(), 1200.0)),
        answer=Timed(("Директор [1].", 2000.0)),
        search=no_search,
        speculative=True,
    )
    assert run.rechecked == 0 and run.wall_ms == 2000.0


def test_recheck_role_full_goes_to_directory() -> None:
    # R2: low «хватает», но назвал роль — переспрос; medium просит справочник.
    fast = Script(decision(lookup_role="финансовый директор"))
    slow = Script(
        decision(
            sufficiency="partial", missing="ФИО", lookup_role="финансовый директор"
        )
    )
    asked: list[str] = []

    def directory(role: str, missing: str) -> tuple[Sequence[Chunk], bool]:
        asked.append(role)
        return [Chunk("Финансовый директор — Кукушкина.")], True

    run = run_decided(
        "Кто (ФИО) согласует?",
        [Chunk("а")],
        decide=slow,
        decide_fast=fast,
        answer=Script("Кукушкина [2]."),
        search=no_search,
        directory=directory,
        recheck="refusals+role",
    )
    assert slow.seen == fast.seen and asked == ["финансовый директор"]
    assert run.rechecked == 1 and run.recheck_reasons == ["role"]
    assert run.directory_hits == 1 and run.text == "Кукушкина [2]."
    assert run.decisions[0]["recheck"] == "role"


def test_recheck_role_only_in_role_mode_and_with_role() -> None:
    # Прежний режим R — «хватает + роль» не переспрашивается; в R2 роль «—»
    # (или пустая) — тоже нет.
    for mode, role in (
        ("refusals", "финансовый директор"),
        ("refusals+role", "—"),
        ("refusals+role", "-"),
        ("refusals+role", ""),
    ):
        slow = Script()
        run = run_decided(
            "Кто?",
            [Chunk("а")],
            decide=slow,
            decide_fast=Script(decision(lookup_role=role)),
            answer=Script("Директор [1]."),
            search=no_search,
            recheck=mode,
        )
        assert slow.seen == [] and run.rechecked == 0 and run.recheck_reasons == []


def test_recheck_reason_refusal_recorded() -> None:
    run = run_decided(
        "Кто согласует?",
        [Chunk("а")],
        decide=Script(decision()),
        decide_fast=Script(decision(sufficiency="none")),
        answer=Script("Согласует директор [1]."),
        search=no_search,
        recheck="refusals+role",
    )
    assert run.recheck_reasons == ["refusal"]
    assert run.decisions[0]["recheck"] == "refusal"


def test_recheck_mode_validated() -> None:
    with pytest.raises(ValueError):
        run_decided(
            "Кто?",
            [Chunk("а")],
            decide=Script(),
            answer=Script(),
            search=no_search,
            recheck="all",
        )


CFO = Entry(
    role="Финансовый директор",
    unit="",
    name="Ярослава Игоревна Кукушкина",
    room="402",
    phone="1120",
)


def _cfo_directory(complete: bool = True):  # noqa: ANN202
    def directory(role: str, missing: str) -> tuple[Sequence[Chunk], bool, list]:
        block = Chunk("— " + CFO.text() + ".", "Оргструктура", ["Справочник"])
        return [block], complete, [CFO]

    return directory


def _run_with_directory(mode: str, *, complete: bool = True, extra: int = 0):  # noqa: ANN202
    partial = decision(
        sufficiency="partial",
        search_query="финансовый директор добавочный",
        lookup_role="финансовый директор",
        missing="добавочный финансового директора",
    )
    decide = Script(partial, *([decision()] * extra))
    answer = Script("черновик", "Ответ [1].")
    run = run_decided(
        "Какой добавочный у того, кто возглавляет комиссию?",
        [Chunk("Комиссию возглавляет финансовый директор."), Chunk("б")],
        decide=decide,
        answer=answer,
        search=lambda q: [Chunk("в")],
        directory=_cfo_directory(complete),
        speculative=True,
        directory_answer=mode,
    )
    return run, answer.seen[-1]


def test_directory_answer_plain_keeps_entry_last() -> None:
    run, messages = _run_with_directory("plain")
    assert run.shown[-1].content.startswith("— Финансовый директор")
    assert not run.directory_first and run.contact == ""
    assert "Контакт по справочнику" not in messages[1].content


def test_directory_answer_first_moves_entry_to_top() -> None:
    """A1: выдержка справочника — первой; run.shown в том же порядке, что
    и в промпте (номера [n] и источники совпадают)."""
    run, messages = _run_with_directory("first")
    assert run.directory_first
    assert [b.content for b in run.shown][1:] == [
        "Комиссию возглавляет финансовый директор.",
        "б",
    ]
    assert run.shown[0].content.startswith("— Финансовый директор")
    user = messages[1].content
    assert user.index("[1] Оргструктура > Справочник") < user.index(
        "Комиссию возглавляет"
    )


def test_directory_answer_contact_adds_line_with_excerpt_number() -> None:
    """A2: строка контакта с номером выдержки справочника; запись остаётся
    выдержкой на прежнем месте."""
    run, messages = _run_with_directory("contact")
    assert run.shown[-1].content.startswith("— Финансовый директор")
    assert run.contact == (
        "Контакт по справочнику: Финансовый директор — Ярослава Игоревна "
        "Кукушкина, кабинет 402, доб. 1120; укажи его в ответе со ссылкой на "
        "выдержку [3]."
    )
    assert messages[1].content.endswith("\n\n" + run.contact)


def test_directory_answer_contact_only_on_complete_hit() -> None:
    """Частичное попадание (нет запрошенного поля) — строки нет, добор как
    раньше."""
    run, messages = _run_with_directory("contact", complete=False, extra=1)
    assert run.directory_partial == 1 and run.queries
    assert run.contact == ""
    assert "Контакт по справочнику" not in messages[1].content


def test_directory_answer_contact_without_entries_from_old_search() -> None:
    """Поиск справочника из двух элементов (без записей) — строки нет."""
    partial = decision(
        sufficiency="partial", search_query="ФИО", lookup_role="финансовый директор"
    )
    answer = Script("Кукушкина [2].")
    run = run_decided(
        "Кто?",
        [Chunk("а")],
        decide=Script(partial),
        answer=answer,
        search=no_search,
        directory=lambda role, missing: ([Chunk("Финансовый директор — К.")], True),
        directory_answer="contact",
    )
    assert run.directory_hits == 1 and run.contact == ""


def test_directory_answer_mode_checked() -> None:
    with pytest.raises(ValueError, match="directory_answer"):
        run_decided(
            "Кто?",
            [Chunk("а")],
            decide=Script(),
            answer=Script(),
            search=no_search,
            directory_answer="code",
        )


# --- Фаза 1: отсечение спекуляции, параллельный общий ответ, таймаут ---


def test_speculation_skip_rules() -> None:
    def skip(question: str, excerpts: int = 3, best: float | None = 0.5, *rules: str):
        return speculation_skip(
            question, excerpts, best, rules=rules, max_distance=0.59
        )

    assert skip("Назови ФИО и кабинет", 3, 0.5, "contact") == "contact"
    assert skip("Какой добавочный телефон у бухгалтерии?", 3, 0.5, "contact")
    assert skip("Сколько дней отпуска?", 3, 0.5, "contact") is None
    assert skip("Кто согласует закупку?", 3, 0.5, "who") == "who"
    assert skip("Кому писать заявление?", 3, 0.5, "who") == "who"
    assert skip("Сколько дней отпуска?", 3, 0.5, "who") is None
    assert skip("Сколько дней отпуска?", 3, 0.65, "gate") == "gate"
    assert skip("Сколько дней отпуска?", 3, 0.59, "gate") is None
    assert skip("Сколько дней отпуска?", 3, None, "gate") is None
    assert skip("Сколько дней отпуска?", 1, 0.5, "single") == "single"
    # Первое сработавшее правило — по порядку списка.
    assert skip("Кто (ФИО)?", 1, 0.65, "single", "contact") == "single"
    assert skip("Сколько?", 3, 0.5) is None
    with pytest.raises(ValueError):
        skip("Сколько?", 3, 0.5, "magic")


def test_wasted_reasons_recorded() -> None:
    run = run_decided(
        "Кто (ФИО)?",
        [Chunk("а")],
        decide=Script(
            decision(sufficiency="partial", search_query="ФИО директора"), decision()
        ),
        answer=Script("черновик [1]", "Кукушкина [2]."),
        search=lambda q: [Chunk("б")],
        speculative=True,
    )
    assert run.wasted_reasons == ["search"]
    assert [label for label, _ in run.steps] == ["spec", "decide", "decide", "answer"]


class ByPrompt:
    """Подделка ответа: общий ответ (build_general_messages) и ответ по
    выдержкам различает по сообщениям — вызовы идут из разных потоков."""

    def __init__(self, question: str, *, general: Completion, faq: Completion) -> None:
        self.general_messages = build_general_messages(question)
        self.general, self.faq = general, faq
        self.calls: list[str] = []

    def __call__(self, messages: list[Message]) -> Completion:
        if messages == self.general_messages:
            self.calls.append("general")
            return self.general
        self.calls.append("faq")
        return self.faq


def test_refusal_parallel_general_used_when_recheck_confirms() -> None:
    question = "Есть ли в компании ипотека?"
    nothing = decision(sufficiency="none")
    answer = ByPrompt(
        question,
        general=timed("В документах компании ответа нет. Обычно…", 2000.0),
        faq=timed("черновик", 1500.0),
    )
    run = run_decided(
        question,
        [Chunk("а")],
        decide=Timed((nothing, 3000.0)),
        decide_fast=Timed((nothing, 1000.0)),
        answer=answer,
        search=no_search,
        speculative=True,
        refusal_parallel=True,
    )
    assert run.route is Route.NOT_FOUND and run.needs_general
    assert run.general is not None and run.general.text.startswith("В документах")
    assert run.wasted_general == 0 and run.wasted == 1
    assert sorted(answer.calls) == ["faq", "general"]
    # low + max(medium, общий) = 1000 + 3000; спекулятивный — параллельно.
    assert run.wall_ms == 4000.0
    assert run.general in run.calls
    labels = [label for label, _ in run.steps]
    assert labels[:1] == ["spec"] and labels[1:] == ["fast", "recheck", "general∥"]


def test_refusal_parallel_general_wasted_when_recheck_flips() -> None:
    question = "Кто согласует закупку?"
    answer = ByPrompt(
        question,
        general=timed("В документах компании ответа нет.", 4000.0),
        faq=timed("Согласует директор [1].", 1000.0),
    )
    run = run_decided(
        question,
        [Chunk("а")],
        decide=Timed((decision(), 2000.0)),
        decide_fast=Timed((decision(sufficiency="none"), 1000.0)),
        answer=answer,
        search=no_search,
        speculative=True,
        refusal_parallel=True,
    )
    assert run.route is Route.ANSWER and run.text == "Согласует директор [1]."
    assert run.general is None and run.wasted_general == 1
    # Время — по самому долгому из пары (общий ответ 4 с): плата за параллель.
    assert run.wall_ms == 5000.0
    assert len(run.calls) == 4  # спекулятивный, low, medium, общий


def test_refusal_parallel_only_on_refusal_routes() -> None:
    # «хватает + роль» (R2) и отказ без быстрого решения — общий не запускается.
    question = "Кто согласует?"
    answer = ByPrompt(
        question,
        general=timed("общий", 1.0),
        faq=timed("Директор [1].", 1.0),
    )
    run = run_decided(
        question,
        [Chunk("а")],
        decide=Script(decision()),
        decide_fast=Script(decision(lookup_role="финансовый директор")),
        answer=answer,
        search=no_search,
        recheck="refusals+role",
        refusal_parallel=True,
    )
    assert "general" not in answer.calls and run.wasted_general == 0
    run = run_decided(
        "Есть ли ипотека?",
        [Chunk("а")],
        decide=Script(decision(sufficiency="none")),
        answer=Script(),
        search=no_search,
        refusal_parallel=True,
    )
    assert run.general is None and run.route is Route.NOT_FOUND


class Late:
    """Подделка решения, которое не уложилось в таймаут."""

    def __init__(self, elapsed_ms: float = 8000.0) -> None:
        self.elapsed_ms = elapsed_ms
        self.calls = 0

    def __call__(self, messages: list[Message]) -> Completion:
        self.calls += 1
        raise CallTimeoutError("cloudru", self.elapsed_ms)


def test_timeout_on_fast_falls_back_to_speculative_answer() -> None:
    slow = Script()
    run = run_decided(
        "Сколько дней отпуска?",
        [Chunk("а")],
        decide=slow,
        decide_fast=Late(),
        answer=Timed(("28 дней [1].", 1500.0)),
        search=no_search,
        speculative=True,
    )
    assert run.timeout and run.timeout_reason == "fast"
    assert run.text == "28 дней [1]." and run.wasted == 0 and run.invalid == 0
    assert slow.seen == [] and run.decision_calls == []
    assert run.wall_ms == 8000.0
    assert run.steps == [("spec", 1500.0), ("fast!timeout", 8000.0)]


def test_timeout_without_speculation_calls_answer() -> None:
    answer = Script("28 дней [1].")
    run = run_decided(
        "Сколько дней отпуска?",
        [Chunk("а")],
        decide=Late(),
        answer=answer,
        search=no_search,
    )
    assert run.timeout_reason == "fast" and run.route is Route.ANSWER
    assert run.text == "28 дней [1]." and len(answer.seen) == 1
    # Обычный ответ — faq-v2.7 по выдержкам (без строки контакта).
    assert answer.seen[0] == build_faq_messages("Сколько дней отпуска?", [Chunk("а")])


def test_timeout_without_excerpts_is_not_found() -> None:
    run = run_decided(
        "Есть ли ипотека?",
        [],
        decide=Late(),
        answer=Script(),
        search=no_search,
    )
    assert run.timeout and run.route is Route.NOT_FOUND and run.needs_general


def test_timeout_on_recheck_keeps_parallel_general_if_needed() -> None:
    question = "Есть ли ипотека?"
    late = Late(8000.0)
    answer = ByPrompt(
        question,
        general=timed("В документах компании ответа нет.", 2000.0),
        faq=timed(NOT_FOUND_ANSWER, 1000.0),
    )
    run = run_decided(
        question,
        [Chunk("а")],
        decide=late,
        decide_fast=Timed((decision(sufficiency="none"), 1000.0)),
        answer=answer,
        search=no_search,
        speculative=True,
        refusal_parallel=True,
    )
    # Перепроверка не уложилась — откат на спекулятивный ответ; он — отказ,
    # и готовый общий ответ пригодился.
    assert run.timeout_reason == "recheck" and late.calls == 1
    assert run.needs_general and run.general is not None and run.wasted_general == 0
    assert run.wall_ms == 9000.0  # low + max(8000, 2000)
    assert run.decisions[-1] == {"timeout": "recheck"}


def test_default_gates_are_known_rules() -> None:
    assert DEFAULT_GATES == ("gate",)  # 22.10: contact — только в shadow
    assert set(DEFAULT_GATES) <= set(SPECULATION_GATES)


def test_contact_rule_ignores_mail_service_and_personal_account() -> None:
    def contact(question: str) -> str | None:
        return speculation_skip(question, 3, 0.4, rules=("contact",), max_distance=0.59)

    assert contact("Назови ФИО и адрес его почты.") == "contact"
    assert contact("На какой адрес почты сообщить о конфликте?") == "contact"
    assert contact("Какая у него электронная почта?") == "contact"
    assert contact("Какой максимальный размер почтового ящика?") is None
    assert contact("Перестала работать корпоративная почта, какой приоритет?") is None
    assert contact("Статус в личном кабинете не появляется. Что делать?") is None
    assert contact("Можно ли подписать договор от имени компании?") is None
    assert contact("В какой комнате сидят айтишники?") == "contact"


def office(role: str, missing: str) -> tuple[Sequence[Chunk], bool]:
    return [Chunk("ОКП — руководитель Таисия Олеговна Пастилова, кабинет 301.")], True


def test_directory_on_refusal_hit_answers_with_entry() -> None:
    # T6-01 / T6-07: «ничего» при названной роли — справочник, попадание —
    # ответ по выдержкам с записью, а не отказ.
    none_role = decision(sufficiency="none", lookup_role="руководитель ОКП")
    answer = Script("Руководитель ОКП — Пастилова, кабинет 301 [2].")
    run = run_decided(
        "Кто руководитель ОКП и где кабинет?",
        [Chunk("а")],
        decide=Script(none_role),
        answer=answer,
        search=no_search,
        directory=office,
        directory_on_refusal=True,
    )
    assert run.route is Route.ANSWER and run.directory_hits == 1
    assert run.text.startswith("Руководитель ОКП") and not run.needs_general
    assert run.shown[-1].content.startswith("ОКП — руководитель")


def test_directory_on_refusal_miss_keeps_refusal() -> None:
    # Промах справочника при «ничего» — прежний отказ, а не ответ по тому,
    # что есть (при «не хватает» промах ведёт к добору / ответу).
    for record, expected in (
        (
            decision(sufficiency="none", lookup_role="директор по качеству"),
            Route.NOT_FOUND,
        ),
        (
            decision(
                match="other",
                subject="Старт-ИИ-2",
                excerpts_subject="Старт-ИИ-1",
                lookup_role="куратор программы",
            ),
            Route.OTHER_SUBJECT,
        ),
    ):
        calls: list[str] = []

        def miss(
            role: str, missing: str, calls: list[str] = calls
        ) -> tuple[Sequence[Chunk], bool]:
            calls.append(role)
            return [], False

        run = run_decided(
            "Кто куратор?",
            [Chunk("а")],
            decide=Script(record),
            answer=Script(),
            search=no_search,
            directory=miss,
            directory_on_refusal=True,
        )
        assert calls and run.directory_misses == 1
        assert run.route is expected and run.needs_general


def test_directory_on_refusal_partial_hit_keeps_refusal() -> None:
    # Запись есть, но без запрошенного поля — тоже прежний отказ.
    def partial(role: str, missing: str) -> tuple[Sequence[Chunk], bool]:
        return [Chunk("ОКП — руководитель Пастилова.")], False

    run = run_decided(
        "Какой телефон у руководителя ОКП?",
        [Chunk("а")],
        decide=Script(decision(sufficiency="none", lookup_role="руководитель ОКП")),
        answer=Script(),
        search=no_search,
        directory=partial,
        directory_on_refusal=True,
    )
    assert run.directory_partial == 1 and run.route is Route.NOT_FOUND


def test_directory_on_refusal_off_by_default_and_out_of_docs_miss() -> None:
    # По умолчанию справочник при отказе не спрашивается; вопрос вне
    # документов с ролью (группа C T7) при промахе — отказ и общий ответ.
    calls: list[str] = []

    def directory(role: str, missing: str) -> tuple[Sequence[Chunk], bool]:
        calls.append(role)
        return [], False

    none_role = decision(sufficiency="none", lookup_role="генеральный директор")
    run = run_decided(
        "Когда у генерального директора день рождения?",
        [Chunk("а")],
        decide=Script(none_role),
        answer=Script(),
        search=no_search,
        directory=directory,
    )
    assert calls == [] and run.route is Route.NOT_FOUND
    run = run_decided(
        "Когда у генерального директора день рождения?",
        [Chunk("а")],
        decide=Script(none_role),
        answer=Script(),
        search=no_search,
        directory=directory,
        directory_on_refusal=True,
    )
    assert calls == ["генеральный директор"] and run.needs_general


def test_directory_on_refusal_rechecks_and_runs_general_in_parallel() -> None:
    # «Про другое» с ролью: маршрут — справочник, но это отказ — перепроверка
    # и общий ответ параллельно; попадание — общий ответ выброшен.
    question = "Кто руководитель ОКП?"
    other = decision(
        match="other",
        subject="ОКП",
        excerpts_subject="ЦУП",
        lookup_role="руководитель ОКП",
    )
    answer = ByPrompt(
        question,
        general=timed("В документах компании ответа нет.", 2000.0),
        faq=timed("Пастилова, кабинет 301 [2].", 1000.0),
    )
    run = run_decided(
        question,
        [Chunk("а")],
        decide=Timed((other, 3000.0)),
        decide_fast=Timed((other, 1000.0)),
        answer=answer,
        search=no_search,
        directory=office,
        refusal_parallel=True,
        directory_on_refusal=True,
    )
    assert run.rechecked == 1 and run.recheck_reasons == ["refusal"]
    assert run.route is Route.ANSWER and run.directory_hits == 1
    assert run.general is None and run.wasted_general == 1
    assert sorted(answer.calls) == ["faq", "general"]
