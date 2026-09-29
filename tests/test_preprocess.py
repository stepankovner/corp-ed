import pytest

from corp_ed.domain.markdown import strip_emphasis
from corp_ed.ingest.preprocess import PAGE_BREAK, preprocess


def test_empty_input() -> None:
    assert preprocess("") == ""
    assert preprocess(" \n\n \t\n") == ""


# --- Таблицы -----------------------------------------------------------------


def test_table_3x3_becomes_three_key_value_lines() -> None:
    markdown = (
        "| Должность | Отпуск | Оклад |\n"
        "|---|---|---|\n"
        "| Инженер | 28 дней | 100 000 |\n"
        "| Юрист | 31 день | 120 000 |\n"
        "| Водитель | 28 дней | 70 000 |"
    )

    assert preprocess(markdown).split("\n") == [
        "Должность: Инженер; Отпуск: 28 дней; Оклад: 100 000",
        "Должность: Юрист; Отпуск: 31 день; Оклад: 120 000",
        "Должность: Водитель; Отпуск: 28 дней; Оклад: 70 000",
    ]


def test_table_surrounded_by_text_is_separated_by_blank_lines() -> None:
    markdown = "Перед таблицей.\n| A | B |\n|---|---|\n| 1 | 2 |\nПосле таблицы."

    assert preprocess(markdown) == "Перед таблицей.\n\nA: 1; B: 2\n\nПосле таблицы."


def test_table_cells_are_cleaned() -> None:
    markdown = (
        "|**№**<br>**п/п**|**Сумма**, руб.:|Col3|\n"
        "|---|:---:|---|\n"
        "|**1.**|_5 000_|есть|"
    )

    assert preprocess(markdown) == "№ п/п: 1.; Сумма, руб.: 5 000; есть"


def test_table_empty_cells_are_skipped() -> None:
    markdown = "| № | Название | Сумма |\n|---|---|---|\n|  | Проект | 5 000 |"

    assert preprocess(markdown) == "Название: Проект; Сумма: 5 000"


def test_table_escaped_pipe_stays_inside_cell() -> None:
    markdown = "| Команда | Смысл |\n|---|---|\n| a \\| b | или |"

    assert preprocess(markdown) == "Команда: a | b; Смысл: или"


def test_single_column_table_becomes_plain_lines() -> None:
    markdown = "| Шаг первый |\n|---|\n| Шаг второй |\n| Шаг третий |"

    assert preprocess(markdown).split("\n") == [
        "Шаг первый",
        "Шаг второй",
        "Шаг третий",
    ]


def test_header_only_table_keeps_its_text() -> None:
    markdown = "|**1.**|**Запрашиваемая сумма гранта**, млн рублей||\n|---|---|---|"

    assert preprocess(markdown) == "1.; Запрашиваемая сумма гранта, млн рублей"


def test_table_continued_on_next_page_gets_previous_header() -> None:
    # Регрессия с реального PDF: таблица переходит на новую страницу,
    # pymupdf4llm отдаёт продолжение без шапки и без |---|.
    markdown = (
        "| № | Направление |\n|---|---|\n| 1.8 | Контроль |"
        f"{PAGE_BREAK}"
        "| 1.9 | Нормы времени |\n| 1.10 | Планирование |"
    )

    assert preprocess(markdown).split("\n") == [
        "№: 1.8; Направление: Контроль",
        "",
        "№: 1.9; Направление: Нормы времени",
        "№: 1.10; Направление: Планирование",
    ]


def test_headerless_rows_with_other_width_get_values_only() -> None:
    markdown = "| A | B |\n|---|---|\n| 1 | 2 |\n\n| x | y | z |"

    assert preprocess(markdown).split("\n\n") == ["A: 1; B: 2", "x; y; z"]


def test_headerless_rows_after_text_get_values_only() -> None:
    markdown = "| A | B |\n|---|---|\n| 1 | 2 |\n\nТекст между.\n\n| 3 | 4 |"

    assert preprocess(markdown).split("\n\n") == ["A: 1; B: 2", "Текст между.", "3; 4"]


def test_pipe_in_text_without_separator_is_not_a_table() -> None:
    markdown = "Выбор: да | нет\nследующая строка"

    assert preprocess(markdown) == markdown


# --- Ссылки ------------------------------------------------------------------


def test_link_becomes_its_text() -> None:
    markdown = (
        "См. [Положение об отпусках](https://portal.example.ru/docs/vacation?id=1)."
    )

    assert preprocess(markdown) == "См. Положение об отпусках."


