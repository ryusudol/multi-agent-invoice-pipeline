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


def _describe(line: LineKey, names: Mapping[str, str]) -> str:
    item, quantity, price = line
    return f"{quantity} × {names.get(item, item)} @ {format_money(price)}"


def line_diff(earlier: Invoice, current: Invoice) -> tuple[list[str], list[str]]:
    before, after = Counter(_line_keys(earlier)), Counter(_line_keys(current))
    # Match on lowercased keys; show the invoice's spelling ("GadgetX", not "gadgetx").
    names = {item_key(i.description).lower(): item_key(i.description) for i in [*earlier.line_items, *current.line_items]}
    added = [_describe(line, names) for line in (after - before).elements()]
    removed = [_describe(line, names) for line in (before - after).elements()]
    return added, removed


EARLIER_STATUS = {
    "paid": "was paid",
    "rejected": "was rejected",
    "needs_review": "is waiting for review",
    "processing": "is still being processed",
}


def _revision_message(number: str, row: Row, before: int, after: int, added: list[str], removed: list[str], currency: str) -> str:
    """'Revision of INV-1004: run #6 ($1,890.00) was paid. This version adds 5 × GadgetX @ $750.00, so the total rises by $4,050.00 to $5,940.00.'"""
    status = EARLIER_STATUS.get(row["status"], f"is {row['status']}")
    changes = []
    if added:
        changes.append("adds " + ", ".join(added))
    if removed:
        changes.append("removes " + ", ".join(removed))
    what = " and ".join(changes) if changes else "changes other details"
    change = after - before
    if change > 0:
        total = f"the total rises by {format_money(change, currency)} to {format_money(after, currency)}"
    elif change < 0:
        total = f"the total drops by {format_money(-change, currency)} to {format_money(after, currency)}"
    else:
        total = f"the total stays at {format_money(after, currency)}"
    return (
        f"Revision of {number}: run #{row['id']} ({format_money(before, currency)}) {status}. "
        f"This version {what}, so {total}."
    )


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
        message = _revision_message(identity.number, row, before_total, after_total, added, removed, currency)
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
