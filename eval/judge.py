"""LLM-судья правильности ответа (M4).

    python -m eval.judge --results <результаты>_e2e.csv [--model yandexgpt]
    python -m eval.judge --results …_e2e_judged.csv --calibrate

Судья получает вопрос, эталон, ответ ассистента и выдержки, которые
ассистент видел (колонка sources в результатах e2e). Возвращает JSON:
    correct — 0 неверно / 1 частично / 2 верно,
    faithful — 1, если все факты ответа есть в выдержках, иначе 0.

Калибровка (ТЗ): прогнать на ответах золотого набора, которые уже
размечены вручную (колонка correct). Судья должен совпадать с человеком
в ≥ 85% случаев (порог — допущение ТЗ), иначе — править промпт судьи.

Ловушка МТС: судья занижал оценку, когда ответ длиннее и полнее эталона.
В промпте явное правило: полнота сверх эталона — не штраф, если факты
подтверждаются выдержками.

Альтернатива — RAGAS. Проверить, работает ли он с YandexGPT через
OpenAI-совместимый API AI Studio, можно только с доступом к Яндексу:
это задача на тебя (см. ЗАДАЧИ_ДЛЯ_ТЕБЯ.md). Если нет — этот скрипт.
"""

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from corp_ed.llm.types import Message, Role
from eval.results import read_csv, write_csv
from eval.yandex import DEFAULT_API, DEFAULT_LLM

JUDGE_PROMPT_VERSION = "judge-v1"
AGREEMENT_TARGET = 0.85
KAPPA_TARGET = 0.6
MIN_CALIBRATION = 40
"""Калибровка (задача 2.7, docs/ml-plan.md): не меньше 40 ручных оценок
Артёма; судье доверяем при совпадении ≥ 85 % И каппе Коэна ≥ 0.6 —
иначе в отчёт идут ручные оценки. Каппа нужна, потому что при перекосе
классов (почти все ответы «верно») высокое совпадение бывает и у судьи,
который всегда ставит «верно»."""

_SYSTEM = """\
Ты — строгий и справедливый проверяющий ответов ассистента, который отвечает \
сотрудникам по документам компании. Оцени ответ и верни только JSON.

Поле correct:
- 2 — ответ по существу верный: ключевые факты эталона есть и ничто им не \
противоречит;
- 1 — частично верный: часть ключевых фактов есть, но что-то важное упущено \
или есть неточность, не меняющая сути;
- 0 — неверный: противоречит эталону, не отвечает на вопрос или отказывается \
отвечать, хотя ответ есть в эталоне.

Правила:
1. Ответ длиннее и подробнее эталона — НЕ ошибка. Дополнительные факты \
не снижают оценку, если они подтверждаются выдержками.
2. Формулировки не важны, важен смысл: «две недели» = «14 дней».
3. Если эталон пустой, вопрос вне документов: correct = 2, если ассистент \
отказался («В документах компании ответа нет» или по смыслу то же), иначе 0.
4. Если вопрос содержит ложную предпосылку, а ассистент её подтвердил — \
correct = 0.

Поле faithful:
- 1 — все факты ответа есть в выдержках (или ответ — отказ);
- 0 — в ответе есть факты, которых нет в выдержках.

Формат: {"correct": 0|1|2, "faithful": 0|1, "reason": "одно короткое предложение"}"""

_TASK = """\
Вопрос сотрудника: {question}

Эталонный ответ: {reference}

Ответ ассистента: {answer}

Выдержки, которые видел ассистент:
{excerpts}"""


@dataclass(frozen=True)
class Verdict:
    correct: int
    faithful: int
    reason: str


def format_excerpts(sources_json: str) -> str:
    try:
        sources = json.loads(sources_json) if sources_json else []
    except json.JSONDecodeError:
        return "(не удалось прочитать)"
    if not sources:
        return "(выдержек не было)"
    parts = []
    for number, source in enumerate(sources, start=1):
        path = " > ".join([source.get("material", ""), *source.get("heading_path", [])])
        parts.append(f"[{number}] {path}\n{source.get('content', '')}")
    return "\n\n".join(parts)


def build_judge_messages(
    question: str, reference: str, answer: str, excerpts: str
) -> list[Message]:
    return [
        Message(role=Role.SYSTEM, content=_SYSTEM),
        Message(
            role=Role.USER,
            content=_TASK.format(
                question=question.strip(),
                reference=reference.strip() or "(пусто — вопрос вне документов)",
                answer=answer.strip(),
                excerpts=excerpts,
            ),
        ),
    ]


