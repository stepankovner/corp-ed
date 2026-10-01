"""Документы Word 97–2003 (.doc) → Markdown (Р-5: третий из новых форматов).

Решение Артёма 29.09 (Р-5): форматы по одному; .xlsx и .pptx приняты
01.10, этот — следующий. Здесь только разбор файла; приём формата —
бэкенд (BH-35 в `docs/backend-handoff.md`). Сторонних библиотек нет:
.doc — двоичный файл в контейнере OLE (MS-CFB), текст и разметка — по
спецификации MS-DOC.

Что берётся из файла:

1. Текст основного документа — по таблице кусков (CLX: Unicode или
   однобайтовые куски). Сноски и надписи — в конце, отдельными абзацами;
   колонтитулы и примечания рецензентов — нет.
2. Режим исправлений: удалённый рецензентом текст (он остаётся в файле с
   пометкой) выбрасывается. Поля: от поля остаётся результат (текст
   ссылки, номер), код
   (`HYPERLINK …`) выбрасывается; оглавление (`TOC`) — целиком: в нём
   названия всех разделов, такой фрагмент вытесняет из выдачи ответы.
3. Заголовки — по стилю абзаца: встроенные «Заголовок 1–9», «Название»,
   уровень структуры в стиле или в самом абзаце; имя стиля «Heading N» /
   «Заголовок N». Абзац, целиком набранный полужирным, — `**…**`:
   `preprocess` сделает из него заголовок, если других заголовков нет
   (как у .docx).
4. Таблицы — по свойствам абзацев (ячейка, конец строки) и описанию
   строки (`sprmTDefTable`): ширины ячеек дают объединение по
   горизонтали, флаги — по вертикали. У широкой таблицы свойства конца
   строки не влезают в FKP — Word кладёт их в поток Data
   (`sprmPHugePapx`). Дальше — правила листа Excel
   (`xlsx.table_lines`): шапка в две строки, группы, «ключ: значение».
   Вложенная таблица — текстом в ячейке внешней.

Файлы Word 6.0/95 — `unsupported_format` (пересохранить в .docx); с
паролем — `encrypted`; не документ Word (.xls, .ppt, RTF с расширением
.doc) — `format_mismatch`.
"""

import re
import struct
from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass, field

from corp_ed.ingest.ooxml import OfficeFileError
from corp_ed.ingest.xlsx import Sheet, table_lines

TITLE_MAX_CHARS = 150
"""Полужирный абзац длиннее — не заголовок."""

_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_FREE, _END, _NO_STREAM = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFF
_WORD_IDENT = 0xA5EC
_NFIB_WORD97 = 0xC1

# Пары (fc, lcb) в FibRgFcLcb97.
_FC_STSHF = 1
_FC_PLCF_BTE_CHPX = 12
_FC_PLCF_BTE_PAPX = 13
_FC_CLX = 33

_SPRM_P_IN_TABLE = 0x2416
_SPRM_P_TTP = 0x2417
_SPRM_P_ITAP = 0x6649
_SPRM_P_OUT_LVL = 0x2640
_SPRM_P_ISTD = 0x4600
_SPRM_P_CHG_TABS = 0xC615
_SPRM_T_DEF_TABLE = 0xD608
_SPRM_T_VERT_MERGE = 0xD62B
_SPRM_C_BOLD = 0x0835
_SPRM_P_HUGE_PAPX = 0x6646
_SPRM_C_R_MARK_DEL = 0x0800

_STI_TITLE = 62
_HEADING_NAME = re.compile(r"^(?:heading|заголовок)\s*([1-9])$", re.IGNORECASE)

_READ_ERRORS = (struct.error, IndexError, ValueError, KeyError, UnicodeDecodeError)


@dataclass
class DocText:
    blocks: list[str]
    """Куски Markdown: заголовки, абзацы, таблицы."""
    headings: int = 0
    tables: int = 0
    footnotes: int = 0


def doc_to_markdown(data: bytes) -> str:
    """Файл .doc → Markdown до preprocess. Вызывать в песочнице."""
    document = read_document(data)
    return "\n\n".join(document.blocks).strip() + "\n" if document.blocks else ""


