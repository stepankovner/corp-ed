from datetime import UTC, datetime, timedelta

import pytest

from corp_ed.domain.gaps import (
    ClusterQuestion,
    GapThresholds,
    MissKind,
    MissSignals,
    classify_miss,
    cluster_priority,
    cluster_questions,
    mask_pii,
)
from corp_ed.llm.types import Role
from corp_ed.prompts.gaps import (
    MAX_QUESTIONS,
    build_gap_messages,
    parse_gap_label,
)

THRESHOLDS = GapThresholds(
    max_distance=0.5, strong_fulltext=0.3, empty_fulltext=0.05, off_topic_distance=0.75
)


def _signals(
    distance: float | None,
    fulltext: float | None = None,
    answered: bool = False,
    feedback: int | None = None,
) -> MissSignals:
    return MissSignals(distance, fulltext, answered, feedback)


# --- classify_miss ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        (_signals(0.3, answered=True), MissKind.ANSWERED),
        (_signals(0.3, answered=True, feedback=1), MissKind.ANSWERED),
        # Ответили, но 👎 — нашли не то.
        (_signals(0.3, answered=True, feedback=-1), MissKind.RETRIEVAL_MISS),
        # Выдержки прошли порог, а модель отказала.
        (_signals(0.45, fulltext=0.0), MissKind.MODEL_REFUSAL),
        # Вектор провален, полнотекст нашёл точное совпадение (код формы).
        (_signals(0.6, fulltext=0.4), MissKind.RETRIEVAL_MISS),
        # Вектор провален, полнотекст пуст — в базе нет.
        (_signals(0.6, fulltext=None), MissKind.GAP),
        (_signals(0.6, fulltext=0.05), MissKind.GAP),
        # Полнотекст что-то нашёл, но слабо.
        (_signals(0.6, fulltext=0.1), MissKind.UNCLEAR),
        # Вектор очень далеко и полнотекст пуст — не про работу.
        (_signals(0.8, fulltext=0.0), MissKind.OFF_TOPIC),
        (_signals(None, fulltext=None), MissKind.OFF_TOPIC),
    ],
)
def test_classify_miss(signals: MissSignals, expected: MissKind) -> None:
    assert classify_miss(signals, THRESHOLDS) is expected


def test_off_topic_is_optional() -> None:
    thresholds = GapThresholds(max_distance=0.5, strong_fulltext=0.3)

    assert classify_miss(_signals(0.9), thresholds) is MissKind.GAP
    assert classify_miss(_signals(None), thresholds) is MissKind.GAP


def test_thresholds_are_validated() -> None:
    with pytest.raises(ValueError, match="empty_fulltext"):
        GapThresholds(max_distance=0.5, strong_fulltext=0.1, empty_fulltext=0.2)


# --- cluster_questions --------------------------------------------------------------


def test_cluster_questions_groups_by_cosine() -> None:
    vectors = [
        [1.0, 0.0, 0.0],
        [0.95, 0.05, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.9, 0.1],
        [0.0, 0.0, 1.0],
    ]

    labels = cluster_questions(vectors, max_distance=0.1)

    assert labels == [0, 0, 1, 1, 2]


def test_cluster_questions_ignores_vector_length() -> None:
    # Косинус: [2, 0] и [1, 0] — одно направление.
    assert cluster_questions([[2.0, 0.0], [1.0, 0.0]], max_distance=0.01) == [0, 0]


def test_cluster_questions_edge_cases() -> None:
    assert cluster_questions([], max_distance=0.2) == []
    assert cluster_questions([[1.0, 0.0]], max_distance=0.2) == [0]
    # Порог 0 — каждый вопрос сам по себе, кроме полных дублей.
    assert cluster_questions(
        [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], max_distance=0.0
    ) == [
        0,
        0,
        1,
    ]


def test_cluster_questions_average_linkage_stops_on_far_chain() -> None:
    # Цепочка a–b–c: соседние близко, a и c далеко. Средняя связь не должна
    # склеить c с кластером {a, b} при строгом пороге.
    vectors = [[1.0, 0.0], [0.9, 0.44], [0.6, 0.8]]

    assert cluster_questions(vectors, max_distance=0.12) == [0, 0, 1]


# --- cluster_priority ---------------------------------------------------------------

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def test_cluster_priority_counts_frequency_users_and_freshness() -> None:
    fresh = [ClusterQuestion("u1", NOW), ClusterQuestion("u2", NOW)]
    old = [ClusterQuestion("u1", NOW - timedelta(days=14))] * 2

    assert cluster_priority(fresh, now=NOW, half_life_days=14) == pytest.approx(4.0)
    # Те же два вопроса, но один пользователь и две недели назад: 2 × 1 × 0.5.
    assert cluster_priority(old, now=NOW, half_life_days=14) == pytest.approx(1.0)
    assert cluster_priority([], now=NOW, half_life_days=14) == 0.0


def test_cluster_priority_rejects_bad_half_life() -> None:
    with pytest.raises(ValueError):
        cluster_priority([ClusterQuestion("u", NOW)], now=NOW, half_life_days=0)


# --- mask_pii -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Пишите на ivan.petrov@corp.ru", "Пишите на [почта]"),
        ("Мой номер +7 (912) 345-67-89", "Мой номер [телефон]"),
        ("звонить 89123456789", "звонить [телефон]"),
        ("Иванов Иван Иванович одобрил?", "[ФИО] одобрил?"),
        ("согласовать с Петровым П.П.", "согласовать с [ФИО]"),
        ("отчёт у А. А. Смирновой", "отчёт у [ФИО]"),
        ("Когда Анна Сергеевна будет?", "Когда [ФИО] будет?"),
        ("Спросить Ольгу Кузнецову про отпуск", "Спросить [ФИО] про отпуск"),
        ("СНИЛС 123-456-789 01", "СНИЛС [СНИЛС]"),
        ("карта 1234 5678 9012 3456", "карта [карта]"),
        ("паспорт 45 06 123456", "паспорт [паспорт]"),
    ],
)
def test_mask_pii(text: str, expected: str) -> None:
    assert mask_pii(text) == expected


def test_mask_pii_keeps_work_terms() -> None:
    text = "Какой размер гранта Старт-ИИ-1 по Положению о конкурсе УМНИК-2026?"

    assert mask_pii(text) == text


# --- промпт gaps-v1 -----------------------------------------------------------------


def test_build_gap_messages_masks_and_caps_questions() -> None:
    questions = ["Как оформить командировку? Звонить +7 912 345 67 89", "  "] + [
        f"Вопрос {i}" for i in range(20)
    ]

    system, user = build_gap_messages(questions)

    assert system.role is Role.SYSTEM and user.role is Role.USER
    assert "+7 912" not in user.content and "[телефон]" in user.content
    assert f"{MAX_QUESTIONS}. " in user.content
    assert f"{MAX_QUESTIONS + 1}. " not in user.content


def test_build_gap_messages_needs_questions() -> None:
    with pytest.raises(ValueError):
        build_gap_messages(["", "  "])


@pytest.mark.parametrize(
    "answer",
    [
        '{"title": "Командировки", "missing": "Нет положения о командировках."}',
        '```json\n{"title": "«Командировки».", '
        '"missing": "Нет положения\\n о командировках."}\n```',
        "Тема: Командировки\nНет положения о командировках.",
    ],
)
def test_parse_gap_label(answer: str) -> None:
    label = parse_gap_label(answer)

    assert label.title == "Командировки"
    assert label.missing == "Нет положения о командировках."
