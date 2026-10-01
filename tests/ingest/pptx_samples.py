# ruff: noqa: E501 — URI пространств имён OOXML длиннее строки.
"""Презентации .pptx для тестов, собранные в коде — без бинарников в git.

Слайд — список фигур (строки XML от функций ниже), заметки, диаграммы и
SmartArt — отдельными частями пакета со связями, как у PowerPoint.
"""

import io
import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
DGM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = f'xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"'


@dataclass
class ChartSpec:
    title: str
    categories: list[str]
    series: list[tuple[str, list[float]]]
    format_code: str = "General"


@dataclass
class SlideSpec:
    shapes: list[str]
    notes: str = ""
    hidden: bool = False
    charts: list[ChartSpec] = field(default_factory=list)
    """Диаграмма i — связь rIdC{i}, фигура — chart_frame(i)."""
    diagrams: list[list[str]] = field(default_factory=list)
    """SmartArt i — тексты узлов, связь rIdD{i}, фигура — diagram_frame(i)."""


def _run(text: str) -> str:
    return f"<a:r><a:t>{escape(text)}</a:t></a:r>"


def paragraph(text: str, *, level: int = 0, bullet: bool = False) -> str:
    props = ""
    if level or bullet:
        inner = '<a:buChar char="•"/>' if bullet else ""
        props = f'<a:pPr lvl="{level}">{inner}</a:pPr>'
    runs = "<a:br/>".join(_run(part) for part in text.split("\n"))
    return f"<a:p>{props}{runs}</a:p>"


def _xfrm(pos: tuple[int, int] | None) -> str:
    if pos is None:
        return ""
    x, y = pos
    return f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="100" cy="100"/></a:xfrm>'


def _frame_xfrm(pos: tuple[int, int] | None) -> str:
    """У graphicFrame координаты — в p:xfrm, а не в spPr/a:xfrm."""
    if pos is None:
        return "<p:xfrm/>"
    x, y = pos
    return f'<p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="100" cy="100"/></p:xfrm>'


def text_shape(
    paragraphs: list[str], *, ph: str | None = None, pos: tuple[int, int] | None = None
) -> str:
    placeholder = f'<p:ph type="{ph}"/>' if ph else ""
    return (
        '<p:sp><p:nvSpPr><p:cNvPr id="1" name="s"/><p:cNvSpPr/>'
        f"<p:nvPr>{placeholder}</p:nvPr></p:nvSpPr><p:spPr>{_xfrm(pos)}</p:spPr>"
        f"<p:txBody><a:bodyPr/>{''.join(paragraphs)}</p:txBody></p:sp>"
    )


def title(text: str) -> str:
    return text_shape([paragraph(text)], ph="title")


def table(
    rows: list[list[str | dict[str, str]]], pos: tuple[int, int] | None = None
) -> str:
    """Ячейка — текст или {"text": …, "gridSpan"/"rowSpan"/"hMerge"/"vMerge": …}."""
    body = ""
    for row in rows:
        cells = ""
        for cell in row:
            spec = {"text": cell} if isinstance(cell, str) else dict(cell)
            text = spec.pop("text", "")
            attrs = "".join(f' {k}="{v}"' for k, v in spec.items())
            cells += (
                f"<a:tc{attrs}><a:txBody><a:bodyPr/>{paragraph(text)}</a:txBody></a:tc>"
            )
        body += f'<a:tr h="100">{cells}</a:tr>'
    return (
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="3" name="t"/>'
        f"<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>{_frame_xfrm(pos)}"
        f'<a:graphic><a:graphicData uri="{A}/table"><a:tbl><a:tblPr firstRow="1"/>'
        f"<a:tblGrid/>{body}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )


def chart_frame(index: int) -> str:
    return (
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="4" name="c"/>'
        "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm/>"
        f'<a:graphic><a:graphicData uri="{C}"><c:chart xmlns:c="{C}" r:id="rIdC{index}"/>'
        "</a:graphicData></a:graphic></p:graphicFrame>"
    )


def diagram_frame(index: int) -> str:
    return (
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="5" name="d"/>'
        "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm/>"
        f'<a:graphic><a:graphicData uri="{DGM}"><dgm:relIds xmlns:dgm="{DGM}" '
        f'r:dm="rIdD{index}" r:lo="x" r:qs="x" r:cs="x"/></a:graphicData></a:graphic>'
        "</p:graphicFrame>"
    )


def group(shapes: list[str], pos: tuple[int, int] | None = None) -> str:
    return (
        '<p:grpSp><p:nvGrpSpPr><p:cNvPr id="6" name="g"/><p:cNvGrpSpPr/><p:nvPr/>'
        f"</p:nvGrpSpPr><p:grpSpPr>{_xfrm(pos)}</p:grpSpPr>{''.join(shapes)}</p:grpSp>"
    )


def _slide_xml(spec: SlideSpec) -> str:
    show = ' show="0"' if spec.hidden else ""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><p:sld {NS}{show}><p:cSld><p:spTree>'
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        f"<p:grpSpPr/>{''.join(spec.shapes)}</p:spTree></p:cSld></p:sld>"
    )


