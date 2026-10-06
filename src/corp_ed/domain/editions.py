"""Новая редакция документа заменяет прежнюю — подсказка администратору (BH-41).

Чистые функции без БД. Когда в базе две редакции одного положения, ответ
приводит обе («данные расходятся»), хотя новая отменяет прежнюю (стенд
BH-38: 10 потерь из 140). Решение Артёма 06.10: какая редакция действует,
решает администратор — пометка «заменён» убирает документ из поиска.
Здесь — правило подсказки при загрузке: «Этот документ заменяет «…»?».
Ничего не убирается без подтверждения (`ml-report.md`, «Старая и новая
редакция»).

Кандидат — уже загруженный документ, у которого название совпадает с
новым с точностью до года и слов «редакция», «версия», а год в названии
старше; или название совпадает, а в тексте нового сказано, что прежняя
редакция утрачивает силу. Номера программ и очередей — не год: «Старт-ИИ-1
(очередь 2)» и «Старт-ИИ-2» — разные документы.
"""

import re
from collections.abc import Hashable, Mapping

_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_EDITION_WORDS = re.compile(r"\b(?:редакц\w*|ред|верси\w*|года|год|г)\b", re.IGNORECASE)
_SEPARATORS = re.compile(r"[\s_\-–—.,;:()\[\]«»\"']+")
_CANCELS_PREVIOUS = re.compile(
    r"(?:предыдущ|прежн)\w*\s+редакц\w*[^.]{0,120}?(?:утрачива\w*\s+сил|отменя\w*)"
    r"|взамен\s+(?:предыдущ|прежн)\w*\s+редакц",
    re.IGNORECASE,
)


def edition_key(title: str) -> str:
    """Название без года, слов «редакция», «версия» и разделителей."""
    text = title.casefold().replace("ё", "е")
    text = _YEAR.sub(" ", text)
    text = _SEPARATORS.sub(" ", text)
    text = _EDITION_WORDS.sub(" ", text)
    return " ".join(text.split())


def edition_year(title: str) -> int | None:
    """Самый поздний год в названии (1900–2099) или None."""
    years = [int(year) for year in _YEAR.findall(title)]
    return max(years) if years else None


def cancels_previous_edition(text: str) -> bool:
    """В тексте сказано, что предыдущая редакция утрачивает силу."""
    return _CANCELS_PREVIOUS.search(text) is not None


def superseded_candidates[T: Hashable](
    new_title: str, new_text: str, existing: Mapping[T, str]
) -> list[T]:
    """Какие из загруженных документов (id → название) новый, похоже, заменяет.

    Название совпадает с точностью до года и слов «редакция», «версия», и
    либо год нового позже, либо в тексте нового сказано, что прежняя
    редакция утрачивает силу (тогда год у прежнего может и не стоять).
    Порядок — как в existing. Пустой ключ названия (только год) — не
    сравниваем.
    """
    key = edition_key(new_title)
    if not key:
        return []
    new_year = edition_year(new_title)
    cancels = cancels_previous_edition(new_text)
    found = []
    for document, title in existing.items():
        if edition_key(title) != key:
            continue
        old_year = edition_year(title)
        newer = new_year is not None and old_year is not None and new_year > old_year
        older = new_year is not None and old_year is not None and new_year < old_year
        if newer or (cancels and not older):
            found.append(document)
    return found
