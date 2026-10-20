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
    lookup_role: str = NONE_MARK
    """Должность, подразделение или роль, чьи имя или контакт нужны
    (decision-v4): поиск по справочнику кодом вместо векторного добора."""
    answer: str = ""


class Route(StrEnum):
    SMALL_TALK = "small_talk"
    OTHER_SUBJECT = "other_subject"
    DIRECTORY = "directory"
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


def lookup_role(decision: Decision) -> str | None:
    """Кого искать в справочнике (decision-v4) или None.

    Прочерк дефисом или коротким тире («-», «–») — тоже «нет роли»: так
    модель иногда пишет вместо «—» (5 решений «хватает» на прогонах R и A4);
    иначе R2 переспрашивал бы их как «хватает + роль».
    """
    role = decision.lookup_role.strip(" «»\"'.-–—")
    return None if not role or role == NONE_MARK else role


def search_query(decision: Decision) -> str | None:
    """Запрос для добора: без «—» и кавычек, не длиннее MAX_QUERY_WORDS слов."""
    words = decision.search_query.strip(" «»\"'.").split()[:MAX_QUERY_WORDS]
    query = " ".join(words)
    return None if not query or query == NONE_MARK else query


def route(decision: Decision, *, can_search: bool, directory: bool = False) -> Route:
    """Что делать дальше.

    Порядок важен: реплика; выдержки про другое (при названном в вопросе) —
    «ответа нет», даже если модель сочла, что их хватает; ничего по теме;
    не хватает — справочник кодом, если он подключён и названа роль
    (decision-v4), иначе добор, если он ещё возможен и есть запрос; иначе —
    ответ по тому, что есть (другой год или редакция — тоже ответ, с
    оговоркой).

    Другой год или редакция — ответ и при «ничего» (решение Артёма 20.10,
    §7.1 п. 3): приёмка 3 — в 8 из 18 модель решения ставила «ничего»,
    оценивая выдержки по спрошенной редакции, хотя промпт велит оценивать
    по найденной. Если по теме в выдержках правда ничего нет, ответ
    откажет сам (правило 3 промпта ответа).
    """
    if decision.kind == "small_talk":
        return Route.SMALL_TALK
    # «Про другое» — только относительно названного в вопросе: без subject
    # сравнивать не с чем (так и в схеме: match — при subject не «—»).
    if decision.match == "other" and quoted(decision.subject):
        return Route.OTHER_SUBJECT
    if decision.sufficiency == "none" and decision.match != "other_edition":
        return Route.NOT_FOUND
    if decision.sufficiency == "partial" and directory and lookup_role(decision):
        return Route.DIRECTORY
    if decision.sufficiency == "partial" and can_search and search_query(decision):
        return Route.SEARCH
    return Route.ANSWER


NOTE_PREFIX = "В документах — "
"""Начало оговорки «В документах — «X», а не «Y».» (решение Артёма 20.10)."""


def quoted(name: str) -> str:
    """Название в «ёлочках» или пусто, если его нет («—»)."""
    name = name.strip().strip("«»\"' ")
    return f"«{name}»" if name and name != NONE_MARK else ""


def mismatch_note(decision: Decision) -> str:
    """«В документах — «X», а не «Y».» — для другого года и другой программы.

    Пусто, если модель не назвала, про что выдержки или о чём вопрос:
    оговорку из догадок не строим.
    """
    present, asked = quoted(decision.excerpts_subject), quoted(decision.subject)
    if not present or not asked or present.casefold() == asked.casefold():
        return ""
    return f"{NOTE_PREFIX}{present}, а не {asked}."


EDITION_CAUTION = "в запрошенной редакции условия могли отличаться"
"""Хвост оговорки при другом годе или редакции (решение Артёма 20.10,
вариант «в»): ответ — по редакции из документов, а в спрошенной (прошлой
или будущей) условия могли быть другими. Ставит код, не модель."""


def edition_note(decision: Decision) -> str:
    """«В документах — «X», а не «Y»; в запрошенной редакции условия могли
    отличаться.» Без названий из решения — общая оговорка, а не пусто: при
    другой редакции её нужно показать всегда."""
    note = mismatch_note(decision)
    if not note:
        note = f"{NOTE_PREFIX}другой год или редакция, чем в вопросе."
    return f"{note[:-1]}; {EDITION_CAUTION}."


def note_end(text: str) -> int | None:
    """Позиция точки, которой кончается оговорка «В документах — …, а не
    ….» в начале ответа: первая точка после «а не» вне «ёлочек» и перед
    пробелом или концом текста («Sprint 2.0», «01.09.2026», «ч. 1» внутри
    названий — не конец). None — точки нет."""
    start = text.find(" а не ")
    depth = 0
    for i in range(max(start, len(NOTE_PREFIX)), len(text)):
        char = text[i]
        if char == "«":
            depth += 1
        elif char == "»":
            depth = max(0, depth - 1)
        elif (
            char == "." and not depth and (i + 1 == len(text) or text[i + 1].isspace())
        ):
            return i
    return None
