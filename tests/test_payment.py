from invoice_agent import db
from invoice_agent.payment import pay

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


def test_pays_and_prints_readme_format(capsys):
    result = pay_1004(189000)
    assert result.status == "paid"
    assert result.amount_cents == 189000
    assert result.idempotency_key == f"{KEY}:delta:0.00->1890.00"
    assert capsys.readouterr().out == "Paid 1890.0 to Precision Parts Ltd.\n"


def test_same_approval_pays_once(capsys):
    first = pay_1004(189000)
    second = pay_1004(189000, run_id=2)
    assert second.status == "already_paid"
    assert second.payment_id == first.payment_id
    assert capsys.readouterr().out.count("Paid") == 1
    assert db.paid_cents(KEY) == 189000


def test_revision_pays_only_the_difference():
    pay_1004(189000)
    revised = pay_1004(594000, run_id=2)
    assert revised.status == "paid"
    assert revised.amount_cents == 405000
    assert revised.previously_paid_cents == 189000
    assert revised.idempotency_key == f"{KEY}:delta:1890.00->5940.00"
    assert db.paid_cents(KEY) == 594000


def test_revision_alone_pays_in_full():
    assert pay_1004(594000).amount_cents == 594000


def test_lower_total_after_payment_pays_nothing(capsys):
    pay_1004(594000)
    capsys.readouterr()
    result = pay_1004(189000, run_id=2)
    assert result.status == "nothing_due"
    assert result.amount_cents == 0
    assert capsys.readouterr().out == ""
