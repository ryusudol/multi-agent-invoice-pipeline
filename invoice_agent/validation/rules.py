"""Deterministic validation rules. Code decides; the Validation agent's matches are only a proposal."""

import difflib
import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from invoice_agent import config
from invoice_agent.ingestion.normalize import (
    cents,
    format_money,
    item_key,
    normalize_currency,
    split_parenthetical,
    to_decimal,
)
from invoice_agent.models import ComputedTotals, Finding, FindingCode, Invoice, LineCheck, Severity

Catalog = Mapping[str, Mapping[str, Any] | sqlite3.Row]  # sku -> {"stock", "unit_price"}


def catalog_match(description: str, catalog: Catalog) -> tuple[str | None, str | None]:
    """Exact match after removing spaces, then again after stripping a trailing parenthetical.

    Returns (sku or None, stripped note). Similar names never match: WidgetC stays unknown.
    """
    if item_key(description) in catalog:
        return item_key(description), None
    name, note = split_parenthetical(description)
    if note and item_key(name) in catalog:
        return item_key(name), note
    return None, note


def suggest(description: str, catalog: Catalog) -> list[str]:
    """Similar catalog names, for a "did you mean" hint only."""
    name, _ = split_parenthetical(description)
    return sorted(difflib.get_close_matches(item_key(name), list(catalog), n=3, cutoff=0.8))


def match_lines(invoice: Invoice, catalog: Catalog) -> list[LineCheck]:
    checks = []
    for n, item in enumerate(invoice.line_items, start=1):
        sku, note = catalog_match(item.description, catalog)
        checks.append(LineCheck(line=n, description=item.description, sku=sku, note=note))
    return checks


