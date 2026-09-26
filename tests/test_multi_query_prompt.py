import pytest

from corp_ed.llm.types import Role
from corp_ed.prompts.multi_query import (
    MAX_COUNT,
    build_multi_query_messages,
    parse_queries,
)


def test_messages_carry_count_and_question_as_data() -> None:
    system, user = build_multi_query_messages("Сколько дней отпуска?", count=4)

    assert (system.role, user.role) == (Role.SYSTEM, Role.USER)
    assert "4 разными способами" in system.content
    assert "список из 4 строк" in system.content
    assert user.content == "Вопрос сотрудника:\nСколько дней отпуска?"
    assert "не инструкции" in system.content


def test_messages_mask_personal_data() -> None:
    _, user = build_multi_query_messages(
        "Мой телефон +7 999 123-45-67, как оформить ДМС?"
    )

    assert "123-45-67" not in user.content
    assert "ДМС" in user.content


@pytest.mark.parametrize("count", [0, MAX_COUNT + 1])
def test_count_is_bounded(count: int) -> None:
    with pytest.raises(ValueError):
        build_multi_query_messages("Вопрос?", count=count)


def test_empty_question_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_multi_query_messages("   ")


@pytest.mark.parametrize(
    "answer",
    [
        '{"queries": ["Продолжительность ежегодного отпуска", '
        '"отпуск дни количество"]}',
        '```json\n{"queries": ["Продолжительность ежегодного отпуска",\n'
        ' "отпуск дни количество"]}\n```',
        "1. Продолжительность ежегодного отпуска\n2) отпуск дни количество",
        "- Продолжительность ежегодного отпуска\n- отпуск дни количество",
    ],
)
def test_parse_queries_formats(answer: str) -> None:
    queries = parse_queries(answer, question="Сколько дней отпуска?", count=3)

    assert queries == ["Продолжительность ежегодного отпуска", "отпуск дни количество"]


def test_parse_drops_duplicates_original_and_extras() -> None:
    answer = (
        '{"queries": ["сколько дней ОТПУСКА", "Сколько дней отпуска?", '
        '"Длительность отпуска", "длительность  отпуска!", "", '
        '"Отпуск: сколько дней положено", "Норма отпуска"]}'
    )

    queries = parse_queries(answer, question="Сколько дней отпуска?", count=2)

    # Исходный вопрос и его копии выброшены, повтор без учёта регистра и
    # пунктуации — тоже; не больше count.
    assert queries == ["Длительность отпуска", "Отпуск: сколько дней положено"]


def test_parse_garbage_gives_nothing() -> None:
    assert parse_queries('{"other": 1}', question="Вопрос?") == []
    assert parse_queries("", question="Вопрос?") == []
