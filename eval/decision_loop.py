"""Цикл «решение до ответа» для eval (попытки 2 и 3: правило, параллельный ответ).

Попытка 3 (ml-agent-plan.md §9, протокол в ml-report.md): rule=True —
реплика целиком и вопрос об ассистенте отвечаются правилом в коде, без
модели и поиска; speculative=True — ответ Flash по первым выдержкам
запускается одновременно с решением (гайд OpenAI по задержкам: проверка и
генерация параллельно, генерацию отбрасывают при отказе). Время цикла —
wall_ms: параллельная пара считается как максимум, остальное — суммой.
Стрима до вердикта нет.

Модель, поиск и ответ подставляются функциями — цикл тестируется на
подделках, а offline_e2e подключает его к нынешнему поиску, порогам BH-37
и общему ответу. Маршрут — corp_ed.domain.decision.route: реплика; про
другое — «ответа нет» с оговоркой; ничего по теме — общий ответ; не хватает
— добор (не больше max_extra, выдержек не больше max_excerpts) и снова
решение; иначе — ответ по тому, что есть. Решение не прошло схему — откат на
обычный ответ по выдержкам.
"""

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from corp_ed.domain.agent import MAX_EXCERPTS, MAX_EXTRA_SEARCHES, drop_citations
from corp_ed.domain.decision import (
    Decision,
    Route,
    lookup_role,
    parse_decision,
    route,
    search_query,
)
from corp_ed.domain.small_talk import rule_reply
from corp_ed.llm.types import Message
from corp_ed.prompts.decision import (
    build_answer_messages,
    build_decision_messages,
    with_edition_note,
)
from corp_ed.prompts.faq import (
    _NOT_FOUND_ANYWHERE,
    NOT_FOUND_ANSWER,
    SourceChunk,
    build_faq_messages,
    build_general_messages,
    is_not_found,
    small_talk_reply,
)
from eval.yandex import Completion

Ask = Callable[[list[Message]], Completion]


@dataclass
class DecidedRun:
    """Итог цикла: текст для сотрудника (до общего ответа) и что было по дороге."""

    text: str
    route: Route
    shown: list[Any]
    decision: Decision | None = None
    """Последнее решение (по нему — оговорки)."""
    decisions: list[dict[str, Any]] = field(default_factory=list)
    calls: list[Completion] = field(default_factory=list)
    decision_calls: list[Completion] = field(default_factory=list)
    """Вызовы шага решения (входят и в calls): их цена и время — отдельно,
    у решения может быть другой поставщик."""
    queries: list[str] = field(default_factory=list)
    added: list[int] = field(default_factory=list)
    invalid: int = 0
    """Решений, не прошедших схему (откат на обычный ответ)."""
    exhausted: bool = False
    """Хотели добрать, но добор ничего нового не дал или лимит."""
    small_talk: bool = False
    by_rule: bool = False
    """Ответ дан правилом в коде, без модели."""
    speculated: bool = False
    """Ответ Flash запускался параллельно с решением."""
    wasted: int = 0
    """Спекулятивных ответов выброшено (их токены — в calls)."""
    directory_hits: int = 0
    """Справочник дал записи (decision-v4) — ответ сразу, без второго решения."""
    directory_misses: int = 0
    """Справочник ничего не дал — векторный добор, как раньше."""
    directory_partial: int = 0
    """Роль в справочнике есть, но без запрошенного поля (почты, телефона):
    записи — выдержкой, и векторный добор, как раньше."""
    wall_ms: float = 0.0
    """Время по стене: параллельная пара — максимум, остальное — сумма."""

    @property
    def needs_general(self) -> bool:
        """Ответа по документам нет — дальше общий ответ с пометкой (Р1)."""
        return not self.small_talk and is_not_found(self.text)


def _key(block: Any) -> str:
    return str(getattr(block, "content", block))


