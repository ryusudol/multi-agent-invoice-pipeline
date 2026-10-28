"""Typed state machine that drives an invoice through the four stages.

A fixed Stage list and a declarative transition table name every path. Each transition is an event,
and run state is saved to SQLite after every stage, so a run that stops at needs_review can be resumed
by resolve_review() from another process (CLI prompt now, dashboard later).
"""

import logging
import sqlite3
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior

from invoice_agent import config, db
from invoice_agent.agents import approval, intake, validation
from invoice_agent.agents.common import AgentDeps
from invoice_agent.approval import policy as policy_engine
from invoice_agent.duplicates import compare, identify
from invoice_agent.events import EventCallback, EventSink, EventType, now_iso
from invoice_agent.ingestion.loader import load_text
from invoice_agent.ingestion.normalize import format_money, normalize_currency
from invoice_agent.models import (
    Decision,
    DuplicateVerdict,
    HumanReview,
    Invoice,
    InvoiceIdentity,
    Outcome,
    PaymentResult,
    PolicyMode,
    PolicyResult,
    RunStatus,
    Usage,
    ValidationReport,
)
from invoice_agent.payment import amount_due, pay
from invoice_agent.validation.rules import compute_totals, match_lines, run_rules

log = logging.getLogger("invoice_agent.orchestrator")


class Stage(StrEnum):
    QUEUED = "queued"  # registered, waiting for the API worker
    LOAD = "load"
    EXTRACT = "extract"  # includes check_extraction: the Intake agent's output validator, with retries
    CHECK_DUPLICATE = "check_duplicate"
    VALIDATE = "validate"
    POLICY_GATE = "policy_gate"
    APPROVAL = "approval"  # VP draft -> Critic -> revisions
    PAYMENT = "payment"
    REJECTION = "rejection"
    # Terminal stages; each one is also the run's final status.
    NEEDS_REVIEW = "needs_review"
    PAID = "paid"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    FAILED = "failed"


DECIDED_BY = {"policy": "policy", "vp_critic": "VP + critic", "human": "human"}

TERMINAL = {Stage.NEEDS_REVIEW, Stage.PAID, Stage.REJECTED, Stage.DUPLICATE, Stage.FAILED}

# (from, condition) -> to. Any stage that raises goes to FAILED.
TRANSITIONS: dict[tuple[Stage, str], Stage] = {
    (Stage.LOAD, "loaded"): Stage.EXTRACT,
    (Stage.EXTRACT, "extracted"): Stage.CHECK_DUPLICATE,
    (Stage.CHECK_DUPLICATE, "identical"): Stage.DUPLICATE,
    (Stage.CHECK_DUPLICATE, "new"): Stage.VALIDATE,
    (Stage.CHECK_DUPLICATE, "conflict"): Stage.VALIDATE,  # carries a human-review finding
    (Stage.VALIDATE, "validated"): Stage.POLICY_GATE,
    (Stage.POLICY_GATE, "gated"): Stage.APPROVAL,
    (Stage.APPROVAL, Outcome.APPROVE): Stage.PAYMENT,
    (Stage.APPROVAL, Outcome.REJECT): Stage.REJECTION,
    (Stage.APPROVAL, Outcome.NEEDS_REVIEW): Stage.NEEDS_REVIEW,
    (Stage.NEEDS_REVIEW, Outcome.APPROVE): Stage.PAYMENT,  # resolve_review()
    (Stage.NEEDS_REVIEW, Outcome.REJECT): Stage.REJECTION,  # resolve_review()
    (Stage.PAYMENT, "paid"): Stage.PAID,
    (Stage.REJECTION, "recorded"): Stage.REJECTED,
}


class RunState(BaseModel):
    run_id: int
    source_file: str
    started_at: str
    stage: Stage = Stage.LOAD
    status: RunStatus = RunStatus.PROCESSING
    source_text: str | None = None
    invoice: Invoice | None = None
    identity: InvoiceIdentity | None = None
    duplicate: DuplicateVerdict | None = None
    validation: ValidationReport | None = None
    policy: PolicyResult | None = None
    decision: Decision | None = None
    payment: PaymentResult | None = None
    usage: Usage = Field(default_factory=Usage)
    error: str | None = None
    finished_at: str | None = None
    duration_ms: int = 0

    @property
    def name(self) -> str:
        return Path(self.source_file).name

    @property
    def currency(self) -> str:
        if self.validation:
            return self.validation.currency
        return normalize_currency(self.invoice.currency) if self.invoice else "USD"

    @property
    def total_cents(self) -> int | None:
        if self.validation:
            return self.validation.computed.payable_cents
        return compute_totals(self.invoice).payable_cents if self.invoice else None