def check_container(data: bytes) -> None:
    """Дешёвые проверки до разбора: контейнер, документ Word, версия, пароль."""
    try:
        cfb = _Cfb(data)
        if cfb.has_stream("EncryptedPackage"):
            raise OfficeFileError("encrypted")
        word = cfb.stream("WordDocument")
        _read_fib(word)
    except OfficeFileError:
        raise
    except _READ_ERRORS as exc:
        raise OfficeFileError("corrupted") from exc


def read_document(data: bytes) -> DocText:
    check_container(data)
    try:
        return _read(data)
    except OfficeFileError:
        raise
    except _READ_ERRORS as exc:
        raise OfficeFileError("corrupted") from exc


# --- Контейнер OLE (MS-CFB) --------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    name: str
    kind: int
    left: int
    right: int
    child: int
    start: int
    size: int


class _Cfb:
    """Чтение потоков из составного файла OLE; цепочки — с защитой от петель."""

    def __init__(self, data: bytes) -> None:
        if len(data) < 512 or not data.startswith(_OLE_SIGNATURE):
            raise OfficeFileError("format_mismatch")
        self.data = data
        major, _, shift, mini_shift = struct.unpack_from("<HHHH", data, 0x1A)
        if shift not in (9, 12) or mini_shift != 6:
            raise OfficeFileError("corrupted")
        self.sector_size = 1 << shift
        self.per_sector = self.sector_size // 4
        n_fat, first_dir = struct.unpack_from("<II", data, 0x2C)
        self.cutoff, first_minifat, _, first_difat, n_difat = struct.unpack_from(
            "<IIIII", data, 0x38
        )

        difat = list(struct.unpack_from("<109I", data, 0x4C))
        seen: set[int] = set()
        sector = first_difat
        for _ in range(n_difat):
            if sector in (_FREE, _END) or sector in seen:
                break
            seen.add(sector)
            entries = struct.unpack_from(f"<{self.per_sector}I", self._sector(sector))
            difat.extend(entries[:-1])
            sector = entries[-1]
        self.fat: list[int] = []
        for fat_sector in [s for s in difat if s not in (_FREE, _END)][:n_fat]:
            self.fat.extend(
                struct.unpack_from(f"<{self.per_sector}I", self._sector(fat_sector))
            )

        directory = b"".join(self._sector(s) for s in self._chain(first_dir, self.fat))
        self.entries = [
            self._entry(directory[i : i + 128], major)
            for i in range(0, len(directory) - 127, 128)
        ]
        if not self.entries:
            raise OfficeFileError("corrupted")
        root = self.entries[0]
        self.mini_stream = b"".join(
            self._sector(s) for s in self._chain(root.start, self.fat)
        )[: root.size]
        minifat_raw = b"".join(
            self._sector(s) for s in self._chain(first_minifat, self.fat)
        )
        self.minifat = list(
            struct.unpack_from(f"<{len(minifat_raw) // 4}I", minifat_raw)
        )
        self.top = {
            entry.name: entry for entry in self._children(0) if entry.kind in (1, 2)
        }

    def _sector(self, number: int) -> bytes:
        offset = (number + 1) * self.sector_size
        if offset + self.sector_size > len(self.data):
            raise OfficeFileError("corrupted")
        return self.data[offset : offset + self.sector_size]

    @staticmethod
    def _chain(start: int, table: list[int]) -> Iterator[int]:
        seen: set[int] = set()
        sector = start
        while sector not in (_END, _FREE):
            if sector >= len(table) or sector in seen:
                raise OfficeFileError("corrupted")
            seen.add(sector)
            yield sector
            sector = table[sector]

    @staticmethod
    def _entry(raw: bytes, major: int) -> _Entry:
        name_length = struct.unpack_from("<H", raw, 0x40)[0]
        name = raw[: max(0, min(name_length, 64) - 2)].decode("utf-16-le", "replace")
        left, right, child = struct.unpack_from("<III", raw, 0x44)
        start = struct.unpack_from("<I", raw, 0x74)[0]
        size = struct.unpack_from("<Q", raw, 0x78)[0]
        if major == 3:
            size &= 0xFFFFFFFF  # в версии 3 старшая половина — мусор
        return _Entry(name, raw[0x42], left, right, child, start, size)

    def _children(self, index: int) -> list[_Entry]:
        result: list[_Entry] = []
        stack = [self.entries[index].child]
        seen: set[int] = set()
        while stack:
            current = stack.pop()
            if current == _NO_STREAM or current in seen or current >= len(self.entries):
                continue
            seen.add(current)
            entry = self.entries[current]
            result.append(entry)
            stack.extend((entry.left, entry.right))
        return result

    def has_stream(self, name: str) -> bool:
        return name in self.top and self.top[name].kind == 2

    def stream(self, name: str) -> bytes:
        entry = self.top.get(name)
        if entry is None or entry.kind != 2:
            raise OfficeFileError("format_mismatch")
        if entry.size > len(self.data):
            raise OfficeFileError("corrupted")
        if entry.size < self.cutoff:
            mini = b"".join(
                self.mini_stream[s * 64 : s * 64 + 64]
                for s in self._chain(entry.start, self.minifat)
            )
            content = mini[: entry.size]
        else:
            content = b"".join(
                self._sector(s) for s in self._chain(entry.start, self.fat)
            )[: entry.size]
        if len(content) < entry.size:
            raise OfficeFileError("corrupted")
        return content