def _notes_xml(text: str) -> str:
    shapes = (
        text_shape([paragraph("картинка слайда")], ph="sldImg")
        + text_shape([paragraph(line) for line in text.split("\n")], ph="body")
        + text_shape([paragraph("7")], ph="sldNum")
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><p:notes {NS}><p:cSld><p:spTree>'
        f"{shapes}</p:spTree></p:cSld></p:notes>"
    )


def _chart_xml(spec: ChartSpec) -> str:
    def cache(values: list[str], kind: str, code: str = "") -> str:
        fmt = f"<c:formatCode>{code}</c:formatCode>" if kind == "numCache" else ""
        points = "".join(
            f'<c:pt idx="{i}"><c:v>{escape(v)}</c:v></c:pt>'
            for i, v in enumerate(values)
        )
        return f'<c:{kind}>{fmt}<c:ptCount val="{len(values)}"/>{points}</c:{kind}>'

    series = ""
    for n, (name, values) in enumerate(spec.series):
        series += (
            f'<c:ser><c:idx val="{n}"/><c:tx><c:strRef><c:f>x</c:f>{cache([name], "strCache")}'
            f"</c:strRef></c:tx><c:cat><c:strRef><c:f>x</c:f>{cache(spec.categories, 'strCache')}"
            f"</c:strRef></c:cat><c:val><c:numRef><c:f>x</c:f>"
            f"{cache([repr(v) for v in values], 'numCache', spec.format_code)}</c:numRef></c:val></c:ser>"
        )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><c:chartSpace xmlns:c="{C}" xmlns:a="{A}">'
        f"<c:chart><c:title><c:tx><c:rich><a:p>{_run(spec.title)}</a:p></c:rich></c:tx></c:title>"
        f"<c:plotArea><c:barChart>{series}</c:barChart></c:plotArea></c:chart></c:chartSpace>"
    )


def _diagram_xml(nodes: list[str]) -> str:
    doc = _run("документ")
    points = f'<dgm:pt modelId="0" type="doc"><dgm:t><a:p>{doc}</a:p></dgm:t></dgm:pt>'
    for n, text in enumerate(nodes, start=1):
        points += (
            f'<dgm:pt modelId="{n}"><dgm:t><a:bodyPr/><a:p>{_run(text)}</a:p></dgm:t></dgm:pt>'
            f'<dgm:pt modelId="{100 + n}" type="parTrans"><dgm:t><a:p>{_run("связь")}</a:p></dgm:t></dgm:pt>'
        )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><dgm:dataModel xmlns:dgm="{DGM}" xmlns:a="{A}">'
        f"<dgm:ptLst>{points}</dgm:ptLst></dgm:dataModel>"
    )


def _rels(items: list[tuple[str, str, str]]) -> str:
    body = "".join(
        f'<Relationship Id="{rid}" Type="{R}/{kind}" Target="{target}"/>'
        for rid, kind, target in items
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{PKG}">{body}</Relationships>'


def pptx(slides: list[SlideSpec], *, order: list[int] | None = None) -> bytes:
    """order — порядок показа (индексы slides); по умолчанию как в списке."""
    order = order if order is not None else list(range(len(slides)))
    buffer = io.BytesIO()
    overrides = '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "_rels/.rels", _rels([("rId1", "officeDocument", "ppt/presentation.xml")])
        )
        ids = "".join(f'<p:sldId id="{256 + i}" r:id="rId{i + 1}"/>' for i in order)
        archive.writestr(
            "ppt/presentation.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><p:presentation {NS}>'
            f"<p:sldIdLst>{ids}</p:sldIdLst></p:presentation>",
        )
        archive.writestr(
            "ppt/_rels/presentation.xml.rels",
            _rels(
                [
                    (f"rId{i + 1}", "slide", f"slides/slide{i + 1}.xml")
                    for i in range(len(slides))
                ]
            ),
        )
        chart_no = diagram_no = 0
        for i, spec in enumerate(slides, start=1):
            archive.writestr(f"ppt/slides/slide{i}.xml", _slide_xml(spec))
            links: list[tuple[str, str, str]] = []
            if spec.notes:
                archive.writestr(
                    f"ppt/notesSlides/notesSlide{i}.xml", _notes_xml(spec.notes)
                )
                links.append(
                    ("rIdN", "notesSlide", f"../notesSlides/notesSlide{i}.xml")
                )
            for n, chart in enumerate(spec.charts):
                chart_no += 1
                archive.writestr(f"ppt/charts/chart{chart_no}.xml", _chart_xml(chart))
                links.append((f"rIdC{n}", "chart", f"../charts/chart{chart_no}.xml"))
            for n, nodes in enumerate(spec.diagrams):
                diagram_no += 1
                archive.writestr(
                    f"ppt/diagrams/data{diagram_no}.xml", _diagram_xml(nodes)
                )
                links.append(
                    (f"rIdD{n}", "diagramData", f"../diagrams/data{diagram_no}.xml")
                )
            if links:
                archive.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", _rels(links))
            overrides += f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            f'<Default Extension="xml" ContentType="application/xml"/>{overrides}</Types>',
        )
    return buffer.getvalue()
