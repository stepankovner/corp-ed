"""Строка запроса для полнотекстовой ветки гибридного поиска (M1).

Проблема. ТЗ предлагает websearch_to_tsquery('russian', :q). Но эта
функция соединяет все слова вне кавычек оператором & (И). Для вопроса
на естественном языке — «Сколько дней отпуска положено сотруднику?» —
чанк найдётся, только если в нём есть ВСЕ значимые слова вопроса
(«положено», «сотруднику» …). На живых вопросах ветка почти всегда
пуста. BM25 у Битрикс24 и МТС работает иначе: любое совпадение даёт
вклад, больше совпадений — выше ранг.

Решение. Передавать в websearch_to_tsquery строку «слово1 or слово2 …»:
«or» эта функция понимает как |. Стоп-слова («сколько», «ли», «в»)
PostgreSQL выкинет сам по словарю russian, а ts_rank_cd поднимет чанки,
где совпало больше слов и они стоят ближе.

Со стороны SQL (бэкенд):
    websearch_to_tsquery('russian', :q)   -- q = to_fulltext_query(question)

Что отбрасывается здесь, до SQL:
- знаки препинания и спецсимволы: «-» перед словом в websearch_to_tsquery
  значит НЕ, кавычки — фразовый поиск, их из вопроса пропускать нельзя;
- слово «or» из самого вопроса (иначе оно станет оператором).
"""

import re
from collections.abc import Container, Iterable

_WORD = re.compile(r"[\w]+(?:[-.][\w]+)*", re.UNICODE)
_OPERATOR_WORDS = {"or", "and", "not"}


def to_fulltext_query(text: str) -> str:
    """Вопрос → «слово1 or слово2 or …» для websearch_to_tsquery.

    Слова с дефисом и точкой внутри («Старт-ИИ-1», «3.2», «1С:ERP» → «1С»,
    «ERP») сохраняются как есть: словарь russian разберёт их сам. Повторы
    убираются, порядок первого появления сохраняется.
    """
    words: list[str] = []
    seen: set[str] = set()
    for match in _WORD.finditer(text):
        word = match.group(0).strip("_")
        key = word.casefold()
        if not word or key in _OPERATOR_WORDS or key in seen:
            continue
        seen.add(key)
        words.append(word)
    return " or ".join(words)


def lexeme_coverage(question: Iterable[str], fragment: Container[str]) -> float:
    """Покрытие: доля лексем вопроса, которые есть в фрагменте (0…1).

    Порог «нашли» для поиска только по словам (BH-51 (а), замер ML 23.10):
    у лучшего по рангу фрагмента покрытие не ниже порога — выдержки идут в
    модель, ниже — «ответа в документах нет». В отличие от ts_rank_cd не
    зависит от длины фрагмента и числа повторов слова, поэтому порог из
    офлайн-замера переносится в продукт как есть.

    Лексемы считает вызывающий — словарём russian (стоп-слова выброшены,
    основы — стеммер Snowball), повторы не важны:
    - продукт: question = tsvector_to_array(to_tsvector('russian', :q)),
      fragment = tsvector_to_array(chunks.fts);
    - eval: eval.bm25.tokenize.

    Вопрос без лексем (одни знаки или стоп-слова) — 0: искать нечем.
    """
    terms = set(question)
    if not terms:
        return 0.0
    return sum(1 for term in terms if term in fragment) / len(terms)
