"""Цикл «решение до ответа» (eval.decision_loop) на подделках модели и поиска."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from corp_ed.domain.decision import Route
from corp_ed.llm.types import Message
from corp_ed.prompts.faq import NOT_FOUND_ANSWER, SMALL_TALK_PREFIX
from eval.decision_loop import run_decided
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