def run_decided(
    question: str,
    initial: Sequence[SourceChunk],
    *,
    decide: Ask,
    answer: Ask,
    search: Callable[[str], Sequence[SourceChunk]],
    reasoning: bool = False,
    merged: bool = False,
    rule: bool = False,
    speculative: bool = False,
    directory: Callable[[str, str], tuple[Sequence[SourceChunk], bool]] | None = None,
    max_extra: int = MAX_EXTRA_SEARCHES,
    max_excerpts: int = MAX_EXCERPTS,
) -> DecidedRun:
    shown: list[Any] = list(initial)[:max_excerpts]
    run = DecidedRun(text=NOT_FOUND_ANSWER, route=Route.NOT_FOUND, shown=shown)
    if rule and (fixed := rule_reply(question)) is not None:
        run.text, run.route, run.small_talk, run.by_rule = (
            fixed,
            Route.SMALL_TALK,
            True,
            True,
        )
        return run
    searches = 0
    spec: Completion | None = None
    while True:
        can_search = searches < max_extra and len(shown) < max_excerpts
        messages = build_decision_messages(
            question,
            shown,
            reasoning=reasoning,
            answer=merged,
            searched=list(zip(run.queries, run.added, strict=True)),
        )
        if speculative and not merged and not searches and shown:
            # Первое решение и ответ по первым выдержкам — одновременно.
            with ThreadPoolExecutor(max_workers=2) as pool:
                decided_f = pool.submit(decide, messages)
                spec_f = pool.submit(answer, build_faq_messages(question, shown))
                completion, spec = decided_f.result(), spec_f.result()
            run.speculated = True
            run.calls.append(spec)
            run.wall_ms += max(completion.latency_ms, spec.latency_ms)
        else:
            completion = decide(messages)
            run.wall_ms += completion.latency_ms
        run.calls.append(completion)
        run.decision_calls.append(completion)
        decision = parse_decision(completion.text)
        run.decision = decision
        if decision is None:
            run.invalid += 1
            run.decisions.append({"invalid": completion.text[:500]})
            step = Route.ANSWER
        else:
            run.decisions.append(decision.model_dump())
            step = route(
                decision, can_search=can_search, directory=directory is not None
            )
        if step is Route.DIRECTORY and decision is not None and directory is not None:
            role = lookup_role(decision)
            assert role is not None
            seen = {_key(block) for block in shown}
            blocks, complete = directory(role, decision.missing)
            found = [b for b in blocks if _key(b) not in seen]
            if found:
                # Записи справочника — выдержкой сверх лимита (они короткие,
                # обрезать их нельзя); спекулятивный ответ их не видел.
                shown.extend(found)
                if spec is not None:
                    run.wasted += 1
                    spec = None
            if found and complete:
                # Все запрошенные поля есть — сразу ответ: второе решение и
                # векторный добор не нужны.
                run.directory_hits += 1
                step = Route.ANSWER
                break
            if found:
                run.directory_partial += 1
            else:
                run.directory_misses += 1
            can_search = searches < max_extra and len(shown) <= max_excerpts
            step = route(decision, can_search=can_search)
        if step is Route.SEARCH and decision is not None:
            query = search_query(decision)
            assert query is not None  # route() ищет только с запросом
            if query.casefold() in {q.casefold() for q in run.queries}:
                # Тот же запрос второй раз ничего нового не даст.
                run.exhausted = True
                step = route(decision, can_search=False)
                break
            searches += 1
            run.queries.append(query)
            seen = {_key(block) for block in shown}
            room = max(0, max_excerpts - len(shown))
            new = [b for b in search(query) if _key(b) not in seen][:room]
            run.added.append(len(new))
            if new:
                shown.extend(new)
                if spec is not None:
                    run.wasted += 1
                    spec = None
                continue
            # Добор ничего не дал — решаем по тому, что есть.
            run.exhausted = True
            step = route(decision, can_search=False)
        elif (
            decision is not None
            and decision.sufficiency == "partial"
            and decision.kind == "question"
            and decision.match != "other"
            and not can_search
        ):
            run.exhausted = True
        break

    run.route = step
    if step is Route.SMALL_TALK:
        run.small_talk = True
        reply_text = decision.answer.strip() if merged and decision else ""
        if reply_text and _NOT_FOUND_ANYWHERE.search(reply_text):
            # Как в BH-45 (small_talk_reply): пометка «ответа нет» перевешивает
            # признак реплики — модель написала обе части, значит, это вопрос.
            run.small_talk = False
            run.route = Route.NOT_FOUND
            run.text = NOT_FOUND_ANSWER
        elif reply_text:
            run.text = drop_citations(reply_text)
        else:
            completion = answer(build_general_messages(question, small_talk=True))
            run.calls.append(completion)
            run.wall_ms += completion.latency_ms
            reply = small_talk_reply(completion.text.strip())
            if reply is None:  # модель всё же сочла это вопросом
                run.small_talk = False
                run.text = NOT_FOUND_ANSWER
            else:
                run.text = drop_citations(reply)
        if spec is not None:
            run.wasted += 1
        return run
    if step in (Route.OTHER_SUBJECT, Route.NOT_FOUND):
        run.text = NOT_FOUND_ANSWER
        if spec is not None:
            run.wasted += 1
        return run
    # Ответ по выдержкам: спекулятивный, если он есть и годится; в варианте
    # одним вызовом — поле ответа; иначе отдельный вызов (и при откате, и
    # если поле пустое). Другой год: спекулятивный ответ по обычному промпту
    # мог отказать (правило 4) — тогда ответ заново с указанием отвечать по
    # найденной редакции.
    text = decision.answer.strip() if merged and decision is not None else ""
    if not text and spec is not None:
        text = spec.text.strip()
        if (
            decision is not None
            and decision.match == "other_edition"
            and is_not_found(text)
        ):
            run.wasted += 1
            text = ""
    if not text:
        completion = answer(build_answer_messages(question, shown, decision))
        run.calls.append(completion)
        run.wall_ms += completion.latency_ms
        text = completion.text.strip()
    run.text = text if is_not_found(text) else with_edition_note(text, decision)
    return run
