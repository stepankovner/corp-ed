"""Память диалога (ML-2): уточняющие вопросы сотрудника.

Решение Артёма 29.09: память нужна в MVP. Сотрудник пишет «А для УМНИК?»
или «А сколько это по времени?» — сам по себе такой вопрос поиск не
поймёт. Поэтому два шага, оба — только когда в диалоге уже есть реплики:

1. **Переписать вопрос в самостоятельный** (build_condense_messages →
   модель → parse_condensed): «А для УМНИК?» после вопроса про размер
   гранта Старт-ИИ-1 → «Какой размер гранта по программе УМНИК?». По
   переписанному вопросу идут поиск, порог и журнал пробелов. Если вопрос
   понятен сам по себе, модель возвращает его без изменений.
2. **Показать модели ответа историю** (format_history →
   build_faq_messages(..., history=…)): чтобы «а подробнее?» или «а это
   точно?» отвечались в контексте разговора. Факты — по-прежнему только
   из выдержек.

Урок M6 (25.09): переформулировки моделью уводили поиск к похожим, но
чужим фрагментам. Поэтому правило переписывания жёсткое: не расширять
вопрос, не добавлять фактов из ответов, самостоятельный вопрос —
без изменений. Проверка — на наборе диалогов (eval, задача ML-2).

Функции чистые: хранение реплик, вызов модели и журнал — у бэкенда
(контракт BH-28 в docs/backend-handoff.md).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from corp_ed.domain.tokens import count_tokens
from corp_ed.llm.types import Message, Role

CONDENSE_PROMPT_VERSION = "condense-v1"
"""Версия промпта переписывания. Бэкенду — писать в журнал рядом с
переписанным вопросом."""

HISTORY_PROMPT_VERSION = "dialogue-v1"
"""Версия блока истории в промпте ответа. Без истории промпт ответа
байт в байт тот же, что faq-v2.4."""

DEFAULT_HISTORY_TURNS = 3
"""Сколько последних пар «вопрос — ответ» брать из диалога."""

HISTORY_MAX_TOKENS = 600
"""Бюджет истории в промпте ответа (токены count_tokens). Выдержкам
остаётся свой бюджет — RAG_CONTEXT_MAX_TOKENS, историю он не съедает."""

ANSWER_PREVIEW_CHARS = 400
"""Сколько символов прошлого ответа показывать модели. Для понимания
вопроса хватает начала ответа; длинные ответы съели бы бюджет."""


@dataclass(frozen=True)
class Turn:
    """Одна пара реплик диалога: вопрос сотрудника и ответ ассистента."""

    question: str
    answer: str


_CITATION = re.compile(r"\s*\[\d+(?:\.\d+)*\]")

_CONDENSE_SYSTEM_PROMPT = """\
Ты помогаешь поиску по документам компании. Тебе дают начало диалога \
сотрудника с ассистентом и новый вопрос сотрудника. Перепиши новый вопрос \
так, чтобы он был понятен без диалога.

Правила:
1. Если новый вопрос понятен сам по себе, верни его без изменений.
2. Если вопрос ссылается на прошлые реплики («а для УМНИК?», «а сколько \
это?», «а в этом случае?»), подставь из диалога то, на что он ссылается: \
программу, документ, тему, условие.
3. Не отвечай на вопрос. Не добавляй сведений из ответов ассистента, \
которых нет в вопросах сотрудника, кроме названия того, о чём речь.
4. Не расширяй вопрос и не объединяй его с прошлыми: один вопрос — о том, \
что спрашивают сейчас.
5. Сохраняй слова сотрудника, опечатки не исправляй.
6. Выведи только текст вопроса — без кавычек, пояснений и слова «Вопрос».

Пример.
Сотрудник: Какой максимальный размер гранта в Старт-ИИ-1?
Ассистент: До 5 млн рублей.
Новый вопрос: А для УМНИК?
Ответ: Какой максимальный размер гранта в УМНИК?

Пример.
Сотрудник: Сколько длится проект по гранту Старт-ИИ-1?
Ассистент: 12 месяцев с даты договора.
Новый вопрос: Нужно ли платить НДС с гранта УМНИК?
Ответ: Нужно ли платить НДС с гранта УМНИК?"""


def recent_turns(
    turns: Sequence[Turn], max_turns: int = DEFAULT_HISTORY_TURNS
) -> list[Turn]:
    """Последние max_turns пар в хронологическом порядке."""
    if max_turns <= 0:
        return []
    return list(turns[-max_turns:])


def _preview(answer: str) -> str:
    text = " ".join(_CITATION.sub("", answer).split())
    if len(text) <= ANSWER_PREVIEW_CHARS:
        return text
    return text[:ANSWER_PREVIEW_CHARS].rstrip() + "…"


def format_history(
    turns: Sequence[Turn],
    *,
    max_turns: int = DEFAULT_HISTORY_TURNS,
    max_tokens: int = HISTORY_MAX_TOKENS,
) -> str:
    """История для промпта: «Сотрудник: … / Ассистент: …», новые реплики
    важнее старых — при нехватке бюджета отбрасываются самые старые.

    Ссылки [n] из прошлых ответов убираются: номера относятся к прошлым
    выдержкам и путали бы модель.
    """
    blocks: list[str] = []
    used = 0
    for turn in reversed(recent_turns(turns, max_turns)):
        block = (
            f"Сотрудник: {' '.join(turn.question.split())}\n"
            f"Ассистент: {_preview(turn.answer)}"
        )
        cost = count_tokens(block)
        if blocks and used + cost > max_tokens:
            break
        blocks.append(block)
        used += cost
    return "\n\n".join(reversed(blocks))


def build_condense_messages(
    history: Sequence[Turn], question: str, *, max_turns: int = DEFAULT_HISTORY_TURNS
) -> list[Message]:
    """Сообщения для переписывания вопроса в самостоятельный.

    Вызывать, только если history не пуст: без истории вопрос уже
    самостоятельный, вызов модели не нужен. Параметры вызова: температура 0,
    max_tokens ~100 (вопрос — одна строка).
    """
    dialogue = format_history(history, max_turns=max_turns)
    return [
        Message(role=Role.SYSTEM, content=_CONDENSE_SYSTEM_PROMPT),
        Message(
            role=Role.USER,
            content=(
                f"Диалог:\n{dialogue}\n\nНовый вопрос: {question.strip()}\nОтвет:"
            ),
        ),
    ]


_LABEL = re.compile(r"^\s*(?:ответ|вопрос|новый вопрос)\s*:\s*", re.IGNORECASE)
_QUOTES = "«»\"'„“”`"


def parse_condensed(text: str, question: str) -> str:
    """Переписанный вопрос из ответа модели; при сомнении — исходный.

    Исходный вопрос возвращается, если ответ пустой, многострочный текст
    без вопроса, подозрительно длинный (модель начала отвечать) или сам
    начинается с отказа.
    """
    original = question.strip()
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return original
    candidate = _LABEL.sub("", lines[0]).strip().strip(_QUOTES).strip()
    if not candidate:
        return original
    limit = max(3 * len(original), 200)
    if len(candidate) > limit:
        return original
    if candidate.casefold().startswith(("в документах", "я не", "не могу")):
        return original
    return candidate
