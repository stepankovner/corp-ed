"""Цикл «решение до ответа» для eval (попытки 2 и 3: правило, параллельный ответ).

Попытка 3 (ml-agent-plan.md §9, протокол в ml-report.md): rule=True —
реплика целиком и вопрос об ассистенте отвечаются правилом в коде, без
модели и поиска; speculative=True — ответ Flash по первым выдержкам
запускается одновременно с решением (гайд OpenAI по задержкам: проверка и
генерация параллельно, генерацию отбрасывают при отказе). Время цикла —
wall_ms: параллельная пара считается как максимум, остальное — суммой.
Стрима до вердикта нет.

Решение на low с перепроверкой отказов (план §7.4 п. 2, Артём 20.10):
decide_fast — быстрый вызов решения (reasoning low); если по нему маршрут —
«ничего» или «про другое» либо sufficiency == "none", то же решение
переспрашивается обычным вызовом decide (medium), и дальше берётся оно.
Так — в каждой точке решения цикла (и после добора). Время: быстрый вызов и
перепроверка — последовательно, спекулятивный ответ — параллельно с этой
цепочкой: max(быстрый + перепроверка, ответ).

Режим recheck="refusals+role" (вариант R2, Артём 20.10): переспрашивается
ещё и «хватает» (маршрут — ответ) при заполненной lookup_role — low называл
роль, но ставил full, и справочник не спрашивался (M-001, T4-06, T3-05).
recheck="refusals" — прежний вариант R. Причина перепроверки — в записи
быстрого решения (поле "recheck") и в DecidedRun.recheck_reasons.

Шаг ответа после справочника (ml-report, «Шаг ответа после справочника»,
Артём 20.10): directory_answer="plain" — как было (запись последней
выдержкой); "first" (A1) — перед ответом выдержки справочника переносятся в
начало shown (номера [n] в ответе и источники — по новому порядку);
"contact" (A2) — при полном попадании в конец сообщения пользователя
дописывается строка контакта, собранная кодом из записей (contact_note),
запись остаётся выдержкой для ссылки. Записи (Entry) справочник отдаёт
третьим элементом; поиск без него (два элемента) — как раньше, без строки.

Модель, поиск и ответ подставляются функциями — цикл тестируется на
подделках, а offline_e2e подключает его к нынешнему поиску, порогам BH-37
и общему ответу. Маршрут — corp_ed.domain.decision.route: реплика; про
другое — «ответа нет» с оговоркой; ничего по теме — общий ответ; не хватает
— добор (не больше max_extra, выдержек не больше max_excerpts) и снова
решение; иначе — ответ по тому, что есть. Решение не прошло схему — откат на
обычный ответ по выдержкам.

Фаза 1 (ml-report, «Фаза 1: хвост отказов, отсечение спекуляции, таймаут»,
план §10.2 п. 1–2, §10.1 п. 3; по умолчанию всё выключено — как было):
- speculation_skip — правила, по которым спекулятивный ответ не запускается
  (признаки до решения: вопрос о контакте, «кто», зона шлюза, одна
  выдержка); вызывающий передаёт speculative=False и пишет причину;
- refusal_parallel=True — при отказе быстрого решения («ничего» / «про
  другое») общий ответ (build_general_messages) идёт параллельно с
  перепроверкой: время low + max(medium, общий); перепроверка вернула не
  отказ — общий ответ выброшен (wasted_general). Готовый общий ответ —
  DecidedRun.general, вызывающий второй раз его не просит;
- таймаут: decide / decide_fast могут бросить TimeoutError (eval.providers
  .CallTimeoutError, complete(timeout=…)) — откат на обычный ответ:
  спекулятивный, если есть, иначе вызов ответа по выдержкам (faq-v2.7);
  выдержек нет — «ответа нет» и общий ответ. DecidedRun.timeout_reason —
  "fast" или "recheck". Так же — eval.providers.ChainExhaustedError (22.10:
  ни одно звено цепочки поставщиков решения не ответило): в записи решения
  "unavailable", шаг — «…!unavailable».
Время каждого вызова по порядку — DecidedRun.steps; причина каждого
выброшенного спекулятивного ответа — wasted_reasons.

Фаза 2 (ml-report, «Фаза 2: совместная приёмка», план §10.4; по умолчанию
выключено): directory_on_refusal=True — отказ решения («ничего» / «про
другое», corp_ed.domain.decision.is_refusal) при названной lookup_role и
подключённом справочнике идёт в справочник. Полное попадание — ответ по
выдержкам с записью; промах или запись без нужного поля — прежний отказ
(route без справочника), а не ответ по тому, что есть. Перепроверка и общий
ответ параллельно — по отказу в самом решении, а не по маршруту: при
попадании общий ответ выброшен (wasted_general).

Отдел спрашивающего (ml-report, «Отдел спрашивающего в поиске по
справочнику», 23.10; по умолчанию выключено): asker_unit=True — справочник
зовётся с текстом вопроса третьим аргументом, directory(role, missing,
question); общая роль («руководитель») при отделе спрашивающего в вопросе
или роль «руководитель <подразделения>» — глава подразделения
(corp_ed.domain.directory.head_for_role). Что нашли — четвёртый элемент
ответа справочника, в DecidedRun.asker_unit.

Защищённый справочник до отказа (ml-report, «Приёмка: отдел
спрашивающего (T8, 23.10)»; по умолчанию выключено): refusal_guard=True
(только вместе с directory_on_refusal) — отказ решения идёт в справочник,
только если (а) вопрос или «чего не хватает» просят контакт
(contact_requested) и (б) запись точная: справочник пятым элементом
ответа говорит, что глава найдена по отделу или lookup дал ровно одну
запись. Иначе — прежний отказ, причина в DecidedRun.refusal_guard.
Обычный маршрут «partial → справочник» защита не трогает. Предел (сухая
проверка на T8): вопросы вне документов вида «Я из АХС, кто подписывает
заявку — руководитель службы? Назови фамилию» проходят обе проверки.
"""