def due_on_approval(state: RunState) -> int | None:
    """What approving would pay right now: the total minus earlier payments for the same invoice key."""
    total = state.total_cents
    return amount_due(state.identity.key, total) if state.identity and total is not None else total


class ReviewError(ValueError):
    pass


def _json(model: BaseModel | None) -> str | None:
    return model.model_dump_json() if model is not None else None


def _save(state: RunState) -> None:
    db.update_run(
        state.run_id,
        status=state.status.value,
        stage=state.stage.value,
        source_text=state.source_text,
        invoice_number=state.identity.number if state.identity else None,
        vendor=state.invoice.vendor if state.invoice else None,
        invoice_key=state.identity.key if state.identity else None,
        fingerprint=state.identity.fingerprint if state.identity else None,
        total_cents=state.total_cents,
        currency=state.currency if state.invoice else None,
        extraction_json=_json(state.invoice),
        duplicate_json=_json(state.duplicate),
        validation_json=_json(state.validation),
        policy_json=_json(state.policy),
        decision_json=_json(state.decision),
        payment_json=_json(state.payment),
        usage_json=_json(state.usage),
        error=state.error,
        finished_at=state.finished_at,
        duration_ms=state.duration_ms,
    )


def load_state(run_id: int) -> RunState:
    row = db.get_run(run_id)
    if row is None:
        raise LookupError(f"Run #{run_id} does not exist")
    return state_from_row(row)


def state_from_row(row: sqlite3.Row) -> RunState:
    def parse[M: BaseModel](model: type[M], column: str) -> M | None:
        return model.model_validate_json(row[column]) if row[column] else None

    return RunState(
        run_id=row["id"],
        source_file=row["source_file"],
        started_at=row["started_at"],
        stage=Stage(row["stage"]),
        status=RunStatus(row["status"]),
        source_text=row["source_text"],
        invoice=parse(Invoice, "extraction_json"),
        identity=InvoiceIdentity(number=row["invoice_number"], key=row["invoice_key"], fingerprint=row["fingerprint"])
        if row["invoice_key"]
        else None,
        duplicate=parse(DuplicateVerdict, "duplicate_json"),
        validation=parse(ValidationReport, "validation_json"),
        policy=parse(PolicyResult, "policy_json"),
        decision=parse(Decision, "decision_json"),
        payment=parse(PaymentResult, "payment_json"),
        usage=parse(Usage, "usage_json") or Usage(),
        error=row["error"],
        finished_at=row["finished_at"],
        duration_ms=row["duration_ms"] or 0,
    )


def _load(state: RunState, sink: EventSink) -> tuple[str, str]:
    path = Path(state.source_file)
    state.source_text = load_text(path)
    how = "PDF text extracted with pdfplumber" if path.suffix.lower() == ".pdf" else "read as raw text"
    return "loaded", f"{len(state.source_text):,} characters, {how}"


def _extract(state: RunState, sink: EventSink) -> tuple[str, str]:
    assert state.source_text is not None
    deps = AgentDeps(sink=sink, stage=Stage.EXTRACT, source_text=state.source_text)
    invoice = intake.extract(state.name, state.source_text, deps, state.usage)
    state.invoice = invoice
    total = format_money(state.total_cents, state.currency)
    risks = f", {len(invoice.risk_signals)} risk signal(s)" if invoice.risk_signals else ""
    n = len(invoice.line_items)
    return "extracted", (
        f"{invoice.invoice_number or 'no number'} · {invoice.vendor or 'no vendor'} · "
        f"{n} item{'s' * (n != 1)} · {total}{risks}"
    )


def _check_duplicate(state: RunState, sink: EventSink) -> tuple[str, str]:
    assert state.invoice is not None
    state.identity = identify(state.invoice)
    earlier = db.earlier_runs(state.identity.number, state.run_id) if state.identity else []
    verdict = compare(
        state.identity, state.invoice, earlier, lambda row: Invoice.model_validate_json(row["extraction_json"])
    )
    state.duplicate = verdict
    if verdict.kind == "identical":
        sink.emit(Stage.CHECK_DUPLICATE, EventType.DUPLICATE, verdict.message, earlier_run_id=verdict.earlier_run_id)
        return "identical", verdict.message
    if verdict.kind in ("revised", "vendor_conflict"):
        return "conflict", verdict.message
    return "new", verdict.message


