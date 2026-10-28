"""The §10 answer key, end to end and offline. Agent outputs replay from tests/fixtures/ (recorded live)."""

import pytest
from rich.console import Console

from invoice_agent import config, db
from invoice_agent.console import render_result
from invoice_agent.models import FindingCode, PolicyMode, RunStatus
from invoice_agent.orchestrator import resolve_review, run_pipeline

from .scripted import ScriptedModels, load_fixture
from .test_orchestrator import AGREE, vp

INVOICES = config.ROOT / "data" / "invoices"
EXTRA = config.ROOT / "data" / "invoices_extra"

# file -> (status, amount paid in cents, finding codes that must be present)
ANSWER_KEY = {
    "invoice_1001.txt": (RunStatus.PAID, 500000, set()),
    "invoice_1002.txt": (RunStatus.REJECTED, None, {FindingCode.INSUFFICIENT_STOCK}),
    "invoice_1003.txt": (RunStatus.REJECTED, None, {FindingCode.OUT_OF_STOCK}),
    "invoice_1004.json": (RunStatus.PAID, 189000, set()),
    "invoice_1004_revised.json": (RunStatus.NEEDS_REVIEW, None, {FindingCode.REVISED_INVOICE}),
    "invoice_1005.json": (RunStatus.REJECTED, None, {FindingCode.INSUFFICIENT_STOCK}),
    "invoice_1006.csv": (RunStatus.PAID, 275000, set()),
    "invoice_1007.csv": (RunStatus.REJECTED, None, {FindingCode.INSUFFICIENT_STOCK, FindingCode.ARITHMETIC}),
    "invoice_1008.txt": (RunStatus.REJECTED, None, {FindingCode.UNKNOWN_ITEM}),
    "invoice_1009.json": (RunStatus.REJECTED, None, {FindingCode.MISSING_FIELD, FindingCode.INVALID_QUANTITY, FindingCode.ARITHMETIC}),
    "invoice_1010.txt": (RunStatus.NEEDS_REVIEW, None, {FindingCode.PRICE_ABOVE_CATALOG}),
    "invoice_1011.pdf": (RunStatus.PAID, 300000, set()),
    "invoice_1011.txt": (RunStatus.DUPLICATE, None, set()),
    "invoice_1012.pdf": (RunStatus.PAID, 997500, set()),
    "invoice_1012.txt": (RunStatus.DUPLICATE, None, set()),
    "invoice_1013.json": (RunStatus.REJECTED, None, {FindingCode.INSUFFICIENT_STOCK, FindingCode.ARITHMETIC}),
    "invoice_1013.pdf": (RunStatus.DUPLICATE, None, set()),
    "invoice_1014.xml": (RunStatus.NEEDS_REVIEW, None, {FindingCode.NON_USD_CURRENCY}),
    "invoice_1015.csv": (RunStatus.PAID, 650000, set()),
    "invoice_1016.json": (RunStatus.REJECTED, None, {FindingCode.UNKNOWN_ITEM}),
}
POLICY_FIXED = {name for name, (status, _, _) in ANSWER_KEY.items() if status in (RunStatus.REJECTED, RunStatus.NEEDS_REVIEW)}


def sample_files():
    return sorted(p for p in INVOICES.iterdir() if p.suffix in config.SUPPORTED_SUFFIXES)


@pytest.fixture
def replay():
    models = ScriptedModels()
    with models.active():
        yield models


def run(models: ScriptedModels, path, script=None):
    models.load(script or load_fixture(path.name))
    state = run_pipeline(path)
    assert models.leftovers() == {}, f"{path.name}: fixture outputs not consumed"
    return state


def finding_codes(state):
    return {f.code for f in state.validation.findings} if state.validation else set()


