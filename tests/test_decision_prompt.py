"""Промпт и схема шага решения (decision-v1), ответ по решению."""

from dataclasses import dataclass, field

from corp_ed.domain.decision import Decision
from corp_ed.domain.directory import Entry
from corp_ed.prompts.decision import (
    build_answer_messages,
    build_decision_messages,
    contact_note,
    decision_response_format,
    decision_schema,
    decision_system_prompt,
    with_edition_note,
    with_other_subject_note,
)
from corp_ed.prompts.faq import (
    _SYSTEM_PROMPT,
    GENERAL_ANSWER_PREFIX,
    NOT_FOUND_ANSWER,
    build_faq_messages,
)


@dataclass
class Chunk:
    content: str
    title: str = "Регламент закупок"
    heading_path: list[str] = field(default_factory=list)


def decision(**fields: str) -> Decision:
    base = {
        "kind": "question",
        "subject": "ГПБ Тех 2025",
        "excerpts_subject": "ГПБ Тех 2026",
        "match": "other_edition",
        "sufficiency": "full",
        "missing": "—",
        "search_query": "—",
    }
    return Decision.model_validate({**base, **fields})


def test_schema_strict_and_ordered() -> None:
    plain = decision_schema(reasoning=False, answer=False)
    assert plain["additionalProperties"] is False
    assert plain["required"] == list(plain["properties"])
    assert "reasoning" not in plain["properties"]
    assert "answer" not in plain["properties"]
    assert plain["properties"]["match"]["enum"] == [
        "same",
        "other_edition",
        "other",
        "n/a",
    ]
    full = decision_schema(reasoning=True, answer=True)
    names = list(full["properties"])
    # Модель пишет слева направо: рассуждение — до решения, ответ — после.
    assert names[0] == "reasoning"
    assert names[-1] == "answer"
    assert all("title" not in spec for spec in full["properties"].values())


def test_response_format() -> None:
    fmt = decision_response_format(reasoning=True, answer=False)
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert "reasoning" in fmt["json_schema"]["schema"]["properties"]


def test_system_prompt_variants() -> None:
    plain = decision_system_prompt(reasoning=False, answer=False)
    assert "reasoning" not in plain
    assert "answer —" not in plain
    assert "reasoning —" in decision_system_prompt(reasoning=True, answer=False)
    merged = decision_system_prompt(reasoning=True, answer=True)
    # Ответ одним вызовом — по правилам ответа faq-v2.7.
    assert "Правила:" in merged and "answer —" in merged


def test_decision_messages_put_excerpts_in_user() -> None:
    messages = build_decision_messages("Кто согласует закупку?", [Chunk("Текст.")])
    assert messages[0].role.value == "system"
    assert "Текст." not in messages[0].content
    assert "[1] Регламент закупок" in messages[1].content
    assert "(конец выдержки [1])" in messages[1].content
    empty = build_decision_messages("Привет!", [])
    assert "(выдержек нет)" in empty[1].content


def test_answer_messages_faq_unless_other_edition() -> None:
    chunks = [Chunk("Правила 2026 года.")]
    same = build_answer_messages("Вопрос?", chunks, decision(match="same"))
    assert same == build_faq_messages("Вопрос?", chunks)
    assert same[0].content == _SYSTEM_PROMPT  # faq-v2.7 байт в байт
    assert build_answer_messages("Вопрос?", chunks, None) == same
    other = build_answer_messages("Вопрос?", chunks, decision(match="other"))
    assert other == same


def test_edition_answer_is_its_own_task() -> None:
    chunks = [Chunk("Правила 2026 года.")]
    system, user = build_answer_messages("Какие требования?", chunks, decision())
    # Правило 4 про другую программу и пример «Старт» → «Развитие» заменены,
    # остальные правила faq-v2.7 — те же.
    assert NOT_FOUND_ANSWER not in system.content.split("4. ")[1].split("5. ")[0]
    assert "Отвечай по той редакции, что в выдержках" in system.content
    assert "„Развитие“" not in system.content
    assert "Положение о премировании (редакция 2026 года)" in system.content
    rest = system.content.split("4. ")[0], system.content.split("5. ")[1][:300]
    assert rest == (
        _SYSTEM_PROMPT.split("4. ")[0],
        _SYSTEM_PROMPT.split("5. ")[1][:300],
    )
    # Что спросили и что нашли — из решения; выдержки и вопрос — как в faq.
    assert "[1] Регламент закупок" in user.content
    assert "Вопрос сотрудника: Какие требования?" in user.content
    assert "Вопрос — про «ГПБ Тех 2025», а выдержки — про «ГПБ Тех 2026»" in (
        user.content
    )
    assert user.content.rstrip().endswith("ничего нет по теме вопроса.")
    assert "Правила 2026 года." not in system.content


def test_edition_facts_without_names() -> None:
    chunks = [Chunk("Положение П-ОТП-07, редакция 4.")]
    same_name = decision(subject="П-ОТП-07", excerpts_subject="П-ОТП-07")
    user = build_answer_messages("Вопрос?", chunks, same_name)[-1].content
    assert "Вопрос — про «П-ОТП-07», а в выдержках — другой год" in user
    unnamed = decision(subject="—", excerpts_subject="—")
    user = build_answer_messages("Вопрос?", chunks, unnamed)[-1].content
    assert "В выдержках — другой год, редакция или дата" in user