def parse_verdict(text: str) -> Verdict | None:
    match = re.search(r"\{.*\}", re.sub(r"```(?:json)?", "", text), re.DOTALL)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
        correct = int(data["correct"])
        faithful = int(data["faithful"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if correct not in (0, 1, 2) or faithful not in (0, 1):
        return None
    return Verdict(
        correct=correct, faithful=faithful, reason=str(data.get("reason", ""))
    )


@dataclass(frozen=True)
class Agreement:
    n: int
    exact: float
    within_one: float
    confusion: dict[tuple[int, int], int]
    kappa: float = 0.0


def agreement(manual: Sequence[int], judge: Sequence[int]) -> Agreement:
    pairs = list(zip(manual, judge, strict=True))
    if not pairs:
        return Agreement(0, 0.0, 0.0, {})
    exact = sum(1 for m, j in pairs if m == j) / len(pairs)
    within_one = sum(1 for m, j in pairs if abs(m - j) <= 1) / len(pairs)
    return Agreement(
        len(pairs), exact, within_one, dict(Counter(pairs)), cohen_kappa(pairs)
    )


def cohen_kappa(pairs: Sequence[tuple[int, int]]) -> float:
    """Каппа Коэна: совпадение сверх случайного при тех же частотах оценок."""
    n = len(pairs)
    if n == 0:
        return 0.0
    observed = sum(1 for m, j in pairs if m == j) / n
    manual = Counter(m for m, _ in pairs)
    judge = Counter(j for _, j in pairs)
    expected = sum(manual[k] * judge[k] for k in manual) / (n * n)
    if expected == 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1 - expected)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.judge")
    parser.add_argument("--results", type=Path, required=True)
    # По умолчанию — модель и API из решения по задаче 1 (Flash через
    # OpenAI-совместимый API); калибровка судьи — задача 2.7, до неё
    # оценки судьи предварительные при любой модели.
    parser.add_argument("--model", default=DEFAULT_LLM)
    parser.add_argument("--api", choices=("native", "openai"), default=DEFAULT_API)
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="сравнить judge_correct с ручной разметкой correct",
    )
    args = parser.parse_args(argv)
    rows = read_csv(args.results)

    if args.calibrate:
        labeled = [
            r for r in rows if r.get("correct", "") != "" and r.get("judge_correct", "")
        ]
        result = agreement(
            [int(r["correct"]) for r in labeled],
            [int(r["judge_correct"]) for r in labeled],
        )
        print(f"Размеченных ответов: {result.n}")
        print(f"Точное совпадение: {result.exact:.1%} (цель ≥ {AGREEMENT_TARGET:.0%})")
        print(f"Расхождение не больше чем на 1 балл: {result.within_one:.1%}")
        print(f"Каппа Коэна: {result.kappa:.2f} (цель ≥ {KAPPA_TARGET})")
        print("Матрица (человек, судья) → число:")
        for (human, judge), count in sorted(result.confusion.items()):
            print(f"  ({human}, {judge}) → {count}")
        if result.n < MIN_CALIBRATION:
            conclusion = f"мало оценок: {result.n} < {MIN_CALIBRATION}"
        elif result.exact >= AGREEMENT_TARGET and result.kappa >= KAPPA_TARGET:
            conclusion = "судье можно доверять"
        else:
            conclusion = "в отчёт — ручные оценки; промпт судьи править"
        print(f"Вывод: {conclusion}.")
        return 0

    from eval.yandex import YandexClient

    client = YandexClient.from_env()
    for number, row in enumerate(rows, start=1):
        messages = build_judge_messages(
            row["question"],
            row.get("expected_answer", ""),
            row["answer"],
            format_excerpts(row.get("sources", "")),
        )
        verdict = parse_verdict(
            client.complete(
                messages, model=args.model, temperature=0.0, api=args.api
            ).text
        )
        row["judge_correct"] = "" if verdict is None else str(verdict.correct)
        row["judge_faithful"] = "" if verdict is None else str(verdict.faithful)
        row["judge_reason"] = (
            "не разобран ответ судьи" if verdict is None else verdict.reason
        )
        print(f"[{number}/{len(rows)}] {row['id']}: {row['judge_correct'] or '?'}")

    out = args.results.with_name(args.results.stem + "_judged.csv")
    write_csv(out, rows)
    print(f"Готово ({JUDGE_PROMPT_VERSION}): {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
