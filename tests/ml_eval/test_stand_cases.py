"""eval.stand_cases — случаи проверки стенда (BH-38) в eval."""

import csv
from pathlib import Path

import pytest

from eval.datasets import GOLDEN_COLUMNS, load_dataset
from eval.stand_cases import (
    FAIL_FORBIDDEN,
    FAIL_KEYWORDS,
    FAIL_ORIGIN,
    OK,
    StandCase,
    golden_rows,
    load_cases,
    normalize,
    verdict,
)

STAND = Path(__file__).parents[1] / "fixtures" / "stand_quality" / "cases.csv"
COLUMNS = (
    "id,category,question,expected_answer,forbidden,expected_material,"
    "in_corpus,expect_origin,dialogue_id,turn,verdict,origin,answer"
)


def _case(answer: str = "28 календарн | 28 дн", **kwargs: object) -> StandCase:
    groups = tuple(
        tuple(normalize(v) for v in group.split(" | ")) for group in answer.split("; ")
    )
    fields: dict[str, object] = {
        "id": "F-1",
        "category": "fact",
        "question": "?",
        "groups": groups,
        "forbidden": (),
        "materials": ("hr.docx",),
        "in_corpus": True,
    }
    fields.update(kwargs)
    return StandCase(**fields)  # type: ignore[arg-type]


def test_every_group_needs_one_variant() -> None:
    case = _case("2318; отдел персонала | кадр")

    assert verdict(case, "Звоните в отдел персонала: 2318.", general=False) == OK
    assert verdict(case, "Звоните по номеру 2318.", general=False) == FAIL_KEYWORDS


def test_typographic_variants_are_equal() -> None:
    case = _case("25-го; ёлк")

    # Неразрывный дефис и пробел, «ё» — как у бэкенда (PR-010 вручную).
    assert verdict(case, "До 25‑го числа, ЕЛКА", general=False) == OK
    assert verdict(_case("1 450"), "1 450 руб.", general=False) == OK


def test_forbidden_word_fails_even_with_keywords() -> None:
    case = _case("48 доллар", forbidden=("35 доллар",))

    assert verdict(case, "48 долларов, раньше 35 долларов", general=False) == (
        FAIL_FORBIDDEN
    )


def test_origin_must_match() -> None:
    answerable = _case()
    outside = _case(in_corpus=False, groups=(), materials=())
    general = "В документах компании ответа нет. Обычно 28 календарных дней."

    # Верные слова в общем ответе на вопрос по документам — не верный ответ.
    assert verdict(answerable, general, general=True) == FAIL_ORIGIN
    assert verdict(outside, general, general=True) == OK
    assert verdict(outside, "По положению — 3 дня [1].", general=False) == FAIL_ORIGIN


def test_load_cases_skips_edge_and_follow_up_turns(tmp_path: Path) -> None:
    path = tmp_path / "cases.csv"
    path.write_text(
        COLUMNS + "\nF-1,fact,Сколько?,28 календарн | 28 дн; отпуск,,a.docx;b.pdf,true,"
        "documents,,1,ok,documents,\n"
        "E-1,edge_indexed,Файл?,,,x.txt,true,documents,,1,,,\n"
        "D-1.1,dialogue,Первый?,да,,a.docx,true,documents,D-1,1,ok,documents,\n"
        "D-1.2,dialogue,А второй?,нет,,a.docx,true,documents,D-1,2,ok,documents,\n"
        "N-1,not_in_docs,Вне?,,,,false,general_knowledge,,1,ok,general_knowledge,\n",
        encoding="utf-8",
    )

    cases = load_cases(path)

    assert [c.id for c in cases] == ["F-1", "D-1.1", "N-1"]
    assert cases[0].groups == (("28 календарн", "28 дн"), ("отпуск",))
    assert cases[0].materials == ("a.docx", "b.pdf")
    assert not cases[2].in_corpus
    assert len(load_cases(path, first_turn_only=False)) == 4


def test_golden_rows_load_as_golden_dataset(tmp_path: Path) -> None:
    path = tmp_path / "golden.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(GOLDEN_COLUMNS))
        writer.writeheader()
        writer.writerows(golden_rows(load_cases(STAND)))

    items = load_dataset(path)

    assert len(items) == 155
    assert sum(not item.in_corpus for item in items) == 15
    assert {item.type for item in items} == {"fact", "out_of_corpus"}


def test_rule_matches_backend_verdicts_on_recorded_answers() -> None:
    # Ответы стенда 02.10 с вердиктами бэкенда: правило должно их повторять.
    cases = {c.id: c for c in load_cases(STAND, first_turn_only=False)}
    with STAND.open(encoding="utf-8-sig", newline="") as file:
        rows = [r for r in csv.DictReader(file) if r["verdict"] in ("ok", "fail")]

    mismatched = [
        r["id"]
        for r in rows
        if (
            verdict(
                cases[r["id"]],
                r["answer"],
                general=r["origin"] == "general_knowledge",
            )
            == OK
        )
        != (r["verdict"] == "ok")
    ]

    assert len(rows) == 179
    assert mismatched == []


@pytest.mark.parametrize(
    ("name", "count", "words"),
    [
        ("stand_long.csv", 15, (55, 80)),
        ("stand_long_val.csv", 20, (25, 80)),
        ("stand_mid_val.csv", 10, (15, 24)),
    ],
)
def test_question_sets_keep_answers_of_their_stand_cases(
    name: str, count: int, words: tuple[int, int]
) -> None:
    # Наборы реранкера на длинных вопросах (04.10): меняется только вопрос —
    # ответ, запрещённые слова и документ взяты из случая стенда, на который
    # указывает note; источники наборов не пересекаются.
    path = Path(__file__).parents[2] / "eval" / name
    with path.open(encoding="utf-8", newline="") as file:
        origin = {row["id"]: row["note"].split()[-1] for row in csv.DictReader(file)}
    source = {case.id: case for case in load_cases(STAND)}
    cases = load_cases(path)

    assert len(cases) == count
    for case in cases:
        stand = source[origin[case.id]]
        assert (case.groups, case.forbidden, case.materials, case.in_corpus) == (
            stand.groups,
            stand.forbidden,
            stand.materials,
            stand.in_corpus,
        )
        assert words[0] <= len(case.question.split()) <= words[1]


def test_question_sets_use_different_stand_cases() -> None:
    seen: list[str] = []
    for name in ("stand_long.csv", "stand_long_val.csv", "stand_mid_val.csv"):
        path = Path(__file__).parents[2] / "eval" / name
        with path.open(encoding="utf-8", newline="") as file:
            seen += [row["note"].split()[-1] for row in csv.DictReader(file)]
    assert len(seen) == len(set(seen)) == 45
