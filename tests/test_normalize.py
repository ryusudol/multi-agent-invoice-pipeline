from datetime import date
from decimal import Decimal

import pytest

from invoice_agent.ingestion.normalize import (
    cents,
    fix_ocr_digits,
    normalize_currency,
    normalize_invoice_number,
    normalize_vendor,
    numbers_in,
    parse_date,
    split_parenthetical,
)
from invoice_agent.validation.rules import catalog_match, suggest


@pytest.mark.parametrize(
    ("raw", "fixed"),
    [
        ("26-Jan-2O26", "26-Jan-2026"),
        ("$3,500.O0", "$3,500.00"),
        ("Ref PO-20260115", "Ref PO-20260115"),
        ("Order Volume", "Order Volume"),
    ],
)
def test_ocr_digit_fixes(raw, fixed):
    assert fix_ocr_digits(raw) == fixed


@pytest.mark.parametrize(
    ("raw", "number"),
    [("INV-1001", "INV-1001"), ("Inv #: 1002", "INV-1002"), ("INV 1012", "INV-1012"), ("#INV-1010", "INV-1010"), ("1002", "INV-1002"), (None, None)],
)
def test_invoice_number(raw, number):
    assert normalize_invoice_number(raw) == number


@pytest.mark.parametrize(
    ("raw", "vendor"),
    [
        ("Widgets Inc.", "widgets inc"),
        ("QuickShip Distributers (formerly FastShip Ltd.)", "quickship distributers"),
        ("", None),
    ],
)
def test_vendor(raw, vendor):
    assert normalize_vendor(raw) == vendor


def test_currency_defaults_to_usd():
    assert normalize_currency(None) == "USD"
    assert normalize_currency("$") == "USD"
    assert normalize_currency("eur") == "EUR"


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [
        ("2026-01-15", date(2026, 1, 15)),
        ("Jan 30 2026", date(2026, 1, 30)),
        ("26-Jan-2O26", date(2026, 1, 26)),
        ("January 27, 2026", date(2026, 1, 27)),
        ("01/28/2026", date(2026, 1, 28)),
        ("yesterday", None),
    ],
)
def test_parse_date(raw, parsed):
    assert parse_date(raw) == parsed


def test_numbers_in_reads_csv_rows_and_thousands():
    found = numbers_in("INV-1007,MegaWidgets Corp,WidgetA,20,250.00,5000.00\nTotal: $3,000.00 TAX (5%)")
    assert {Decimal(20), Decimal(250), Decimal(5000), Decimal(3000), Decimal(5)} <= found


def test_cents_avoids_float_drift():
    assert cents(1472.8) == 147280
    assert cents(22562.80) == 2256280


def test_parenthetical_split():
    assert split_parenthetical("WidgetA (rush order)") == ("WidgetA", "rush order")
    assert split_parenthetical("WidgetA") == ("WidgetA", None)


def test_catalog_match(catalog):
    assert catalog_match("WidgetA", catalog) == "WidgetA"
    assert catalog_match("Widget A", catalog) == "WidgetA"
    assert catalog_match("Gadget X", catalog) == "GadgetX"
    assert catalog_match("WidgetA (rush order)", catalog) == "WidgetA"


def test_widget_c_stays_unknown(catalog):
    assert catalog_match("WidgetC", catalog) == None
    assert suggest("WidgetC", catalog) == ["WidgetA", "WidgetB"]
    assert catalog_match("SuperGizmo", catalog) == None
