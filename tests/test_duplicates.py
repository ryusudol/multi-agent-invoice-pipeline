from invoice_agent.duplicates import compare, identify
from invoice_agent.models import FindingCode

from .conftest import line, make_invoice

INV_1011_TXT = make_invoice(
    invoice_number="INV-1011",
    vendor="Summit Manufacturing Co.",
    invoice_date="2026-01-20",
    line_items=[line("WidgetA", 6, 250.0, stated_amount=1500.0), line("WidgetB", 3, 500.0, stated_amount=1500.0)],
    stated_subtotal=3000.0,
    stated_tax_rate=0.0,
    stated_tax_amount=0.0,
    stated_total=3000.0,
)
# The PDF has no subtotal or tax line.
INV_1011_PDF = INV_1011_TXT.model_copy(update={"stated_subtotal": None, "stated_tax_rate": None, "stated_tax_amount": None})

INV_1004 = make_invoice(
    invoice_number="INV-1004",
    vendor="Precision Parts Ltd.",
    invoice_date="2026-01-22",
    currency="USD",
    line_items=[line("WidgetA", 3, 250.0), line("WidgetB", 2, 500.0)],
    stated_subtotal=1750.0,
    stated_tax_rate=0.08,
    stated_tax_amount=140.0,
    stated_total=1890.0,
)
INV_1004_REVISED = INV_1004.model_copy(
    update={
        "line_items": [*INV_1004.line_items, line("GadgetX", 5, 750.0)],
        "stated_subtotal": 5500.0,
        "stated_tax_amount": 440.0,
        "stated_total": 5940.0,
        "revision": "R1",
    }
)

_1013_LINES = [(15, 250.0, "WidgetA"), (10, 500.0, "WidgetB"), (5, 750.0, "GadgetX"), (5, 240.0, "WidgetA"), (8, 480.0, "WidgetB"), (3, 750.0, "GadgetX"), (2, 250.0, "WidgetA"), (1, 750.0, "GadgetX")]
INV_1013_JSON = make_invoice(
    invoice_number="INV-1013",
    vendor="Atlas Industrial Supply",
    invoice_date="2026-01-24",
    currency="USD",
    line_items=[line(item, qty, price, stated_amount=qty * price) for qty, price, item in _1013_LINES],
    stated_subtotal=21040.0,
    stated_tax_rate=0.07,
    stated_tax_amount=1472.80,
    stated_total=22562.80,
)
# The PDF states no currency, and carries per-line notes.
INV_1013_PDF = INV_1013_JSON.model_copy(update={"currency": None, "line_items": [li.model_copy(update={"note": "Sample"}) for li in INV_1013_JSON.line_items]})


def row(run_id, invoice, status="paid", source_file="earlier"):
    identity = identify(invoice)
    return {
        "id": run_id,
        "status": status,
        "source_file": source_file,
        "invoice_key": identity.key,
        "fingerprint": identity.fingerprint,
        "vendor": invoice.vendor,
        "invoice": invoice,
    }


def verdict(invoice, *earlier):
    return compare(identify(invoice), invoice, list(earlier), lambda r: r["invoice"])


def test_same_invoice_in_txt_and_pdf_is_identical():
    assert identify(INV_1011_TXT).fingerprint == identify(INV_1011_PDF).fingerprint
    result = verdict(INV_1011_TXT, row(1, INV_1011_PDF))
    assert result.kind == "identical"
    assert result.earlier_run_id == 1


def test_currency_default_makes_1013_json_and_pdf_identical():
    result = verdict(INV_1013_PDF, row(7, INV_1013_JSON, status="rejected"))
    assert result.kind == "identical"
    assert result.earlier_status == "rejected"


def test_repeated_lines_are_not_merged():
    merged = INV_1013_JSON.model_copy(update={"line_items": [line("WidgetA", 22, 250.0)]})
    assert identify(merged).fingerprint != identify(INV_1013_JSON).fingerprint


def test_revision_is_same_number_different_content():
    result = verdict(INV_1004_REVISED, row(4, INV_1004))
    assert result.kind == "revised"
    assert "added 5 × gadgetx @ $750.00" in result.message
    assert "total +$4,050.00" in result.message
    [finding] = result.findings
    assert finding.code == FindingCode.REVISED_INVOICE
    assert finding.data["total_change_cents"] == 405000


def test_same_number_different_vendor():
    other = INV_1004.model_copy(update={"vendor": "Someone Else LLC"})
    result = verdict(other, row(4, INV_1004))
    assert result.kind == "vendor_conflict"
    assert "Precision Parts Ltd." in result.message
    assert result.findings[0].code == FindingCode.VENDOR_CONFLICT


def test_new_and_missing_number():
    assert verdict(INV_1004).kind == "new"
    no_number = INV_1004.model_copy(update={"invoice_number": None})
    assert compare(identify(no_number), no_number, [], lambda r: r).kind == "no_number"


def test_vendor_parenthetical_ignored_in_key():
    a = make_invoice(vendor="QuickShip Distributers (formerly FastShip Ltd.)")
    b = make_invoice(vendor="QuickShip Distributers")
    assert identify(a).key == identify(b).key
