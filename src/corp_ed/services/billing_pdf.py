"""Счёт и акт в PDF своим шаблоном (решение владельца 09.10).

Нужен, когда банк документ не отдаёт: PAYMENTS_PROVIDER=none у команды,
сбой API банка, акт по оплате картой без реквизитов в банке. С Точкой
PDF счёта и акта берётся у неё (services/payments/tochka.py).

Без новых зависимостей и системных библиотек: страницу собирает PDFium
через pypdfium2 (Apache-2.0 / BSD-3, уже стоит ради pdfplumber), шрифт с
кириллицей — DejaVu Sans (свободная лицензия Bitstream Vera; в образе —
пакет fonts-dejavu-core, путь — PAYMENTS_PDF_FONT). Шрифт встраивается
целиком: документ весит ~0,4 МБ, зато открывается где угодно.

Реквизиты продавца — из настроек сервера (BILLING_SELLER_*), не из кода.
"""

import ctypes
import io
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from corp_ed.core.config import SellerSettings

A4 = (595.0, 842.0)
MARGIN = 50.0
INK = (22, 22, 26)
MUTED = (94, 94, 102)


class PdfUnavailableError(Exception):
    """Шрифта нет — документ не собрать (PAYMENTS_PDF_FONT)."""


@dataclass(frozen=True)
class PdfParty:
    name: str
    inn: str | None
    kpp: str | None
    address: str | None


@dataclass(frozen=True)
class PdfLine:
    name: str
    amount_kopecks: int


def money(kopecks: int) -> str:
    """29 700,00 — с неразрывными пробелами между разрядами."""
    rubles, rest = divmod(kopecks, 100)
    return f"{rubles:,}".replace(",", " ") + f",{rest:02d}"