# --- FIB, куски текста, свойства ---------------------------------------------------


@dataclass(frozen=True)
class _Fib:
    table_stream: str
    ccp: tuple[int, ...]
    """ccpText, ccpFtn, ccpHdd, ccpMcr, ccpAtn, ccpEdn, ccpTxbx, ccpHdrTxbx."""
    pairs: tuple[tuple[int, int], ...]

    def pair(self, index: int) -> tuple[int, int]:
        return self.pairs[index] if index < len(self.pairs) else (0, 0)


def _read_fib(word: bytes) -> _Fib:
    if len(word) < 0x22:
        raise OfficeFileError("format_mismatch")
    ident, nfib = struct.unpack_from("<HH", word, 0)
    if ident != _WORD_IDENT:
        raise OfficeFileError("format_mismatch")
    if nfib < _NFIB_WORD97:
        raise OfficeFileError("unsupported_format")  # Word 6.0 / 95
    flags = struct.unpack_from("<H", word, 0x0A)[0]
    if flags & 0x0100 or flags & 0x8000:  # fEncrypted, fObfuscated
        raise OfficeFileError("encrypted")
    position = 0x20
    csw = struct.unpack_from("<H", word, position)[0]
    position += 2 + 2 * csw
    cslw = struct.unpack_from("<H", word, position)[0]
    position += 2
    longs = struct.unpack_from(f"<{cslw}i", word, position)
    position += 4 * cslw
    count = struct.unpack_from("<H", word, position)[0]
    position += 2
    pairs = tuple(
        struct.unpack_from("<II", word, position + 8 * i) for i in range(count)
    )
    if len(longs) < 11:
        raise OfficeFileError("corrupted")
    return _Fib(
        table_stream="1Table" if flags & 0x0200 else "0Table",
        ccp=tuple(max(0, value) for value in longs[3:11]),
        pairs=pairs,
    )


@dataclass(frozen=True)
class _Piece:
    cp_start: int
    cp_end: int
    fc: int
    compressed: bool


def _pieces(table: bytes, fib: _Fib) -> list[_Piece]:
    fc, lcb = fib.pair(_FC_CLX)
    clx = table[fc : fc + lcb]
    position = 0
    while position < len(clx) and clx[position] == 0x01:  # Prc — пропустить
        size = struct.unpack_from("<h", clx, position + 1)[0]
        position += 3 + max(size, 0)
    if position >= len(clx) or clx[position] != 0x02:
        raise OfficeFileError("corrupted")
    length = struct.unpack_from("<I", clx, position + 1)[0]
    plc = clx[position + 5 : position + 5 + length]
    count = (len(plc) - 4) // 12
    cps = struct.unpack_from(f"<{count + 1}I", plc, 0)
    pieces: list[_Piece] = []
    for i in range(count):
        _, fc_raw, _ = struct.unpack_from("<HIH", plc, 4 * (count + 1) + 8 * i)
        compressed = bool(fc_raw & 0x40000000)
        offset = fc_raw & 0x3FFFFFFF
        pieces.append(
            _Piece(
                cps[i], cps[i + 1], offset // 2 if compressed else offset, compressed
            )
        )
    return pieces


