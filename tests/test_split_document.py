import pytest

from corp_ed.domain.split import (
    ChunkDraft,
    format_breadcrumbs,
    split_document,
    split_sections,
)
from corp_ed.domain.tokens import count_tokens

MARKUP = ("#", "**", "|", "__", "`")


def _split(
    markdown: str,
    *,
    chunk_tokens: int = 400,
    overlap_tokens: int = 0,
    title: str = "Документ",
) -> list[ChunkDraft]:
    return split_document(
        markdown, title=title, chunk_tokens=chunk_tokens, overlap_tokens=overlap_tokens
    )


def _body(chunk: ChunkDraft) -> str:
    """Тело чанка без первой строки-крошек."""
    return chunk.llm_text.split("\n", 1)[1]


# --- Граничные случаи ----------------------------------------------------------


def test_empty_document() -> None:
    assert _split("") == []
    assert _split("\n\n  \n") == []


def test_document_with_only_headings() -> None:
    assert _split("# Раздел\n\n## Подраздел\n\n### Ещё") == []


def test_single_short_paragraph() -> None:
    chunks = _split("Отпуск — 28 дней.", title="Положение об отпусках.docx")

    assert chunks == [
        ChunkDraft(
            position=0,
            heading_path=[],
            embed_text="Положение об отпусках\nОтпуск — 28 дней.",
            llm_text="Положение об отпусках\nОтпуск — 28 дней.",
        )
    ]


@pytest.mark.parametrize(
    ("chunk_tokens", "overlap_tokens"), [(0, 0), (-1, 0), (10, 10), (10, 11), (10, -1)]
)
def test_invalid_sizes(chunk_tokens: int, overlap_tokens: int) -> None:
    with pytest.raises(ValueError):
        _split("Текст.", chunk_tokens=chunk_tokens, overlap_tokens=overlap_tokens)


# --- Секции и крошки -----------------------------------------------------------


def test_chunk_does_not_cross_section_boundary() -> None:
    markdown = (
        "## Отпуск\n\nОтпуск — 28 дней.\n\n## Больничный\n\nБольничный оплачивается."
    )

    chunks = _split(markdown, chunk_tokens=400)

    # Обе секции влезли бы в один чанк, но граница секции важнее размера.
    assert [_body(c) for c in chunks] == [
        "Отпуск — 28 дней.",
        "Больничный оплачивается.",
    ]
    assert [c.heading_path for c in chunks] == [["Отпуск"], ["Больничный"]]


def test_heading_path_on_three_levels() -> None:
    markdown = (
        "# Положение об отпусках\n\nВведение.\n\n"
        "## Раздел 3\n\nОбщее.\n\n"
        "### 3.2 Перенос отпуска\n\nПеренос по заявлению.\n\n"
        "### 3.3 Отзыв из отпуска\n\nТолько с согласия.\n\n"
        "## Раздел 4\n\nДругое."
    )

    chunks = _split(markdown, title="Положение об отпусках")

    assert [c.heading_path for c in chunks] == [
        ["Положение об отпусках"],
        ["Положение об отпусках", "Раздел 3"],
        ["Положение об отпусках", "Раздел 3", "3.2 Перенос отпуска"],
        ["Положение об отпусках", "Раздел 3", "3.3 Отзыв из отпуска"],
        ["Положение об отпусках", "Раздел 4"],
    ]
    # Название документа совпадает с H1 — в крошках не дублируется.
    assert chunks[2].llm_text.split("\n")[0] == (
        "Положение об отпусках > Раздел 3 > 3.2 Перенос отпуска"
    )


def test_heading_level_skip_and_return() -> None:
    markdown = "# A\n\n### C\n\nтекст c\n\n## B\n\nтекст b"

    chunks = _split(markdown, title="")

    assert [c.heading_path for c in chunks] == [["A", "C"], ["A", "B"]]


def test_text_before_first_heading_has_only_title() -> None:
    chunks = _split("Преамбула.\n\n# Раздел\n\nТекст.", title="Док")

    assert chunks[0].heading_path == []
    assert chunks[0].llm_text == "Док\nПреамбула."


def test_heading_markup_is_cleaned_in_path() -> None:
    chunks = _split("## **3.2 Перенос** `отпуска`\n\nТекст.")

    assert chunks[0].heading_path == ["3.2 Перенос отпуска"]


def test_hash_in_code_block_is_not_a_section() -> None:
    markdown = "## Установка\n\n```\n# это комментарий\nrun()\n```"

    chunks = _split(markdown)

    assert len(chunks) == 1
    assert chunks[0].heading_path == ["Установка"]
    assert "# это комментарий" in chunks[0].llm_text


