"""Генератор файлов корпуса из Markdown-исходников: md, txt, docx, xlsx,
pptx, pdf — только на том, что уже есть в проекте.

Библиотек Office в зависимостях нет (python-docx, openpyxl, python-pptx,
reportlab — нет), поэтому файлы собираются так же, как в тестах разбора
(`tests/ingest/*_samples.py`): пакеты OOXML — стандартным zipfile, PDF —
строками в формате. Результат читается теми же разборщиками, что и в
продукте (`corp_ed.ingest`), — это проверяет `test_corpus_build.py`.

PDF: готового шрифта с кириллицей в зависимостях нет, поэтому буквы заданы
кодировкой с /Differences и именами глифов (afii…, список имён Adobe), ширины —
явным массивом /Widths. Текст из такого PDF извлекается верно (проверено тем же
разбором, что в продукте), а нарисует его просмотрщик шрифтом-заменителем:
шрифт NorthWindSans не встроен, на экране вид может отличаться.

    python -m tests.acceptance.corpus.build ВЫХОДНАЯ_ПАПКА [формат ...]

Без форматов — каждый документ в формате из манифеста; с форматами —
в каждом из перечисленных. Плюс `staff.xlsx`: справочник всех 120
сотрудников (имена случайно составлены генератором, совпадения случайны).
"""

import random
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape

from tests.ingest import pptx_samples as pptx_lib
from tests.ingest import samples
from tests.ingest import xlsx_samples as xlsx_lib

FORMATS = ("md", "txt", "docx", "xlsx", "pptx", "pdf")
SUFFIX = {fmt: f".{fmt}" for fmt in FORMATS}
CORPUS = Path(__file__).resolve().parent

BlockKind = Literal["h1", "h2", "p", "li", "table"]


@dataclass
class Block:
    kind: BlockKind
    text: str = ""
    rows: list[list[str]] = field(default_factory=list)


def _plain(text: str) -> str:
    """Убрать **жирный** из Markdown-строки."""
    return text.replace("**", "")


