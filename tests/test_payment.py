import pytest

from invoice_agent import db, payment
from invoice_agent.payment import amount_due, pay

KEY = "INV-1004/precision parts ltd"


def pay_1004(total_cents: int, run_id: int = 1):
    return pay(
        run_id=run_id,
        invoice_number="INV-1004",
        invoice_key=KEY,
        vendor="Precision Parts Ltd.",
        approved_total_cents=total_cents,
        currency="USD",
    )


@pytest.fixture
def bank(monkeypatch):
    """Records every call to the mock banking API."""
    calls = []
    real = payment.mock_payment
    monkeypatch.setattr(payment, "mock_payment", lambda *args: calls.append(args) or real(*args))
    return calls


def test_pays(bank):
    result = pay_1004(189000)
    assert result.status == "paid"
    assert result.amount_cents == 189000
    assert result.idempotency_key == f"{KEY}:delta:0.00->1890.00"
    assert bank == [("Precision Parts Ltd.", 1890.0, "USD", result.idempotency_key)]


def test_same_approval_pays_once(bank):
    first = pay_1004(189000)
    second = pay_1004(189000, run_id=2)
    assert second.status == "already_paid"
    assert second.payment_id == first.payment_id
    assert len(bank) == 1
    assert db.paid_cents(KEY) == 189000


def test_revision_pays_only_the_difference():
    pay_1004(189000)
    revised = pay_1004(594000, run_id=2)
    assert revised.status == "paid"
    assert revised.amount_cents == 405000
    assert revised.previously_paid_cents == 189000
    assert revised.idempotency_key == f"{KEY}:delta:1890.00->5940.00"
    assert db.paid_cents(KEY) == 594000


def test_lower_total_after_payment_pays_nothing(bank):
    pay_1004(594000)
    result = pay_1004(189000, run_id=2)
    assert result.status == "nothing_due"
    assert result.amount_cents == 0
    assert len(bank) == 1


def test_amount_due_matches_what_pay_sends():
    assert amount_due(KEY, 189000) == 189000
    pay_1004(189000)
    assert amount_due(KEY, 189000) == 0
    assert amount_due(KEY, 594000) == 405000
    assert pay_1004(594000, run_id=2).amount_cents == 405000
    assert amount_due(KEY, 594000) == 0
