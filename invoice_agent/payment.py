"""Mock payment with duplicate-payment prevention.

Pays the approved total minus what was already paid for the same invoice key, once per
idempotency key `{invoice key}:delta:{previously paid}->{approved total}`.
"""

from invoice_agent import db
from invoice_agent.events import now_iso
from invoice_agent.models import PaymentResult


def mock_payment(vendor: str, amount: float, currency: str = "USD", idempotency_key: str | None = None) -> dict:
    """A mock banking API that takes a currency and an idempotency key. It doesn't print; the Payment
    stage line reports each payment."""
    return {"status": "success", "currency": currency, "idempotency_key": idempotency_key}


def _amount(amount_cents: int) -> str:
    return f"{amount_cents / 100:.2f}"


def amount_due(invoice_key: str, approved_total_cents: int) -> int:
    """What pay() would send now: 0 if this total was already reached, else the unpaid difference."""
    if db.payment_for_total(invoice_key, approved_total_cents) is not None:
        return 0
    return max(approved_total_cents - db.paid_cents(invoice_key), 0)


def pay(
    *, run_id: int, invoice_number: str, invoice_key: str, vendor: str, approved_total_cents: int, currency: str
) -> PaymentResult:
    def result(status, amount_cents, previously_paid, key, payment_id=None) -> PaymentResult:
        return PaymentResult(
            status=status,
            amount_cents=amount_cents,
            currency=currency,
            vendor=vendor,
            approved_total_cents=approved_total_cents,
            previously_paid_cents=previously_paid,
            idempotency_key=key,
            payment_id=payment_id,
        )

    existing = db.payment_for_total(invoice_key, approved_total_cents)
    if existing is not None:
        previously = approved_total_cents - existing["amount_cents"]
        return result("already_paid", existing["amount_cents"], previously, existing["idempotency_key"], existing["id"])

    previously_paid = db.paid_cents(invoice_key)
    key = f"{invoice_key}:delta:{_amount(previously_paid)}->{_amount(approved_total_cents)}"
    delta = approved_total_cents - previously_paid
    if delta <= 0:
        return result("nothing_due", 0, previously_paid, key)

    payment_id = db.insert_payment(
        run_id=run_id,
        invoice_number=invoice_number,
        invoice_key=invoice_key,
        vendor=vendor,
        amount_cents=delta,
        currency=currency,
        approved_total_cents=approved_total_cents,
        idempotency_key=key,
        paid_at=now_iso(),
    )
    if payment_id is None:  # another process paid this exact delta first
        row = db.get_payment_by_key(key)
        assert row is not None
        return result("already_paid", row["amount_cents"], previously_paid, key, row["id"])

    mock_payment(vendor, delta / 100, currency, key)
    return result("paid", delta, previously_paid, key, payment_id)
