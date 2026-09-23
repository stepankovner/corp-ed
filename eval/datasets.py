"""Наборы вопросов для eval: золотой (A5) и серебряный (A6).

Золотой — eval/golden.csv, формат из ТЗ:
    id,question,expected_answer,expected_material,expected_section,in_corpus,type

Серебряный — eval/silver.csv (пишет generate_silver.py):
    id,question,material,position,heading_path,evidence,split,chunk_config

Соглашение по золотому набору: вопросы из интервью Влада имеют id с
префиксом «iv» (iv01…iv05) — в формате ТЗ нет отдельной колонки для
источника вопроса, а в проверке состава их нужно посчитать.

Проверка состава: python -m eval.datasets eval/golden.csv
"""

import csv
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

QUESTION_TYPES = ("fact", "procedure", "negation", "comparison", "out_of_corpus")
GOLDEN_COLUMNS = (
    "id",
    "question",
    "expected_answer",
    "expected_material",
    "expected_section",
    "in_corpus",
    "type",
)
SILVER_COLUMNS = (
    "id",
    "question",
    "material",
    "position",
    "heading_path",
    "evidence",
    "split",
    "chunk_config",
)
INTERVIEW_PREFIX = "iv"

_TRUE = {"true", "1", "yes", "да"}
_FALSE = {"false", "0", "no", "нет", ""}


@dataclass(frozen=True)
class EvalItem:
    """Вопрос набора. Поля, которых нет в наборе, пустые."""

    id: str
    question: str
    in_corpus: bool
    type: str
    expected_answer: str = ""
    expected_material: str = ""
    expected_section: str = ""
    evidence: str = ""
    split: str = ""


def _parse_bool(value: str, *, row_id: str) -> bool:
    folded = value.strip().casefold()
    if folded in _TRUE:
        return True
    if folded in _FALSE:
        return False
    raise ValueError(f"{row_id}: in_corpus must be true/false, got {value!r}")


def load_dataset(path: Path) -> list[EvalItem]:
    """Прочитать золотой или серебряный набор (формат определяется по колонкам)."""
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        columns = set(reader.fieldnames or [])
        rows = list(reader)

    if set(GOLDEN_COLUMNS) <= columns:
        items = [_golden_item(row) for row in rows]
    elif set(SILVER_COLUMNS) <= columns:
        items = [_silver_item(row) for row in rows]
    else:
        raise ValueError(
            f"{path}: unknown dataset format, columns: {sorted(columns)}; "
            f"expected golden {GOLDEN_COLUMNS} or silver {SILVER_COLUMNS}"
        )

    ids = Counter(item.id for item in items)
    duplicates = [item_id for item_id, count in ids.items() if count > 1]
    if duplicates:
        raise ValueError(f"{path}: duplicate ids: {duplicates}")
    return items


def _golden_item(row: dict[str, str]) -> EvalItem:
    row_id = row["id"].strip()
    question_type = row["type"].strip()
    if question_type not in QUESTION_TYPES:
        raise ValueError(
            f"{row_id}: unknown type {question_type!r}, expected {QUESTION_TYPES}"
        )
    if not row["question"].strip():
        raise ValueError(f"{row_id}: empty question")
    return EvalItem(
        id=row_id,
        question=row["question"].strip(),
        in_corpus=_parse_bool(row["in_corpus"], row_id=row_id),
        type=question_type,
        expected_answer=row["expected_answer"].strip(),
        expected_material=row["expected_material"].strip(),
        expected_section=row["expected_section"].strip(),
    )


def _silver_item(row: dict[str, str]) -> EvalItem:
    return EvalItem(
        id=row["id"].strip(),
        question=row["question"].strip(),
        in_corpus=True,
        type="silver",
        expected_material=row["material"].strip(),
        evidence=row["evidence"].strip(),
        split=row["split"].strip(),
    )


def check_golden_composition(items: list[EvalItem]) -> list[str]:
    """Расхождения золотого набора с составом из ТЗ (A5). Пусто — всё в порядке.

    40 вопросов: 25 по корпусу (из них ≥ 5 negation), 10 вне корпуса,
    5 дословных из интервью Влада (id с префиксом iv).
    """
    problems: list[str] = []
    interview = [item for item in items if item.id.startswith(INTERVIEW_PREFIX)]
    regular = [item for item in items if not item.id.startswith(INTERVIEW_PREFIX)]
    in_corpus = [item for item in regular if item.in_corpus]
    outside = [item for item in regular if not item.in_corpus]
    negation = [item for item in in_corpus if item.type == "negation"]

    expectations = [
        (len(items), 40, "всего вопросов"),
        (len(in_corpus), 25, "по корпусу (без интервью)"),
        (len(outside), 10, "вне корпуса (без интервью)"),
        (len(interview), 5, f"из интервью (id на «{INTERVIEW_PREFIX}»)"),
    ]
    for actual, expected, label in expectations:
        if actual != expected:
            problems.append(f"{label}: {actual}, по ТЗ {expected}")
    if len(negation) < 5:
        problems.append(
            f"negation среди вопросов по корпусу: {len(negation)}, нужно ≥ 5"
        )

    for item in items:
        if item.in_corpus and item.type == "out_of_corpus":
            problems.append(f"{item.id}: in_corpus=true, но type=out_of_corpus")
        if not item.in_corpus and item.type != "out_of_corpus":
            problems.append(f"{item.id}: in_corpus=false, но type={item.type}")
        if item.in_corpus and not item.expected_material:
            problems.append(f"{item.id}: вопрос по корпусу без expected_material")
        if item.in_corpus and not item.expected_answer:
            problems.append(f"{item.id}: вопрос по корпусу без expected_answer")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m eval.datasets <golden.csv>")
        return 2
    items = load_dataset(Path(argv[0]))
    types = Counter(item.type for item in items)
    print(f"{len(items)} вопросов: " + ", ".join(f"{t}={n}" for t, n in types.items()))
    problems = check_golden_composition(items) if items[0].type != "silver" else []
    for problem in problems:
        print(f"  - {problem}")
    print("Состав соответствует ТЗ." if not problems else "Состав НЕ соответствует ТЗ.")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
