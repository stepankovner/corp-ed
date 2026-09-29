from pathlib import Path

import pytest

from eval.datasets import (
    GOLDEN_COLUMNS,
    SILVER_COLUMNS,
    EvalItem,
    check_golden_composition,
    load_dataset,
    select_split,
    stratified_split,
    write_split,
)
from eval.relevance import (
    RetrievedChunk,
    canonical_ranking,
    evidence_coverage,
    gold_key,
    is_relevant,
    normalize_material,
    section_matches,
)


def _write(path: Path, header: tuple[str, ...], rows: list[list[str]]) -> Path:
    lines = [",".join(header)] + [",".join(f'"{cell}"' for cell in row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- Загрузка наборов --------------------------------------------------------------


def test_load_golden_from_spec_example(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "golden.csv",
        GOLDEN_COLUMNS,
        [
            [
                "g01",
                "Сколько дней отпуска положено?",
                "28 календарных дней",
                "Положение об отпусках.docx",
                "3.1",
                "true",
                "fact",
            ],
            ["g26", "Какая погода в Москве?", "", "", "", "false", "out_of_corpus"],
        ],
    )

    items = load_dataset(path)

    assert items[0] == EvalItem(
        id="g01",
        question="Сколько дней отпуска положено?",
        in_corpus=True,
        type="fact",
        expected_answer="28 календарных дней",
        expected_material="Положение об отпусках.docx",
        expected_section="3.1",
    )
    assert items[1].in_corpus is False


def test_load_silver(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "silver.csv",
        SILVER_COLUMNS,
        [
            [
                "s001",
                "Когда подают заявление?",
                "Положение",
                "12",
                "Раздел 3",
                "Заявление подаётся за 14 дней.",
                "dev",
                "v2-400-50",
            ]
        ],
    )

    item = load_dataset(path)[0]

    assert (item.type, item.split, item.evidence) == (
        "silver",
        "dev",
        "Заявление подаётся за 14 дней.",
    )


def test_load_rejects_unknown_type_and_duplicates(tmp_path: Path) -> None:
    bad_type = _write(
        tmp_path / "a.csv", GOLDEN_COLUMNS, [["g1", "?", "", "", "", "true", "opinion"]]
    )
    duplicate = _write(
        tmp_path / "b.csv",
        GOLDEN_COLUMNS,
        [["g1", "?", "", "", "", "false", "out_of_corpus"]] * 2,
    )

    with pytest.raises(ValueError, match="unknown type"):
        load_dataset(bad_type)
    with pytest.raises(ValueError, match="duplicate"):
        load_dataset(duplicate)


def test_load_rejects_unknown_format(tmp_path: Path) -> None:
    path = _write(tmp_path / "x.csv", ("a", "b"), [["1", "2"]])

    with pytest.raises(ValueError, match="unknown dataset format"):
        load_dataset(path)


def _item(
    item_id: str, in_corpus: bool, question_type: str, level: str = "detail"
) -> EvalItem:
    return EvalItem(
        id=item_id,
        question=f"Вопрос {item_id}?",
        in_corpus=in_corpus,
        type=question_type,
        expected_answer="ответ" if in_corpus else "",
        expected_material="Док" if in_corpus else "",
        level=level if in_corpus else "",
    )


def test_composition_matches_spec() -> None:
    items = (
        [_item(f"g{i:02}", True, "negation" if i < 5 else "fact") for i in range(25)]
        + [_item(f"o{i:02}", False, "out_of_corpus") for i in range(10)]
        + [_item(f"iv{i:02}", True, "procedure") for i in range(5)]
    )

    assert check_golden_composition(items) == []


def test_composition_allows_more_questions_than_spec() -> None:
    # Числа ТЗ — минимум: в dev добавлен запас (25.09), это не ошибка.
    items = (
        [_item(f"g{i:02}", True, "negation" if i < 6 else "fact") for i in range(37)]
        + [_item(f"o{i:02}", False, "out_of_corpus") for i in range(10)]
        + [_item(f"iv{i:02}", True, "procedure") for i in range(5)]
    )

    assert check_golden_composition(items) == []


def test_composition_problems_are_reported() -> None:
    items = [_item("g01", True, "fact"), _item("g02", False, "fact")]

    problems = check_golden_composition(items)

    assert any("всего вопросов: 2" in p for p in problems)
    assert any("negation" in p for p in problems)
    assert any("g02: in_corpus=false, но type=fact" in p for p in problems)


# --- Релевантность ------------------------------------------------------------------

GOLDEN = EvalItem(
    id="g01",
    question="Сколько дней отпуска?",
    in_corpus=True,
    type="fact",
    expected_material="Положение об отпусках.docx",
    expected_section="3.1",
)
SILVER = EvalItem(
    id="s01",
    question="За сколько подают заявление?",
    in_corpus=True,
    type="silver",
    expected_material="Положение об отпусках",
    evidence="Заявление подаётся не позднее чем за 14 дней.",
)


def _chunk(
    content: str,
    path: list[str] | None = None,
    material: str = "Положение об отпусках",
    chunk_id: str = "c",
) -> RetrievedChunk:
    return RetrievedChunk(
        id=chunk_id, material=material, content=content, heading_path=path or []
    )


def test_normalize_material() -> None:
    assert (
        normalize_material(" Положение об  Отпусках.DOCX ") == "положение об отпусках"
    )


@pytest.mark.parametrize(
    "heading", ["3.1 Продолжительность", "3.1. Продолжительность", "3.1"]
)
def test_section_matches_heading_path(heading: str) -> None:
    assert section_matches("3.1", _chunk("текст", ["Раздел 3", heading]))


def test_section_does_not_match_prefix_of_other_number() -> None:
    assert not section_matches("3.1", _chunk("текст", ["3.12 Другое"]))


def test_section_matches_line_in_content_without_heading_path() -> None:
    assert section_matches("3.1", _chunk("Раздел 3\n3.1. Отпуск составляет 28 дней."))


def test_golden_relevance_needs_material_and_section() -> None:
    right = _chunk("…", ["3.1 Продолжительность"])
    wrong_section = _chunk("…", ["3.2 Перенос"])
    wrong_material = _chunk("…", ["3.1 Продолжительность"], material="Памятка")

    assert is_relevant(GOLDEN, right)
    assert not is_relevant(GOLDEN, wrong_section)
    assert not is_relevant(GOLDEN, wrong_material)


def test_evidence_coverage() -> None:
    content = "Раздел 3\nЗаявление подаётся не позднее, чем за 14 дней!"

    assert evidence_coverage(SILVER.evidence, content) == 1.0
    assert evidence_coverage(SILVER.evidence, "Совсем другой текст.") == 0.0
    assert evidence_coverage("", content) == 0.0


def test_silver_relevance_independent_of_chunking() -> None:
    # Одна и та же цитата в чанках разных нарезок — релевантны оба.
    small = _chunk("Заявление подаётся не позднее чем за 14 дней.", chunk_id="v2-12")
    big = _chunk(
        "Общие положения. " * 20 + SILVER.evidence + " Далее.", chunk_id="v1-3"
    )

    assert is_relevant(SILVER, small)
    assert is_relevant(SILVER, big)


def test_evidence_split_by_chunk_boundary_is_partially_covered() -> None:
    half = _chunk("Заявление подаётся не позднее чем")

    assert not is_relevant(SILVER, half)


def test_out_of_corpus_is_never_relevant() -> None:
    item = EvalItem(id="o1", question="Погода?", in_corpus=False, type="out_of_corpus")

    assert not is_relevant(item, _chunk("что угодно"))


def test_canonical_ranking() -> None:
    chunks = [
        _chunk("…", ["3.2"], chunk_id="a"),
        _chunk("…", ["3.1"], chunk_id="b"),
        _chunk("…", ["3.1"], chunk_id="c"),
    ]

    assert canonical_ranking(GOLDEN, chunks) == [
        "a",
        gold_key(GOLDEN),
        gold_key(GOLDEN),
    ]


# --- Золотой набор: уровень, split, разбиение (задача 2.1) --------------------------


def test_golden_level_and_split_columns(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "golden.csv",
        (*GOLDEN_COLUMNS, "level", "split"),
        [
            [
                "g01",
                "Сколько дней?",
                "28",
                "Док",
                "3.1",
                "true",
                "fact",
                "Деталь",
                "dev",
            ],
            [
                "g02",
                "Про что документ?",
                "про отпуск",
                "Док",
                "",
                "true",
                "fact",
                "тема",
                "",
            ],
        ],
    )

    first, second = load_dataset(path)

    assert (first.level, first.split) == ("detail", "dev")
    assert (second.level, second.split) == ("topic", "")


def test_golden_evidence_decides_relevance(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "golden.csv",
        (*GOLDEN_COLUMNS, "level", "evidence", "author"),
        [
            [
                "g01",
                "За сколько подавать заявление?",
                "За 14 дней",
                "Положение об отпусках",
                "3",
                "true",
                "fact",
                "деталь",
                "Заявление подаётся не позднее чем за 14 дней.",
                "llm",
            ]
        ],
    )

    (item,) = load_dataset(path)
    same_section = _chunk("Отпуск — 28 дней.", ["3 Отпуск"])
    with_quote = _chunk("…Заявление подаётся не позднее чем за 14 дней.", ["3 Отпуск"])

    # Раздел 3 — несколько чанков: засчитывается только чанк с цитатой.
    assert item.evidence == "Заявление подаётся не позднее чем за 14 дней."
    assert not is_relevant(item, same_section)
    assert is_relevant(item, with_quote)


def test_golden_rejects_unknown_level(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "golden.csv",
        (*GOLDEN_COLUMNS, "level"),
        [["g01", "Вопрос?", "ответ", "Док", "", "true", "fact", "средний"]],
    )

    with pytest.raises(ValueError, match="unknown level"):
        load_dataset(path)


def test_composition_reports_duplicates_and_missing_level() -> None:
    items = [
        _item("g01", True, "fact"),
        EvalItem("g02", "вопрос G01?!", True, "fact", "ответ", "Док"),
    ]

    problems = check_golden_composition(items)

    # Регистр, «ё» и знаки препинания не делают вопрос новым.
    assert any("g02: тот же вопрос, что g01" in p for p in problems)
    assert any("g02: вопрос по корпусу без level" in p for p in problems)


def _golden_40() -> list[EvalItem]:
    return (
        [_item(f"g{i:02}", True, "negation", "detail") for i in range(5)]
        + [_item(f"g{i:02}", True, "fact", "detail") for i in range(5, 17)]
        + [_item(f"g{i:02}", True, "fact", "topic") for i in range(17, 25)]
        + [_item(f"o{i:02}", False, "out_of_corpus") for i in range(10)]
        + [_item(f"iv{i:02}", True, "procedure", "topic") for i in range(5)]
    )


def test_stratified_split_balances_every_stratum() -> None:
    items = _golden_40()

    assignment = stratified_split(items)

    assert sorted(assignment) == sorted(item.id for item in items)
    for prefix, total in (("o", 10), ("iv", 5)):
        holdout = [
            i for i, s in assignment.items() if i.startswith(prefix) and s == "holdout"
        ]
        assert len(holdout) in (total // 2, (total + 1) // 2)
    negation = [assignment[f"g{i:02}"] for i in range(5)]
    assert negation.count("holdout") in (2, 3)
    # Нечётные слои (5 отрицаний, 5 из интервью) делятся по очереди: итог 20/20.
    assert list(assignment.values()).count("holdout") == 20


def test_stratified_split_is_deterministic_and_order_free() -> None:
    items = _golden_40()

    assert stratified_split(items) == stratified_split(list(reversed(items)))
    assert stratified_split(items, seed="другой") != stratified_split(items)


def test_select_split_hides_holdout_unless_asked() -> None:
    dev = EvalItem("g01", "a?", True, "fact", split="dev")
    holdout = EvalItem("g02", "b?", True, "fact", split="holdout")
    plain = EvalItem("s01", "c?", True, "silver")

    assert select_split([dev, holdout, plain], "") == [dev, plain]
    assert select_split([dev, holdout, plain], "holdout") == [holdout]


def test_write_split_adds_column(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "golden.csv",
        GOLDEN_COLUMNS,
        [["g01", "Вопрос?", "ответ", "Док", "", "true", "fact"]],
    )

    write_split(path, {"g01": "holdout"})

    assert load_dataset(path)[0].split == "holdout"
