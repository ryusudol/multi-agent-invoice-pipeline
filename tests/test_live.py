"""Real Grok extraction compared against the recorded fixtures. Run with `pytest -m live`."""

import pytest
from pydantic_ai import models

from invoice_agent import config, db
from invoice_agent.agents.common import AgentDeps
from invoice_agent.agents.intake import extract
from invoice_agent.duplicates import identify
from invoice_agent.events import EventSink, now_iso
from invoice_agent.ingestion.loader import load_text
from invoice_agent.ingestion.normalize import cents
from invoice_agent.models import Invoice, Usage

from .scripted import FIXTURES, load_fixture

pytestmark = pytest.mark.live

SOURCES = {p.name: p for folder in ("invoices", "invoices_extra") for p in (config.ROOT / "data" / folder).iterdir()}
NAMES = sorted(p.stem for p in FIXTURES.glob("*.json"))


@pytest.fixture(autouse=True)
def allow_grok(monkeypatch):
    if not config.xai_api_key():
        pytest.skip("XAI_API_KEY is not set")
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)


def amounts(invoice: Invoice) -> dict:
    return {
        "subtotal": cents(invoice.stated_subtotal),
        "tax": cents(invoice.stated_tax_amount),
        "shipping": cents(invoice.stated_shipping),
        "total": cents(invoice.stated_total),
    }


@pytest.mark.parametrize("name", NAMES)
def test_extraction_matches_fixture(name):
    path = SOURCES[name]
    text = load_text(path)
    sink = EventSink(db.create_run(str(path), now_iso()))
    actual = extract(name, text, AgentDeps(sink=sink, stage="extract", source_text=text), Usage())
    expected = Invoice.model_validate(load_fixture(name)["intake"][0])

    # Same identity and content fingerprint = same number, vendor, date, lines, total, currency.
    assert identify(actual) == identify(expected)
    assert amounts(actual) == amounts(expected)