_ONES = (
    "",
    "один",
    "два",
    "три",
    "четыре",
    "пять",
    "шесть",
    "семь",
    "восемь",
    "девять",
)
_ONES_FEMININE = ("", "одна", "две") + _ONES[3:]
_TEENS = (
    "десять",
    "одиннадцать",
    "двенадцать",
    "тринадцать",
    "четырнадцать",
    "пятнадцать",
    "шестнадцать",
    "семнадцать",
    "восемнадцать",
    "девятнадцать",
)
_TENS = (
    "",
    "",
    "двадцать",
    "тридцать",
    "сорок",
    "пятьдесят",
    "шестьдесят",
    "семьдесят",
    "восемьдесят",
    "девяносто",
)
_HUNDREDS = (
    "",
    "сто",
    "двести",
    "триста",
    "четыреста",
    "пятьсот",
    "шестьсот",
    "семьсот",
    "восемьсот",
    "девятьсот",
)
_SCALES = (
    ("", "", "", False),
    ("тысяча", "тысячи", "тысяч", True),
    ("миллион", "миллиона", "миллионов", False),
    ("миллиард", "миллиарда", "миллиардов", False),
)


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _triplet(n: int, feminine: bool) -> list[str]:
    words = [_HUNDREDS[n // 100]]
    rest = n % 100
    if 10 <= rest < 20:
        words.append(_TEENS[rest - 10])
    else:
        words.append(_TENS[rest // 10])
        words.append((_ONES_FEMININE if feminine else _ONES)[rest % 10])
    return [word for word in words if word]


def amount_in_words(kopecks: int) -> str:
    """«Двадцать девять тысяч семьсот рублей 00 копеек» — для счёта."""
    rubles, rest = divmod(kopecks, 100)
    if rubles == 0:
        words = ["ноль"]
    else:
        words = []
        groups: list[int] = []
        value = rubles
        while value:
            groups.append(value % 1000)
            value //= 1000
        for index in range(len(groups) - 1, -1, -1):
            group = groups[index]
            if not group:
                continue
            one, few, many, feminine = _SCALES[index]
            words += _triplet(group, feminine)
            if one:
                words.append(_plural(group, one, few, many))
    text = " ".join(words)
    currency = _plural(rubles, "рубль", "рубля", "рублей")
    cents = _plural(rest, "копейка", "копейки", "копеек")
    return f"{text[0].upper()}{text[1:]} {currency} {rest:02d} {cents}"


_MONTHS = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)


def month_name(month: date) -> str:
    return f"{_MONTHS[month.month - 1]} {month.year}"


class _Canvas:
    """Страница A4 и текст одним шрифтом. Координаты — сверху вниз."""

    def __init__(self, font_path: str) -> None:
        try:
            data = Path(font_path).read_bytes()
        except OSError as exc:
            raise PdfUnavailableError(font_path) from exc
        self.doc = pdfium.PdfDocument.new()
        self.page = self.doc.new_page(*A4)
        # Буфер шрифта живёт, пока жив документ: PDFium читает его при save.
        self._font_data = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        self.font = pdfium_c.FPDFText_LoadFont(
            self.doc.raw,
            self._font_data,
            len(data),
            pdfium_c.FPDF_FONT_TRUETYPE,
            True,
        )
        if not self.font:
            raise PdfUnavailableError(font_path)
        self.y = MARGIN

    def _object(self, text: str, size: float) -> object:
        obj = pdfium_c.FPDFPageObj_CreateTextObj(self.doc.raw, self.font, size)
        buffer = ctypes.create_string_buffer((text + "\0").encode("utf-16-le"))
        pdfium_c.FPDFText_SetText(
            obj, ctypes.cast(buffer, ctypes.POINTER(pdfium_c.FPDF_WCHAR))
        )
        return obj

    def width(self, text: str, size: float) -> float:
        if not text:
            return 0.0
        obj = self._object(text, size)
        left, bottom = ctypes.c_float(), ctypes.c_float()
        right, top = ctypes.c_float(), ctypes.c_float()
        pdfium_c.FPDFPageObj_GetBounds(obj, left, bottom, right, top)
        pdfium_c.FPDFPageObj_Destroy(obj)
        return float(right.value - left.value)

    def text(
        self,
        x: float,
        text: str,
        size: float = 10,
        *,
        color: tuple[int, int, int] = INK,
        right: bool = False,
    ) -> None:
        if not text:
            return
        obj = self._object(text, size)
        pdfium_c.FPDFPageObj_SetFillColor(obj, *color, 255)
        left = x - self.width(text, size) if right else x
        pdfium_c.FPDFPageObj_Transform(obj, 1, 0, 0, 1, left, A4[1] - self.y - size)
        pdfium_c.FPDFPage_InsertObject(self.page.raw, obj)

    def wrap(self, text: str, size: float, width: float) -> list[str]:
        lines: list[str] = []
        for paragraph in text.split("\n"):
            current = ""
            for word in paragraph.split():
                candidate = f"{current} {word}".strip()
                if current and self.width(candidate, size) > width:
                    lines.append(current)
                    current = word
                else:
                    current = candidate
            lines.append(current)
        return lines

    def paragraph(
        self,
        x: float,
        text: str,
        size: float = 10,
        *,
        width: float | None = None,
        color: tuple[int, int, int] = INK,
        gap: float = 4,
    ) -> None:
        for line in self.wrap(text, size, width or (A4[0] - MARGIN - x)):
            self.text(x, line, size, color=color)
            self.y += size + 3
        self.y += gap

    def rule(
        self, x1: float = MARGIN, x2: float = A4[0] - MARGIN, w: float = 0.6
    ) -> None:
        y = A4[1] - self.y
        path = pdfium_c.FPDFPageObj_CreateNewPath(x1, y)
        pdfium_c.FPDFPath_LineTo(path, x2, y)
        pdfium_c.FPDFPageObj_SetStrokeColor(path, *INK, 255)
        pdfium_c.FPDFPageObj_SetStrokeWidth(path, w)
        pdfium_c.FPDFPath_SetDrawMode(path, pdfium_c.FPDF_FILLMODE_NONE, True)
        pdfium_c.FPDFPage_InsertObject(self.page.raw, path)

    def save(self) -> bytes:
        pdfium_c.FPDFPage_GenerateContent(self.page.raw)
        out = io.BytesIO()
        self.doc.save(out)
        return out.getvalue()


def _party_lines(party: PdfParty) -> str:
    codes = [
        f"ИНН {party.inn}" if party.inn else "",
        f"КПП {party.kpp}" if party.kpp else "",
    ]
    parts = [party.name, ", ".join(code for code in codes if code), party.address or ""]
    return ", ".join(part for part in parts if part)


def _seller_party(seller: SellerSettings) -> PdfParty:
    name = seller.name or "kronto"
    address = seller.address
    if seller.ogrn:
        address = (
            f"ОГРНИП {seller.ogrn}, {address}" if address else f"ОГРНИП {seller.ogrn}"
        )
    return PdfParty(name=name, inn=seller.inn or None, kpp=None, address=address)


def _table(canvas: _Canvas, lines: list[PdfLine], total: int) -> None:
    x_num, x_name, x_qty, x_sum = (
        MARGIN,
        MARGIN + 24,
        A4[0] - MARGIN - 150,
        A4[0] - MARGIN,
    )
    canvas.rule()
    canvas.y += 5
    canvas.text(x_num, "№", 9, color=MUTED)
    canvas.text(x_name, "Наименование услуги", 9, color=MUTED)
    canvas.text(x_qty, "Кол-во", 9, color=MUTED)
    canvas.text(x_sum, "Сумма, ₽", 9, color=MUTED, right=True)
    canvas.y += 15
    canvas.rule(w=0.3)
    canvas.y += 6
    for index, line in enumerate(lines, start=1):
        top = canvas.y
        canvas.text(x_num, str(index), 10)
        canvas.text(x_qty, "1 усл.", 10)
        canvas.text(x_sum, money(line.amount_kopecks), 10, right=True)
        canvas.y = top
        canvas.paragraph(x_name, line.name, 10, width=x_qty - x_name - 12, gap=4)
    canvas.rule(w=0.3)
    canvas.y += 6
    canvas.text(x_qty - 60, "Итого:", 10)
    canvas.text(x_sum, money(total), 10, right=True)
    canvas.y += 14
    canvas.text(x_qty - 60, "Без НДС", 10, color=MUTED)
    canvas.y += 20


def render_invoice(
    *,
    font_path: str,
    seller: SellerSettings,
    number: str,
    issued: date,
    buyer: PdfParty,
    lines: list[PdfLine],
    total_kopecks: int,
    purpose: str,
    due: date | None,
) -> bytes:
    """Счёт на оплату: продавец с банковскими реквизитами, покупатель,
    позиции, итог прописью, назначение платежа с номером счёта."""
    canvas = _Canvas(font_path)
    seller_party = _seller_party(seller)
    canvas.paragraph(MARGIN, "Получатель", 8, color=MUTED, gap=0)
    canvas.paragraph(MARGIN, _party_lines(seller_party), 10)
    bank = [
        f"Банк: {seller.bank_name}" if seller.bank_name else "",
        f"БИК {seller.bik}" if seller.bik else "",
        f"р/с {seller.account}" if seller.account else "",
        f"к/с {seller.corr_account}" if seller.corr_account else "",
    ]
    canvas.paragraph(MARGIN, ", ".join(item for item in bank if item), 10, gap=14)
    canvas.paragraph(
        MARGIN, f"Счёт на оплату № {number} от {issued:%d.%m.%Y}", 16, gap=10
    )
    canvas.paragraph(MARGIN, "Покупатель", 8, color=MUTED, gap=0)
    canvas.paragraph(MARGIN, _party_lines(buyer), 10, gap=12)
    _table(canvas, lines, total_kopecks)
    canvas.paragraph(MARGIN, f"Всего к оплате: {amount_in_words(total_kopecks)}.", 10)
    canvas.paragraph(
        MARGIN,
        "НДС не облагается: продавец применяет УСН (гл. 26.2 НК РФ).",
        9,
        color=MUTED,
    )
    if due is not None:
        canvas.paragraph(MARGIN, f"Оплатить до {due:%d.%m.%Y}.", 10)
    canvas.paragraph(MARGIN, "Назначение платежа", 8, color=MUTED, gap=0)
    canvas.paragraph(MARGIN, purpose, 10, gap=8)
    canvas.paragraph(
        MARGIN,
        f"Укажите номер счёта {number} в назначении платежа — тогда оплата "
        "зачтётся автоматически.",
        9,
        color=MUTED,
        gap=24,
    )
    canvas.paragraph(MARGIN, f"{seller_party.name} _______________", 10)
    return canvas.save()


def render_act(
    *,
    font_path: str,
    seller: SellerSettings,
    number: str,
    issued: date,
    month: date,
    buyer: PdfParty,
    lines: list[PdfLine],
    total_kopecks: int,
) -> bytes:
    """Акт об оказании услуг за месяц."""
    canvas = _Canvas(font_path)
    seller_party = _seller_party(seller)
    canvas.paragraph(MARGIN, f"Акт № {number} от {issued:%d.%m.%Y}", 16, gap=2)
    canvas.paragraph(
        MARGIN, f"об оказании услуг за {month_name(month)}", 11, color=MUTED, gap=12
    )
    canvas.paragraph(MARGIN, "Исполнитель", 8, color=MUTED, gap=0)
    canvas.paragraph(MARGIN, _party_lines(seller_party), 10)
    canvas.paragraph(MARGIN, "Заказчик", 8, color=MUTED, gap=0)
    canvas.paragraph(MARGIN, _party_lines(buyer), 10, gap=12)
    _table(canvas, lines, total_kopecks)
    canvas.paragraph(
        MARGIN, f"Всего оказано услуг на сумму: {amount_in_words(total_kopecks)}.", 10
    )
    canvas.paragraph(
        MARGIN,
        "Услуги оказаны полностью и в срок. Заказчик претензий по объёму, "
        "качеству и срокам оказания услуг не имеет.",
        10,
        gap=30,
    )
    top = canvas.y
    canvas.text(MARGIN, "Исполнитель _______________", 10)
    canvas.text(A4[0] / 2 + 10, "Заказчик _______________", 10)
    canvas.y = top + 14
    canvas.text(MARGIN, seller_party.name, 8, color=MUTED)
    canvas.text(A4[0] / 2 + 10, buyer.name, 8, color=MUTED)
    return canvas.save()
