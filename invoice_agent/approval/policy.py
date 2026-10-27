"""Hard approval rules. The policy gate decides which outcomes the LLM may choose; it has veto power."""

from invoice_agent import config
from invoice_agent.ingestion.normalize import format_money
from invoice_agent.models import FindingCode, PolicyMode, PolicyResult, ValidationReport

# Warnings that always need a human: no FX conversion, no price exceptions, no silent revisions.
HUMAN_REVIEW_CODES = {
    FindingCode.NON_USD_CURRENCY,
    FindingCode.PRICE_ABOVE_CATALOG,
    FindingCode.REVISED_INVOICE,
    FindingCode.VENDOR_CONFLICT,
}


def evaluate(report: ValidationReport) -> PolicyResult:
    if report.errors:
        return PolicyResult(mode=PolicyMode.REJECT, reasons=[f.message for f in report.errors])

    review = [f for f in report.warnings if f.code in HUMAN_REVIEW_CODES]
    if review:
        return PolicyResult(mode=PolicyMode.HUMAN_REVIEW, reasons=[f.message for f in review])

    reasons, checklist = [], []
    total = report.computed.payable_cents or 0
    if total > config.STRICT_REVIEW_THRESHOLD_CENTS:
        threshold = format_money(config.STRICT_REVIEW_THRESHOLD_CENTS)
        reasons.append(f"Total {format_money(total, report.currency)} is over {threshold}")
        checklist.append(
            f"Total over {threshold}: confirm every line's item, quantity, and price against the catalog and stock"
        )
    for finding in report.warnings:
        if finding.code == FindingCode.RISK_SIGNAL:
            reasons.append(f"Risk signal: {finding.message}")
            checklist.append(f"Address risk signal: {finding.message}")
    if reasons:
        return PolicyResult(mode=PolicyMode.STRICT, reasons=reasons, checklist=checklist)
    return PolicyResult(mode=PolicyMode.STANDARD, reasons=["All checks passed"])
