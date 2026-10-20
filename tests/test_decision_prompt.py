"""Промпт и схема шага решения (decision-v1), ответ по решению."""

from dataclasses import dataclass, field

from corp_ed.domain.decision import Decision
from corp_ed.prompts.decision import (
    build_answer_messages,
    build_decision_messages,
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


def test_edition_note() -> None:
    assert with_edition_note("Требования такие [1].", decision()) == (
        "В документах — «ГПБ Тех 2026», а не «ГПБ Тех 2025». Требования такие [1]."
    )
    assert with_edition_note("Ответ [1].", decision(match="same")) == "Ответ [1]."
    # Модель написала оговорку сама (edition-v1) — вторую не добавляем.
    own = "В документах — редакция 4 положения, а не третья. Не более 6 [1]."
    assert with_edition_note(own, decision()) == own
    # Не из чего построить и модель не написала — ответ как есть.
    unnamed = decision(subject="П-ОТП-07", excerpts_subject="П-ОТП-07")
    assert with_edition_note("Не более 6 [1].", unnamed) == "Не более 6 [1]."


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