def _line_cents(quantity: float | None, unit_price: float | None) -> int | None:
    if quantity is None or unit_price is None:
        return None
    return int((to_decimal(quantity) * to_decimal(unit_price) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def compute_totals(invoice: Invoice) -> ComputedTotals:
    line_amounts = [_line_cents(item.quantity, item.unit_price) for item in invoice.line_items]
    complete = bool(line_amounts) and all(amount is not None for amount in line_amounts)
    subtotal = sum(a for a in line_amounts if a is not None) if complete else None
    tax: int | None = None
    if invoice.stated_tax_amount is not None:
        tax = cents(invoice.stated_tax_amount)
    elif invoice.stated_tax_rate is not None and subtotal is not None:
        tax = int((Decimal(subtotal) * to_decimal(invoice.stated_tax_rate)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    total = None
    if subtotal is not None:
        total = subtotal + (tax or 0) + (cents(invoice.stated_shipping) or 0)
    stated_total = cents(invoice.stated_total)
    return ComputedTotals(
        line_amounts_cents=line_amounts,
        subtotal_cents=subtotal,
        tax_cents=tax,
        total_cents=total,
        payable_cents=stated_total if stated_total is not None else total,
    )


def _differs(stated: int | None, computed: int | None) -> bool:
    return stated is not None and computed is not None and abs(stated - computed) > config.MONEY_TOLERANCE_CENTS


def _error(code: FindingCode, message: str, line: int | None = None, **data: Any) -> Finding:
    return Finding(code=code, severity=Severity.ERROR, message=message, line=line, data=data)


def _warning(code: FindingCode, message: str, line: int | None = None, **data: Any) -> Finding:
    return Finding(code=code, severity=Severity.WARNING, message=message, line=line, data=data)


def check_required(invoice: Invoice) -> list[Finding]:
    findings = []
    if not invoice.vendor:
        findings.append(_error(FindingCode.MISSING_FIELD, "Vendor name is missing", field="vendor"))
    if not invoice.invoice_number:
        findings.append(_error(FindingCode.MISSING_FIELD, "Invoice number is missing", field="invoice_number"))
    if not invoice.line_items:
        findings.append(_error(FindingCode.MISSING_FIELD, "Invoice has no line items", field="line_items"))
    return findings


def check_quantities_and_prices(invoice: Invoice) -> list[Finding]:
    findings = []
    for n, item in enumerate(invoice.line_items, start=1):
        if item.quantity is None:
            findings.append(_error(FindingCode.MISSING_FIELD, f"Line {n} '{item.description}' has no quantity", n))
        elif item.quantity <= 0:
            findings.append(
                _error(
                    FindingCode.INVALID_QUANTITY,
                    f"Line {n} '{item.description}' has invalid quantity {item.quantity:g}",
                    n,
                    quantity=item.quantity,
                )
            )
        if item.unit_price is None:
            findings.append(_error(FindingCode.MISSING_FIELD, f"Line {n} '{item.description}' has no unit price", n))
        elif item.unit_price < 0:
            findings.append(
                _error(FindingCode.INVALID_PRICE, f"Line {n} '{item.description}' has negative unit price", n)
            )
    return findings


def check_items_and_stock(lines: list[LineCheck], invoice: Invoice, catalog: Catalog) -> list[Finding]:
    findings = []
    requested: dict[str, float] = defaultdict(float)
    for check, item in zip(lines, invoice.line_items, strict=True):
        if check.sku is None:
            hints = suggest(check.description, catalog)
            message = f"Line {check.line} '{check.description}' is not in inventory"
            if hints:
                message += f" (did you mean {' or '.join(hints)}?)"
            findings.append(_error(FindingCode.UNKNOWN_ITEM, message, check.line, suggestions=hints))
        elif item.quantity is not None:
            requested[check.sku] += item.quantity
    for sku, quantity in requested.items():
        stock = int(catalog[sku]["stock"])
        if stock == 0:
            findings.append(
                _error(FindingCode.OUT_OF_STOCK, f"{sku} is out of stock ({quantity:g} requested)", sku=sku, requested=quantity, stock=0)
            )
        elif quantity > stock:
            findings.append(
                _error(
                    FindingCode.INSUFFICIENT_STOCK,
                    f"{sku}: {quantity:g} requested across the invoice, only {stock} in stock",
                    sku=sku,
                    requested=quantity,
                    stock=stock,
                )
            )
    return findings


def check_arithmetic(invoice: Invoice, totals: ComputedTotals, currency: str) -> list[Finding]:
    findings = []

    def mismatch(label: str, stated: int | None, computed: int | None, line: int | None = None) -> None:
        if not _differs(stated, computed):
            return
        assert stated is not None and computed is not None
        diff = stated - computed
        direction = "overstated" if diff > 0 else "understated"
        findings.append(
            _error(
                FindingCode.ARITHMETIC,
                f"{label} {direction} by {format_money(abs(diff), currency)}: "
                f"stated {format_money(stated, currency)}, computed {format_money(computed, currency)}",
                line,
                field=label.lower(),
                stated_cents=stated,
                computed_cents=computed,
            )
        )

    for n, (item, computed) in enumerate(zip(invoice.line_items, totals.line_amounts_cents, strict=True), start=1):
        mismatch(f"Line {n} amount", cents(item.stated_amount), computed, n)
    mismatch("Subtotal", cents(invoice.stated_subtotal), totals.subtotal_cents)
    if invoice.stated_tax_rate is not None and invoice.stated_tax_amount is not None and totals.subtotal_cents is not None:
        expected = int(
            (Decimal(totals.subtotal_cents) * to_decimal(invoice.stated_tax_rate)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        mismatch("Tax", cents(invoice.stated_tax_amount), expected)
    mismatch("Total", cents(invoice.stated_total), totals.total_cents)
    return findings


def check_prices(lines: list[LineCheck], invoice: Invoice, catalog: Catalog, currency: str) -> list[Finding]:
    """Above-catalog prices go to human review. Only for USD invoices and items with a catalog price."""
    if currency != "USD":
        return []
    findings = []
    for check, item in zip(lines, invoice.line_items, strict=True):
        if check.sku is None or item.unit_price is None:
            continue
        catalog_price = catalog[check.sku]["unit_price"]
        if catalog_price is None:
            continue
        invoice_cents, catalog_cents = cents(item.unit_price), cents(catalog_price)
        assert invoice_cents is not None and catalog_cents is not None
        if invoice_cents > catalog_cents:
            quantity = item.quantity if item.quantity and item.quantity > 0 else 0
            over = int((invoice_cents - catalog_cents) * to_decimal(quantity))
            findings.append(
                _warning(
                    FindingCode.PRICE_ABOVE_CATALOG,
                    f"Line {check.line} '{check.description}': {format_money(invoice_cents)} vs catalog "
                    f"{format_money(catalog_cents)} (+{format_money(over)} on {quantity:g} units)",
                    check.line,
                    sku=check.sku,
                    invoice_price_cents=invoice_cents,
                    catalog_price_cents=catalog_cents,
                    overcharge_cents=over,
                )
            )
    return findings


def check_currency(invoice: Invoice) -> list[Finding]:
    currency = normalize_currency(invoice.currency)
    if currency == "USD":
        return []
    return [
        _warning(
            FindingCode.NON_USD_CURRENCY,
            f"Invoice is in {currency}, not USD; no FX conversion is done",
            currency=currency,
        )
    ]


def risk_findings(invoice: Invoice) -> list[Finding]:
    return [
        _warning(FindingCode.RISK_SIGNAL, f"{signal.description} (“{signal.quote}”)", quote=signal.quote)
        for signal in invoice.risk_signals
    ]


def run_rules(invoice: Invoice, lines: list[LineCheck], catalog: Catalog) -> tuple[list[Finding], ComputedTotals]:
    currency = normalize_currency(invoice.currency)
    totals = compute_totals(invoice)
    findings = [
        *check_required(invoice),
        *check_quantities_and_prices(invoice),
        *check_items_and_stock(lines, invoice, catalog),
        *check_arithmetic(invoice, totals, currency),
        *check_prices(lines, invoice, catalog, currency),
        *check_currency(invoice),
        *risk_findings(invoice),
    ]
    return findings, totals
