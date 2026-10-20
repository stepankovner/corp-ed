"""Политика цикла агента с инструментом поиска (вариант 1, agent-v1).

Чистые функции без сети и БД: лимиты, разбор запроса инструмента,
служебная разметка вызова в тексте ответа, сторож сущностей. Решения
Артёма 20.10 и практики — `docs/ml-agent-plan.md` §7–8.
"""

import json
import re
from collections.abc import Iterable

MAX_EXTRA_SEARCHES = 2
"""Доборов на вопрос — не больше (решение Артёма 20.10). Параллельные
вызовы инструмента считаются каждый отдельно; после лимита — вызов модели
без инструментов: она обязана ответить."""

MAX_EXCERPTS = 8
"""Выдержек в контексте модели — не больше, как у второго шага поиска
(BH-43): длинный контекст и похожие, но не те фрагменты портят ответ."""

_RAW_TOOL_CALL = re.compile(
    r"\[TOOL_CALL_START\]|<tool_call>|\"name\"\s*:\s*\"search_documents\"",
    re.IGNORECASE,
)


def parse_search_query(arguments: str) -> str | None:
    """Запрос из аргументов вызова search_documents или None.

    Аргументы — строка JSON от поставщика (у gpt-oss кириллица экранами);
    невалидный JSON, нет поля query или оно пустое — None: модели уходит
    ответ «запрос не разобран», поиск не делается.
    """
    try:
        data = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    query = data.get("query")
    if not isinstance(query, str):
        return None
    query = " ".join(query.split())
    return query or None


def has_raw_tool_call(text: str) -> bool:
    """В тексте ответа — вызов инструмента разметкой, а не ответ.

    Так отвечает Flash при tool_choice="none" (проверка 20.10):
    «[TOOL_CALL_START]search_documents {"query": …}». Показывать
    сотруднику нельзя.
    """
    return _RAW_TOOL_CALL.search(text) is not None


_QUOTED = re.compile(r"«([^«»]{2,80})»")
_NUMBERED = re.compile(
    r"(?<![\w-])([A-Za-zА-Яа-яЁё]+(?:[-‐‑–—][A-Za-zА-Яа-яЁё]+)*[-‐‑–—]\d+(?:[.,]\d+)?)(?![\w-])"
)
_VERSIONED = re.compile(r"(?<![\w-])([A-Za-zА-Яа-яЁё]+\s+\d+\.\d+)(?![\w.])")
_DASHES = str.maketrans({"‐": "-", "‑": "-", "–": "-", "—": "-", " ": " "})


def _norm(text: str) -> str:
    text = text.casefold().replace("ё", "е").translate(_DASHES)
    return " ".join(text.split())


def named_entities(question: str) -> list[str]:
    """Названия программ и документов в вопросе, по которым сторож сверяет
    выдержки: текст в «…», слово с номером через дефис («Старт-ИИ-2»,
    «УМНИК-2025») и слово с версией («Спринт 3.0»). Остальные слова не
    берём — сторож должен срабатывать только на явные названия."""
    found: list[str] = []
    for pattern in (_QUOTED, _NUMBERED, _VERSIONED):
        for match in pattern.finditer(question):
            name = match.group(1).strip()
            if name and _norm(name) not in {_norm(f) for f in found}:
                found.append(name)
    return found


def missing_entities(question: str, texts: Iterable[str]) -> list[str]:
    """Названия из вопроса, которых нет ни в одном из текстов.

    Сторож (вариант «A1+сторож»): ответ по документам, а названной в
    вопросе программы нет в процитированных выдержках, — значит, модель
    перенесла условия соседней программы; ответ заменяется отказом.
    Сравнение — без регистра, «ё» = «е», все тире = «-».
    """
    haystack = " ".join(_norm(text) for text in texts)
    return [name for name in named_entities(question) if _norm(name) not in haystack]


_CITED = re.compile(r"\[(\d+)\]")


def cited_numbers(answer: str) -> list[int]:
    """Номера выдержек, на которые ссылается ответ, по порядку, без повторов."""
    seen: list[int] = []
    for match in _CITED.finditer(answer):
        number = int(match.group(1))
        if number not in seen:
            seen.append(number)
    return seen
