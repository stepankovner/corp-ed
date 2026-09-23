"""Расширение вопроса словарём сокращений компании (M5).

Проблема (Альфа-Банк): векторный поиск не находил систему, когда
пользователь писал её аббревиатуру («Кто архитектор системы АО?»).
Помог единый справочник сокращений для нормализации запроса.

У нас словарь живёт на уровне тенанта — таблица glossary(term, expansion),
её заполняет админ компании. expand_query дописывает расшифровки к вопросу
перед эмбеддингом и полнотекстовым поиском:

    «Как оформить ДМС?» + {"ДМС": "добровольное медицинское страхование"}
    → «Как оформить ДМС? (ДМС — добровольное медицинское страхование)»

Дописываем, а не заменяем: в документах может встречаться и сокращение,
и полная форма. Для полнотекстовой ветки строку потом пропускать через
to_fulltext_query — там все слова соединятся через «or».

Сравнить с multi-query (M6) на eval: если словарь закрывает большую часть
случаев, лишний вызов LLM на переформулировки не нужен.
"""

import re
from collections.abc import Mapping

_CYRILLIC_ENDING = "[а-яё]{0,3}"


def expand_query(question: str, glossary: Mapping[str, str]) -> str:
    """Дописать к вопросу расшифровки найденных в нём терминов.

    Термин ищется как отдельное слово без учёта регистра. У сокращения,
    написанного заглавными («ДМС», «СЭД»), допускается падежное окончание:
    «по ДМСу», «в СЭДе». Термин, чья расшифровка уже есть в вопросе,
    не дописывается повторно. Порядок расшифровок — порядок появления
    терминов в вопросе.
    """
    found: list[tuple[int, str, str]] = []
    folded_question = question.casefold()

    for term, expansion in glossary.items():
        term = term.strip()
        expansion = expansion.strip()
        if not term or not expansion or expansion.casefold() in folded_question:
            continue
        match = _term_pattern(term).search(question)
        if match is not None:
            found.append((match.start(), term, expansion))

    if not found:
        return question

    found.sort(key=lambda item: item[0])
    notes = "; ".join(f"{term} — {expansion}" for _, term, expansion in found)
    return f"{question.rstrip()} ({notes})"


def _term_pattern(term: str) -> re.Pattern[str]:
    """Шаблон поиска термина.

    Сокращение заглавными: либо оно же заглавными с окончанием строчными
    («ДМСу»), либо целиком в любом регистре («дмс»). Окончание в любом
    регистре дало бы ложные совпадения: «ОС» + «ень» = «осень».
    """
    escaped = re.escape(term)
    if term.isupper() and len(term) >= 2:
        return re.compile(
            rf"(?<!\w)(?:{escaped}{_CYRILLIC_ENDING}|(?i:{escaped}))(?!\w)"
        )
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)
