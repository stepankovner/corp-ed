"""Документы .doc (Word 97–2003) для тестов, собранные в коде — без бинарников.

Минимальная, но честная по MS-DOC / MS-CFB сборка: контейнер OLE (сектор
512 байт; мелкие потоки — в мини-потоке), FIB, текст кусками (Unicode или
однобайтовые), страницы FKP со свойствами абзацев и символов, таблица
стилей STSH, таблица кусков CLX. Разбор проверяется и на файлах, которые
сохранил сам Word (`sandbox/formats`, замер Р-5).
"""

import struct
from dataclasses import dataclass, field

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_FREE, _END, _FAT, _NO = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF
SECTOR = 512

FOOTNOTE = "\x02"
"""Ссылка на сноску в тексте абзаца: k-я ссылка — k-я строка `footnotes`."""
ENDNOTE = ""
"""Ссылка на концевую сноску; в поток пишется тем же знаком \\x02."""
SUP, END_SUP = "", ""
"""Начало и конец верхнего индекса в тексте абзаца (sprmCIss = 1); в поток
сами знаки не пишутся."""


@dataclass
class Row:
    """Описание строки таблицы для sprmTDefTable."""

    bounds: list[int]
    """Границы ячеек (rgdxaCenter), на одну больше, чем ячеек."""
    flags: list[int] = field(default_factory=list)
    """tcgrf ячеек: 0x0003 — влита в предыдущую, 0x0060 — начало
    объединения по вертикали, 0x0020 — продолжение."""


@dataclass
class Para:
    text: str
    istd: int = 0
    cell_end: bool = False
    """Конец ячейки таблицы (знак \\x07 вместо \\r)."""
    ttp: bool = False
    """Конец строки таблицы."""
    row: Row | None = None
    in_table: bool = False
    bold: bool = False
    outline: int | None = None
    compressed: bool = False
    """Однобайтовый кусок (cp1252) вместо Unicode."""
    raw_props: bool = True
    """False — без свойств абзаца (проверка запасного пути)."""
    deleted: bool = False
    """Текст удалён в режиме исправлений (sprmCFRMarkDel)."""
    huge: bool = False
    """Свойства абзаца — в потоке Data (sprmPHugePapx), как у Word для
    конца строки широкой таблицы."""


@dataclass
class Style:
    istd: int
    sti: int
    name: str
    outline: int | None = None
    base: int = 0x0FFF
    bold: bool = False


def _sprm(code: int, operand: bytes) -> bytes:
    return struct.pack("<H", code) + operand


def _papx_grpprl(para: Para) -> bytes:
    out = b""
    if para.in_table or para.cell_end or para.ttp:
        out += _sprm(0x2416, b"\x01")
    if para.ttp:
        out += _sprm(0x2417, b"\x01")
        if para.row is not None:
            count = len(para.row.bounds) - 1
            flags = para.row.flags or [0] * count
            data = bytes([count]) + struct.pack(f"<{count + 1}h", *para.row.bounds)
            data += b"".join(struct.pack("<HH", f, 0) + b"\0" * 16 for f in flags)
            out += _sprm(0xD608, struct.pack("<H", len(data) + 1) + data)
    if para.outline is not None:
        out += _sprm(0x2640, bytes([para.outline]))
    return out