import re
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from corp_ed.domain.agent import MAX_EXCERPTS, MAX_EXTRA_SEARCHES, drop_citations
from corp_ed.domain.decision import (
    Decision,
    Route,
    is_refusal,
    lookup_role,
    parse_decision,
    route,
    search_query,
)
from corp_ed.domain.directory import requested_fields
from corp_ed.domain.small_talk import rule_reply
from corp_ed.llm.types import Message
from corp_ed.prompts.decision import (
    build_answer_messages,
    build_decision_messages,
    contact_note,
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
from eval.providers import ChainExhaustedError
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
    asker_unit: list[str] = field(default_factory=list)
    """Глава подразделения вместо общей роли (asker_unit=True): по строке на
    обращение к справочнику, где сработало («ОКП (вопрос) → руководитель
    Имя»)."""
    refusal_guard: list[str] = field(default_factory=list)
    """Защищённый справочник до отказа (refusal_guard=True): по строке на
    отказ, не пропущенный в справочник («нет контакта» / «не точно»)."""
    directory_misses: int = 0
    """Справочник ничего не дал — векторный добор, как раньше."""
    directory_partial: int = 0
    """Роль в справочнике есть, но без запрошенного поля (почты, телефона):
    записи — выдержкой, и векторный добор, как раньше."""
    wall_ms: float = 0.0
    """Время по стене: параллельная пара — максимум, остальное — сумма."""
    rechecked: int = 0
    """Решений, переспрошенных обычным вызовом после отказа быстрого
    (decide_fast); в decisions — оба, быстрое первым."""
    recheck_reasons: list[str] = field(default_factory=list)
    """Причина каждой перепроверки: "refusal" или "role" (по порядку)."""
    directory_found: list[tuple[Any, list[Any], bool]] = field(default_factory=list)
    """Выдержки справочника, добавленные в shown: (блок, записи Entry,
    полное ли попадание)."""
    contact: str = ""
    """Строка контакта, дописанная в промпт ответа (A2); пусто — не было."""
    directory_first: bool = False
    """Выдержки справочника перенесены в начало перед ответом (A1)."""
    wasted_reasons: list[str] = field(default_factory=list)
    """Почему выброшен каждый спекулятивный ответ: directory, search,
    refusal, small_talk, edition."""
    steps: list[tuple[str, float]] = field(default_factory=list)
    """Вызовы по порядку и их время, мс: fast / decide / recheck (с
    «!timeout» — не дождались), spec, answer, small_talk, general∥."""
    general: Completion | None = None
    """Общий ответ, готовый параллельно с перепроверкой (refusal_parallel);
    его токены — в calls, время — в wall_ms."""
    wasted_general: int = 0
    """Общих ответов выброшено: перепроверка вернула не отказ."""
    timeout_reason: str = ""
    """Таймаут шага решения: "fast" (быстрый вызов или единственный),
    "recheck" (перепроверка); пусто — не было."""

    @property
    def timeout(self) -> bool:
        return bool(self.timeout_reason)

    @property
    def needs_general(self) -> bool:
        """Ответа по документам нет — дальше общий ответ с пометкой (Р1)."""
        return not self.small_talk and is_not_found(self.text)


def _key(block: Any) -> str:
    return str(getattr(block, "content", block))


RECHECK_MODES = ("refusals", "refusals+role")
"""Что перепроверять: только отказы (R) или ещё «хватает» при роли (R2)."""

DIRECTORY_ANSWER_MODES = ("plain", "first", "contact")
"""Как подать справочник в ответ: как было, первой выдержкой (A1), строкой
контакта в промпте (A2)."""

SPECULATION_GATES = ("contact", "who", "gate", "single")
"""Правила отсечения спекулятивного ответа (фаза 1) — по признакам до
решения: contact — вопрос просит ФИО / кабинет / телефон / почту (при
попадании в справочник спекулятивный ответ выбрасывается всегда); who —
«кто» / «кому» (роль); gate — лучшая дистанция в зоне шлюза BH-37 (дальше
max_distance); single — одна выдержка."""

DEFAULT_GATES = ("gate",)
"""Правила gated по умолчанию. Решение Артёма 22.10: в бою — только gate;
contact — в теневом режиме (сухой прогон стенда: gate + contact задели бы
16 % вопросов по документам, половину — задержкой ≈ 1,6 с без экономии;
выигрыш contact — доли копейки на вопрос). Числа фазы 1 (21.10,
пакет регрессии 32 вопроса: stand13 + no + NP2 стенда; ml-report, «Фаза 1»).
Из 29 спекулятивных ответов выброшено 24; gate сэкономил бы 7 выброшенных и
не задержал ни одного нужного, contact — 6 и 1 (M-001, ≈ 1,7 с), вместе — 12
и 1; who — 5 и 3, single — 8 и 3 (одна выдержка — частый случай и у верных
ответов). Вариант (gate + contact + параллельный хвост + таймаут 8 с) в том
же окне: выброшено 24 → 13, цена 0,255 → 0,211 ₽ на вопрос, верных не
меньше. Пакет перекошен к отказам (19 из 32): на смеси стенда (155, сухой
прогон) gate и contact задели бы 23 из 140 вопросов по документам (16 %),
примерно половину — задержкой ≈ 1,6 с без экономии; включать по журналу
теневого режима."""

_CONTACT = re.compile(
    r"фио|фамили|\bимя\b|отчеств|зовут|кабинет|\bкомнат[аеуы]?\b|телефон|"
    r"добавочн|\bдоб\b|e-?mail|адрес\w*\s+(?:\w+\s+)?почт|электронн\w*\s+почт",
    re.IGNORECASE,
)
# Проверка на 337 вопросах наборов (21.10): «почт» целиком ловил «почтовый
# ящик» и «корпоративная почта перестала работать», «имени» — «от имени
# компании», «кабинет» — «в личном кабинете»; почта — только «адрес (его)
# почты» / «электронная почта», личный кабинет вырезается до поиска.
_PERSONAL_ACCOUNT = re.compile(r"личн\w*\s+кабинет\w*", re.IGNORECASE)
_WHO = re.compile(r"\bкто\b|\bкому\b", re.IGNORECASE)


def speculation_skip(
    question: str,
    excerpts: int,
    best_distance: float | None,
    *,
    rules: Sequence[str],
    max_distance: float,
) -> str | None:
    """Первое сработавшее правило отсечения (None — спекулировать)."""
    for rule in rules:
        if rule not in SPECULATION_GATES:
            raise ValueError(f"правило {rule!r} не из {SPECULATION_GATES}")
        if (
            (
                rule == "contact"
                and _CONTACT.search(
                    _PERSONAL_ACCOUNT.sub("", question.replace("ё", "е"))
                )
            )
            or (rule == "who" and _WHO.search(question))
            or (
                rule == "gate"
                and best_distance is not None
                and best_distance > max_distance
            )
            or (rule == "single" and excerpts == 1)
        ):
            return rule
    return None


def contact_requested(question: str, missing: str) -> bool:
    """Просят ли контакт (ФИО / кабинет / телефон / добавочный / почту):
    по «чего не хватает» решения (requested_fields) или по самому вопросу
    (правило contact отсечения спекуляции)."""
    return bool(requested_fields(missing)) or (
        speculation_skip(question, 0, None, rules=("contact",), max_distance=0.0)
        is not None
    )


def _recheck_reason(decision: Decision | None, step: Route, mode: str) -> str | None:
    """Почему быстрое решение переспросить обычным вызовом (None — не надо)."""
    if step in (Route.NOT_FOUND, Route.OTHER_SUBJECT) or (
        decision is not None
        and (decision.sufficiency == "none" or is_refusal(decision))
    ):
        # is_refusal: «про другое» при справочнике до отказа даёт маршрут
        # DIRECTORY, но это всё ещё отказ — перепроверяем.
        return "refusal"
    if (
        mode == "refusals+role"
        and step is Route.ANSWER
        and decision is not None
        and decision.sufficiency == "full"
        and lookup_role(decision)
    ):
        return "role"
    return None


@dataclass
class _Decided:
    completions: list[Completion]
    decision: Decision | None
    step: Route
    records: list[dict[str, Any]]
    rechecked: bool = False
    reason: str | None = None
    wall_ms: float = 0.0
    steps: list[tuple[str, float]] = field(default_factory=list)
    general: Completion | None = None
    timeout: str = ""


_REFUSALS = (Route.NOT_FOUND, Route.OTHER_SUBJECT)


def _decide_chain(
    messages: list[Message],
    *,
    decide: Ask,
    decide_fast: Ask | None,
    can_search: bool,
    directory: bool,
    recheck: str = "refusals",
    general: Callable[[], Completion] | None = None,
    directory_on_refusal: bool = False,
) -> _Decided:
    """Одно решение: быстрый вызов и, при отказе (или «хватает + роль» в
    режиме refusals+role), перепроверка обычным. general — общий ответ:
    при отказе быстрого («ничего» / «про другое») — параллельно с
    перепроверкой. Таймаут вызова (TimeoutError) — решения нет, timeout."""

    def once(
        ask: Ask, label: str
    ) -> tuple[Completion | None, Decision | None, Route, dict[str, Any], float]:
        started = time.perf_counter()
        try:
            completion = ask(messages)
        except TimeoutError as exc:
            elapsed = getattr(exc, "elapsed_ms", None)
            ms = (
                float(elapsed)
                if elapsed is not None
                else (time.perf_counter() - started) * 1000
            )
            return None, None, Route.ANSWER, {"timeout": label}, ms
        except ChainExhaustedError:
            # Цепочка поставщиков (eval.providers.DecisionChain): ни одно
            # звено не ответило — решения нет, откат как при таймауте.
            ms = (time.perf_counter() - started) * 1000
            return None, None, Route.ANSWER, {"timeout": label, "unavailable": True}, ms
        decision = parse_decision(completion.text)
        ms = completion.latency_ms
        if decision is None:
            record = {"invalid": completion.text[:500]}
            return completion, None, Route.ANSWER, record, ms
        step = route(
            decision,
            can_search=can_search,
            directory=directory,
            directory_on_refusal=directory_on_refusal,
        )
        return completion, decision, step, decision.model_dump(), ms

    def failed(label: str, record: dict[str, Any]) -> str:
        return (
            f"{label}!unavailable" if record.get("unavailable") else f"{label}!timeout"
        )

    label = "fast" if decide_fast is not None else "decide"
    completion, decision, step, record, ms = once(decide_fast or decide, label)
    result = _Decided(
        [completion] if completion is not None else [],
        decision,
        step,
        [record],
        wall_ms=ms,
        steps=[(label if completion is not None else failed(label, record), ms)],
    )
    if completion is None:
        result.timeout = "fast"
        return result
    reason = _recheck_reason(decision, step, recheck)
    if decide_fast is not None and reason is not None:
        refusing = step in _REFUSALS or (
            step is Route.DIRECTORY and decision is not None and is_refusal(decision)
        )
        if general is not None and reason == "refusal" and refusing:
            with ThreadPoolExecutor(max_workers=2) as pool:
                recheck_f = pool.submit(once, decide, "recheck")
                general_f = pool.submit(general)
                completion, decision, step, record, ms = recheck_f.result()
                result.general = general_f.result()
            result.wall_ms += max(ms, result.general.latency_ms)
        else:
            completion, decision, step, record, ms = once(decide, "recheck")
            result.wall_ms += ms
        result.steps.append(
            ("recheck" if completion is not None else failed("recheck", record), ms)
        )
        if result.general is not None:
            result.steps.append(("general∥", result.general.latency_ms))
        if completion is not None:
            result.completions.append(completion)
        else:
            result.timeout = "recheck"
        result.records[0] = {**result.records[0], "fast": True, "recheck": reason}
        result.records.append(record)
        result.decision, result.step, result.rechecked = decision, step, True
        result.reason = reason
    return result


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
    directory: Callable[..., tuple[Any, ...]] | None = None,
    decide_fast: Ask | None = None,
    recheck: str = "refusals",
    directory_answer: str = "plain",
    refusal_parallel: bool = False,
    directory_on_refusal: bool = False,
    asker_unit: bool = False,
    refusal_guard: bool = False,
    max_extra: int = MAX_EXTRA_SEARCHES,
    max_excerpts: int = MAX_EXCERPTS,
) -> DecidedRun:
    """Цикл решения (см. докстринг модуля). Общий ответ, готовый параллельно с
    перепроверкой, остаётся в run.general, только если он и нужен (ответа по
    документам нет); иначе — выброшен (wasted_general)."""
    run = _run_decided(
        question,
        initial,
        decide=decide,
        answer=answer,
        search=search,
        reasoning=reasoning,
        merged=merged,
        rule=rule,
        speculative=speculative,
        directory=directory,
        decide_fast=decide_fast,
        recheck=recheck,
        directory_answer=directory_answer,
        refusal_parallel=refusal_parallel,
        directory_on_refusal=directory_on_refusal,
        asker_unit=asker_unit,
        refusal_guard=refusal_guard,
        max_extra=max_extra,
        max_excerpts=max_excerpts,
    )
    if run.general is not None and not run.needs_general:
        run.general = None
        run.wasted_general += 1
    return run


