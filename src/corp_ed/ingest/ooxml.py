"""Общее для файлов Office Open XML (.xlsx, .pptx): пакет, связи, разбор XML.

.xlsx и .pptx — zip с XML (ECMA-376). Читаем их `zipfile` и потоковым
`iterparse` из defusedxml с `forbid_dtd=True`. DTD Office не пишет, поэтому
любое объявление `<!DOCTYPE` (и `<!ENTITY` в нём) отклоняет сам разборщик —
в любом месте пролога и в любой кодировке части (UTF-8, UTF-16), — и
часть считается повреждённой. Это не поиск байтов в начале части:
комментарий перед DTD или часть в UTF-16 такой поиск не видел.
Внешние сущности не загружаются; expat ≥ 2.4.1 вдобавок защищён от
«billion laughs» (проверяется тестом).

Ошибки — `OfficeFileError(code)` с кодами `ExtractionError` бэкенда:
`format_mismatch`, `encrypted`, `corrupted`, `archive_too_large`,
`document_too_large`.
"""

import io
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from posixpath import basename, dirname, join, normpath
from typing import Literal
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException  # type: ignore[import-untyped]
from defusedxml.ElementTree import iterparse  # type: ignore[import-untyped]

MAX_UNCOMPRESSED = 200 * 1024 * 1024
MAX_ENTRIES = 5000
MAX_COMPRESSION_RATIO = 200
"""Как у docx в `extract.py`: zip-бомба — 40 КБ, которые распаковываются
в гигабайты."""

Event = Literal["start", "end"]

PackageKind = Literal["spreadsheet", "presentation"]
_CONTENT_TYPE_MARKERS: dict[PackageKind, tuple[str, ...]] = {
    "spreadsheet": ("spreadsheetml", "ms-excel"),
    "presentation": ("presentationml", "ms-powerpoint"),
}
_DEFAULT_MAIN_PART: dict[PackageKind, str] = {
    "spreadsheet": "xl/workbook.xml",
    "presentation": "ppt/presentation.xml",
}

READ_ERRORS = (
    zipfile.BadZipFile,
    zlib.error,
    EOFError,
    ParseError,
    ValueError,
    KeyError,
    IndexError,
)
"""Что при разборе значит «файл повреждён» (→ `corrupted`)."""


class OfficeFileError(Exception):
    """Файл нельзя принять. code — те же коды, что у `ExtractionError`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Relation:
    type: str
    id: str
    target: str
    """Путь цели внутри архива."""


def check_package(data: bytes, kind: PackageKind) -> None:
    """Дешёвые проверки до разбора: сигнатура, пароль, zip-бомба, тип.

    Тип — по `[Content_Types].xml` главной части пакета: .docx или .pptx,
    переименованный в .xlsx, — `format_mismatch`, а не пустой документ.
    """
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        # OLE2: либо файл с паролем (EncryptedPackage), либо старый .xls/.ppt.
        if "EncryptedPackage".encode("utf-16-le") in data:
            raise OfficeFileError("encrypted")
        raise OfficeFileError("format_mismatch")
    if not data.startswith(b"PK\x03\x04"):
        raise OfficeFileError("format_mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            # Сначала размеры: связи читаем уже из проверенного архива.
            if len(entries) > MAX_ENTRIES:
                raise OfficeFileError("archive_too_large")
            total = 0
            for entry in entries:
                total += entry.file_size
                ratio = entry.file_size / max(entry.compress_size, 1)
                if total > MAX_UNCOMPRESSED or ratio > MAX_COMPRESSION_RATIO:
                    raise OfficeFileError("archive_too_large")

            names = {entry.filename for entry in entries}
            if "[Content_Types].xml" not in names:
                raise OfficeFileError("format_mismatch")
            part = main_part(archive, names, kind)
            content_type = _content_types(archive).get("/" + part, "")
            if part not in names or not any(
                marker in content_type for marker in _CONTENT_TYPE_MARKERS[kind]
            ):
                # zip, но другой документ Office или просто архив.
                raise OfficeFileError("format_mismatch")
    except (zipfile.BadZipFile, zlib.error, EOFError, ParseError, ValueError) as exc:
        raise OfficeFileError("corrupted") from exc


def open_archive(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


def local(tag: str) -> str:
    """Имя без пространства имён: Strict OOXML и Transitional — одно и то же."""
    return tag.rsplit("}", 1)[-1]


def attr(element: Element, name: str) -> str | None:
    """Атрибут по локальному имени (r:id в любом пространстве имён)."""
    for key, value in element.attrib.items():
        if local(key) == name:
            return value
    return None


def children(element: Element, name: str) -> list[Element]:
    return [child for child in element if local(child.tag) == name]


def child(element: Element, name: str) -> Element | None:
    for item in element:
        if local(item.tag) == name:
            return item
    return None


def parse(
    archive: zipfile.ZipFile, part: str, events: tuple[Event, ...] = ("end",)
) -> Iterator[tuple[str, Element]]:
    """Часть пакета потоком; с DTD (`<!DOCTYPE`, `<!ENTITY`) — `corrupted`.

    DTD отклоняет разборщик (docstring модуля), а не поиск байтов."""
    with archive.open(part) as stream:
        try:
            yield from iterparse(stream, events=events, forbid_dtd=True)
        except DefusedXmlException as exc:
            raise OfficeFileError("corrupted") from exc


def parse_tree(archive: zipfile.ZipFile, part: str) -> Element:
    """Небольшая часть (слайд, диаграмма) целиком — корень дерева."""
    root: Element | None = None
    for event, element in parse(archive, part, ("start", "end")):
        if event == "start" and root is None:
            root = element
    if root is None:
        raise OfficeFileError("corrupted")
    return root


def main_part(archive: zipfile.ZipFile, names: set[str], kind: PackageKind) -> str:
    for relation in relations(archive, "", names):
        if relation.type.endswith("/officeDocument"):
            return relation.target
    return _DEFAULT_MAIN_PART[kind]


def relations(archive: zipfile.ZipFile, part: str, names: set[str]) -> list[Relation]:
    """Связи части (`""` — корень пакета); внешние ссылки пропускаются."""
    rels_part = join(dirname(part), "_rels", basename(part) + ".rels")
    if rels_part not in names:
        return []
    found: list[Relation] = []
    for _, element in parse(archive, rels_part):
        if local(element.tag) != "Relationship":
            continue
        if element.get("TargetMode") == "External":
            continue
        target = element.get("Target", "")
        if target.startswith("/"):
            path = target.lstrip("/")
        else:
            path = normpath(join(dirname(part), target))
        found.append(Relation(element.get("Type", ""), element.get("Id", ""), path))
    return found


def first_target(found: list[Relation], type_suffix: str) -> str:
    for relation in found:
        if relation.type.endswith(type_suffix):
            return relation.target
    return ""


def _content_types(archive: zipfile.ZipFile) -> dict[str, str]:
    """PartName → ContentType из `[Content_Types].xml` (только Override)."""
    types: dict[str, str] = {}
    for _, element in parse(archive, "[Content_Types].xml"):
        if local(element.tag) == "Override":
            types[element.get("PartName", "")] = element.get("ContentType", "")
    return types


def rel_id(element: Element) -> str:
    """`r:id` (и `r:dm`/`r:embed` и т. п. не путать) — атрибут `id` в
    пространстве имён связей. У `p:sldId` есть ещё простой `id`, числовой."""
    for key, value in element.attrib.items():
        if key.startswith("{") and local(key) == "id":
            return value
    return ""