def test_bare_url_is_removed() -> None:
    markdown = "Заявку подать на портале https://portal.example.ru/hr, срок — 3 дня."

    assert preprocess(markdown) == "Заявку подать на портале, срок — 3 дня."


def test_url_in_parentheses_leaves_no_empty_brackets() -> None:
    markdown = "Регламент (https://example.ru/reg) действует с 2026 года."

    assert preprocess(markdown) == "Регламент действует с 2026 года."


def test_image_link_and_autolink() -> None:
    markdown = "![Схема процесса](img/scheme.png) и <https://example.ru>"

    assert preprocess(markdown) == "Схема процесса и"


def test_email_is_kept() -> None:
    markdown = "Пишите на hr@example.ru или <mailto:help@example.ru>"

    assert preprocess(markdown) == "Пишите на hr@example.ru или help@example.ru"


def test_reference_links() -> None:
    markdown = "Читай [регламент][1].\n\n[1]: https://example.ru/reg"

    assert preprocess(markdown) == "Читай регламент."


# --- Заголовки ---------------------------------------------------------------


def test_headings_are_preserved() -> None:
    markdown = (
        "# Положение об отпусках\n\nТекст.\n\n## Раздел 3\n\n"
        "### 3.2 Перенос отпуска\n\nЕщё текст.\n\n###### Глубоко"
    )

    result = preprocess(markdown)

    assert "# Положение об отпусках" in result.split("\n")
    assert "## Раздел 3" in result.split("\n")
    assert "### 3.2 Перенос отпуска" in result.split("\n")
    assert "###### Глубоко" in result.split("\n")


def test_heading_markup_is_removed_but_level_kept() -> None:
    markdown = "## **<u>1) Критерий «Новизна»</u>**<sup>**18**</sup>\n\nТекст."

    assert preprocess(markdown) == "## 1) Критерий «Новизна»\n\nТекст."


def test_empty_heading_is_dropped() -> None:
    assert preprocess("## ****\n\nТекст.") == "Текст."


def test_bold_lines_become_headings_when_document_has_none() -> None:
    markdown = (
        "**Общие положения**\n\nТекст раздела.\n\n"
        "**3.2. Перенос отпуска**\n\nПеренос по заявлению."
    )

    assert preprocess(markdown).split("\n\n") == [
        "## Общие положения",
        "Текст раздела.",
        "### 3.2. Перенос отпуска",
        "Перенос по заявлению.",
    ]


def test_bold_lines_are_not_promoted_when_headings_exist() -> None:
    markdown = "# Документ\n\n**Важно**\n\nТекст."

    assert preprocess(markdown) == markdown


def test_long_bold_paragraph_is_not_promoted() -> None:
    sentence = "**" + " ".join(["слово"] * 20) + "**"

    assert preprocess(sentence) == sentence


def test_hash_inside_code_block_is_not_a_heading() -> None:
    markdown = "**Установка**\n\n```\n# комментарий\n```"

    assert preprocess(markdown) == "## Установка\n\n```\n# комментарий\n```"


# --- Колонтитулы и оглавление ------------------------------------------------


def test_repeated_page_edge_lines_are_removed() -> None:
    pages = [
        f"ООО «Ромашка». Положение об отпусках\n\n"
        f"Абзац {n}.1.\n\nАбзац {n}.2.\n\nАбзац {n}.3.\n\n"
        f"Редакция 2 от 01.09.2026, лист {n}"
        for n in range(1, 6)
    ]

    result = preprocess(PAGE_BREAK.join(pages))

    assert "Ромашка" not in result
    assert "Редакция" not in result
    assert result.split("\n\n") == [
        f"Абзац {n}.{k}." for n in range(1, 6) for k in range(1, 4)
    ]


def test_numbered_articles_at_page_top_are_not_furniture() -> None:
    # Каждая страница открывается новой статьёй: после маскировки цифр
    # строки совпали бы. Заголовки и вторые от края строки маской не
    # сравниваются.
    pages = [
        f"Шапка документа\n\nСтатья {n}. Права работника\n\nТекст {n}.\n\n"
        f"Ещё текст {n}.\n\nФутер"
        for n in range(1, 6)
    ]

    result = preprocess(PAGE_BREAK.join(pages))

    assert result.count("Статья") == 5
    assert "Шапка" not in result
    assert "Футер" not in result


