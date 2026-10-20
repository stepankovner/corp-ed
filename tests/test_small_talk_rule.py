"""Реплики правилом (domain/small_talk.py): узко и без ложных срабатываний."""

import csv
from pathlib import Path

import pytest

from corp_ed.domain.small_talk import classify, rule_reply

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("Привет", "greeting"),
        ("Привет!!!", "greeting"),
        ("Здравствуйте, коллеги", "greeting"),
        ("добрый день, ассистент", "greeting"),
        ("Hi", "greeting"),
        ("Спасибо большое)))", "thanks"),
        ("спасибо", "thanks"),
        ("Благодарю за ответ!", None),  # не из списка — модели
        ("спасибо за ответ", "thanks"),
        ("Пока!", "bye"),
        ("ок", "ack"),
        ("Понял, спасибо", "thanks"),
        ("Кто ты?", "about"),
        ("ты бот?", "about"),
        ("Ты бот или человек?", "about"),
        ("Что ты умеешь?", "about"),
        ("как тебя зовут", "about"),
        ("А ты меня понимаешь?", "about"),
        ("Привет, спасибо!", "thanks"),
    ],
)
def test_classify(message: str, kind: str | None) -> None:
    assert classify(message) == kind


@pytest.mark.parametrize(
    "message",
    [
        "Привет! Какой лимит на такси?",
        "Спасибо, а кто согласует отсрочку платежа?",
        "Кто ты по должности в компании?",
        "Что ты знаешь о положении об отпусках?",
        "Сколько будет 2+2?",
        "Какой сегодня день?",
        "Расскажи анекдот",
        "Да, хочу оформить отпуск",
        "Привет, подскажи телефон бухгалтерии",
        "",
        "   ",
    ],
)
def test_not_small_talk(message: str) -> None:
    assert classify(message) is None


def test_reply_text() -> None:
    assert rule_reply("Привет").startswith("Здравствуйте")  # type: ignore[union-attr]
    assert "документ" in rule_reply("кто ты?")  # type: ignore[arg-type]
    assert rule_reply("Какой лимит на такси?") is None


def _rows(name: str) -> list[dict[str, str]]:
    with (ROOT / "eval" / name).open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def test_existing_sets_no_false_positives() -> None:
    """Вопросы по документам и общие вопросы правило не трогает; на
    чистых репликах старых наборов — ловит хотя бы половину (остальное —
    модели: правило узкое намеренно)."""
    rows = _rows("small_talk.csv") + _rows("small_talk_fresh.csv")
    questions = [
        r for r in rows if r["bucket"] in ("question_docs", "question_general")
    ]
    assert all(classify(r["question"]) is None for r in questions), [
        r["question"] for r in questions if classify(r["question"])
    ]
    small = [r for r in rows if r["bucket"] == "small_talk"]
    caught = sum(classify(r["question"]) is not None for r in small)
    assert caught >= len(small) // 2
