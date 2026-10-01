"""Разбор .pptx → Markdown (Р-5, BH-34): слайды, таблицы, диаграммы, защита."""

import io
import zipfile

import pytest

from corp_ed.ingest import pptx as pptx_module
from corp_ed.ingest.ooxml import OfficeFileError
from corp_ed.ingest.pptx import (
    check_container,
    pptx_to_markdown,
    read_presentation,
)
from corp_ed.ingest.preprocess import preprocess
from tests.ingest.pptx_samples import (
    ChartSpec,
    SlideSpec,
    chart_frame,
    diagram_frame,
    group,
    paragraph,
    pptx,
    table,
    text_shape,
    title,
)


def _lines(data: bytes) -> list[str]:
    markdown = pptx_to_markdown(data)
    return [line for line in preprocess(markdown).splitlines() if line.strip()]


def test_title_bullets_and_footer_placeholders() -> None:
    data = pptx(
        [
            SlideSpec(
                [
                    title("Первый день"),
                    text_shape(
                        [
                            paragraph("Пропуск — на ресепшене"),
                            paragraph("Пароль от Wi-Fi — в письме", level=1),
                            paragraph("Обед в 13:00", bullet=True),
                        ]
                    ),
                    text_shape([paragraph("7")], ph="sldNum"),
                    text_shape([paragraph("ООО «Пример»")], ph="ftr"),
                ]
            )
        ]
    )
    assert _lines(data) == [
        "# Первый день",
        "Пропуск — на ресепшене",
        "- Пароль от Wi-Fi — в письме",
        "- Обед в 13:00",
    ]


def test_shapes_are_read_top_to_bottom_left_to_right() -> None:
    data = pptx(
        [
            SlideSpec(
                [
                    title("Кто поможет"),
                    text_shape([paragraph("Внизу")], pos=(0, 5000)),
                    text_shape([paragraph("Справа вверху")], pos=(6000, 1000)),
                    text_shape([paragraph("Слева вверху")], pos=(0, 1000)),
                ]
            )
        ]
    )
    assert _lines(data)[1:] == ["Слева вверху", "Справа вверху", "Внизу"]


def test_slide_without_title_takes_first_short_line_or_number() -> None:
    data = pptx(
        [
            SlideSpec(
                [text_shape([paragraph("Фишинг: как распознать\nПишет «срочно»")])]
            ),
            SlideSpec([table([["Имя", "Телефон"], ["Анна", "1021"]])]),
        ]
    )
    assert _lines(data) == [
        "# Фишинг: как распознать",
        "Пишет «срочно»",
        "# Слайд 2",
        "Имя: Анна; Телефон: 1021",
    ]


def test_hidden_slides_are_skipped_and_order_follows_slide_list() -> None:
    data = pptx(
        [
            SlideSpec([title("Первый в файле")]),
            SlideSpec([title("Скрытый")], hidden=True),
            SlideSpec([title("Показывается первым")]),
        ],
        order=[2, 0, 1],
    )
    presentation = read_presentation(data)

    assert [s.title for s in presentation.slides] == [
        "Показывается первым",
        "Первый в файле",
    ]
    assert presentation.hidden_slides == [3]


def test_table_with_two_row_header_merges_and_group_row() -> None:
    data = pptx(
        [
            SlideSpec(
                [
                    title("Численность"),
                    table(
                        [
                            [
                                {"text": "Отдел", "rowSpan": "2"},
                                {"text": "Численность", "gridSpan": "2"},
                                {"hMerge": "1"},
                            ],
                            [{"vMerge": "1"}, "2025", "2026"],
                            [
                                {"text": "Москва", "gridSpan": "3"},
                                {"hMerge": "1"},
                                {"hMerge": "1"},
                            ],
                            ["Продажи", "34", "41"],
                        ]
                    ),
                ]
            )
        ]
    )
    presentation = read_presentation(data)
    assert presentation.tables == 1
    assert _lines(data) == [
        "# Численность",
        "## Москва",
        "Отдел: Продажи; Численность — 2025: 34; Численность — 2026: 41",
    ]