def test_batch_matches_answer_key(replay, capsys):
    assert [p.name for p in sample_files()] == list(ANSWER_KEY)  # Python sorted(): .pdf before .txt
    states = {p.name: run(replay, p) for p in sample_files()}

    for name, (status, paid, codes) in ANSWER_KEY.items():
        state = states[name]
        assert state.status == status, f"{name}: {state.status} ({state.error or ''})"
        assert (state.payment.amount_cents if state.payment else None) == paid, name
        assert codes <= finding_codes(state), f"{name}: {finding_codes(state)}"

    # Specific evidence the answer key calls out.
    messages = lambda name: " | ".join(f.message for f in states[name].validation.findings)  # noqa: E731
    assert "GadgetX: 20 requested" in messages("invoice_1002.txt")
    assert "Total understated by $110.00" in messages("invoice_1007.csv")
    assert "SuperGizmo" in messages("invoice_1008.txt") and "MegaSprocket" in messages("invoice_1008.txt")
    assert "Total overstated by $50.00" in messages("invoice_1013.json")
    for item in ("WidgetA: 22 requested", "WidgetB: 18 requested", "GadgetX: 9 requested"):
        assert item in messages("invoice_1013.json")
    assert "WidgetC" in messages("invoice_1016.json")
    revision = next(f for f in states["invoice_1004_revised.json"].validation.findings if f.code == FindingCode.REVISED_INVOICE)
    assert revision.data["total_change_cents"] == 405000
    assert "adds 5 × GadgetX @ $750.00" in revision.message
    rush = next(f for f in states["invoice_1010.txt"].validation.findings if f.code == FindingCode.PRICE_ABOVE_CATALOG)
    assert rush.data["overcharge_cents"] == 20000
    assert states["invoice_1013.pdf"].duplicate.earlier_status == RunStatus.REJECTED
    assert states["invoice_1011.txt"].duplicate.earlier_run_id == states["invoice_1011.pdf"].run_id

    # The review card shows what approving would pay: the revision only owes the difference.
    card = Console(width=200, record=True)
    render_result(card, states["invoice_1004_revised.json"])
    text = card.export_text()
    assert "Due on approval  $4,050.00 ($1,890.00 already paid)" in text
    assert "Due date         2026-02-22" in text
    card = Console(width=200, record=True)
    render_result(card, states["invoice_1003.txt"])
    assert "Due date   yesterday (not a date)" in card.export_text()
    card = Console(width=200, record=True)
    render_result(card, states["invoice_1010.txt"])
    assert "Due on approval" not in card.export_text()  # nothing paid yet, so it would repeat Total

    # Human review: approving the revision pays only the difference; approving 1010 pays its total.
    revised = resolve_review(states["invoice_1004_revised.json"].run_id, "approve", "PO amendment confirmed")
    assert (revised.status, revised.payment.amount_cents) == (RunStatus.PAID, 405000)
    rush_order = resolve_review(states["invoice_1010.txt"].run_id, "approve", "Rush pricing agreed")
    assert (rush_order.status, rush_order.payment.amount_cents) == (RunStatus.PAID, 718500)
    assert db.paid_cents("INV-1004/precision parts ltd") == 594000

    # Synthetic over-$10K invoice: approved after strict review.
    extra = run(replay, EXTRA / "invoice_2001.txt")
    assert extra.policy.mode == PolicyMode.STRICT
    policy_line = next(e["message"] for e in db.list_events(extra.run_id) if e["type"] == "stage_completed" and e["stage"] == "policy_gate")
    assert policy_line == "strict: Total $12,500.00 is over $10,000.00"  # the $10K rule stays visible
    assert (extra.status, extra.payment.amount_cents) == (RunStatus.PAID, 1250000)


def test_revision_alone_pays_in_full(replay):
    fixture = load_fixture("invoice_1004_revised.json")
    script = {"intake": fixture["intake"], "validation": fixture["validation"], "vp": [vp("approve")], "critic": [AGREE]}
    state = run(replay, INVOICES / "invoice_1004_revised.json", script)
    assert (state.status, state.payment.amount_cents) == (RunStatus.PAID, 594000)


def test_fixtures_cannot_loosen_policy_outcomes(replay):
    """Every VP draft says approve; outcomes the policy fixed stay rejected or needs_review."""
    for path in sample_files():
        script = load_fixture(path.name)
        script["vp"] = [vp("approve")] * len(script.get("vp", []))
        state = run(replay, path, script)
        if path.name in POLICY_FIXED:
            assert state.status == ANSWER_KEY[path.name][0], path.name
            assert state.payment is None
