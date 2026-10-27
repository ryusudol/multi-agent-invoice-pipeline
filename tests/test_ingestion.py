from pathlib import Path

import pytest

from invoice_agent import config
from invoice_agent.ingestion.check import check_extraction
from invoice_agent.ingestion.loader import UnsupportedFileError, load_text

from .conftest import line, make_invoice

INVOICES = config.ROOT / "data" / "invoices"


def source(name: str) -> str:
    return load_text(INVOICES / name)


def test_loads_text_formats():
    assert "INV-1001" in source("invoice_1001.txt")
    assert "<currency>EUR</currency>" in source("invoice_1014.xml")
    assert "field,value" in source("invoice_1006.csv")


def test_pdf_text_is_extracted():
    text = source("invoice_1012.pdf")
    assert "QuickShip Distributers" in text
    assert "$3,500.O0" in text  # OCR noise survives extraction; normalization handles it


def test_unsupported_file(tmp_path: Path):
    path = tmp_path / "invoice.docx"
    path.write_text("x")
    with pytest.raises(UnsupportedFileError):
        load_text(path)


def test_correct_extraction_passes():
    assert check_extraction(make_invoice(), source("invoice_1001.txt")) == []


def test_hallucinated_values_are_caught():
    invoice = make_invoice(stated_total=5500.0, vendor="Widget Corp")
    problems = check_extraction(invoice, source("invoice_1001.txt"))
    assert any("stated_total" in p for p in problems)
    assert any("vendor" in p for p in problems)


def test_invented_tax_rate_is_caught():
    # 1010 has a "Sales Tax" line but no rate.
    invoice = make_invoice(
        invoice_number="INV-1010",
        vendor="Consolidated Materials Group",
        invoice_date="January 27, 2026",
        line_items=[line("WidgetA", 8, 250.0)],
        stated_subtotal=6700.0,
        stated_tax_rate=0.05,
        stated_tax_amount=335.0,
        stated_shipping=150.0,
        stated_total=7185.0,
    )
    problems = check_extraction(invoice, source("invoice_1010.txt"))
    assert problems == [
        "stated_tax_rate 0.05 is not written in the document; use null unless a rate is written"
    ]


def test_nulls_negatives_and_ocr_pass():
    # 1009: blank vendor -> null, negative quantity, no due date.
    inv_1009 = make_invoice(
        invoice_number="INV-1009",
        vendor=None,
        due_date=None,
        line_items=[line("WidgetA", -5, 250.0), line("WidgetB", 2, 500.0)],
        stated_subtotal=1000.0,
        stated_total=-250.0,
        currency="USD",
    )
    assert check_extraction(inv_1009, source("invoice_1009.json")) == []

    # 1012 PDF: vendor split across two lines, OCR '2O26' and '$3,500.O0', spaced item names, 5% rate.
    inv_1012 = make_invoice(
        invoice_number="INV 1012",
        vendor="QuickShip Distributers (formerly FastShip Ltd.)",
        invoice_date="26-Jan-2O26",
        line_items=[line("Widget A", 12, 250.0, stated_amount=3000.0), line("WidgetB", 7, 500.0, stated_amount=3500.0)],
        stated_subtotal=9500.0,
        stated_tax_rate=0.05,
        stated_tax_amount=475.0,
        stated_total=9975.0,
    )
    assert check_extraction(inv_1012, source("invoice_1012.pdf")) == []


def test_unparseable_invoice_date_is_caught_but_due_date_is_not():
    text = source("invoice_1003.txt")
    base = dict(
        invoice_number="INV-1003",
        vendor="Fraudster LLC",
        line_items=[line("FakeItem", 100, 1000.0)],
        stated_subtotal=None,
        stated_tax_rate=None,
        stated_tax_amount=None,
        stated_total=100000.0,
    )
    assert check_extraction(make_invoice(**base, invoice_date="2026-01-20", due_date="yesterday"), text) == []
    problems = check_extraction(make_invoice(**base, invoice_date="the twentieth"), text)
    assert any("invoice_date" in p for p in problems)


def test_blank_strings_become_null():
    assert make_invoice(vendor="  ").vendor is None