def test_chart_values_become_a_table() -> None:
    data = pptx(
        [
            SlideSpec(
                [title("Выручка"), chart_frame(0)],
                charts=[
                    ChartSpec(
                        "Выручка, млн ₽",
                        ["I квартал", "II квартал"],
                        [("2024", [118, 125]), ("2025", [126, 133.5])],
                    )
                ],
            ),
            SlideSpec(
                [title("Расходы"), chart_frame(0)],
                charts=[ChartSpec("Доли", ["Аренда"], [("2025", [0.14])], "0%")],
            ),
        ]
    )
    assert read_presentation(data).charts == 2
    assert _lines(data) == [
        "# Выручка",
        "Диаграмма: Выручка, млн ₽",
        "I квартал; 2024: 118; 2025: 126",
        "II квартал; 2024: 125; 2025: 133,5",
        "# Расходы",
        "Диаграмма: Доли",
        "Аренда; 2025: 14%",
    ]


def test_smartart_nodes_and_group_shapes() -> None:
    data = pptx(
        [
            SlideSpec(
                [
                    title("Как оформить отпуск"),
                    diagram_frame(0),
                    group([text_shape([paragraph("В группе")])], pos=(0, 9000)),
                ],
                diagrams=[["Заявление", "Согласование", "Приказ"]],
            )
        ]
    )
    assert read_presentation(data).diagrams == 1
    assert _lines(data) == [
        "# Как оформить отпуск",
        "- Заявление",
        "- Согласование",
        "- Приказ",
        "В группе",
    ]


def test_speaker_notes_follow_the_slide_without_slide_number() -> None:
    data = pptx(
        [SlideSpec([title("Пароли")], notes="Пароль никому не сообщать.\nДаже ИТ.")]
    )
    assert _lines(data) == [
        "# Пароли",
        "Заметки докладчика:",
        "Пароль никому не сообщать.",
        "Даже ИТ.",
    ]


def test_title_line_breaks_are_joined() -> None:
    data = pptx([SlideSpec([title("Итоги 2025\nи планы")])])
    assert _lines(data) == ["# Итоги 2025 и планы"]


def test_superscript_runs_do_not_glue_to_numbers() -> None:
    raised = '<a:r><a:rPr baseline="30000"/><a:t>1</a:t></a:r>'
    body = (
        f"<a:p><a:r><a:t>Бюджет 5 000 000</a:t></a:r>{raised}"
        "<a:r><a:t> ₽</a:t></a:r></a:p>"
    )
    data = pptx([SlideSpec([title("Итоги"), text_shape([body])])])
    assert "5 000 000<sup>1</sup> ₽" in pptx_to_markdown(data)
    assert _lines(data) == ["# Итоги", "Бюджет 5 000 000 ₽"]


def test_container_checks() -> None:
    from tests.ingest.samples import docx
    from tests.ingest.xlsx_samples import SheetSpec, xlsx

    for wrong in (docx([("Текст", None)]), xlsx([SheetSpec("Лист1", {"A1": "x"})])):
        with pytest.raises(OfficeFileError) as error:
            check_container(wrong)
        assert error.value.code == "format_mismatch"

    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    with pytest.raises(OfficeFileError) as error:
        check_container(ole + "EncryptedPackage".encode("utf-16-le"))
    assert error.value.code == "encrypted"


def test_doctype_in_slide_is_corrupted() -> None:
    data = pptx([SlideSpec([title("x")])])
    source = zipfile.ZipFile(io.BytesIO(data))
    patched = io.BytesIO()
    with zipfile.ZipFile(patched, "w") as archive:
        for item in source.infolist():
            content = source.read(item)
            if item.filename == "ppt/slides/slide1.xml":
                content = content.replace(b"<p:sld", b"<!DOCTYPE p:sld><p:sld", 1)
            archive.writestr(item, content)
    with pytest.raises(OfficeFileError) as error:
        read_presentation(patched.getvalue())
    assert error.value.code == "corrupted"


def test_too_many_slides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pptx_module, "MAX_SLIDES", 1)
    data = pptx([SlideSpec([title("1")]), SlideSpec([title("2")])])
    with pytest.raises(OfficeFileError) as error:
        read_presentation(data)
    assert error.value.code == "document_too_large"


def test_empty_presentation_gives_empty_markdown() -> None:
    assert pptx_to_markdown(pptx([])) == ""
