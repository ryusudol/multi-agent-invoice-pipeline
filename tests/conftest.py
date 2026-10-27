from typing import Any

import pytest
from pydantic_ai import models

from invoice_agent import config, db
from invoice_agent.models import Invoice, LineItem

# Offline by default: any test that reaches a real model fails loudly. Live tests turn this back on.
models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "inventory.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    db.init_db(path)
    return path


@pytest.fixture
def catalog():
    return db.load_inventory()


def line(description: str, quantity: float | None, unit_price: float | None, **kw: Any) -> LineItem:
    return LineItem(description=description, quantity=quantity, unit_price=unit_price, **kw)


def make_invoice(**overrides: Any) -> Invoice:
    """A clean INV-1001 unless overridden."""
    fields: dict[str, Any] = {
        "invoice_number": "INV-1001",
        "vendor": "Widgets Inc.",
        "invoice_date": "2026-01-15",
        "due_date": "2026-02-01",
        "currency": None,
        "line_items": [line("WidgetA", 10, 250.0), line("WidgetB", 5, 500.0)],
        "stated_subtotal": 5000.0,
        "stated_tax_rate": 0.0,
        "stated_tax_amount": 0.0,
        "stated_shipping": None,
        "stated_total": 5000.0,
    }
    fields.update(overrides)
    return Invoice(**fields)
