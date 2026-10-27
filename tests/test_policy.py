from invoice_agent.approval.policy import evaluate
from invoice_agent.models import (
    ComputedTotals,
    Finding,
    FindingCode,
    Outcome,
    PolicyMode,
    Severity,
    ValidationReport,
)


def report(total_cents=500000, *findings: tuple[FindingCode, Severity]) -> ValidationReport:
    return ValidationReport(
        lines=[],
        findings=[Finding(code=code, severity=severity, message=code.value) for code, severity in findings],
        computed=ComputedTotals(line_amounts_cents=[], subtotal_cents=total_cents, tax_cents=0, total_cents=total_cents, payable_cents=total_cents),
        currency="USD",
    )


def test_errors_reject_even_with_review_warnings():
    result = evaluate(
        report(
            500000,
            (FindingCode.PRICE_ABOVE_CATALOG, Severity.WARNING),
            (FindingCode.INSUFFICIENT_STOCK, Severity.ERROR),
        )
    )
    assert result.mode == PolicyMode.REJECT
    assert result.fixed_outcome == Outcome.REJECT
    assert result.reasons == ["insufficient_stock"]


def test_review_codes_need_a_human():
    for code in (FindingCode.NON_USD_CURRENCY, FindingCode.PRICE_ABOVE_CATALOG, FindingCode.REVISED_INVOICE, FindingCode.VENDOR_CONFLICT):
        result = evaluate(report(500000, (code, Severity.WARNING)))
        assert result.mode == PolicyMode.HUMAN_REVIEW
        assert result.fixed_outcome == Outcome.NEEDS_REVIEW


def test_human_review_wins_over_strict():
    result = evaluate(report(1_500_000, (FindingCode.NON_USD_CURRENCY, Severity.WARNING)))
    assert result.mode == PolicyMode.HUMAN_REVIEW


def test_over_10k_is_strict_with_checklist():
    result = evaluate(report(1_250_000))
    assert result.mode == PolicyMode.STRICT
    assert result.fixed_outcome is None
    assert "over $10,000.00" in result.reasons[0]
    assert len(result.checklist) == 1


def test_exactly_10k_is_standard():
    assert evaluate(report(1_000_000)).mode == PolicyMode.STANDARD


def test_risk_signal_is_strict():
    result = evaluate(report(500000, (FindingCode.RISK_SIGNAL, Severity.WARNING)))
    assert result.mode == PolicyMode.STRICT
    assert result.checklist == ["Address risk signal: risk_signal"]


def test_clean_is_standard():
    assert evaluate(report()).mode == PolicyMode.STANDARD
