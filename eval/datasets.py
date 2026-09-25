"""Наборы вопросов для eval: золотой (A5) и серебряный (A6).

Золотой — eval/private/golden.csv (живые вопросы, в git не попадает),
формат из ТЗ:
    id,question,expected_answer,expected_material,expected_section,in_corpus,type
и две необязательные колонки (задача 2.1, 25.09):
    level — topic (вопрос на уровне темы) / detail (про конкретную деталь);
            по-русски тоже можно: тема / деталь;
    split — dev / holdout, ставит `python -m eval.datasets split`.
Holdout не открывается до финального прогона: скрипты берут только dev,
пока holdout не запрошен явно (`--split holdout`), см. select_split.

Серебряный — eval/silver.csv (пишет generate_silver.py):
    id,question,material,position,heading_path,evidence,split,chunk_config

Соглашение по золотому набору: вопросы из интервью Влада имеют id с
префиксом «iv» (iv01…iv05) — в формате ТЗ нет отдельной колонки для
источника вопроса, а в проверке состава их нужно посчитать.

Проверка:  python -m eval.datasets eval/private/golden.csv
Разбиение: python -m eval.datasets split eval/private/golden.csv
Как писать вопросы — docs/ml-golden-guide.md.
"""

import csv
import hashlib
import re
import sys
from collections import Counter, defaultdict
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
LEVELS = ("topic", "detail")
_LEVEL_ALIASES = {"тема": "topic", "деталь": "detail"}
SPLITS = ("dev", "holdout")

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
    level: str = ""


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
    level = row.get("level", "").strip().casefold()
    level = _LEVEL_ALIASES.get(level, level)
    if level and level not in LEVELS:
        raise ValueError(f"{row_id}: unknown level {level!r}, expected {LEVELS}")
    split = row.get("split", "").strip().casefold()
    if split and split not in SPLITS:
        raise ValueError(f"{row_id}: unknown split {split!r}, expected {SPLITS}")
    return EvalItem(
        id=row_id,
        question=row["question"].strip(),
        in_corpus=_parse_bool(row["in_corpus"], row_id=row_id),
        type=question_type,
        expected_answer=row["expected_answer"].strip(),
        expected_material=row["expected_material"].strip(),
        expected_section=row["expected_section"].strip(),
        level=level,
        split=split,
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

    seen: dict[str, str] = {}
    for item in items:
        key = _question_key(item.question)
        if key in seen:
            problems.append(f"{item.id}: тот же вопрос, что {seen[key]}")
        seen.setdefault(key, item.id)
        if item.in_corpus and item.type == "out_of_corpus":
            problems.append(f"{item.id}: in_corpus=true, но type=out_of_corpus")
        if not item.in_corpus and item.type != "out_of_corpus":
            problems.append(f"{item.id}: in_corpus=false, но type={item.type}")
        if item.in_corpus and not item.expected_material:
            problems.append(f"{item.id}: вопрос по корпусу без expected_material")
        if item.in_corpus and not item.expected_answer:
            problems.append(f"{item.id}: вопрос по корпусу без expected_answer")
        if item.in_corpus and not item.level:
            problems.append(f"{item.id}: вопрос по корпусу без level (topic / detail)")
    return problems


def _question_key(question: str) -> str:
    return " ".join(
        re.sub(r"[^\w\s]", " ", question.casefold().replace("ё", "е")).split()
    )


def select_split(items: list[EvalItem], split: str) -> list[EvalItem]:
    """Вопросы нужного split. Без явного split holdout НЕ отдаётся.

    Holdout золотого набора открывается один раз — на финальном прогоне
    (задача 2.6). Всё, что подбирает параметры, должно видеть только dev,
    иначе итоговые числа завышены подгонкой.
    """
    if split:
        return [item for item in items if item.split == split]
    return [item for item in items if item.split != "holdout"]


def stratified_split(
    items: list[EvalItem], *, seed: str = "corp-ed", holdout_share: float = 0.5
) -> dict[str, str]:
    """id → dev / holdout, поровну внутри каждого слоя.

    Слой — (по корпусу или нет, тип, уровень, из интервью или нет): так в
    dev и holdout одинаковая доля отрицаний, сравнений и вопросов вне
    корпуса. Порядок внутри слоя — по хешу seed + id: детерминированно и не
    зависит от порядка строк. Нечётный остаток слоя уходит то в dev, то в
    holdout, по очереди между слоями, чтобы итог был близок к 50/50.
    """
    if not 0 < holdout_share < 1:
        raise ValueError("holdout_share must be in (0, 1)")
    strata: dict[tuple[bool, str, str, bool], list[EvalItem]] = defaultdict(list)
    for item in items:
        key = (
            item.in_corpus,
            item.type,
            item.level,
            item.id.startswith(INTERVIEW_PREFIX),
        )
        strata[key].append(item)

    assignment: dict[str, str] = {}
    extra_to_holdout = False
    for key in sorted(strata):
        group = sorted(
            strata[key],
            key=lambda item: hashlib.sha256(f"{seed}:{item.id}".encode()).hexdigest(),
        )
        exact = len(group) * holdout_share
        holdout = int(exact)
        if exact - holdout > 1e-9:
            holdout += int(extra_to_holdout)
            extra_to_holdout = not extra_to_holdout
        for position, item in enumerate(group):
            assignment[item.id] = "holdout" if position < holdout else "dev"
    return assignment


def write_split(
    path: Path, assignment: dict[str, str], out: Path | None = None
) -> None:
    """Дописать колонку split в CSV набора (остальные колонки как были)."""
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    if "split" not in fields:
        fields.append("split")
    for row in rows:
        row["split"] = assignment[row["id"].strip()]
    with (out or path).open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "split":
        path = Path(argv[1])
        items = load_dataset(path)
        if any(item.split for item in items):
            print("split уже проставлен — не переразбиваю (holdout мог быть открыт).")
            return 1
        assignment = stratified_split(items)
        write_split(path, assignment)
        counts = Counter(assignment.values())
        print(f"dev {counts['dev']}, holdout {counts['holdout']} → {path}")
        return 0
    if len(argv) != 1:
        print("usage: python -m eval.datasets [split] <golden.csv>")
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
