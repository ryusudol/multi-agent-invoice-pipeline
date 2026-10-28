from invoice_agent.models import FindingCode, RiskSignal, Severity
from invoice_agent.validation.rules import match_lines, run_rules

from .conftest import line, make_invoice


def findings_for(invoice, catalog):
    findings, totals = run_rules(invoice, match_lines(invoice, catalog), catalog)
    return findings, totals


def codes(findings):
    return [f.code for f in findings]


def test_clean_invoice_has_no_findings(catalog):
    findings, totals = findings_for(make_invoice(), catalog)
    assert findings == []
    assert totals.payable_cents == 500000


def test_required_fields_and_invalid_quantity(catalog):
    # 1009: empty vendor, quantity -5, subtotal and total don't match.
    invoice = make_invoice(
        vendor=None,
        line_items=[line("WidgetA", -5, 250.0), line("WidgetB", 2, 500.0)],
        stated_subtotal=1000.0,
        stated_total=-250.0,
    )
    findings, _ = findings_for(invoice, catalog)
    assert FindingCode.MISSING_FIELD in codes(findings)
    assert FindingCode.INVALID_QUANTITY in codes(findings)
    subtotal = next(f for f in findings if f.code == FindingCode.ARITHMETIC)
    assert "Subtotal overstated by $1,250.00" in subtotal.message
    assert all(f.severity == Severity.ERROR for f in findings)


def test_no_line_items_is_an_error(catalog):
    findings, _ = findings_for(make_invoice(line_items=[], stated_subtotal=None, stated_total=None), catalog)
    assert codes(findings) == [FindingCode.MISSING_FIELD]


def test_unknown_item(catalog):
    invoice = make_invoice(line_items=[line("WidgetC", 3, 350.0)], stated_subtotal=None, stated_tax_amount=None, stated_total=1050.0)
    findings, _ = findings_for(invoice, catalog)
    assert codes(findings) == [FindingCode.UNKNOWN_ITEM]
    assert "did you mean WidgetA or WidgetB?" in findings[0].message


def test_stock_is_summed_per_item(catalog):
    # 1013: WidgetA 15 + 5 + 2 = 22 > 15, even though no single line exceeds stock.
    invoice = make_invoice(
        line_items=[line("WidgetA", 15, 250.0), line("WidgetA", 5, 240.0), line("WidgetA", 2, 250.0)],
        stated_subtotal=None,
        stated_tax_amount=None,
        stated_total=None,
    )
    findings, _ = findings_for(invoice, catalog)
    assert codes(findings) == [FindingCode.INSUFFICIENT_STOCK]
    assert findings[0].data == {"sku": "WidgetA", "requested": 22, "stock": 15}


def test_quantity_equal_to_stock_passes(catalog):
    # 1004_revised: 5 GadgetX, stock exactly 5.
    invoice = make_invoice(line_items=[line("GadgetX", 5, 750.0)], stated_subtotal=3750.0, stated_total=3750.0)
    assert findings_for(invoice, catalog)[0] == []


def test_zero_stock_is_out_of_stock_not_mismatch(catalog):
    invoice = make_invoice(line_items=[line("FakeItem", 100, 1000.0)], stated_subtotal=None, stated_tax_amount=None, stated_total=100000.0)
    findings, _ = findings_for(invoice, catalog)
    assert codes(findings) == [FindingCode.OUT_OF_STOCK]  # FakeItem has no catalog price: no price check


def test_total_understated(catalog):
    # 1007: 14,750 + 6% tax 885 = 15,635, stated 15,525.
    invoice = make_invoice(
        line_items=[line("WidgetA", 10, 250.0), line("WidgetB", 5, 500.0), line("GadgetX", 3, 750.0)],
        stated_subtotal=7250.0,
        stated_tax_rate=0.06,
        stated_tax_amount=435.0,
        stated_total=7575.0,
    )
    findings, totals = findings_for(invoice, catalog)
    assert totals.total_cents == 768500
    assert [f.message for f in findings] == ["Total understated by $110.00: stated $7,575.00, computed $7,685.00"]


def test_total_overbilled_with_tax_rate(catalog):
    # 1013 numbers: subtotal 21,040, 7% tax 1,472.80, grand total stated +$50.
    invoice = make_invoice(
        line_items=[line("WidgetA", 84.16, 250.0)],
        stated_subtotal=21040.0,
        stated_tax_rate=0.07,
        stated_tax_amount=1472.80,
        stated_total=22562.80,
    )
    findings, _ = findings_for(invoice, catalog)
    arithmetic = [f for f in findings if f.code == FindingCode.ARITHMETIC]
    assert [f.message for f in arithmetic] == ["Total overstated by $50.00: stated $22,562.80, computed $22,512.80"]


def test_tax_and_shipping_without_rate(catalog):
    # 1010: Sales Tax with no rate and Shipping; arithmetic is correct.
    invoice = make_invoice(
        line_items=[line("WidgetA", 8, 250.0), line("WidgetB", 4, 500.0), line("GadgetX", 2, 750.0), line("WidgetA (rush order)", 4, 300.0)],
        stated_subtotal=6700.0,
        stated_tax_rate=None,
        stated_tax_amount=335.0,
        stated_shipping=150.0,
        stated_total=7185.0,
    )
    findings, totals = findings_for(invoice, catalog)
    assert totals.total_cents == 718500
    assert codes(findings) == [FindingCode.PRICE_ABOVE_CATALOG]
    assert findings[0].data["overcharge_cents"] == 20000
    assert findings[0].line == 4


def test_line_amount_mismatch(catalog):
    invoice = make_invoice(line_items=[line("WidgetA", 10, 250.0, stated_amount=2600.0), line("WidgetB", 5, 500.0)])
    findings, _ = findings_for(invoice, catalog)
    assert [f.message for f in findings] == ["WidgetA amount overstated by $100.00: stated $2,600.00, computed $2,500.00"]


def test_price_below_catalog_is_fine(catalog):
    invoice = make_invoice(line_items=[line("WidgetA", 5, 240.0)], stated_subtotal=1200.0, stated_total=1200.0)
    assert findings_for(invoice, catalog)[0] == []


def test_price_check_skipped_for_non_usd(catalog):
    invoice = make_invoice(currency="EUR", line_items=[line("WidgetA", 4, 300.0)], stated_subtotal=1200.0, stated_total=1200.0)
    findings, _ = findings_for(invoice, catalog)
    assert codes(findings) == [FindingCode.NON_USD_CURRENCY]
    assert findings[0].severity == Severity.WARNING


def test_missing_currency_and_dollar_sign_mean_usd(catalog):
    assert findings_for(make_invoice(currency=None), catalog)[0] == []
    assert findings_for(make_invoice(currency="$"), catalog)[0] == []


def test_risk_signals_are_warnings(catalog):
    invoice = make_invoice(risk_signals=[RiskSignal(description="Pressure to pay now", quote="URGENT")])
    findings, _ = findings_for(invoice, catalog)
    assert codes(findings) == [FindingCode.RISK_SIGNAL]
    assert findings[0].severity == Severity.WARNING
