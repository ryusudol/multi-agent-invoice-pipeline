"""Duplicate detection on extracted content, so the same invoice in two formats is caught."""

import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from invoice_agent.ingestion.normalize import (
    cents,
    format_money,
    item_key,
    normalize_currency,
    normalize_invoice_number,
    normalize_vendor,
    parse_date,
    squash,
    to_decimal,
)
from invoice_agent.models import DuplicateVerdict, Finding, FindingCode, Invoice, InvoiceIdentity, Severity
from invoice_agent.validation.rules import compute_totals

LineKey = tuple[str, str, int | None]  # (item, quantity, unit price cents)


def _line_keys(invoice: Invoice) -> list[LineKey]:
    return [
        (
            item_key(item.description).lower(),
            str(to_decimal(item.quantity).normalize()) if item.quantity is not None else "",
            cents(item.unit_price),
        )
        for item in invoice.line_items
    ]


def _core(invoice: Invoice) -> dict[str, Any]:
    """Fields every format carries. Due date, address, notes, terms, and the tax line are left out."""
    invoice_date = parse_date(invoice.invoice_date)
    return {
        "number": normalize_invoice_number(invoice.invoice_number),
        "vendor": normalize_vendor(invoice.vendor),
        "date": invoice_date.isoformat() if invoice_date else squash(invoice.invoice_date or ""),
        "lines": sorted(_line_keys(invoice), key=repr),  # multiset: order ignored, repeats kept
        "total": compute_totals(invoice).payable_cents,
        "currency": normalize_currency(invoice.currency),
    }


def identify(invoice: Invoice) -> InvoiceIdentity | None:
    number = normalize_invoice_number(invoice.invoice_number)
    if number is None:
        return None
    vendor = normalize_vendor(invoice.vendor) or ""
    digest = hashlib.sha256(json.dumps(_core(invoice), sort_keys=True).encode()).hexdigest()
    return InvoiceIdentity(number=number, vendor=vendor, key=f"{number}/{vendor}", fingerprint=digest[:16])


def _describe(line: LineKey) -> str:
    item, quantity, price = line
    return f"{quantity} × {item} @ {format_money(price)}"


def line_diff(earlier: Invoice, current: Invoice) -> tuple[list[str], list[str]]:
    before, after = Counter(_line_keys(earlier)), Counter(_line_keys(current))
    added = [_describe(line) for line in (after - before).elements()]
    removed = [_describe(line) for line in (before - after).elements()]
    return added, removed


Row = Mapping[str, Any] | sqlite3.Row


def compare(
    identity: InvoiceIdentity | None, invoice: Invoice, earlier: Sequence[Row], load_invoice: Callable[[Row], Invoice]
) -> DuplicateVerdict:
    """Compare against earlier runs (newest first) with the same normalized invoice number.

    `earlier` rows need id, source_file, status, invoice_key, fingerprint, vendor.
    `load_invoice(row)` returns that run's Invoice.
    """
    if identity is None:
        return DuplicateVerdict(kind="no_number", message="No invoice number, so duplicate check skipped")
    if not earlier:
        return DuplicateVerdict(kind="new", message=f"{identity.number} has not been seen before")

    same_key = [row for row in earlier if row["invoice_key"] == identity.key]
    for row in same_key:
        if row["fingerprint"] == identity.fingerprint:
            return DuplicateVerdict(
                kind="identical",
                earlier_run_id=row["id"],
                earlier_status=row["status"],
                message=f"Identical to run #{row['id']} ({row['source_file']}), which is {row['status']}",
            )

    if same_key:
        row = same_key[0]
        previous = load_invoice(row)
        added, removed = line_diff(previous, invoice)
        before_total = compute_totals(previous).payable_cents or 0
        after_total = compute_totals(invoice).payable_cents or 0
        change = after_total - before_total
        currency = normalize_currency(invoice.currency)
        parts = []
        if added:
            parts.append("added " + ", ".join(added))
        if removed:
            parts.append("removed " + ", ".join(removed))
        sign = "+" if change >= 0 else "-"
        parts.append(f"total {sign}{format_money(abs(change), currency)}")
        message = f"Same number as run #{row['id']} ({row['status']}) with different content: " + "; ".join(parts)
        return DuplicateVerdict(
            kind="revised",
            earlier_run_id=row["id"],
            earlier_status=row["status"],
            message=message,
            findings=[
                Finding(
                    code=FindingCode.REVISED_INVOICE,
                    severity=Severity.WARNING,
                    message=message,
                    data={
                        "earlier_run_id": row["id"],
                        "added": added,
                        "removed": removed,
                        "total_change_cents": change,
                    },
                )
            ],
        )

    row = earlier[0]
    message = (
        f"{identity.number} was already used by vendor '{row['vendor']}' (run #{row['id']}, {row['status']}); "
        f"this invoice is from '{invoice.vendor}'"
    )
    return DuplicateVerdict(
        kind="vendor_conflict",
        earlier_run_id=row["id"],
        earlier_status=row["status"],
        message=message,
        findings=[
            Finding(
                code=FindingCode.VENDOR_CONFLICT,
                severity=Severity.WARNING,
                message=message,
                data={"earlier_run_id": row["id"], "earlier_vendor": row["vendor"]},
            )
        ],
    )
