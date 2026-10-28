"""Orchestrator paths with scripted agents: self-correction, policy veto, critique loop, review, failure."""

import pytest

from invoice_agent import config, db
from invoice_agent.models import Outcome, PolicyMode, RunStatus
from invoice_agent.orchestrator import ReviewError, load_state, resolve_review, run_pipeline

from .scripted import ScriptedModels

INVOICES = config.ROOT / "data" / "invoices"


def lines(*items):
    return [{"description": d, "quantity": q, "unit_price": p} for d, q, p in items]


def intake(**overrides):
    out = {
        "invoice_number": "INV-1001",
        "vendor": "Widgets Inc.",
        "invoice_date": "2026-01-15",
        "due_date": "2026-02-01",
        "currency": "USD",
        "line_items": lines(("WidgetA", 10, 250.0), ("WidgetB", 5, 500.0)),
        "stated_subtotal": 5000.0,
        "stated_tax_rate": 0.0,
        "stated_tax_amount": 0.0,
        "stated_shipping": None,
        "stated_total": 5000.0,
        "risk_signals": [],
    }
    out.update(overrides)
    return out


INTAKE_1010 = intake(
    invoice_number="INV-1010",
    vendor="Consolidated Materials Group",
    invoice_date="January 27, 2026",
    due_date="February 26, 2026",
    line_items=lines(("WidgetA", 8, 250.0), ("WidgetB", 4, 500.0), ("GadgetX", 2, 750.0), ("WidgetA (rush order)", 4, 300.0)),
    stated_subtotal=6700.0,
    stated_tax_rate=None,
    stated_tax_amount=335.0,
    stated_shipping=150.0,
    stated_total=7185.0,
)
INTAKE_1002 = intake(
    invoice_number="1002",
    vendor="Gadgets Co.",
    invoice_date="Jan 30 2026",
    due_date="2026-01-30",
    line_items=lines(("GadgetX", 20, 750.0)),
    stated_subtotal=None,
    stated_tax_rate=None,
    stated_tax_amount=None,
    stated_total=15000.0,
)


def matching(*skus):
    return {"lines": [{"line": n, "sku": sku, "note": "checked"} for n, sku in enumerate(skus, 1)], "summary": "checked"}


def vp(decision="approve", rationale="Evidence supports this."):
    return {"decision": decision, "rationale": rationale, "checklist_responses": []}


AGREE = {"agrees": True, "issues": [], "summary": "Supported by the evidence."}
OBJECT = {"agrees": False, "issues": ["Rationale omits the stock check"], "summary": "Needs work."}


def event_types(run_id):
    return [row["type"] for row in db.list_events(run_id)]


@pytest.fixture
def scripted():
    models = ScriptedModels()
    with models.active():
        yield models


def test_1001_runs_all_four_stages(scripted):
    scripted.load({"intake": [intake()], "validation": [matching("WidgetA", "WidgetB")], "vp": [vp()], "critic": [AGREE]})
    state = run_pipeline(INVOICES / "invoice_1001.txt")
    assert state.status == RunStatus.PAID
    assert state.payment.amount_cents == 500000
    stages = [row["stage"] for row in db.list_events(state.run_id) if row["type"] == "stage_completed"]
    assert stages == ["load", "extract", "check_duplicate", "validate", "policy_gate", "approval", "payment"]
    assert scripted.leftovers() == {}


def test_extraction_self_corrects(scripted):
    bad = {**INTAKE_1010, "stated_tax_rate": 0.05}  # 1010 states no rate
    scripted.load({"intake": [bad, INTAKE_1010], "validation": [matching("WidgetA", "WidgetB", "GadgetX", "WidgetA")], "vp": [vp("needs_review")], "critic": [AGREE]})
    state = run_pipeline(INVOICES / "invoice_1010.txt")
    assert state.status == RunStatus.NEEDS_REVIEW
    assert state.invoice.stated_tax_rate is None
    retries = [row for row in db.list_events(state.run_id) if row["type"] == "extraction_retry"]
    assert len(retries) == 1
    assert "stated_tax_rate 0.05" in retries[0]["message"]


def test_extraction_gives_up_after_retry_cap_and_batch_continues(scripted):
    bad = intake(stated_total=9999.0)
    scripted.load({"intake": [bad] * (config.EXTRACTION_RETRIES + 1)})
    failed = run_pipeline(INVOICES / "invoice_1001.txt")
    assert failed.status == RunStatus.FAILED
    assert f"after {config.EXTRACTION_RETRIES} retries" in failed.error
    assert event_types(failed.run_id).count("extraction_retry") == config.EXTRACTION_RETRIES + 1

    # A failed run doesn't block reprocessing the same invoice.
    scripted.load({"intake": [intake()], "validation": [matching("WidgetA", "WidgetB")], "vp": [vp()], "critic": [AGREE]})
    assert run_pipeline(INVOICES / "invoice_1001.txt").status == RunStatus.PAID


def test_llm_cannot_approve_a_policy_rejection(scripted):
    scripted.load({"intake": [INTAKE_1002], "validation": [matching("GadgetX")], "vp": [vp("approve")], "critic": [AGREE]})
    state = run_pipeline(INVOICES / "invoice_1002.txt")
    assert state.status == RunStatus.REJECTED
    assert state.decision.decided_by == "policy"
    assert "policy_override" in event_types(state.run_id)
    assert state.payment is None


