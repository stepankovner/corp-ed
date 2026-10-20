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


def test_answer_messages_edition_hint_only_for_other_edition() -> None:
    chunks = [Chunk("Правила 2026 года.")]
    same = build_answer_messages("Вопрос?", chunks, decision(match="same"))
    assert same == build_faq_messages("Вопрос?", chunks)
    edition = build_answer_messages("Вопрос?", chunks, decision())
    assert edition[0] == same[0]
    assert edition[-1].content.startswith(same[-1].content)
    assert "не отказывай" in edition[-1].content
    assert build_answer_messages("Вопрос?", chunks, None) == same


def test_edition_note() -> None:
    assert with_edition_note("Требования такие [1].", decision()) == (
        "В документах — «ГПБ Тех 2026», а не «ГПБ Тех 2025». Требования такие [1]."
    )
    assert with_edition_note("Ответ [1].", decision(match="same")) == "Ответ [1]."


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
