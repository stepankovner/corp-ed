"""Свой PDF счёта и акта: кириллица читается, суммы и номер на месте,
итог прописью; без шрифта — понятная ошибка, а не пустой документ."""

from datetime import date
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from corp_ed.core.config import PaymentSettings, SellerSettings
from corp_ed.services.billing_pdf import (
    PdfLine,
    PdfParty,
    PdfUnavailableError,
    amount_in_words,
    money,
    render_act,
    render_invoice,
)

FONT = PaymentSettings().pdf_font
needs_font = pytest.mark.skipif(not Path(FONT).exists(), reason="нет шрифта DejaVu")

SELLER = SellerSettings(
    name="ИП Тестов Тест Тестович",
    inn="500100732259",
    ogrn="304500116000157",
    address="Москва",
    bank_name="ООО «Банк Точка»",
    bik="044525104",
    account="40802810000000000001",
    corr_account="30101810745374525104",
)
BUYER = PdfParty(
    name="ООО «Ромашка»",
    inn="7707083893",
    kpp="773601001",
    address="Москва, Тверская, 1",
)
LINES = [
    PdfLine(
        "Доступ к kronto, тариф «Базовый», 30 мест, 09.10.2026–08.11.2026", 29_700_00
    ),
    PdfLine("Пакет 500 кредитов", 1_490_00),
]


def _text(pdf: bytes) -> str:
    document = pdfium.PdfDocument(pdf)
    return "".join(page.get_textpage().get_text_range() for page in document)


def test_amount_in_words() -> None:
    assert (
        amount_in_words(31_190_00)
        == "Тридцать одна тысяча сто девяносто рублей 00 копеек"
    )
    assert amount_in_words(1_21) == "Один рубль 21 копейка"
    assert amount_in_words(2_000_000_02) == "Два миллиона рублей 02 копейки"
    assert amount_in_words(12_45) == "Двенадцать рублей 45 копеек"
    assert amount_in_words(0) == "Ноль рублей 00 копеек"
    assert money(1_234_567_89) == "1 234 567,89"


@needs_font
def test_invoice_pdf_has_number_amounts_and_purpose() -> None:
    pdf = render_invoice(
        font_path=FONT,
        seller=SELLER,
        number="KR-00042",
        issued=date(2026, 10, 9),
        buyer=BUYER,
        lines=LINES,
        total_kopecks=31_190_00,
        purpose="Оплата по счёту № KR-00042 от 09.10.2026 за доступ к сервису "
        "kronto. Без НДС",
        due=date(2026, 10, 14),
    )

    assert pdf.startswith(b"%PDF")
    text = _text(pdf)
    assert "Счёт на оплату № KR-00042 от 09.10.2026" in text
    assert "ООО «Ромашка»" in text
    assert "ИНН 7707083893" in text
    assert "БИК 044525104" in text
    assert "31 190,00" in text
    assert "Тридцать одна тысяча сто девяносто рублей 00 копеек" in text
    assert "Оплатить до 14.10.2026" in text


@needs_font
def test_act_pdf_names_the_month() -> None:
    pdf = render_act(
        font_path=FONT,
        seller=SELLER,
        number="3",
        issued=date(2026, 11, 1),
        month=date(2026, 10, 1),
        buyer=BUYER,
        lines=LINES[:1],
        total_kopecks=29_700_00,
    )
    text = _text(pdf)
    assert "Акт № 3 от 01.11.2026" in text
    assert "за октябрь 2026" in text
    assert "29 700,00" in text
    assert "претензий" in text


def test_missing_font_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(PdfUnavailableError):
        render_act(
            font_path=str(tmp_path / "none.ttf"),
            seller=SELLER,
            number="1",
            issued=date(2026, 11, 1),
            month=date(2026, 10, 1),
            buyer=BUYER,
            lines=LINES,
            total_kopecks=1,
        )
