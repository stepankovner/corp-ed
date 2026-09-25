"""Отчёт о пробелах: чего не хватает в документах компании (задача ML 3).

Ночная задача бэкенда (docs/backend-handoff.md) берёт вопросы из qa_log и:
1. classify_miss — делит промахи на классы. Клиенту показываем только gap
   («в базе нет»), остальное — внутренний мониторинг качества;
2. cluster_questions — группирует gap-вопросы по смыслу (векторы вопросов
   уже посчитаны при ответе, повторно эмбеддинг не нужен);
3. cluster_priority — какой пробел закрывать первым;
4. mask_pii → промпт prompts/gaps.py — название темы кластера и «какого
   материала не хватает». Персональные данные в LLM не уходят.

Все пороги — параметры (GapThresholds), не константы: их значения
подбираются на eval (задача 3.4) и зависят от модели эмбеддингов.
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray


class MissKind(StrEnum):
    ANSWERED = "answered"
    """Ответили и не получили 👎 — не промах."""
    GAP = "gap"
    """В базе нет: вектор не прошёл порог и полнотекст ничего не нашёл."""
    RETRIEVAL_MISS = "retrieval_miss"
    """В базе есть, но не нашли: сильный полнотекст при проваленном векторе,
    или ответили, а пользователь поставил 👎."""
    MODEL_REFUSAL = "model_refusal"
    """Выдержки прошли порог, а модель отказала."""
    OFF_TOPIC = "off_topic"
    """Вопрос не про работу («какая погода?»): вектор очень далеко."""
    UNCLEAR = "unclear"
    """Полнотекст что-то нашёл, но слабо: ни gap, ни retrieval_miss."""


@dataclass(frozen=True)
class MissSignals:
    """Что бэкенд пишет в qa_log при ответе."""

    best_vector_distance: float | None
    """Косинусное расстояние лучшего векторного кандидата; None — поиск пуст."""
    best_fulltext_score: float | None
    """ts_rank_cd лучшего полнотекстового совпадения; None — ветка пуста."""
    answer_given: bool
    feedback: int | None = None
    """+1 — 👍, -1 — 👎, None — оценки нет."""


@dataclass(frozen=True)
class GapThresholds:
    max_distance: float
    """Порог отказа по вектору — тот же, что RAG_FAQ_MAX_DISTANCE."""
    strong_fulltext: float
    """Полнотекст не ниже — «точное совпадение есть»: это retrieval_miss."""
    empty_fulltext: float = 0.0
    """Полнотекст не выше — считаем пустым. Запрос через OR почти всегда
    что-то находит по общим словам («грант»), поэтому «пусто» — это порог,
    а не буквально ноль строк."""
    off_topic_distance: float | None = None
    """Вектор дальше — вопрос не про работу. None — класс не используется."""

    def __post_init__(self) -> None:
        if self.empty_fulltext > self.strong_fulltext:
            raise ValueError("empty_fulltext must not exceed strong_fulltext")


def classify_miss(signals: MissSignals, thresholds: GapThresholds) -> MissKind:
    """Класс вопроса для отчёта о пробелах.

    Порядок проверок важен: ответ с 👎 — это «нашли не то» (retrieval_miss),
    даже если вектор прошёл порог; отказ при прошедшем пороге — вина модели
    или промпта, а не базы.
    """
    if signals.answer_given:
        return MissKind.RETRIEVAL_MISS if signals.feedback == -1 else MissKind.ANSWERED

    distance = signals.best_vector_distance
    if distance is not None and distance <= thresholds.max_distance:
        return MissKind.MODEL_REFUSAL

    score = signals.best_fulltext_score or 0.0
    if score >= thresholds.strong_fulltext:
        return MissKind.RETRIEVAL_MISS
    if score > thresholds.empty_fulltext:
        return MissKind.UNCLEAR
    if thresholds.off_topic_distance is not None and (
        distance is None or distance > thresholds.off_topic_distance
    ):
        return MissKind.OFF_TOPIC
    return MissKind.GAP


FloatMatrix = NDArray[np.float64]


def cluster_questions(
    vectors: Sequence[Sequence[float]] | FloatMatrix, *, max_distance: float
) -> list[int]:
    """Агломеративная кластеризация по косинусу, средняя связь.

    Сливаем два ближайших кластера, пока среднее косинусное расстояние
    между ними не больше max_distance. Возвращает номер кластера для
    каждого вопроса: 0, 1, … по порядку первого появления.

    Сложность O(n³) по времени в худшем случае и O(n²) по памяти: на
    ночной пачке до нескольких тысяч вопросов тенанта — секунды. Больше —
    ограничить окно (например, последние 30 дней).
    """
    matrix = np.asarray(vectors, dtype=np.float64)
    n = len(matrix)
    if n == 0:
        return []
    if matrix.ndim != 2:
        raise ValueError("vectors must be a 2-D array")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    unit = matrix / np.where(norms == 0, 1.0, norms)
    distance: FloatMatrix = 1.0 - unit @ unit.T
    np.fill_diagonal(distance, np.inf)

    members: list[list[int]] = [[i] for i in range(n)]
    active = np.ones(n, dtype=bool)
    while active.sum() > 1:
        masked = np.where(active[:, None] & active[None, :], distance, np.inf)
        flat = int(np.argmin(masked))
        a, b = divmod(flat, n)
        if masked[a, b] > max_distance:
            break
        size_a, size_b = len(members[a]), len(members[b])
        # Средняя связь (Ланс — Уильямс): расстояние нового кластера до
        # остальных — среднее, взвешенное по размерам.
        merged = (size_a * distance[a] + size_b * distance[b]) / (size_a + size_b)
        distance[a, :] = merged
        distance[:, a] = merged
        distance[a, a] = np.inf
        distance[b, :] = np.inf
        distance[:, b] = np.inf
        members[a].extend(members[b])
        members[b] = []
        active[b] = False

    labels = [0] * n
    order: dict[int, int] = {}
    for root in range(n):
        for index in members[root]:
            labels[index] = root
    for index in range(n):
        order.setdefault(labels[index], len(order))
    return [order[label] for label in labels]


@dataclass(frozen=True)
class ClusterQuestion:
    user_id: str
    asked_at: datetime


def cluster_priority(
    questions: Sequence[ClusterQuestion], *, now: datetime, half_life_days: float
) -> float:
    """Приоритет кластера = частота × уникальные пользователи × свежесть.

    Свежесть — среднее по вопросам 0.5 ** (возраст в днях / half_life_days):
    вопрос сегодняшний весит 1, вопрос возрастом в half_life — 0.5. Формула
    — предложение ML (задача 3.2); окончательную согласовать с Артёмом.
    """
    if not questions:
        return 0.0
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    users = len({q.user_id for q in questions})
    freshness = sum(
        math.pow(
            0.5, max(0.0, (now - q.asked_at).total_seconds() / 86400) / half_life_days
        )
        for q in questions
    ) / len(questions)
    return len(questions) * users * freshness


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(
    r"(?<![\w+])(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)"
)
_CARD = re.compile(r"(?<!\d)(?:\d{4}[\s-]?){3}\d{4}(?!\d)")
_SNILS = re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}[\s-]\d{2}(?!\d)")
_PASSPORT = re.compile(r"(?<!\d)\d{2}\s?\d{2}\s\d{6}(?!\d)")
_WORD = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?"
_PATRONYMIC = re.compile(
    r"[А-ЯЁ][а-яё]+(?:ович|евич|ьич|овна|евна|ична|инична)[а-я]{0,3}"
)
_INITIALS = re.compile(
    rf"\b{_WORD}\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.|\b[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?{_WORD}"
)
_CAPITALIZED_RUN = re.compile(rf"\b{_WORD}(?:\s+{_WORD})+\b")
_FIRST_NAMES = frozenset(
    [
        "александр",
        "алексей",
        "анатолий",
        "андрей",
        "антон",
        "аркадий",
        "артём",
        "артем",
        "богдан",
        "борис",
        "вадим",
        "валентин",
        "валерий",
        "василий",
        "виктор",
        "виталий",
        "владимир",
        "владислав",
        "влад",
        "всеволод",
        "вячеслав",
        "геннадий",
        "георгий",
        "глеб",
        "григорий",
        "даниил",
        "денис",
        "дмитрий",
        "евгений",
        "егор",
        "иван",
        "игорь",
        "илья",
        "кирилл",
        "константин",
        "лев",
        "леонид",
        "максим",
        "марк",
        "матвей",
        "михаил",
        "никита",
        "николай",
        "олег",
        "павел",
        "пётр",
        "петр",
        "роман",
        "руслан",
        "сергей",
        "семён",
        "семен",
        "степан",
        "тимофей",
        "тимур",
        "фёдор",
        "федор",
        "юрий",
        "ярослав",
        "александра",
        "алина",
        "алиса",
        "алла",
        "анастасия",
        "анна",
        "антонина",
        "валентина",
        "валерия",
        "вера",
        "вероника",
        "виктория",
        "галина",
        "дарья",
        "диана",
        "ева",
        "евгения",
        "екатерина",
        "елена",
        "елизавета",
        "жанна",
        "зоя",
        "инна",
        "ирина",
        "карина",
        "кира",
        "ксения",
        "лариса",
        "лидия",
        "любовь",
        "людмила",
        "маргарита",
        "марина",
        "мария",
        "надежда",
        "наталья",
        "наталия",
        "нина",
        "оксана",
        "ольга",
        "полина",
        "светлана",
        "софия",
        "софья",
        "таисия",
        "тамара",
        "татьяна",
        "ульяна",
        "юлия",
        "яна",
    ]
)
_NAME_STEMS = frozenset(
    name[:-1] if name[-1] in "аяйьо" else name for name in _FIRST_NAMES
)
_NOT_NAMES = frozenset(
    [
        "когда",
        "где",
        "как",
        "кто",
        "что",
        "чем",
        "почему",
        "зачем",
        "куда",
        "откуда",
        "можно",
        "нужно",
        "надо",
        "сколько",
        "какой",
        "какая",
        "какое",
        "какие",
        "каков",
        "если",
        "прошу",
        "подскажите",
        "скажите",
        "спросить",
        "уточнить",
        "уточните",
        "подайте",
        "согласовать",
        "передайте",
        "напишите",
        "позвоните",
        "направьте",
        "здравствуйте",
        "добрый",
        "привет",
        "по",
        "для",
        "при",
        "на",
        "в",
        "во",
        "с",
        "со",
        "к",
        "о",
        "об",
        "от",
        "до",
        "из",
        "у",
    ]
)


def _is_first_name(word: str) -> bool:
    """Имя в любом падеже: «Ольга», «Ольгу», «Ивану» (по основе, до +3 букв)."""
    word = word.lower().replace("ё", "е")
    if word in _FIRST_NAMES:
        return True
    return any(
        word.startswith(stem) and 0 < len(word) - len(stem) <= 3 for stem in _NAME_STEMS
    )


def _mask_person_run(match: re.Match[str]) -> str:
    """Ряд слов с заглавной: ФИО, если там отчество или известное имя.

    Служебные слова в начале фразы («Когда», «Спросить») фамилией не
    считаются и остаются в тексте.
    """
    words = match.group(0).split()
    lead: list[str] = []
    while words and words[0].lower() in _NOT_NAMES:
        lead.append(words.pop(0))
    has_patronymic = any(_PATRONYMIC.fullmatch(w) for w in words)
    has_name = any(_is_first_name(w) for w in words)
    if len(words) >= 2 and (has_patronymic or has_name):
        return " ".join([*lead, "[ФИО]"])
    return match.group(0)


def mask_pii(text: str) -> str:
    """Скрыть персональные данные перед отправкой вопросов в LLM.

    Почта, телефон, карта, СНИЛС, паспорт — по шаблону; ФИО с инициалами;
    ряд из 2+ слов с заглавной, если в нём есть отчество или имя из списка
    частых русских имён (в любом падеже). Лучше скрыть лишнее («Анна
    Каренина»), чем отправить живое ФИО.
    Имя без фамилии («спросить у Ольги») не скрывается: без контекста
    это не идентифицирует человека.
    """
    text = _EMAIL.sub("[почта]", text)
    text = _PHONE.sub("[телефон]", text)
    text = _SNILS.sub("[СНИЛС]", text)
    text = _CARD.sub("[карта]", text)
    text = _PASSPORT.sub("[паспорт]", text)
    text = _INITIALS.sub("[ФИО]", text)
    return _CAPITALIZED_RUN.sub(_mask_person_run, text)