def _validate(state: RunState, sink: EventSink) -> tuple[str, str]:
    assert state.invoice is not None and state.duplicate is not None
    invoice = state.invoice
    catalog = db.load_inventory()
    lines = match_lines(invoice, catalog)
    summary, overrides = "", []
    if invoice.line_items:
        deps = AgentDeps(sink=sink, stage=Stage.VALIDATE, catalog=catalog)
        proposal = validation.match_items(invoice, deps, state.usage)
        summary = proposal.summary
        overrides = validation.reconcile(lines, proposal, deps)
    findings, totals = run_rules(invoice, lines, catalog)
    findings += state.duplicate.findings
    state.validation = ValidationReport(
        lines=lines,
        findings=findings,
        computed=totals,
        currency=normalize_currency(invoice.currency),
        agent_summary=summary,
        overrides=overrides,
    )
    for finding in findings:
        sink.emit(Stage.VALIDATE, EventType.FINDING, f"[{finding.severity}] {finding.message}", **finding.model_dump(mode="json"))
    report = state.validation
    errors, warnings = len(report.errors), len(report.warnings)
    return "validated", f"{errors} error{'s' * (errors > 1)}, {warnings} warning{'s' * (warnings > 1)}"


def _policy_gate(state: RunState, sink: EventSink) -> tuple[str, str]:
    assert state.validation is not None
    state.policy = policy_engine.evaluate(state.validation)
    message = f"{state.policy.mode}: " + "; ".join(state.policy.reasons)
    sink.emit(Stage.POLICY_GATE, EventType.POLICY, message, **state.policy.model_dump(mode="json"))
    # Reject and human-review reasons are findings already on screen; strict ones (the $10K rule) are not.
    if state.policy.mode in (PolicyMode.REJECT, PolicyMode.HUMAN_REVIEW):
        return "gated", f"{state.policy.mode}"
    return "gated", message


def _approval(state: RunState, sink: EventSink) -> tuple[str, str]:
    assert state.invoice is not None and state.validation is not None and state.policy is not None
    deps = AgentDeps(sink=sink, stage=Stage.APPROVAL, catalog=db.load_inventory())
    decision = approval.decide(state.invoice, state.validation, state.policy, deps, state.usage)
    state.decision = decision
    sink.emit(Stage.APPROVAL, EventType.DECISION, f"{decision.outcome}: {decision.rationale}", **decision.model_dump(mode="json"))
    if decision.outcome == Outcome.NEEDS_REVIEW:
        sink.emit(Stage.APPROVAL, EventType.REVIEW_REQUESTED, "Waiting for a human approve/reject decision")
    return decision.outcome, f"{decision.outcome} (decided by {DECIDED_BY[decision.decided_by]})"


def _payment(state: RunState, sink: EventSink) -> tuple[str, str]:
    assert state.identity is not None and state.invoice is not None and state.invoice.vendor
    assert state.total_cents is not None
    result = pay(
        run_id=state.run_id,
        invoice_number=state.identity.number,
        invoice_key=state.identity.key,
        vendor=state.invoice.vendor,
        approved_total_cents=state.total_cents,
        currency=state.currency,
    )
    state.payment = result
    amount = format_money(result.amount_cents, result.currency)
    if result.status == "paid":
        message = f"Paid {amount} to {result.vendor}"
        if result.previously_paid_cents:
            message += f" (approved {format_money(result.approved_total_cents, result.currency)} minus {format_money(result.previously_paid_cents, result.currency)} already paid)"
    elif result.status == "already_paid":
        message = f"Already paid {amount} under {result.idempotency_key}; no new payment"
    else:
        message = f"Nothing due: {format_money(result.previously_paid_cents, result.currency)} already paid"
    sink.emit(Stage.PAYMENT, EventType.PAYMENT, message, **result.model_dump(mode="json"))
    return "paid", message


def _rejection(state: RunState, sink: EventSink) -> tuple[str, str]:
    assert state.decision is not None
    reasons = state.decision.reasons or [state.decision.rationale]
    if state.decision.review:
        reasons = [f"Reviewer ({state.decision.review.reviewer}): {state.decision.review.comment or 'rejected'}", *reasons]
    message = "Rejected: " + "; ".join(reasons)
    sink.emit(Stage.REJECTION, EventType.REJECTION, message, reasons=reasons, rationale=state.decision.rationale)
    return "recorded", message


# Each handler returns (condition for TRANSITIONS, one-line summary for the live output).
HANDLERS: dict[Stage, Callable[[RunState, EventSink], tuple[str, str]]] = {
    Stage.LOAD: _load,
    Stage.EXTRACT: _extract,
    Stage.CHECK_DUPLICATE: _check_duplicate,
    Stage.VALIDATE: _validate,
    Stage.POLICY_GATE: _policy_gate,
    Stage.APPROVAL: _approval,
    Stage.PAYMENT: _payment,
    Stage.REJECTION: _rejection,
}


