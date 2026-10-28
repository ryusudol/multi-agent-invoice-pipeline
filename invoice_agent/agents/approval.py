"""VP Approver and Critic agents, and the critique loop between them.

The policy gate fixes or limits the outcome before either agent runs. Code enforces that limit on
every draft, so the LLM can make an outcome stricter (escalate to a human) but never looser.
"""

from typing import Literal

from pydantic_ai import Agent

from invoice_agent import config
from invoice_agent.agents.common import MODEL, AgentDeps, get_invoice_history, lookup_item, run_agent
from invoice_agent.events import EventType
from invoice_agent.ingestion.normalize import cents, format_money
from invoice_agent.models import (
    ApprovalDraft,
    Critique,
    Decision,
    Invoice,
    Outcome,
    PolicyMode,
    PolicyResult,
    Usage,
    ValidationReport,
)

THRESHOLD = format_money(config.STRICT_REVIEW_THRESHOLD_CENTS)

VP_INSTRUCTIONS = f"""\
You are the VP of Finance at Acme Corp. Automated checks have already extracted and validated the invoice;
you write the approval decision and its rationale for the accounts-payable team.

Policy (enforced by code; you cannot override it):
- Any error-level finding: reject.
- Non-USD currency, a price above catalog, a revised invoice, or an invoice-number conflict: a human
  must review it (needs_review).
- Total over {THRESHOLD}, or risk signals: strict review. You choose approve, reject, or needs_review,
  and you must address every checklist item with evidence. Risk signals are warnings: one never blocks
  payment on its own. Escalate or reject only when the case file or your tool results corroborate it
  (for example, invoice history shows another vendor for this number, or the document asks to redirect
  payment); otherwise say why it does not change the decision.
- Otherwise: approve, unless you find a concrete problem in the evidence; then choose needs_review.

When the case file gives a fixed outcome, your decision must be that outcome and your job is a clear
rationale. Ground every statement in the case file or your tool results and cite specific findings and
amounts. Do not invent facts. At most 120 words.
"""

CRITIC_INSTRUCTIONS = """\
You are an independent internal auditor. You check a VP's invoice decision draft before it takes effect.

Check that:
1. Every factual claim in the rationale matches the case file (amounts, items, quantities, findings).
2. The decision is allowed by the policy shown in the case file and fits the evidence.
3. For strict review, every checklist item is addressed with evidence.
4. No material finding is ignored.

Risk signals are warnings: one blocks payment only when the evidence corroborates it. Object to an
approval that ignores a risk signal, and to an escalation that rests on an uncorroborated one.

Set agrees=true only if there is no material issue. Wording preferences are not material.
List each issue concretely so the VP can fix it.
"""

vp_agent = Agent(
    MODEL,
    name="vp",
    deps_type=AgentDeps,
    output_type=ApprovalDraft,
    instructions=VP_INSTRUCTIONS,
    tools=[lookup_item, get_invoice_history],
    defer_model_check=True,
)

critic_agent = Agent(
    MODEL,
    name="critic",
    deps_type=AgentDeps,
    output_type=Critique,
    instructions=CRITIC_INSTRUCTIONS,
    tools=[lookup_item, get_invoice_history],
    defer_model_check=True,
)


def _amount_rows(invoice: Invoice, report: ValidationReport) -> list[str]:
    """Stated vs computed for subtotal, tax, shipping, total, so the VP sees how the total is built."""
    currency, computed = report.currency, report.computed
    rate = f" (rate {invoice.stated_tax_rate:.2%})" if invoice.stated_tax_rate is not None else " (no rate stated)"
    rows = [
        ("Subtotal", cents(invoice.stated_subtotal), computed.subtotal_cents),
        (f"Tax{rate}", cents(invoice.stated_tax_amount), computed.tax_cents),
        ("Shipping", cents(invoice.stated_shipping), cents(invoice.stated_shipping)),
        ("Total", cents(invoice.stated_total), computed.total_cents),
    ]
    out = ["Amounts (stated on invoice / computed by code):"]
    for label, stated, calc in rows:
        if stated is None and (calc is None or label == "Shipping"):
            continue
        out.append(f"  {label}: {format_money(stated, currency)} / {format_money(calc, currency)}")
    return out