def test_wrong_agent_match_is_blocked(scripted):
    out = intake(
        invoice_number="INV-1016",
        line_items=lines(("WidgetA", 4, 250.0), ("WidgetB", 2, 500.0), ("WidgetC", 3, 350.0)),
        invoice_date="2026-01-27",
        due_date="2026-02-27",
        stated_subtotal=3050.0,
        stated_tax_rate=0.06,
        stated_tax_amount=183.0,
        stated_total=3233.0,
    )
    scripted.load({"intake": [out], "validation": [matching("WidgetA", "WidgetB", "WidgetA")], "vp": [vp("reject")], "critic": [AGREE]})
    state = run_pipeline(INVOICES / "invoice_1016.json")
    assert state.status == RunStatus.REJECTED
    assert state.validation.lines[2].sku is None
    assert "match_override" in event_types(state.run_id)


def test_standard_critic_objection_escalates(scripted):
    scripted.load({"intake": [intake()], "validation": [matching("WidgetA", "WidgetB")], "vp": [vp()], "critic": [OBJECT]})
    state = run_pipeline(INVOICES / "invoice_1001.txt")
    assert state.status == RunStatus.NEEDS_REVIEW
    assert state.decision.critic_issues == OBJECT["issues"]


def test_standard_vp_reject_becomes_review(scripted):
    scripted.load({"intake": [intake()], "validation": [matching("WidgetA", "WidgetB")], "vp": [vp("reject")], "critic": [AGREE]})
    state = run_pipeline(INVOICES / "invoice_1001.txt")
    assert state.status == RunStatus.NEEDS_REVIEW


def test_strict_review_revises_until_critic_agrees(scripted, monkeypatch):
    monkeypatch.setattr(config, "STRICT_REVIEW_THRESHOLD_CENTS", 100000)  # 1001's $5,000 becomes strict
    scripted.load({"intake": [intake()], "validation": [matching("WidgetA", "WidgetB")], "vp": [vp(), vp()], "critic": [OBJECT, AGREE]})
    state = run_pipeline(INVOICES / "invoice_1001.txt")
    assert state.policy.mode == PolicyMode.STRICT
    assert state.decision.rounds == 2
    assert state.status == RunStatus.PAID


def test_strict_review_without_agreement_goes_to_human(scripted, monkeypatch):
    monkeypatch.setattr(config, "STRICT_REVIEW_THRESHOLD_CENTS", 100000)
    rounds = config.MAX_CRITIQUE_ROUNDS
    scripted.load({"intake": [intake()], "validation": [matching("WidgetA", "WidgetB")], "vp": [vp()] * rounds, "critic": [OBJECT] * rounds})
    state = run_pipeline(INVOICES / "invoice_1001.txt")
    assert state.status == RunStatus.NEEDS_REVIEW
    assert state.decision.rounds == rounds
    assert f"did not agree within {rounds} round(s)" in state.decision.reasons[-1]


def test_resolve_review_from_saved_state(scripted):
    scripted.load({"intake": [INTAKE_1010], "validation": [matching("WidgetA", "WidgetB", "GadgetX", "WidgetA")], "vp": [vp("needs_review")], "critic": [AGREE]})
    run_id = run_pipeline(INVOICES / "invoice_1010.txt").run_id

    # Everything resolve_review needs comes from the database, as it would in a new process.
    assert load_state(run_id).status == RunStatus.NEEDS_REVIEW
    state = resolve_review(run_id, "approve", "Rush order agreed by phone", reviewer="vp")
    assert state.status == RunStatus.PAID
    assert state.payment.amount_cents == 718500
    assert state.decision.decided_by == "human"
    assert state.decision.review.comment == "Rush order agreed by phone"
    assert state.payment.status == "paid"

    with pytest.raises(ReviewError):
        resolve_review(run_id, Outcome.APPROVE, "again")


def test_review_rejection_is_recorded(scripted):
    scripted.load({"intake": [INTAKE_1010], "validation": [matching("WidgetA", "WidgetB", "GadgetX", "WidgetA")], "vp": [vp("needs_review")], "critic": [AGREE]})
    run_id = run_pipeline(INVOICES / "invoice_1010.txt").run_id
    state = resolve_review(run_id, "reject", "Rush pricing not agreed")
    assert state.status == RunStatus.REJECTED
    assert state.payment is None
    rejection = next(row for row in db.list_events(run_id) if row["type"] == "rejection")
    assert "Rush pricing not agreed" in rejection["message"]


def test_unreadable_file_fails_cleanly(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("   ")
    state = run_pipeline(path)
    assert state.status == RunStatus.FAILED
    assert "no text" in state.error


def test_interrupted_run_is_failed_and_does_not_block_rerun(scripted, monkeypatch):
    from invoice_agent import orchestrator

    def interrupt(state, sink):
        raise KeyboardInterrupt

    script = {"intake": [intake()], "validation": [matching("WidgetA", "WidgetB")], "vp": [vp()], "critic": [AGREE]}
    scripted.load(script)
    validate = orchestrator.HANDLERS[orchestrator.Stage.VALIDATE]
    monkeypatch.setitem(orchestrator.HANDLERS, orchestrator.Stage.VALIDATE, interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(INVOICES / "invoice_1001.txt")
    assert load_state(1).status == RunStatus.FAILED  # saved with its fingerprint, but failed runs don't count

    monkeypatch.setitem(orchestrator.HANDLERS, orchestrator.Stage.VALIDATE, validate)
    scripted.load(script)
    assert run_pipeline(INVOICES / "invoice_1001.txt").status == RunStatus.PAID