def _papx_page(runs: list[tuple[int, int, bytes]]) -> bytes:
    page = bytearray(SECTOR)
    count = len(runs)
    struct.pack_into(f"<{count + 1}I", page, 0, *[r[0] for r in runs], runs[-1][1])
    top = 511
    for i, (_, _, data) in enumerate(runs):
        if not data:
            continue
        blob = (
            bytes([(len(data) + 1) // 2]) + data
            if len(data) % 2
            else bytes([0, len(data) // 2]) + data
        )
        top -= len(blob)
        top -= top % 2
        page[top : top + len(blob)] = blob
        page[4 * (count + 1) + 13 * i] = top // 2
    assert top > 4 * (count + 1) + 13 * count, "FKP переполнена"
    page[511] = count
    return bytes(page)


def _chpx_page(runs: list[tuple[int, int, bytes]]) -> bytes:
    page = bytearray(SECTOR)
    count = len(runs)
    struct.pack_into(f"<{count + 1}I", page, 0, *[r[0] for r in runs], runs[-1][1])
    top = 511
    for i, (_, _, grpprl) in enumerate(runs):
        if not grpprl:
            continue
        blob = bytes([len(grpprl)]) + grpprl
        top -= len(blob)
        top -= top % 2
        page[top : top + len(blob)] = blob
        page[4 * (count + 1) + i] = top // 2
    page[511] = count
    return bytes(page)


def _stsh(styles: list[Style]) -> bytes:
    by_istd = {s.istd: s for s in styles}
    count = max(by_istd, default=-1) + 1
    stshi = struct.pack("<HHHHHH", count, 10, 0, 0, 0, 0) + struct.pack("<HHH", 0, 0, 0)
    out = struct.pack("<H", len(stshi)) + stshi
    for istd in range(count):
        style = by_istd.get(istd)
        if style is None:
            out += struct.pack("<H", 0)
            continue
        base = struct.pack(
            "<HHHHH", style.sti & 0x0FFF, 1 | (style.base << 4), 2 | (0x0FFF << 4), 0, 0
        )
        xstz = (
            struct.pack("<H", len(style.name))
            + style.name.encode("utf-16-le")
            + b"\0\0"
        )
        papx = struct.pack("<H", istd) + (
            _sprm(0x2640, bytes([style.outline])) if style.outline is not None else b""
        )
        chpx = _sprm(0x0835, b"\x01") if style.bold else b""

        def lp(upx: bytes) -> bytes:
            return struct.pack("<H", len(upx)) + upx + (b"\0" if len(upx) % 2 else b"")

        std = base + xstz + lp(papx) + lp(chpx)
        out += struct.pack("<H", len(std)) + std
    return out


def _plc_bte(pages: list[tuple[int, int, int]]) -> bytes:
    """(первый fc, последний fc, номер страницы) → PlcBte."""
    fcs = [p[0] for p in pages] + [pages[-1][1]]
    return struct.pack(f"<{len(fcs)}I", *fcs) + struct.pack(
        f"<{len(pages)}I", *[p[2] for p in pages]
    )


def word_streams(
    paragraphs: list[Para],
    *,
    styles: list[Style] | None = None,
    footnotes: list[str] | None = None,
    endnotes: list[str] | None = None,
    note_tables: bool = True,
    nfib: int = 0x00C1,
    flags: int = 0x1200,
) -> dict[str, bytes]:
    """Потоки WordDocument и 1Table.

    Сноски — как у Word: история сносок после основного текста (каждая
    начинается знаком \\x02, в конце истории — лишний знак абзаца), позиции
    ссылок — PlcffndRef, границы текстов — PlcffndTxt (у концевых —
    PlcfendRef, PlcfendTxt). Знак ссылки в тексте — не полужирный, как
    стиль «Знак сноски». note_tables=False — без этих таблиц. Текст между
    SUP и END_SUP — верхний индекс прямым форматированием (как Ctrl+Shift+=).
    """
    word = bytearray(1024)
    pieces: list[tuple[int, int, int]] = []
    cp = 0
    para_fc: list[tuple[int, int, Para]] = []
    refs: dict[str, list[int]] = {FOOTNOTE: [], ENDNOTE: []}

    def add(text: str, compressed: bool) -> tuple[int, int]:
        nonlocal cp
        fc = len(word)
        raw = (
            text.encode("cp1252")
            if compressed
            else text.encode("utf-16-le", "surrogatepass")
        )
        word.extend(raw)
        # CP считается в единицах UTF-16: эмодзи — две.
        units = len(raw) if compressed else len(raw) // 2
        pieces.append((cp, cp + units, (fc * 2) | 0x40000000 if compressed else fc))
        cp += units
        return fc, len(word)

    # Особые символы абзаца: (начало FC, конец FC, вид) — "ref" или "sup".
    special: list[tuple[int, int, str]] = []
    for para in paragraphs:
        mark = "\x07" if para.cell_end or para.ttp else "\r"
        kinds: list[tuple[str, str]] = []
        raised = False
        for char in para.text + mark:
            if char in (SUP, END_SUP):
                raised = char == SUP
                continue
            kind = "ref" if char in refs else "sup" if raised else ""
            kinds.append((char, kind))
        stored = "".join(char for char, _ in kinds).replace(ENDNOTE, FOOTNOTE)
        para_cp = cp
        start, end = add(stored, para.compressed)
        units = 0
        for (char, kind), unit in zip(kinds, stored, strict=True):
            size = (
                len(unit.encode("cp1252"))
                if para.compressed
                else len(unit.encode("utf-16-le", "surrogatepass")) // 2
            )
            fc = start + (units if para.compressed else 2 * units)
            if kind == "ref":
                refs[char].append(para_cp + units)
            if kind:
                special.append((fc, fc + (size if para.compressed else 2 * size), kind))
            units += size
        para_fc.append((start, end, para))
    ccp_text = cp

    def story(notes: list[str]) -> tuple[int, list[int]]:
        story_start, bounds = cp, []
        for note in notes:
            bounds.append(cp - story_start)
            add("\x02 " + note + "\r", False)
        if notes:
            bounds += [cp - story_start, cp - story_start + 3]
            add("\r", False)
        return cp - story_start, bounds

    ccp_ftn, footnote_bounds = story(footnotes or [])
    ccp_edn, endnote_bounds = story(endnotes or [])

    def pages_for(
        runs: list[tuple[int, int, bytes]], build: object
    ) -> list[tuple[int, int, int]]:
        result = []
        for i in range(0, len(runs), 8):
            chunk = runs[i : i + 8]
            if len(word) % SECTOR:
                word.extend(b"\0" * (SECTOR - len(word) % SECTOR))
            number = len(word) // SECTOR
            word.extend(build(chunk))  # type: ignore[operator]
            result.append((chunk[0][0], chunk[-1][1], number))
        return result

    data_stream = bytearray(b"\0" * 16)

    def papx_data(para: Para) -> bytes:
        if not para.raw_props:
            return b""
        grpprl = _papx_grpprl(para)
        if para.huge:
            offset = len(data_stream)
            data_stream.extend(struct.pack("<h", len(grpprl)) + grpprl)
            grpprl = _sprm(0x6646, struct.pack("<I", offset))
        return struct.pack("<H", para.istd) + grpprl

    papx_runs = [(s, e, papx_data(p)) for s, e, p in para_fc]
    chpx_runs: list[tuple[int, int, bytes]] = []
    for s, e, p in para_fc:
        deleted = _sprm(0x0800, b"\x01") if p.deleted else b""
        props = (_sprm(0x0835, b"\x01") if p.bold else b"") + deleted
        by_kind = {"ref": deleted, "sup": props + _sprm(0x2A48, b"\x01")}
        cursor = s
        for run_start, run_end, kind in [r for r in special if s <= r[0] < e]:
            if cursor < run_start:
                chpx_runs.append((cursor, run_start, props))
            chpx_runs.append((run_start, run_end, by_kind[kind]))
            cursor = run_end
        if cursor < e:
            chpx_runs.append((cursor, e, props))
    papx_pages = pages_for(papx_runs, _papx_page)
    chpx_pages = pages_for(chpx_runs, _chpx_page)

    table = bytearray()
    stsh = _stsh(styles or [])
    stsh_fc = len(table)
    table.extend(stsh)
    papx_plc = _plc_bte(papx_pages)
    papx_fc = len(table)
    table.extend(papx_plc)
    chpx_plc = _plc_bte(chpx_pages)
    chpx_fc = len(table)
    table.extend(chpx_plc)
    cps = [p[0] for p in pieces] + [pieces[-1][1]]
    plc = struct.pack(f"<{len(cps)}I", *cps) + b"".join(
        struct.pack("<HIH", 0, fc_raw, 0) for _, _, fc_raw in pieces
    )
    clx = b"\x02" + struct.pack("<I", len(plc)) + plc
    clx_fc = len(table)
    table.extend(clx)
    note_pairs: dict[int, tuple[int, int]] = {}
    for (ref_index, text_index), positions, bounds in (
        ((2, 3), refs[FOOTNOTE], footnote_bounds),
        ((46, 47), refs[ENDNOTE], endnote_bounds),
    ):
        if not note_tables or not positions:
            continue
        ref_plc = struct.pack(f"<{len(positions) + 1}I", *positions, cp) + struct.pack(
            f"<{len(positions)}h", *range(1, len(positions) + 1)
        )
        note_pairs[ref_index] = (len(table), len(ref_plc))
        table.extend(ref_plc)
        text_plc = struct.pack(f"<{len(bounds)}I", *bounds)
        note_pairs[text_index] = (len(table), len(text_plc))
        table.extend(text_plc)

    fib = bytearray(900)
    struct.pack_into("<HH", fib, 0, 0xA5EC, nfib)
    struct.pack_into("<H", fib, 0x0A, flags)
    struct.pack_into("<H", fib, 0x20, 14)
    struct.pack_into("<H", fib, 0x3E, 22)
    longs = [0] * 22
    longs[0], longs[3], longs[4], longs[8] = len(word), ccp_text, ccp_ftn, ccp_edn
    struct.pack_into("<22i", fib, 0x40, *longs)
    struct.pack_into("<H", fib, 0x98, 93)
    pairs = {
        1: (stsh_fc, len(stsh)),
        12: (chpx_fc, len(chpx_plc)),
        13: (papx_fc, len(papx_plc)),
        33: (clx_fc, len(clx)),
        **note_pairs,
    }
    for index, (fc, lcb) in pairs.items():
        struct.pack_into("<II", fib, 0x9A + 8 * index, fc, lcb)
    word[0:900] = fib
    if len(word) < 4096:
        word.extend(b"\0" * (4096 - len(word)))
    streams = {"WordDocument": bytes(word), "1Table": bytes(table)}
    if len(data_stream) > 16:
        streams["Data"] = bytes(data_stream)
    return streams


def cfb(streams: dict[str, bytes], *, fat_loop: bool = False) -> bytes:
    """Контейнер OLE v3: потоки меньше 4096 байт — в мини-потоке."""
    names = list(streams)
    mini_stream = bytearray()
    minifat: list[int] = []
    starts: dict[str, int] = {}
    for name in names:
        data = streams[name]
        if len(data) < 4096:
            count = max(1, (len(data) + 63) // 64)
            first = len(minifat)
            starts[name] = first
            minifat.extend(list(range(first + 1, first + count)) + [_END])
            mini_stream.extend(data.ljust(count * 64, b"\0"))

    chains: list[tuple[str, bytes]] = []
    entries_count = len(names) + 1
    directory_size = ((entries_count * 128 + SECTOR - 1) // SECTOR) * SECTOR
    chains.append(("dir", b"\0" * directory_size))
    if minifat:
        chains.append(("minifat", struct.pack(f"<{len(minifat)}I", *minifat)))
        chains.append(("ministream", bytes(mini_stream)))
    for name in names:
        if name not in starts:
            chains.append((name, streams[name]))

    sector_counts = [max(1, (len(data) + SECTOR - 1) // SECTOR) for _, data in chains]
    total = sum(sector_counts)
    fat_sectors = 1
    while fat_sectors * (SECTOR // 4) < total + fat_sectors:
        fat_sectors += 1
    fat = [_FREE] * (fat_sectors * (SECTOR // 4))
    for i in range(fat_sectors):
        fat[i] = _FAT
    first: dict[str, int] = {}
    body = bytearray()
    sector = fat_sectors
    for (key, data), count in zip(chains, sector_counts, strict=True):
        first[key] = sector
        for k in range(count):
            fat[sector + k] = sector + k + 1 if k < count - 1 else _END
        if fat_loop and key == "dir":
            fat[sector + count - 1] = sector  # цепочка сама на себя
        body.extend(data.ljust(count * SECTOR, b"\0"))
        sector += count

    directory = bytearray(directory_size)

    def entry(
        index: int, name: str, kind: int, start: int, size: int, child: int, right: int
    ) -> None:
        raw = bytearray(128)
        encoded = (name + "\0").encode("utf-16-le")
        raw[: len(encoded)] = encoded
        struct.pack_into("<H", raw, 0x40, len(encoded))
        raw[0x42], raw[0x43] = kind, 1
        struct.pack_into("<III", raw, 0x44, _NO, right, child)
        struct.pack_into("<I", raw, 0x74, start)
        struct.pack_into("<Q", raw, 0x78, size)
        directory[index * 128 : index * 128 + 128] = raw

    entry(0, "Root Entry", 5, first.get("ministream", _END), len(mini_stream), 1, _NO)
    for i, name in enumerate(names, start=1):
        start = starts[name] if name in starts else first[name]
        entry(
            i, name, 2, start, len(streams[name]), _NO, i + 1 if i < len(names) else _NO
        )
    body[: len(directory)] = directory  # каталог — первая цепочка после FAT

    header = bytearray(SECTOR)
    header[:8] = _OLE
    struct.pack_into("<HHHHH", header, 0x18, 0x003E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<II", header, 0x2C, fat_sectors, first["dir"])
    struct.pack_into(
        "<IIIII",
        header,
        0x38,
        4096,
        first.get("minifat", _END),
        1 if minifat else 0,
        _END,
        0,
    )
    difat = [i for i in range(fat_sectors)] + [_FREE] * (109 - fat_sectors)
    struct.pack_into("<109I", header, 0x4C, *difat)
    fat_bytes = struct.pack(f"<{len(fat)}I", *fat)
    return bytes(header) + fat_bytes + bytes(body)


def doc(paragraphs: list[Para], **options: object) -> bytes:
    return cfb(word_streams(paragraphs, **options))  # type: ignore[arg-type]