def _story(
    word: bytes, pieces: list[_Piece], cp_start: int, cp_end: int
) -> tuple[str, list[int]]:
    """Текст отрезка CP и позиция (FC) каждого символа в WordDocument."""
    parts: list[str] = []
    fcs: list[int] = []
    for piece in pieces:
        low, high = max(piece.cp_start, cp_start), min(piece.cp_end, cp_end)
        if low >= high:
            continue
        count = high - low
        if piece.compressed:
            start = piece.fc + (low - piece.cp_start)
            raw = word[start : start + count]
            text = raw.decode("cp1252", "replace")
            positions = range(start, start + count)
        else:
            start = piece.fc + 2 * (low - piece.cp_start)
            raw = word[start : start + 2 * count]
            # По единице UTF-16 на символ: суррогатная пара — два символа,
            # позиции не съезжают; склеивает пары `_clean`.
            text = "".join(map(chr, struct.unpack(f"<{len(raw) // 2}H", raw)))
            positions = range(start, start + 2 * count, 2)
        if len(text) != count:
            raise OfficeFileError("corrupted")
        parts.append(text)
        fcs.extend(positions)
    return "".join(parts), fcs


@dataclass(frozen=True)
class _Run:
    start: int
    end: int
    istd: int
    grpprl: bytes


def _fkp_runs(
    word: bytes, table: bytes, fib: _Fib, kind: str, data_stream: bytes = b""
) -> list[_Run]:
    """Свойства абзацев (PAPX) или символов (CHPX) по страницам FKP."""
    fc, lcb = fib.pair(_FC_PLCF_BTE_PAPX if kind == "papx" else _FC_PLCF_BTE_CHPX)
    if lcb < 8:
        return []
    plc = table[fc : fc + lcb]
    count = (len(plc) - 4) // 8
    pages = struct.unpack_from(f"<{count}I", plc, 4 * (count + 1))
    runs: list[_Run] = []
    for number in pages:
        offset = (number & 0x3FFFFF) * 512
        page = word[offset : offset + 512]
        if len(page) < 512:
            raise OfficeFileError("corrupted")
        crun = page[511]
        bounds = struct.unpack_from(f"<{crun + 1}I", page, 0)
        for i in range(crun):
            istd, grpprl = 0, b""
            if kind == "papx":
                where = 2 * page[4 * (crun + 1) + 13 * i]
                if where:
                    size = page[where]
                    data = (
                        page[where + 1 : where + 2 * size]
                        if size
                        else page[where + 2 : where + 2 + 2 * page[where + 1]]
                    )
                    if len(data) >= 2:
                        istd = struct.unpack_from("<H", data, 0)[0]
                        grpprl = _expand_huge_papx(data[2:], data_stream)
            else:
                where = 2 * page[4 * (crun + 1) + i]
                if where:
                    grpprl = page[where + 1 : where + 1 + page[where]]
            runs.append(_Run(bounds[i], bounds[i + 1], istd, grpprl))
    runs.sort(key=lambda run: run.start)
    return runs


def _expand_huge_papx(grpprl: bytes, data_stream: bytes) -> bytes:
    """sprmPHugePapx: свойства абзаца не влезли в FKP и лежат в потоке Data.

    Так Word хранит конец строки широкой таблицы: описание ячеек
    (sprmTDefTable) большое. В FKP остаётся смещение в Data, там —
    cbGrpprl (2 байта) и сами свойства.
    """
    result = b""
    for sprm, operand in _sprms(grpprl):
        if sprm == _SPRM_P_HUGE_PAPX and len(operand) == 4:
            offset = struct.unpack_from("<I", operand, 0)[0]
            if offset + 2 > len(data_stream):
                raise OfficeFileError("corrupted")
            size = struct.unpack_from("<h", data_stream, offset)[0]
            result += data_stream[offset + 2 : offset + 2 + max(size, 0)]
        else:
            result += struct.pack("<H", sprm) + operand
    return result


