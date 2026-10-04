"""Конструктор PDF для тестов ingest.pdf — без pymupdf и без бинарников.

Стандартные шрифты PDF (Helvetica, Helvetica-Bold) — только латиница,
этого хватает для структуры: кегль, жирность, верхний индекс, линии
таблиц, колонки, страницы. Координаты — пункты от левого нижнего угла
листа A4 (595 × 842).
"""

from dataclasses import dataclass, field

WIDTH, HEIGHT = 595, 842


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


@dataclass
class Page:
    ops: list[str] = field(default_factory=list)

    def text(
        self, x: float, y: float, value: str, size: float = 11, bold: bool = False
    ) -> "Page":
        font = "F2" if bold else "F1"
        self.ops.append(f"BT /{font} {size} Tf {x} {y} Td ({_escape(value)}) Tj ET")
        return self

    def line(self, x1: float, y1: float, x2: float, y2: float) -> "Page":
        self.ops.append(f"0.5 w {x1} {y1} m {x2} {y2} l S")
        return self

    def table(
        self,
        x: float,
        top: float,
        widths: list[float],
        rows: list[list[str]],
        row_height: float = 20,
    ) -> "Page":
        """Таблица с сеткой: линии по всем границам, текст в ячейках."""
        right = x + sum(widths)
        bottom = top - row_height * len(rows)
        for i in range(len(rows) + 1):
            self.line(x, top - i * row_height, right, top - i * row_height)
        edge = x
        for width in [0.0, *widths]:
            edge += width
            self.line(edge, top, edge, bottom)
        for r, row in enumerate(rows):
            left = x
            for width, value in zip(widths, row, strict=False):
                if value:
                    self.text(left + 4, top - (r + 1) * row_height + 6, value, size=10)
                left += width
        return self


def pdf(pages: list[Page]) -> bytes:
    """Страницы → байты PDF с верной таблицей xref."""
    objects: list[str] = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "",  # Pages — после того как известны номера страниц
        _font("Helvetica"),
        _font("Helvetica-Bold"),
    ]
    kids = []
    for page in pages:
        content = "\n".join(page.ops)
        length = len(content.encode("latin-1"))
        objects.append(f"<< /Length {length} >>\nstream\n{content}\nendstream")
        content_ref = len(objects)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {WIDTH} {HEIGHT}] "
            "/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
            f"/Contents {content_ref} 0 R >>"
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


def _font(name: str) -> str:
    encoding = "/Encoding /WinAnsiEncoding"
    return f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} {encoding} >>"