def parse(markdown: str) -> list[Block]:
    """Markdown исходников → блоки. Понимаются: # и ## заголовки, списки
    «- » и «1. », таблицы с | и всё остальное как абзацы (по строке)."""
    blocks: list[Block] = []
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("## "):
            blocks.append(Block("h2", _plain(line[3:])))
        elif line.startswith("# "):
            blocks.append(Block("h1", _plain(line[2:])))
        elif line.startswith("|"):
            cells = [_plain(c.strip()) for c in line.strip().strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue  # разделитель шапки
            if blocks and blocks[-1].kind == "table":
                blocks[-1].rows.append(cells)
            else:
                blocks.append(Block("table", rows=[cells]))
        elif line.startswith("- "):
            blocks.append(Block("li", _plain(line[2:])))
        elif line[:1].isdigit() and ". " in line[:4]:
            blocks.append(Block("li", _plain(line)))
        else:
            blocks.append(Block("p", _plain(line)))
    return blocks


def _flat(block: Block) -> str:
    if block.kind == "table":
        return "\n".join(" | ".join(row) for row in block.rows)
    return ("– " if block.kind == "li" and not block.text[:1].isdigit() else "") + (
        block.text
    )


# --- md, txt ------------------------------------------------------------------


def to_md(markdown: str, extras: Sequence[str]) -> bytes:
    text = markdown.rstrip() + "".join(f"\n\n{extra}" for extra in extras) + "\n"
    return text.encode()


def to_txt(blocks: list[Block], extras: Sequence[str]) -> bytes:
    lines = [_flat(block) for block in blocks] + list(extras)
    return ("\n".join(lines) + "\n").encode()


# --- docx -----------------------------------------------------------------------


def _w_paragraph(text: str, style: str | None = None) -> str:
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return (
        f'<w:p>{props}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
    )


def _w_table(rows: list[list[str]]) -> str:
    body = "".join(
        "<w:tr>"
        + "".join(f"<w:tc>{_w_paragraph(cell)}</w:tc>" for cell in row)
        + "</w:tr>"
        for row in rows
    )
    return f"<w:tbl>{body}</w:tbl>"


def to_docx(blocks: list[Block], extras: Sequence[str]) -> bytes:
    parts = []
    for block in blocks:
        if block.kind == "h1":
            parts.append(_w_paragraph(block.text, "Heading1"))
        elif block.kind == "h2":
            parts.append(_w_paragraph(block.text, "Heading2"))
        elif block.kind == "table":
            parts.append(_w_table(block.rows))
        else:
            parts.append(_w_paragraph(_flat(block)))
    parts.extend(_w_paragraph(extra) for extra in extras)
    return samples.docx([], body_xml="".join(parts))


# --- xlsx -----------------------------------------------------------------------


def _column(index: int) -> str:
    return chr(ord("A") + index)


def to_xlsx(blocks: list[Block], extras: Sequence[str]) -> bytes:
    """Один лист: текст — по строке в колонке A, таблицы — построчно."""
    cells: dict[str, xlsx_lib.Cell] = {}
    row = 1
    for block in blocks:
        if block.kind == "table":
            for values in block.rows:
                for index, value in enumerate(values[:26]):
                    cells[f"{_column(index)}{row}"] = value
                row += 1
        else:
            cells[f"A{row}"] = _flat(block)
            row += 1
    for extra in extras:
        cells[f"A{row}"] = extra
        row += 1
    return xlsx_lib.xlsx([xlsx_lib.SheetSpec("Документ", cells)])


# --- pptx -----------------------------------------------------------------------


def to_pptx(blocks: list[Block], extras: Sequence[str]) -> bytes:
    """Заголовок и всё до первого «##» — первый слайд, дальше слайд на раздел."""
    slides: list[tuple[str, list[str]]] = [("", [])]
    for block in blocks:
        if block.kind in ("h1", "h2"):
            if block.kind == "h2" or not slides[0][0]:
                if block.kind == "h2":
                    slides.append((block.text, []))
                else:
                    slides[0] = (block.text, slides[0][1])
            continue
        slides[-1][1].append(_flat(block))
    slides[-1][1].extend(extras)
    specs = [
        pptx_lib.SlideSpec(
            shapes=[
                pptx_lib.title(heading),
                pptx_lib.text_shape(
                    [pptx_lib.paragraph(line) for line in body], ph="body"
                ),
            ]
        )
        for heading, body in slides
        if heading or body
    ]
    return pptx_lib.pptx(specs)


# --- pdf ------------------------------------------------------------------------


@cache
def _glyph_names() -> dict[str, str]:
    """Символ → имя глифа из списка имён Adobe (его читает pdfminer)."""
    from pdfminer.glyphlist import glyphname2unicode

    names: dict[str, str] = {}
    for name, char in sorted(
        glyphname2unicode.items(), key=lambda item: (len(item[0]), item[0])
    ):
        if len(char) == 1 and not name.startswith("uni") and char not in names:
            names[char] = name
    return names


def _wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    return [*lines, current] if current else lines


def to_pdf(blocks: list[Block], extras: Sequence[str]) -> bytes:
    # (текст, кегль, жирный)
    lines: list[tuple[str, int, bool]] = []
    for block in blocks:
        if block.kind == "h1":
            lines += [(line, 16, True) for line in _wrap(block.text, 55)]
        elif block.kind == "h2":
            lines += [(line, 13, True) for line in _wrap(block.text, 70)]
        elif block.kind == "table":
            lines += [(" | ".join(row), 10, False) for row in block.rows]
        else:
            lines += [(line, 11, False) for line in _wrap(_flat(block), 84)]
    for extra in extras:
        lines += [(line, 11, False) for line in _wrap(extra, 84)]

    codes: dict[str, int] = {}
    for text, _, _ in lines:
        for char in text:
            if ord(char) > 127 and char not in codes:
                codes[char] = 128 + len(codes)
    if len(codes) > 127:
        raise ValueError("в документе больше 127 нелатинских символов")
    names = _glyph_names()
    differences = " ".join(f"/{names[char]}" for char in codes)

    def encode(text: str) -> str:
        data = bytes(codes[c] if ord(c) > 127 else ord(c) for c in text)
        # latin-1 — чтобы байты 128–255 остались байтами в строке PDF
        return (
            data.decode("latin-1")
            .replace("\\", "\\\\")
            .replace("(", "\\(")
            .replace(")", "\\)")
        )

    pages: list[list[str]] = [[]]
    y = 800.0
    for text, size, bold in lines:
        leading = size * 1.5
        if y - leading < 56:
            pages.append([])
            y = 800.0
        y -= leading
        font = "F2" if bold else "F1"
        pages[-1].append(f"BT /{font} {size} Tf 56 {y:.1f} Td ({encode(text)}) Tj ET")

    encoding = (
        "<< /Type /Encoding /BaseEncoding /WinAnsiEncoding "
        f"/Differences [128 {differences}] >>"
    )

    # Ширины заданы явно: у кириллических глифов в метриках Helvetica их нет,
    # и разборщик склеил бы буквы в одну точку.
    def widths(average: int) -> str:
        values = [278 if code == 32 else average for code in range(32, 256)]
        return "[" + " ".join(map(str, values)) + "]"

    def font(name: str, average: int, descriptor: int) -> str:
        return (
            f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} /Encoding 5 0 R "
            f"/FirstChar 32 /LastChar 255 /Widths {widths(average)} "
            f"/FontDescriptor {descriptor} 0 R >>"
        )

    def descriptor(name: str, flags: int) -> str:
        # Имя шрифта не стандартное: у стандартных pdfminer берёт ширины
        # из своих таблиц по имени глифа и /Widths игнорирует, а у
        # кириллицы там ширин нет.
        return (
            f"<< /Type /FontDescriptor /FontName /{name} /Flags {flags} "
            "/FontBBox [-166 -225 1000 931] /ItalicAngle 0 /Ascent 718 "
            "/Descent -207 /CapHeight 718 /StemV 88 /MissingWidth 540 >>"
        )

    objects: list[str] = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "",
        font("NorthWindSans", 540, 6),
        font("NorthWindSans-Bold", 590, 7),
        encoding,
        descriptor("NorthWindSans", 32),
        descriptor("NorthWindSans-Bold", 262176),
    ]
    kids = []
    for ops in pages:
        content = "\n".join(ops)
        length = len(content.encode("latin-1"))
        objects.append(f"<< /Length {length} >>\nstream\n{content}\nendstream")
        stream_ref = len(objects)
        objects.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            "/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
            f"/Contents {stream_ref} 0 R >>"
        )
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"

    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    out += f"startxref\n{xref}\n%%EOF\n".encode()
    return out


