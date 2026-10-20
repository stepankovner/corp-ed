"""Решение до ответа: про то ли выдержки, хватает ли их, что делать дальше.

Разбор попытки 1 агента (ml-report.md, «Разбор причин», 20.10): три
решения — реплика ли это, про то ли выдержки, хватает ли их — были
спрятаны внутрь одного ответа модели и включались частными приёмами
(строка «Дополнительный поиск», служебная строка реплики, правило про
соседнюю программу). Модели Яндекса с выдержками в промпте инструмент
поиска сами не вызывают, а явный шаг решения проходят. Поэтому решение —
отдельный шаг со строгой схемой, а что делать дальше, решает код
(стратегия, решения Артёма 20.10 — ml-agent-plan.md §7.1).

Здесь — модель решения (единственный источник схемы: JSON-схему для
запроса строит prompts/decision.py из неё же) и маршрут. Чистые функции,
без сети. Порядок полей — порядок генерации: рассуждение первым, ответ
последним (если поставщик сохраняет порядок; у Cloud.ru — по алфавиту).
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

Kind = Literal["question", "small_talk"]
Match = Literal["same", "other_edition", "other", "n/a"]
Sufficiency = Literal["full", "partial", "none"]

NONE_MARK = "—"
MAX_QUERY_WORDS = 12


class Decision(BaseModel):
    """Решение модели по сообщению и выдержкам.

    reasoning — черновик рассуждения (вариант с ним; сотруднику не
    показывается, пишется в журнал); answer — ответ в варианте «решение и
    ответ одним вызовом». Оба необязательны: схема без них — те же поля.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reasoning: str = ""
    kind: Kind
    subject: str
    excerpts_subject: str
    match: Match
    sufficiency: Sufficiency
    missing: str
    search_query: str
    answer: str = ""


class Route(StrEnum):
    SMALL_TALK = "small_talk"
    OTHER_SUBJECT = "other_subject"
    SEARCH = "search"
    NOT_FOUND = "not_found"
    ANSWER = "answer"


def parse_decision(text: str) -> Decision | None:
    """Решение из ответа модели или None, если схема не соблюдена.

    Поставщик ограничивает вывод схемой на своей стороне, но не все
    одинаково строго — проверка здесь вторая линия. None — откат на
    обычный путь ответа.
    """
    try:
        return Decision.model_validate_json(text)
    except ValidationError:
        return None


def search_query(decision: Decision) -> str | None:
    """Запрос для добора: без «—» и кавычек, не длиннее MAX_QUERY_WORDS слов."""
    words = decision.search_query.strip(" «»\"'.").split()[:MAX_QUERY_WORDS]
    query = " ".join(words)
    return None if not query or query == NONE_MARK else query


def route(decision: Decision, *, can_search: bool) -> Route:
    """Что делать дальше.

    Порядок важен: реплика; выдержки про другое (при названном в вопросе) —
    «ответа нет», даже если модель сочла, что их хватает; ничего по теме;
    не хватает — добор, если он ещё возможен и есть запрос; иначе — ответ
    по тому, что есть (другой год или редакция — тоже ответ, с оговоркой).
    """
    if decision.kind == "small_talk":
        return Route.SMALL_TALK
    # «Про другое» — только относительно названного в вопросе: без subject
    # сравнивать не с чем (так и в схеме: match — при subject не «—»).
    if decision.match == "other" and _quoted(decision.subject):
        return Route.OTHER_SUBJECT
    if decision.sufficiency == "none":
        return Route.NOT_FOUND
    if decision.sufficiency == "partial" and can_search and search_query(decision):
        return Route.SEARCH
    return Route.ANSWER


def _quoted(name: str) -> str:
    name = name.strip().strip("«»\"' ")
    return f"«{name}»" if name and name != NONE_MARK else ""


def mismatch_note(decision: Decision) -> str:
    """«В документах — «X», а не «Y».» — для другого года и другой программы.

    Пусто, если модель не назвала, про что выдержки или о чём вопрос:
    оговорку из догадок не строим.
    """
    present, asked = _quoted(decision.excerpts_subject), _quoted(decision.subject)
    if not present or not asked or present.casefold() == asked.casefold():
        return ""
    return f"В документах — {present}, а не {asked}."