def _sprms(grpprl: bytes) -> Iterator[tuple[int, bytes]]:
    """Свойства (sprm, операнд); у переменных операнд — вместе с длиной."""
    position = 0
    while position + 2 <= len(grpprl):
        sprm = struct.unpack_from("<H", grpprl, position)[0]
        position += 2
        spra = sprm >> 13
        if spra in (0, 1):
            size = 1
        elif spra in (2, 4, 5):
            size = 2
        elif spra == 3:
            size = 4
        elif spra == 7:
            size = 3
        elif position >= len(grpprl):
            return
        elif sprm == _SPRM_T_DEF_TABLE:
            size = struct.unpack_from("<H", grpprl, position)[0] + 1
        elif sprm == _SPRM_P_CHG_TABS and grpprl[position] == 255:
            deleted = grpprl[position + 1]
            added = grpprl[position + 2 + 4 * deleted]
            size = 3 + 4 * deleted + 3 * added
        else:
            size = 1 + grpprl[position]
        yield sprm, grpprl[position : position + size]
        position += size


class _Lookup:
    """Свойства по позиции символа (FC): двоичный поиск по прогонам FKP."""

    def __init__(self, runs: list[_Run]) -> None:
        self.runs = runs
        self.starts = [run.start for run in runs]
        self.bold: list[int | None] = [_direct_bold(run.grpprl) for run in runs]
        self.deleted: list[bool] = [
            any(s == _SPRM_C_R_MARK_DEL and o[0] for s, o in _sprms(run.grpprl))
            for run in runs
        ]

    def index(self, fc: int) -> int | None:
        index = bisect_right(self.starts, fc) - 1
        if index >= 0 and fc < self.runs[index].end:
            return index
        return None

    def at(self, fc: int) -> _Run | None:
        index = self.index(fc)
        return self.runs[index] if index is not None else None


def _direct_bold(grpprl: bytes) -> int | None:
    """Операнд sprmCFBold прогона символов (0, 1, 0x80, 0x81) или None."""
    value: int | None = None
    for sprm, operand in _sprms(grpprl):
        if sprm == _SPRM_C_BOLD:
            value = operand[0]
    return value


# --- Стили ------------------------------------------------------------------------


@dataclass(frozen=True)
class _Style:
    sti: int
    base: int
    name: str
    outline: int | None
    bold: bool | None


def _styles(table: bytes, fib: _Fib) -> dict[int, _Style]:
    fc, lcb = fib.pair(_FC_STSHF)
    if lcb < 6:
        return {}
    stsh = table[fc : fc + lcb]
    header_size = struct.unpack_from("<H", stsh, 0)[0]
    count, base_size = struct.unpack_from("<HH", stsh, 2)
    position = 2 + header_size
    styles: dict[int, _Style] = {}
    for istd in range(count):
        if position + 2 > len(stsh):
            break
        size = struct.unpack_from("<H", stsh, position)[0]
        std = stsh[position + 2 : position + 2 + size]
        position += 2 + size
        if size == 0 or len(std) < base_size + 2:
            continue
        word0, word1, word2 = struct.unpack_from("<HHH", std, 0)
        kind, upx_count = word1 & 0x000F, word2 & 0x000F
        chars = struct.unpack_from("<H", std, base_size)[0]
        name = std[base_size + 2 : base_size + 2 + 2 * chars].decode(
            "utf-16-le", "replace"
        )
        cursor = base_size + 4 + 2 * chars
        outline: int | None = None
        bold: bool | None = None
        for k in range(upx_count):
            if cursor + 2 > len(std):
                break
            upx_size = struct.unpack_from("<H", std, cursor)[0]
            upx = std[cursor + 2 : cursor + 2 + upx_size]
            cursor += 2 + upx_size + (upx_size & 1)
            if kind == 1 and k == 0:  # абзацный стиль: istd + свойства абзаца
                for sprm, operand in _sprms(upx[2:]):
                    if sprm == _SPRM_P_OUT_LVL:
                        outline = operand[0]
            elif (kind == 1 and k == 1) or (kind == 2 and k == 0):
                for sprm, operand in _sprms(upx):
                    if sprm == _SPRM_C_BOLD:
                        bold = operand[0] == 1
        styles[istd] = _Style(word0 & 0x0FFF, word1 >> 4, name, outline, bold)
    return styles


def _style_chain(styles: dict[int, _Style], istd: int) -> Iterator[_Style]:
    seen: set[int] = set()
    while istd in styles and istd not in seen:
        seen.add(istd)
        yield styles[istd]
        istd = styles[istd].base