def _describe_error(exc: Exception, stage: Stage) -> str:
    if isinstance(exc, UnexpectedModelBehavior) and "output retries" in str(exc):
        if stage == Stage.EXTRACT:
            return f"Extraction still failed its checks after {config.EXTRACTION_RETRIES} retries"
        return f"The {stage} agent kept returning invalid output: {exc}"
    if isinstance(exc, ModelAPIError):
        return f"Grok API error: {exc}"
    return f"{type(exc).__name__}: {exc}"


def _transition(sink: EventSink, source: Stage, target: Stage, condition: str) -> None:
    sink.emit(source, EventType.TRANSITION, f"{source} → {target} ({condition})", source=source, target=target, condition=condition)


def _drive(state: RunState, stage: Stage, sink: EventSink) -> RunState:
    started = time.perf_counter()
    while stage not in TERMINAL:
        state.stage = stage
        _save(state)
        sink.emit(stage, EventType.STAGE_STARTED, f"{stage} started")
        try:
            condition, summary = HANDLERS[stage](state, sink)
            sink.emit(stage, EventType.STAGE_COMPLETED, summary)
            next_stage = TRANSITIONS[(stage, condition)]
        except Exception as exc:
            log.exception("stage %s failed for run %s", stage, state.run_id)
            condition, next_stage = "error", Stage.FAILED
            state.error = _describe_error(exc, stage)
            sink.emit(stage, EventType.ERROR, state.error, exception=type(exc).__name__)
        except BaseException:
            # Ctrl-C: don't leave a half-finished 'processing' run behind to block reprocessing.
            state.error = "Interrupted"
            state.stage, state.status, state.finished_at = Stage.FAILED, RunStatus.FAILED, now_iso()
            _save(state)
            sink.emit(stage, EventType.ERROR, "Interrupted before the run finished")
            raise
        _transition(sink, stage, next_stage, condition)
        stage = next_stage

    state.stage = stage
    state.status = RunStatus(stage.value)
    state.finished_at = now_iso()
    state.duration_ms += int((time.perf_counter() - started) * 1000)
    _save(state)
    sink.emit(
        stage,
        EventType.RUN_FINISHED,
        f"Run #{state.run_id} finished: {state.status}",
        status=state.status,
        duration_ms=state.duration_ms,
        usage=state.usage.model_dump(),
    )
    return state


def create_run(path: str | Path) -> int:
    """Register a run before it starts, so the API can hand back its id while it is queued."""
    return db.create_run(str(path), now_iso())


def run_pipeline(path: str | Path, on_event: EventCallback | None = None, run_id: int | None = None) -> RunState:
    path = Path(path)
    run_id = run_id or create_run(path)
    row = db.get_run(run_id)
    assert row is not None
    state = RunState(run_id=run_id, source_file=str(path), started_at=row["started_at"])
    sink = EventSink(run_id, on_event)
    sink.emit(Stage.LOAD, EventType.RUN_STARTED, f"Run #{run_id} started for {path.name}", source_file=str(path))
    return _drive(state, Stage.LOAD, sink)


def resolve_review(
    run_id: int, decision: Outcome | str, comment: str, reviewer: str = "human", on_event: EventCallback | None = None
) -> RunState:
    """Continue a needs_review run into payment or rejection with a human decision."""
    outcome = Outcome(decision)
    if outcome not in (Outcome.APPROVE, Outcome.REJECT):
        raise ReviewError("A review decision must be approve or reject")
    state = load_state(run_id)
    if state.status != RunStatus.NEEDS_REVIEW or not db.claim_review(run_id):
        raise ReviewError(f"Run #{run_id} is {state.status}, not waiting for review")

    review = HumanReview(outcome=outcome, comment=comment, reviewer=reviewer, reviewed_at=now_iso())
    assert state.decision is not None
    state.decision = state.decision.model_copy(update={"outcome": outcome, "decided_by": "human", "review": review})
    state.status = RunStatus.PROCESSING
    sink = EventSink(run_id, on_event)
    sink.emit(
        Stage.NEEDS_REVIEW,
        EventType.REVIEW_RESOLVED,
        f"{reviewer} chose {outcome}" + (f": {comment}" if comment else ""),
        **review.model_dump(mode="json"),
    )
    next_stage = TRANSITIONS[(Stage.NEEDS_REVIEW, outcome)]
    _transition(sink, Stage.NEEDS_REVIEW, next_stage, outcome)
    return _drive(state, next_stage, sink)