def test_positions_are_sequential_across_sections() -> None:
    markdown = "# A\n\nПервый.\n\n# B\n\nВторой.\n\n# C\n\nТретий."

    assert [c.position for c in _split(markdown)] == [0, 1, 2]


@pytest.mark.parametrize(
    ("title", "path", "expected"),
    [
        ("Положение.docx", ["Раздел 3"], "Положение > Раздел 3"),
        ("Отчёт.PDF", [], "Отчёт"),
        ("Положение", ["положение", "Раздел"], "Положение > Раздел"),
        ("", ["A", "B"], "A > B"),
        ("  Док  ", ["  много   пробелов "], "Док > много пробелов"),
        ("", [], ""),
    ],
)
def test_format_breadcrumbs(title: str, path: list[str], expected: str) -> None:
    assert format_breadcrumbs(title, path) == expected


# --- Два представления ----------------------------------------------------------


def test_embed_text_has_no_markup_llm_text_keeps_it() -> None:
    markdown = (
        "## Порядок\n\n**Важно:** заявление подаётся за _14 дней_.\n\n"
        "- первый шаг\n- второй шаг\n\n> цитата из приказа\n\n"
        "| колонка | другая |\n\nКод `SAP-1`."
    )

    chunk = _split(markdown)[0]

    for symbol in MARKUP:
        assert symbol not in chunk.embed_text, symbol
    assert "**Важно:**" in chunk.llm_text
    assert "- первый шаг" in chunk.llm_text
    assert "Важно: заявление подаётся за 14 дней." in chunk.embed_text


def test_both_views_start_with_breadcrumbs() -> None:
    chunk = _split("# Раздел\n\nТекст.", title="Док")[0]

    assert chunk.embed_text.startswith("Док > Раздел\n")
    assert chunk.llm_text.startswith("Док > Раздел\n")


# --- Границы и размер -----------------------------------------------------------


def test_article_abbreviation_is_not_split() -> None:
    first = "Согласно ст. 7 ТК РФ отпуск составляет 28 календарных дней."
    second = "Перенос возможен по заявлению работника."
    limit = count_tokens(first) + 1  # влезает одно предложение, но не два

    chunks = _split(f"{first} {second}", chunk_tokens=limit)

    assert [_body(c) for c in chunks] == [first, second]


def test_initials_and_te_are_not_split() -> None:
    text = (
        "Приказ подписал И. И. Иванов, т. е. генеральный директор. Второе предложение."
    )
    limit = (
        count_tokens("Приказ подписал И. И. Иванов, т. е. генеральный директор.") + 1
    )

    chunks = _split(text, chunk_tokens=limit)

    assert (
        _body(chunks[0]) == "Приказ подписал И. И. Иванов, т. е. генеральный директор."
    )


def test_long_sentence_is_split_by_words() -> None:
    sentence = " ".join(f"слово{i}" for i in range(200)) + "."

    chunks = _split(sentence, chunk_tokens=50)

    assert len(chunks) > 1
    assert all(count_tokens(_body(c)) <= 50 for c in chunks)
    # Ни одно слово не потеряно и не порвано.
    words = " ".join(_body(c) for c in chunks).split()
    assert words == sentence.split()


def test_word_longer_than_limit_is_split_by_characters() -> None:
    # Непериодичная строка: одинаковые куски схлопнула бы дедупликация.
    word = "".join(str(i) for i in range(200))

    chunks = _split(word, chunk_tokens=20)

    assert all(count_tokens(_body(c)) <= 20 for c in chunks)
    assert "".join(_body(c) for c in chunks) == word


def test_every_chunk_body_fits_limit_with_overlap() -> None:
    paragraphs = [
        " ".join(f"Предложение {p}.{s} о правилах работы компании." for s in range(8))
        for p in range(12)
    ]
    markdown = "## Правила\n\n" + "\n\n".join(paragraphs)

    chunks = _split(markdown, chunk_tokens=100, overlap_tokens=20)

    assert len(chunks) > 5
    assert all(count_tokens(_body(c)) <= 100 for c in chunks)


def test_overlap_repeats_whole_sentences_from_previous_chunk() -> None:
    sentences = [f"Пункт номер {i} правил внутреннего распорядка." for i in range(10)]
    one = count_tokens(sentences[0])

    chunks = _split(
        " ".join(sentences), chunk_tokens=one * 3 + 2, overlap_tokens=one + 1
    )

    assert len(chunks) > 1
    for previous, current in zip(chunks, chunks[1:], strict=False):
        last_sentence = _body(previous).rsplit(". ", 1)[-1]
        assert _body(current).startswith(last_sentence.rstrip("."))