def _run_decided(
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
    directory: Callable[..., tuple[Any, ...]] | None = None,
    decide_fast: Ask | None = None,
    recheck: str = "refusals",
    directory_answer: str = "plain",
    refusal_parallel: bool = False,
    directory_on_refusal: bool = False,
    asker_unit: bool = False,
    refusal_guard: bool = False,
    max_extra: int = MAX_EXTRA_SEARCHES,
    max_excerpts: int = MAX_EXCERPTS,
) -> DecidedRun:
    if recheck not in RECHECK_MODES:
        raise ValueError(f"recheck: {recheck!r} не из {RECHECK_MODES}")
    if directory_answer not in DIRECTORY_ANSWER_MODES:
        raise ValueError(
            f"directory_answer: {directory_answer!r} не из {DIRECTORY_ANSWER_MODES}"
        )
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

    def general() -> Completion:
        return answer(build_general_messages(question))

    def discard(reason: str) -> None:
        run.wasted += 1
        run.wasted_reasons.append(reason)

    while True:
        can_search = searches < max_extra and len(shown) < max_excerpts
        messages = build_decision_messages(
            question,
            shown,
            reasoning=reasoning,
            answer=merged,
            searched=list(zip(run.queries, run.added, strict=True)),
        )

        def chain(
            messages: list[Message] = messages, can: bool = can_search
        ) -> _Decided:
            return _decide_chain(
                messages,
                decide=decide,
                decide_fast=decide_fast,
                can_search=can,
                directory=directory is not None,
                recheck=recheck,
                general=general if refusal_parallel else None,
                directory_on_refusal=directory_on_refusal,
            )

        if speculative and not merged and not searches and shown:
            # Первое решение (с перепроверкой — вся цепочка) и ответ по первым
            # выдержкам — одновременно.
            with ThreadPoolExecutor(max_workers=2) as pool:
                decided_f = pool.submit(chain)
                spec_f = pool.submit(answer, build_faq_messages(question, shown))
                decided, spec = decided_f.result(), spec_f.result()
            run.speculated = True
            run.calls.append(spec)
            run.steps.append(("spec", spec.latency_ms))
            run.wall_ms += max(decided.wall_ms, spec.latency_ms)
        else:
            decided = chain()
            run.wall_ms += decided.wall_ms
        run.steps.extend(decided.steps)
        if decided.general is not None:
            run.calls.append(decided.general)
            if run.general is not None:  # прежний не понадобился
                run.wasted_general += 1
            run.general = decided.general
        run.calls.extend(decided.completions)
        run.decision_calls.extend(decided.completions)
        run.rechecked += decided.rechecked
        if decided.reason is not None:
            run.recheck_reasons.append(decided.reason)
        decision, step = decided.decision, decided.step
        run.decision = decision
        run.decisions.extend(decided.records)
        if decided.timeout:
            # Таймаут решения — откат на обычный ответ (спекулятивный, если
            # есть, иначе вызов ответа); выдержек нет — «ответа нет».
            run.timeout_reason = decided.timeout
            if not shown:
                step = Route.NOT_FOUND
            break
        if decision is None:
            run.invalid += 1
        if step is Route.DIRECTORY and decision is not None and directory is not None:
            role = lookup_role(decision)
            assert role is not None
            seen = {_key(block) for block in shown}
            # Защищённый справочник до отказа: (а) — до вызова, (б) — по
            # пятому элементу ответа справочника; иначе прежний отказ.
            guarded = refusal_guard and is_refusal(decision)
            blocked = ""
            result: tuple[Any, ...]
            if guarded and not contact_requested(question, decision.missing):
                blocked = "нет контакта"
                result = ([], False, [], "")
            else:
                result = (
                    directory(role, decision.missing, question)
                    if asker_unit
                    else directory(role, decision.missing)
                )
                if guarded and not (len(result) > 4 and result[4]):
                    blocked = "не точно"
            if blocked:
                run.refusal_guard.append(f"{blocked}: {role}")
                result = ([], False, [], *result[3:4])
            blocks, complete = result[0], bool(result[1])
            entries = list(result[2]) if len(result) > 2 else []
            if len(result) > 3 and result[3]:
                run.asker_unit.append(str(result[3]))
            found = [b for b in blocks if _key(b) not in seen]
            if found:
                # Записи справочника — выдержкой сверх лимита (они короткие,
                # обрезать их нельзя); спекулятивный ответ их не видел.
                shown.extend(found)
                run.directory_found.extend(
                    (block, entries, complete) for block in found
                )
                if spec is not None:
                    discard("directory")
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
                    discard("search")
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
            run.steps.append(("small_talk", completion.latency_ms))
            run.wall_ms += completion.latency_ms
            reply = small_talk_reply(completion.text.strip())
            if reply is None:  # модель всё же сочла это вопросом
                run.small_talk = False
                run.text = NOT_FOUND_ANSWER
            else:
                run.text = drop_citations(reply)
        if spec is not None:
            discard("small_talk")
        return run
    if step in _REFUSALS:
        run.text = NOT_FOUND_ANSWER
        if spec is not None:
            discard("refusal")
        return run
    # Ответ по выдержкам: спекулятивный, если он есть и годится; в варианте
    # одним вызовом — поле ответа; иначе отдельный вызов (и при откате, и
    # если поле пустое). Другой год или редакция: спекулятивный ответ — по
    # обычному промпту, его правило 4 велит отказать (приёмка 3: отказ и
    # после переспроса) — выбрасываем сразу, ответ — промптом edition-v1.
    edition = decision is not None and decision.match == "other_edition"
    text = decision.answer.strip() if merged and decision is not None else ""
    if not text and spec is not None:
        if edition:
            discard("edition")
        else:
            text = spec.text.strip()
    if not text:
        contact = _directory_answer(run, shown, directory_answer)
        completion = answer(
            build_answer_messages(question, shown, decision, contact=contact)
        )
        run.calls.append(completion)
        run.steps.append(("answer", completion.latency_ms))
        run.wall_ms += completion.latency_ms
        text = completion.text.strip()
    run.text = text if is_not_found(text) else with_edition_note(text, decision)
    return run


def _directory_answer(run: DecidedRun, shown: list[Any], mode: str) -> str:
    """Подача справочника в ответ (A1 / A2); возвращает строку контакта.

    "first" — выдержки справочника в начало shown (тот же список, что
    run.shown: номера [n] и источники — по новому порядку). "contact" —
    строка по записям полного попадания с номером его выдержки."""
    present = [
        (block, entries, complete)
        for block, entries, complete in run.directory_found
        if any(block is item for item in shown)
    ]
    if not present:
        return ""
    if mode == "first":
        blocks = [block for block, _, _ in present]
        rest = [item for item in shown if not any(item is b for b in blocks)]
        shown[:] = blocks + rest
        run.directory_first = True
        return ""
    if mode == "contact":
        for block, entries, complete in present:
            if not complete:
                continue
            number = next(k for k, item in enumerate(shown, 1) if item is block)
            run.contact = contact_note(entries, number)
            if run.contact:
                return run.contact
    return ""