# --- общий вход ----------------------------------------------------------------


def render(markdown: str, fmt: str, extras: Sequence[str] = ()) -> bytes:
    """Исходник Markdown → байты файла нужного формата.

    extras — абзацы, дописанные в конец (шаг «правка документа»)."""
    if fmt == "md":
        return to_md(markdown, extras)
    blocks = parse(markdown)
    renderers = {
        "txt": to_txt,
        "docx": to_docx,
        "xlsx": to_xlsx,
        "pptx": to_pptx,
        "pdf": to_pdf,
    }
    if fmt not in renderers:
        raise ValueError(f"неизвестный формат: {fmt}")
    return renderers[fmt](blocks, extras)


def read_source(relative: str) -> str:
    return (CORPUS / relative).read_text(encoding="utf-8")


# --- справочник сотрудников ------------------------------------------------------

_SURNAMES = [
    "Абрамов", "Белов", "Васильев", "Гусев", "Давыдов", "Егоров", "Жуков",
    "Зайцев", "Исаев", "Карпов", "Лазарев", "Медведев", "Новиков", "Осипов",
    "Попов", "Рябов", "Сорокин", "Титов", "Уваров", "Фролов", "Хохлов",
    "Чернов", "Шубин", "Щербаков", "Яковлев", "Воронин", "Дроздов", "Ефимов",
    "Журавлёв", "Игнатов",
]  # fmt: skip
_MALE = [
    "Александр", "Борис", "Виктор", "Георгий", "Денис", "Евгений", "Илья",
    "Леонид", "Максим", "Николай",
]  # fmt: skip
_FEMALE = [
    "Анна", "Валерия", "Галина", "Дарья", "Елена", "Инна", "Людмила",
    "Марина", "Оксана", "Светлана",
]  # fmt: skip
_PATRONYMIC = [
    "Александрович", "Борисович", "Викторович", "Игоревич", "Олегович",
    "Сергеевич",
]  # fmt: skip
_DEPARTMENTS = (
    ("Кадры", 9),
    ("Бухгалтерия", 12),
    ("Продажи", 44),
    ("ИТ", 15),
    ("Юристы", 6),
    ("Руководство, администрация и производство", 34),
)


def staff_rows(domain: str = "severny-veter.test") -> list[list[str]]:
    """120 строк: номер, ФИО, подразделение, почта. Детерминировано."""
    rng = random.Random(20261015)
    rows: list[list[str]] = [["№", "ФИО", "Подразделение", "Почта"]]
    number = 0
    for department, count in _DEPARTMENTS:
        for _ in range(count):
            number += 1
            female = rng.random() < 0.5
            surname = rng.choice(_SURNAMES)
            first = rng.choice(_FEMALE if female else _MALE)
            patronymic = rng.choice(_PATRONYMIC)
            if female:
                surname += "а"
                patronymic = patronymic.removesuffix("ич") + "на"
            rows.append(
                [
                    str(number),
                    f"{surname} {first} {patronymic}",
                    department,
                    f"staff{number:03d}@{domain}",
                ]
            )
    return rows


def staff_xlsx() -> bytes:
    cells: dict[str, xlsx_lib.Cell] = {"A1": "Справочник сотрудников (вымышленный)"}
    for r, values in enumerate(staff_rows(), start=3):
        for c, value in enumerate(values):
            cells[f"{_column(c)}{r}"] = value
    return xlsx_lib.xlsx([xlsx_lib.SheetSpec("Сотрудники", cells)])


def main(argv: Sequence[str] | None = None) -> int:
    from tests.acceptance.model import load_manifest

    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return 2
    out = Path(args[0])
    formats = args[1:]
    for fmt in formats:
        if fmt not in FORMATS:
            print(f"неизвестный формат: {fmt}", file=sys.stderr)
            return 2
    manifest = load_manifest()
    out.mkdir(parents=True, exist_ok=True)
    written = 0
    for document in manifest.documents.values():
        source = read_source(document.file)
        for fmt in formats or [document.format]:
            name = f"{document.id}-{Path(document.file).stem}{SUFFIX[fmt]}"
            (out / name).write_bytes(render(source, fmt))
            written += 1
    (out / "staff.xlsx").write_bytes(staff_xlsx())
    print(f"{written} файлов и staff.xlsx — в {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