def case_file(invoice: Invoice, report: ValidationReport, policy: PolicyResult) -> str:
    currency = report.currency
    lines = []
    for check, item, amount in zip(report.lines, invoice.line_items, report.computed.line_amounts_cents, strict=True):
        quantity = f"{item.quantity:g}" if item.quantity is not None else "?"
        match = f"catalog {check.sku}" if check.sku else "not in catalog"
        lines.append(
            f"  {check.line}. {item.description} — {quantity} × {format_money(cents(item.unit_price), currency)}"
            f" = {format_money(amount, currency)} ({match})"
        )
    findings = [f"  - [{f.severity}] {f.message}" for f in report.findings] or ["  (none)"]
    if policy.fixed_outcome:
        allowed = f"fixed outcome: {policy.fixed_outcome}"
    elif policy.mode == PolicyMode.STRICT:
        allowed = "approve, reject, or needs_review"
    else:
        allowed = "approve, or needs_review if you find a concrete problem"
    parts = [
        "INVOICE (as stated on the document)",
        f"  Number: {invoice.invoice_number}   Vendor: {invoice.vendor}   Date: {invoice.invoice_date}"
        f"   Due: {invoice.due_date}   Currency: {currency}",
        "Lines:",
        *lines,
        *_amount_rows(invoice, report),
        "",
        "FINDINGS",
        *findings,
    ]
    if report.agent_summary:
        parts += ["", f"VALIDATION AGENT: {report.agent_summary}"]
    parts += [
        "",
        "POLICY",
        f"  Mode: {policy.mode} ({allowed})",
        *[f"  Reason: {reason}" for reason in policy.reasons],
        *[f"  Checklist: {item}" for item in policy.checklist],
    ]
    return "\n".join(parts)


def _draft_text(draft: ApprovalDraft) -> str:
    responses = "\n".join(f"  - {r}" for r in draft.checklist_responses) or "  (none)"
    return f"Decision: {draft.decision}\nRationale: {draft.rationale}\nChecklist responses:\n{responses}"


def _enforce(draft: ApprovalDraft, policy: PolicyResult, deps: AgentDeps) -> ApprovalDraft:
    """Code has the last word: fixed outcomes stay fixed, and a clean invoice can only be escalated."""
    allowed_outcome = policy.fixed_outcome
    if allowed_outcome is None and policy.mode == PolicyMode.STANDARD and draft.decision == Outcome.REJECT:
        allowed_outcome = Outcome.NEEDS_REVIEW
    if allowed_outcome is None or draft.decision == allowed_outcome:
        return draft
    deps.sink.emit(
        deps.stage,
        EventType.POLICY_OVERRIDE,
        f"VP drafted {draft.decision}; policy ({policy.mode}) sets {allowed_outcome}",
        drafted=draft.decision,
        enforced=allowed_outcome,
    )
    return draft.model_copy(update={"decision": allowed_outcome})


def decide(invoice: Invoice, report: ValidationReport, policy: PolicyResult, deps: AgentDeps, usage: Usage) -> Decision:
    case = case_file(invoice, report, policy)
    max_rounds = config.MAX_CRITIQUE_ROUNDS if policy.mode == PolicyMode.STRICT else 1

    draft = _enforce(run_agent(vp_agent, case, deps, usage), policy, deps)
    agreed, critique, rounds = False, None, 0
    for rounds in range(1, max_rounds + 1):
        critique = run_agent(critic_agent, f"{case}\n\nVP DRAFT\n{_draft_text(draft)}", deps, usage)
        agreed = critique.agrees
        deps.sink.emit(
            deps.stage,
            EventType.CRITIQUE,
            f"Round {rounds}: critic {'agrees' if agreed else 'objects'} — {critique.summary}",
            round=rounds,
            agrees=agreed,
            issues=critique.issues,
        )
        if agreed or rounds == max_rounds:
            break
        issue_text = "\n".join(f"  - {issue}" for issue in critique.issues)
        revise = f"{case}\n\nYOUR PREVIOUS DRAFT\n{_draft_text(draft)}\n\nAUDITOR'S ISSUES\n{issue_text}\n\nRevise your draft."
        draft = _enforce(run_agent(vp_agent, revise, deps, usage), policy, deps)

    issues = [] if agreed or critique is None else critique.issues
    fixed = policy.fixed_outcome
    decided_by: Literal["policy", "vp_critic"]
    if fixed is not None:
        outcome, decided_by, reasons = fixed, "policy", policy.reasons
    elif agreed:
        outcome, decided_by, reasons = draft.decision, "vp_critic", policy.reasons
    else:
        outcome, decided_by = Outcome.NEEDS_REVIEW, "vp_critic"
        reasons = [*policy.reasons, f"VP and Critic did not agree within {max_rounds} round(s)"]
    return Decision(
        outcome=outcome,
        decided_by=decided_by,
        policy_mode=policy.mode,
        rationale=draft.rationale,
        reasons=reasons,
        rounds=rounds,
        critic_issues=issues,
    )