CAUTION = "; в запрошенной редакции условия могли отличаться."


def test_edition_note() -> None:
    # Оговорка из решения — с хвостом про запрошенную редакцию (вариант «в»).
    assert with_edition_note("Требования такие [1].", decision()) == (
        f"В документах — «ГПБ Тех 2026», а не «ГПБ Тех 2025»{CAUTION} "
        "Требования такие [1]."
    )
    assert with_edition_note("Ответ [1].", decision(match="same")) == "Ответ [1]."
    # Модель написала оговорку сама (edition-v1) — второй нет, хвост — в её конец.
    own = "В документах — редакция 4 положения, а не третья. Не более 6 [1]."
    assert with_edition_note(own, decision()) == (
        f"В документах — редакция 4 положения, а не третья{CAUTION} Не более 6 [1]."
    )
    # Точки внутри названий и «ёлочек» — не конец оговорки.
    dotted = (
        "В документах — «Приоритетные направления Sprint 2.0 (24.04.2026)», "
        "а не «Спринт 3.0». Направления такие [1]."
    )
    assert with_edition_note(dotted, decision()) == (
        "В документах — «Приоритетные направления Sprint 2.0 (24.04.2026)», "
        f"а не «Спринт 3.0»{CAUTION} Направления такие [1]."
    )
    dated = "В документах — оргструктура на 01.09.2026, а не на январь. Кукушкина [1]."
    assert with_edition_note(dated, decision()).startswith(
        f"В документах — оргструктура на 01.09.2026, а не на январь{CAUTION} Кук"
    )
    # Хвост уже есть — не дублируем; ответ без точки — хвост в конец.
    once = with_edition_note(own, decision())
    assert with_edition_note(once, decision()) == once
    assert with_edition_note("В документах — редакция 4, а не 3", decision()) == (
        f"В документах — редакция 4, а не 3{CAUTION}"
    )
    # Не из чего построить и модель не написала — общая оговорка, не пусто.
    unnamed = decision(subject="П-ОТП-07", excerpts_subject="П-ОТП-07")
    assert with_edition_note("Не более 6 [1].", unnamed) == (
        f"В документах — другой год или редакция, чем в вопросе{CAUTION} "
        "Не более 6 [1]."
    )


def test_other_subject_note_goes_after_marker() -> None:
    other = decision(
        match="other", subject="Старт-ИИ-2", excerpts_subject="Старт-ИИ-1 (очередь 2)"
    )
    general = f"{GENERAL_ANSWER_PREFIX}\nОбщие сведения."
    noted = with_other_subject_note(general, other)
    assert noted.startswith(
        f"{NOT_FOUND_ANSWER} В документах — «Старт-ИИ-1 (очередь 2)», "
        "а не «Старт-ИИ-2»."
    )
    assert noted.endswith("Общие сведения.")
    assert with_other_subject_note(NOT_FOUND_ANSWER, other).endswith("«Старт-ИИ-2».")
    assert with_other_subject_note(general, decision()) == general


def test_contact_note_from_entries() -> None:
    """A2: строка контакта собирается кодом из записей справочника."""
    cfo = Entry(
        role="Финансовый директор",
        unit="",
        name="Ярослава Игоревна Кукушкина",
        room="402",
        phone="1120",
        email="kukushkina@stk-test.example",
    )
    assert contact_note([cfo], 4) == (
        "Контакт по справочнику: Финансовый директор — Ярослава Игоревна "
        "Кукушкина, кабинет 402, доб. 1120, kukushkina@stk-test.example; укажи "
        "его в ответе со ссылкой на выдержку [4]."
    )
    hr = Entry(role="", unit="Отдел персонала", name="", room="210")
    assert contact_note([hr], 1) == (
        "Контакт по справочнику: Отдел персонала — кабинет 210; укажи его в "
        "ответе со ссылкой на выдержку [1]."
    )
    two = contact_note([cfo, hr], 2)
    assert two.startswith("Контакты по справочнику: Финансовый директор")
    assert "; Отдел персонала — кабинет 210; укажи нужный" in two
    assert contact_note([cfo, hr, cfo], 2) == ""  # неоднозначно — без строки
    assert contact_note([], 1) == ""


def test_answer_messages_with_contact_keep_faq_prompt() -> None:
    """Строка контакта — только абзац в конце сообщения пользователя;
    без неё сообщения байт в байт faq-v2.7."""
    chunks = [Chunk("Отчёт сдаётся главному бухгалтеру."), Chunk("— Бухгалтерия.")]
    plain = build_answer_messages("Куда?", chunks, decision(match="same"), contact="")
    assert plain == build_faq_messages("Куда?", chunks)
    note = "Контакт по справочнику: X; укажи его в ответе со ссылкой на выдержку [2]."
    got = build_answer_messages("Куда?", chunks, decision(match="same"), contact=note)
    assert got[0] == plain[0] and got[0].content == _SYSTEM_PROMPT
    assert got[1].content == plain[1].content + "\n\n" + note
    # Другая редакция — своя постановка, строку не ставим.
    edition = build_answer_messages("Куда?", chunks, decision(), contact=note)
    assert note not in edition[1].content