def test_short_pages_are_not_mistaken_for_furniture() -> None:
    # На странице из трёх строк края покрывают всё, а после маскировки
    # цифр абзацы совпадают. Такие страницы в подсчёт частоты не идут.
    pages = [f"Пункт {n}.\n\nТекст {n}.\n\nИтог {n}." for n in range(1, 6)]

    result = preprocess(PAGE_BREAK.join(pages))

    assert result.count("Пункт") == 5
    assert result.count("Итог") == 5


def test_line_on_minority_of_pages_is_kept() -> None:
    pages = [
        "Приложение № 1 к Договору\n\nА",
        "Б",
        "В",
        "Приложение № 2 к Договору\n\nГ",
    ]

    result = preprocess(PAGE_BREAK.join(pages))

    assert "Приложение № 1 к Договору" in result
    assert "Приложение № 2 к Договору" in result


@pytest.mark.parametrize("line", ["4", "- 4 -", "стр. 4", "Страница 4 из 85", "4/85"])
def test_page_number_lines_are_removed_without_page_breaks(line: str) -> None:
    assert (
        preprocess(f"Абзац один.\n\n{line}\n\nАбзац два.")
        == "Абзац один.\n\nАбзац два."
    )


def test_numbered_list_marker_is_not_a_page_number() -> None:
    markdown = "1. Первый пункт\n2. Второй пункт"

    assert preprocess(markdown) == markdown


def test_table_of_contents_is_removed() -> None:
    markdown = (
        "# Положение\n\n## Содержание\n\n1. Общие положения ........ 3\n"
        "2. Отпуск ........ 5\n\n## 1. Общие положения\n\nТекст."
    )

    assert preprocess(markdown) == "# Положение\n\n## 1. Общие положения\n\nТекст."


def test_toc_removal_stops_at_next_heading_of_any_level() -> None:
    # Регрессия с реального PDF (УМНИК-2026): «# СОДЕРЖАНИЕ» уровнем выше
    # всех разделов «### …». Удалять надо только оглавление.
    markdown = (
        "# СОДЕРЖАНИЕ\n\n1. ОБЩИЕ ПОЛОЖЕНИЯ ........ 3\n2. ПОРЯДОК ........ 7\n\n"
        "### 1. ОБЩИЕ ПОЛОЖЕНИЯ\n\nТекст первого.\n\n### 2. ПОРЯДОК\n\nТекст второго."
    )

    assert preprocess(markdown) == (
        "### 1. ОБЩИЕ ПОЛОЖЕНИЯ\n\nТекст первого.\n\n### 2. ПОРЯДОК\n\nТекст второго."
    )


def test_section_called_contents_without_toc_body_is_kept() -> None:
    markdown = "## Содержание\n\nВ этом разделе описано, что входит в пакет документов."

    assert preprocess(markdown) == markdown


def test_dot_leaders_are_removed_everywhere() -> None:
    assert preprocess("Раздел 1 ............ 12") == "Раздел 1"


# --- Символы и пробелы -------------------------------------------------------


def test_unicode_is_normalized() -> None:
    decomposed_y = "и\u0306"  # «й» двумя кодовыми точками, как отдаёт PDF
    markdown = f"Мо{decomposed_y}\u00a0отпуск\u00adный\u200b день\r\n"

    assert preprocess(markdown) == "Мой отпускный день"


def test_whitespace_is_normalized() -> None:
    markdown = "Строка   с  пробелами   \n\n\n\n\nДругая\t строка  "

    assert preprocess(markdown) == "Строка с пробелами\n\nДругая строка"


def test_list_indentation_is_kept() -> None:
    markdown = "- пункт\n  - вложенный пункт"

    assert preprocess(markdown) == markdown


def test_markdownify_escapes_are_removed() -> None:
    assert preprocess("Поле tenant\\_id и 5\\*3") == "Поле tenant_id и 5*3"


def test_html_inline_tags() -> None:
    markdown = "Срок<sup>13</sup> — <u>важно</u>, строка<br>дальше"

    assert preprocess(markdown) == "Срок — важно, строка дальше"


# --- Свойства ----------------------------------------------------------------


def test_preprocess_is_idempotent() -> None:
    markdown = (
        "**Заголовок**\n\n| A | B |\n|---|---|\n| 1 | [x](http://y.ru) |\n\n"
        "Текст https://z.ru   конец.\n\n\n\n4\n\n- пункт"
    )

    once = preprocess(markdown)

    assert preprocess(once) == once


def test_strip_emphasis_keeps_underscore_inside_words() -> None:
    assert strip_emphasis("**жирный** _курсив_ *тоже* file_name") == (
        "жирный курсив тоже file_name"
    )