def test_zero_overlap_adds_nothing() -> None:
    sentences = [f"Пункт {i} регламента." for i in range(6)]
    one = count_tokens(sentences[0])

    chunks = _split(" ".join(sentences), chunk_tokens=one * 2 + 1, overlap_tokens=0)

    assert " ".join(_body(c) for c in chunks) == " ".join(sentences)


def test_overlap_does_not_cross_sections() -> None:
    markdown = "## A\n\nПервая секция закончилась.\n\n## B\n\nВторая секция."

    chunks = _split(markdown, chunk_tokens=100, overlap_tokens=30)

    assert _body(chunks[1]) == "Вторая секция."


def test_table_row_is_not_split_between_chunks() -> None:
    rows = [
        f"Должность: Сотрудник {i}; Отпуск: {20 + i} дней; Оклад: {i} руб."
        for i in range(10)
    ]
    limit = count_tokens(rows[0]) * 3

    chunks = _split("## Таблица\n\n" + "\n".join(rows), chunk_tokens=limit)

    bodies_lines = [line for c in chunks for line in _body(c).split("\n")]
    assert bodies_lines == rows


def test_table_row_with_periods_is_not_sentence_split() -> None:
    row = "№: 1.; Мера: Грант. Субсидия.; Срок: 2 мес."

    chunks = _split(
        f"{row}\n{row.replace('1.', '2.')}", chunk_tokens=count_tokens(row) + 1
    )

    assert _body(chunks[0]) == row


def test_long_table_row_is_not_used_as_word_tail_overlap() -> None:
    # Строка таблицы длиннее перекрытия: обрывок «…; Сумма: 5 000 000» без
    # номера заявки в начале следующего чанка вводил бы в заблуждение.
    long_row = (
        "№ заявки: С1ИИ-600988; Название: "
        + " ".join(["слово"] * 30)
        + "; Сумма: 5 000 000"
    )
    next_row = "№ заявки: С1ИИ-601103; Название: Другой проект; Сумма: 3 000 000"
    # Длинная строка влезает целиком, но не вместе со следующей.
    limit = count_tokens(long_row) + 20

    chunks = _split(f"{long_row}\n{next_row}", chunk_tokens=limit, overlap_tokens=20)

    assert [_body(c) for c in chunks] == [long_row, next_row]


def test_list_items_keep_their_lines() -> None:
    markdown = (
        "Шаги:\n\n1. Написать заявление.\n2. Согласовать с руководителем.\n"
        "3. Передать в отдел кадров."
    )

    chunk = _split(markdown)[0]

    assert _body(chunk) == markdown


def test_hard_wrapped_lines_are_joined() -> None:
    markdown = "Отпуск предоставляется\nежегодно и составляет\n28 календарных дней."

    assert _body(_split(markdown)[0]) == (
        "Отпуск предоставляется ежегодно и составляет 28 календарных дней."
    )


def test_exact_duplicate_chunks_are_dropped() -> None:
    form = "## Форма заявления\n\nПрошу предоставить отпуск."
    chunks = _split(f"{form}\n\n{form}")

    assert len(chunks) == 1


def test_same_text_in_different_sections_is_kept() -> None:
    markdown = (
        "## Для офиса\n\nЗаявление за 14 дней.\n\n"
        "## Для удалёнки\n\nЗаявление за 14 дней."
    )

    assert len(_split(markdown)) == 2


# --- Секции (M2) ----------------------------------------------------------------


def test_split_sections_groups_chunks_and_keeps_full_markdown() -> None:
    markdown = "# Док\n\n## A\n\n**Первое.** Второе.\n\n## B\n\nТретье."

    sections = split_sections(markdown, title="Док", chunk_tokens=5, overlap_tokens=0)

    assert [s.heading_path for s in sections] == [["Док", "A"], ["Док", "B"]]
    assert sections[0].llm_text == "Док > A\n**Первое.** Второе."
    assert [c.position for s in sections for c in s.chunks] == [0, 1, 2]
    assert [s.position for s in sections] == [0, 1]


def test_split_document_is_flattened_sections() -> None:
    markdown = "# A\n\nРаз. Два. Три.\n\n# B\n\nЧетыре."
    kwargs = {"title": "Док", "chunk_tokens": 4, "overlap_tokens": 0}

    flat = split_document(markdown, **kwargs)  # type: ignore[arg-type]
    nested = split_sections(markdown, **kwargs)  # type: ignore[arg-type]

    assert flat == [chunk for section in nested for chunk in section.chunks]
