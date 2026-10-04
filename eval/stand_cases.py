"""Случаи проверки стенда (BH-38) в eval: загрузка, золотой формат, вердикт.

Корпус и случаи бэкенда — `tests/fixtures/stand_quality` (синтетическая
компания, проверка стенда 02.10). Вопросы писались до замеров ML и
независимы от его наборов, поэтому на них ML перепроверяет решения,
подобранные на своих данных (порог BH-37 — 04.10).

Вердикт — правило стенда (README корпуса, «Как проверяли»): в ответе есть
хотя бы одно слово из каждой группы `expected_answer` (группы через «; »,
варианты через « | ») и нет ни одного из `forbidden`, без учёта регистра;
неразрывный и обычный пробел равны. Плюс два уточнения:
- неразрывный дефис и «ё» сравниваются как «-» и «е» — бэкенд засчитал
  PR-010 вручную именно из-за неразрывного дефиса;
- происхождение: у вопроса по документам общий ответ с пометкой «в
  документах ответа нет» неверен, даже если нужные слова в нём есть
  (модель могла знать их сама — та же ловушка, что у судьи на x09 и x19);
  у вопроса вне документов верен только общий ответ с пометкой.

    python -m eval.stand_cases golden <cases.csv> <golden.csv>
    python -m eval.stand_cases score <cases.csv> <run_e2e.csv> [...]

golden — для `eval.offline_e2e --dataset` (диалоги — только первая
реплика, `edge_indexed` — мимо: у них нет записанных ответов). score —
вердикт по CSV прогона offline_e2e (колонки id, answer, general_answer):
сводка в консоль и `<прогон>_stand.csv` рядом.
"""

import csv
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from eval.datasets import GOLDEN_COLUMNS

OK = "ok"
FAIL_KEYWORDS = "keywords"
FAIL_FORBIDDEN = "forbidden"
FAIL_ORIGIN = "origin"


@dataclass(frozen=True)
class StandCase:
    id: str
    category: str
    question: str
    groups: tuple[tuple[str, ...], ...]
    forbidden: tuple[str, ...]
    materials: tuple[str, ...]
    in_corpus: bool
    expected_answer: str = ""
    dialogue_id: str = ""
    turn: int = 1


def normalize(text: str) -> str:
    text = text.casefold().replace("ё", "е")
    for dash in ("‐", "‑"):
        text = text.replace(dash, "-")
    return " ".join(text.replace(" ", " ").split())


def _variants(text: str) -> tuple[str, ...]:
    return tuple(normalize(v) for v in text.split(" | ") if v.strip())


def load_cases(path: Path, *, first_turn_only: bool = True) -> list[StandCase]:
    """Случаи с записанным ответом: без `edge_indexed`, у диалогов — по
    умолчанию только первая реплика (порог и поиск у следующих зависят от
    переписанного вопроса — это eval.dialogue_eval)."""
    with path.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    cases = []
    for row in rows:
        turn = int(row.get("turn") or 1)
        if row["category"] == "edge_indexed" or (first_turn_only and turn != 1):
            continue
        cases.append(
            StandCase(
                id=row["id"].strip(),
                category=row["category"].strip(),
                question=row["question"].strip(),
                groups=tuple(
                    variants
                    for group in row["expected_answer"].split("; ")
                    if (variants := _variants(group))
                ),
                forbidden=_variants(row.get("forbidden", "")),
                materials=tuple(
                    m.strip() for m in row["expected_material"].split(";") if m.strip()
                ),
                in_corpus=row["in_corpus"].strip().casefold() == "true",
                expected_answer=row["expected_answer"].strip(),
                dialogue_id=row.get("dialogue_id", "").strip(),
                turn=turn,
            )
        )
    return cases


def verdict(case: StandCase, answer: str, *, general: bool) -> str:
    """OK или причина ошибки. general — общий ответ с пометкой «в
    документах ответа нет» (offline_e2e: general_answer)."""
    text = normalize(answer)
    if any(word in text for word in case.forbidden):
        return FAIL_FORBIDDEN
    if not case.in_corpus:
        return OK if general else FAIL_ORIGIN
    if general:
        return FAIL_ORIGIN
    if all(any(v in text for v in group) for group in case.groups):
        return OK
    return FAIL_KEYWORDS


def golden_rows(cases: Iterable[StandCase]) -> list[dict[str, str]]:
    """Формат золотого набора для offline_e2e. type — только «есть / нет в
    документах»: категории стенда (таблицы, опечатки…) в колонку не
    влезают и для прогона не нужны."""
    return [
        {
            "id": case.id,
            "question": case.question,
            "expected_answer": case.expected_answer,
            "expected_material": ";".join(case.materials),
            "expected_section": "",
            "in_corpus": "true" if case.in_corpus else "false",
            "type": "fact" if case.in_corpus else "out_of_corpus",
        }
        for case in cases
    ]


def score_run(cases: Sequence[StandCase], run: Path) -> list[dict[str, str]]:
    by_id = {case.id: case for case in cases}
    with run.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    scored = []
    for row in rows:
        case = by_id.get(row["id"])
        if case is None:
            continue
        general = row["general_answer"].strip().casefold() == "true"
        scored.append(
            {
                "id": case.id,
                "category": case.category,
                "in_corpus": str(case.in_corpus).lower(),
                "general": str(general).lower(),
                "verdict": verdict(case, row["answer"], general=general),
                "question": case.question,
                "answer": row["answer"],
            }
        )
    return scored


def _write(rows: Sequence[dict[str, str]], path: Path, columns: Sequence[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)


def main(argv: Sequence[str]) -> None:
    if len(argv) < 3 or argv[0] not in ("golden", "score"):
        raise SystemExit(__doc__)
    cases = load_cases(Path(argv[1]))
    if argv[0] == "golden":
        _write(golden_rows(cases), Path(argv[2]), GOLDEN_COLUMNS)
        print(f"{len(cases)} случаев -> {argv[2]}")
        return
    for name in argv[2:]:
        run = Path(name)
        scored = score_run(cases, run)
        pos = [r for r in scored if r["in_corpus"] == "true"]
        neg = [r for r in scored if r["in_corpus"] == "false"]
        print(
            f"{run.name}: по документам верно {sum(r['verdict'] == OK for r in pos)}"
            f"/{len(pos)}, вне документов с пометкой "
            f"{sum(r['verdict'] == OK for r in neg)}/{len(neg)}"
        )
        if scored:
            _write(scored, run.with_name(f"{run.stem}_stand.csv"), list(scored[0]))


if __name__ == "__main__":
    main(sys.argv[1:])