def _heading_level(styles: dict[int, _Style], istd: int, outline: int | None) -> int:
    if outline is not None:
        return outline + 1 if outline < 9 else 0
    for style in _style_chain(styles, istd):
        if 1 <= style.sti <= 9:
            return style.sti
        if style.sti == _STI_TITLE:
            return 1
        if style.outline is not None:
            return style.outline + 1 if style.outline < 9 else 0
        match = _HEADING_NAME.match(style.name.strip())
        if match:
            return int(match.group(1))
    return 0


def _style_bold(styles: dict[int, _Style], istd: int) -> bool:
    for style in _style_chain(styles, istd):
        if style.bold is not None:
            return style.bold
    return False


# --- Документ ---------------------------------------------------------------------


@dataclass
class _Paragraph:
    text: str
    mark: str
    """\\r — конец абзаца, \\x07 — конец ячейки или строки таблицы."""
    istd: int = 0
    in_table: bool = False
    ttp: bool = False
    depth: int = 0
    outline: int | None = None
    tdef: bytes = b""
    vmerge: dict[int, int] = field(default_factory=dict)
    bold: bool = False


@dataclass
class _Row:
    cells: list[str]
    tdef: bytes
    vmerge: dict[int, int]


def _read(data: bytes) -> DocText:
    cfb = _Cfb(data)
    word = cfb.stream("WordDocument")
    fib = _read_fib(word)
    table = cfb.stream(fib.table_stream)
    pieces = _pieces(table, fib)
    data_stream = cfb.stream("Data") if cfb.has_stream("Data") else b""
    papx = _Lookup(_fkp_runs(word, table, fib, "papx", data_stream))
    chpx = _Lookup(_fkp_runs(word, table, fib, "chpx"))
    styles = _styles(table, fib)
    has_ttp = any(
        sprm == _SPRM_P_TTP for run in papx.runs for sprm, _ in _sprms(run.grpprl)
    )

    ccp_text, ccp_ftn, ccp_hdd, ccp_mcr, ccp_atn, ccp_edn, ccp_txbx, _ = fib.ccp
    text, fcs = _without_fields(
        *_without_deleted(*_story(word, pieces, 0, ccp_text), chpx)
    )
    paragraphs = _paragraphs(text, fcs, papx, chpx, styles, has_ttp=has_ttp)
    document = _assemble(paragraphs, styles)

    notes: list[str] = []
    for start, length in (
        (ccp_text, ccp_ftn),
        (ccp_text + ccp_ftn + ccp_hdd + ccp_mcr + ccp_atn, ccp_edn),
    ):
        if length:
            story, story_fcs = _without_fields(
                *_story(word, pieces, start, start + length)
            )
            notes.extend(
                p.text for p in _paragraphs(story, story_fcs, None, None, {}) if p.text
            )
    if notes:
        document.footnotes = len(notes)
        document.blocks.append("Сноски:\n" + "\n".join(notes))
    if ccp_txbx:
        start = ccp_text + ccp_ftn + ccp_hdd + ccp_mcr + ccp_atn + ccp_edn
        story, story_fcs = _without_fields(
            *_story(word, pieces, start, start + ccp_txbx)
        )
        boxes = [
            p.text for p in _paragraphs(story, story_fcs, None, None, {}) if p.text
        ]
        document.blocks.extend(boxes)
    return document


def _without_deleted(text: str, fcs: list[int], chpx: _Lookup) -> tuple[str, list[int]]:
    """Режим исправлений: удалённый текст остаётся в файле с пометкой
    sprmCFRMarkDel — выбрасываем его, иначе в ответ попадут обе версии.
    Знаки абзацев остаются, чтобы не склеить соседние абзацы."""
    if not any(chpx.deleted):
        return text, fcs
    kept_chars: list[str] = []
    kept_fcs: list[int] = []
    for char, fc in zip(text, fcs, strict=True):
        index = chpx.index(fc)
        if index is None or not chpx.deleted[index] or char in "\r\x07":
            kept_chars.append(char)
            kept_fcs.append(fc)
    return "".join(kept_chars), kept_fcs


