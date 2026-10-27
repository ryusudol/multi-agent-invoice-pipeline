"""Checks an extraction against its source document. Failures trigger an Intake retry.

Only extraction errors are caught here: values that are not in the document (hallucinations)
and dates that can't be parsed. Real errors in the document, like a wrong total, pass through
for Validation to catch. Null fields are never a failure.
"""

from invoice_agent.ingestion.normalize import normalize_currency, numbers_in, parse_date, squash, to_decimal
from invoice_agent.models import Invoice


def check_extraction(invoice: Invoice, source: str) -> list[str]:
    """Return problems as short sentences the model can act on. Empty means the extraction passes."""
    source_text = squash(source)
    source_numbers = numbers_in(source)
    problems: list[str] = []

    def text_missing(label: str, value: str | None) -> None:
        if value is not None and squash(value) not in source_text:
            problems.append(f"{label} {value!r} does not appear in the document")

    def number_missing(label: str, value: float | None) -> None:
        if value is not None and abs(to_decimal(value)) not in source_numbers:
            problems.append(f"{label} {value} does not appear in the document")

    text_missing("invoice_number", invoice.invoice_number)
    text_missing("vendor", invoice.vendor)
    if invoice.invoice_date is not None and parse_date(invoice.invoice_date) is None:
        problems.append(f"invoice_date {invoice.invoice_date!r} is not a date; copy the date exactly as written")
    currency = normalize_currency(invoice.currency)
    if invoice.currency and currency != "USD" and squash(currency) not in source_text and invoice.currency not in source:
        problems.append(f"currency {invoice.currency!r} does not appear in the document")

    for n, item in enumerate(invoice.line_items, start=1):
        text_missing(f"line {n} description", item.description)
        number_missing(f"line {n} quantity", item.quantity)
        number_missing(f"line {n} unit_price", item.unit_price)
        number_missing(f"line {n} stated_amount", item.stated_amount)

    number_missing("stated_subtotal", invoice.stated_subtotal)
    number_missing("stated_tax_amount", invoice.stated_tax_amount)
    number_missing("stated_shipping", invoice.stated_shipping)
    number_missing("stated_total", invoice.stated_total)
    if invoice.stated_tax_rate is not None:
        rate = abs(to_decimal(invoice.stated_tax_rate))
        if rate not in source_numbers and rate * 100 not in source_numbers:
            problems.append(
                f"stated_tax_rate {invoice.stated_tax_rate} is not written in the document; use null unless a rate is written"
            )
    return problems


def retry_message(problems: list[str]) -> str:
    return (
        "Your extraction has values that are not in the source document:\n- "
        + "\n- ".join(problems)
        + "\nCopy values exactly as written. Use null for anything the document does not state; do not compute values."
    )