def _without_fields(text: str, fcs: list[int]) -> tuple[str, list[int]]:
    """Поля: код выбросить, результат оставить; оглавление (TOC) — целиком."""
    if "\x13" not in text:
        return text, fcs
    kept_chars: list[str] = []
    kept_fcs: list[int] = []
    stack: list[list[str]] = []  # [состояние, код]
    for char, fc in zip(text, fcs, strict=True):
        if char == "\x13":
            stack.append(["code", ""])
            continue
        if char == "\x14" and stack:
            code = stack[-1][1].split()
            stack[-1][0] = "drop" if code and code[0].upper() == "TOC" else "result"
            continue
        if char == "\x15" and stack:
            stack.pop()
            continue
        if stack and stack[-1][0] == "code":
            stack[-1][1] += char
        keep = all(state == "result" for state, _ in stack)
        if keep or char in "\r\x07":
            kept_chars.append(char)
            kept_fcs.append(fc)
    return "".join(kept_chars), kept_fcs


_CONTROLS = str.maketrans(
    {"\x0b": "\n", "\x0c": "\n", "\x1e": "-", "\x1f": "", "\xa0": " ", "\t": " "}
)
_OTHER_CONTROLS = re.compile(r"[\x00-\x09\x0e-\x1f]")


def _clean(raw: str) -> str:
    text = _OTHER_CONTROLS.sub("", raw.translate(_CONTROLS))
    # Пары суррогатов, разрезанные посимвольным чтением, — снова в символы.
    text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    lines = (" ".join(line.split()) for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _paragraphs(
    text: str,
    fcs: list[int],
    papx: _Lookup | None,
    chpx: _Lookup | None,
    styles: dict[int, _Style],
    *,
    has_ttp: bool = True,
) -> list[_Paragraph]:
    result: list[_Paragraph] = []
    start = 0
    for index, char in enumerate(text):
        if char not in "\r\x07":
            continue
        raw = text[start:index]
        paragraph = _Paragraph(_clean(raw), char)
        run = papx.at(fcs[index]) if papx is not None else None
        if run is not None:
            _apply_papx(paragraph, run)
        if (
            not has_ttp
            and char == "\x07"
            and not raw
            and index
            and text[index - 1] == "\x07"
        ):
            paragraph.ttp = (
                True  # без свойств: пустая ячейка после ячейки — конец строки
            )
        if chpx is not None and paragraph.text:
            paragraph.bold = _is_bold(
                raw, fcs[start:index], chpx, styles, paragraph.istd
            )
        result.append(paragraph)
        start = index + 1
    tail = _clean(text[start:])
    if tail:
        result.append(_Paragraph(tail, "\r"))
    return result


def _apply_papx(paragraph: _Paragraph, run: _Run) -> None:
    paragraph.istd = run.istd
    for sprm, operand in _sprms(run.grpprl):
        if sprm == _SPRM_P_IN_TABLE:
            paragraph.in_table = operand[0] != 0
        elif sprm == _SPRM_P_TTP:
            paragraph.ttp = operand[0] != 0
        elif sprm == _SPRM_P_ITAP:
            paragraph.depth = struct.unpack_from("<i", operand, 0)[0]
        elif sprm == _SPRM_P_OUT_LVL:
            paragraph.outline = operand[0]
        elif sprm == _SPRM_P_ISTD:
            paragraph.istd = struct.unpack_from("<H", operand, 0)[0]
        elif sprm == _SPRM_T_DEF_TABLE:
            paragraph.tdef = operand[2:]
        elif sprm == _SPRM_T_VERT_MERGE and len(operand) >= 3:
            paragraph.vmerge[operand[1]] = operand[2]


def _is_bold(
    raw: str, fcs: list[int], chpx: _Lookup, styles: dict[int, _Style], istd: int
) -> bool:
    """Все видимые символы абзаца — полужирные (прямо или по стилю)."""
    style_bold = _style_bold(styles, istd)
    for char, fc in zip(raw, fcs, strict=True):
        if char.isspace() or ord(char) < 0x20:
            continue
        index = chpx.index(fc)
        value = chpx.bold[index] if index is not None else None
        bold = (
            style_bold
            if value is None or value == 0x80
            else value == 1 or (value == 0x81 and not style_bold)
        )
        if not bold:
            return False
    return True


def _assemble(paragraphs: list[_Paragraph], styles: dict[int, _Style]) -> DocText:
    document = DocText(blocks=[])
    level = 0
    rows: list[_Row] = []
    cells: list[str] = []
    parts: list[str] = []

    def flush_table() -> None:
        nonlocal rows
        if rows:
            lines = table_lines(_sheet(rows), base_level=level)
            if lines:
                document.blocks.append("\n".join(lines))
                document.tables += 1
            rows = []

    for paragraph in paragraphs:
        in_table = (
            paragraph.in_table or paragraph.depth >= 1 or paragraph.mark == "\x07"
        )
        if in_table:
            if paragraph.depth >= 2:
                if paragraph.text:  # вложенная таблица — текстом в ячейке
                    parts.append(paragraph.text)
                continue
            if paragraph.ttp:
                rows.append(_Row(cells, paragraph.tdef, paragraph.vmerge))
                cells, parts = [], []
            elif paragraph.mark == "\x07":
                parts.append(paragraph.text)
                cells.append(" ".join(p for p in parts if p))
                parts = []
            elif paragraph.text:
                parts.append(paragraph.text)
            continue

        if cells:  # строка без конца строки (повреждённая разметка) — не терять
            rows.append(_Row(cells, b"", {}))
            cells = []
        flush_table()
        if not paragraph.text:
            continue
        heading = _heading_level(styles, paragraph.istd, paragraph.outline)
        if heading and "\n" not in paragraph.text:
            level = min(heading, 6)
            document.blocks.append("#" * level + " " + paragraph.text)
            document.headings += 1
        elif (
            paragraph.bold
            and len(paragraph.text) <= TITLE_MAX_CHARS
            and "\n" not in paragraph.text
        ):
            document.blocks.append(f"**{paragraph.text}**")
        else:
            document.blocks.append(paragraph.text)
    if cells:
        rows.append(_Row(cells, b"", {}))
    flush_table()
    return document


def _row_layout(row: _Row) -> tuple[list[int], list[int]] | None:
    """Границы ячеек (rgdxaCenter) и флаги ячеек (tcgrf) из sprmTDefTable."""
    if not row.tdef:
        return None
    count = row.tdef[0]
    if count != len(row.cells) or len(row.tdef) < 1 + 2 * (count + 1):
        return None
    bounds = list(struct.unpack_from(f"<{count + 1}h", row.tdef, 1))
    first_tc = 1 + 2 * (count + 1)
    present = min(count, (len(row.tdef) - first_tc) // 20)
    flags = [
        struct.unpack_from("<H", row.tdef, first_tc + 20 * k)[0] for k in range(present)
    ]
    flags += [0] * (count - present)
    for cell, code in row.vmerge.items():
        if cell < count:
            flags[cell] = (flags[cell] & ~0x0060) | ((code & 0x3) << 5)
    return bounds, flags


def _sheet(rows: list[_Row]) -> Sheet:
    """Строки таблицы → сетка с объединениями (как лист Excel)."""
    layouts = [_row_layout(row) for row in rows]
    grid = sorted({b for layout in layouts if layout for b in layout[0]})
    cells: dict[tuple[int, int], str] = {}
    rects: list[list[int]] = []
    vertical: dict[int, list[int]] = {}  # первый столбец → [r1, c1, r2, c2]

    for r, (row, layout) in enumerate(zip(rows, layouts, strict=True), start=1):
        if layout is None:
            for c, text in enumerate(row.cells, start=1):
                if text:
                    cells[(r, c)] = text
            continue
        bounds, flags = layout
        previous: list[int] | None = None
        for index, text in enumerate(row.cells):
            c1 = grid.index(bounds[index]) + 1
            c2 = max(c1, grid.index(bounds[index + 1]))
            horizontal, vertical_code = flags[index] & 0x3, (flags[index] >> 5) & 0x3
            if horizontal in (2, 3) and previous is not None:
                previous[3] = max(previous[3], c2)  # старое объединение по горизонтали
                continue
            if vertical_code == 1 and c1 in vertical:
                vertical[c1][2] = r  # продолжение объединения сверху
                previous = vertical[c1]
                continue
            for key in [k for k in vertical if c1 <= k <= c2]:
                rects.append(vertical.pop(key))
            if text:
                cells[(r, c1)] = text
            rect = [r, c1, r, c2]
            if vertical_code == 3:
                vertical[c1] = rect
            else:
                rects.append(rect)
            previous = rect
    rects.extend(vertical.values())
    merges = [(a, b, c, d) for a, b, c, d in rects if (a, b) != (c, d)]
    return Sheet(name="", cells=cells, merges=merges)
